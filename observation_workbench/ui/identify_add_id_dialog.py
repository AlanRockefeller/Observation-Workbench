"""Focused, journal-first Add ID entry for one captured observation."""

from __future__ import annotations

import logging
from uuid import UUID

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.client import INatClient
from observation_workbench.models import StudyTaxon
from observation_workbench.services.identify_actions import (
    IdentifyActionManager,
    ObservationUUIDResolution,
)
from observation_workbench.ui.taxon_autocomplete import TaxonAutocompleteField

log = logging.getLogger(__name__)


class IdentifyAddIDDialog(QDialog):
    """Collect one normal identification without performing an API write.

    The dialog owns only its captured observation/account context.  It asks
    the application-scoped action manager to resolve identity and journal the
    action; its parent performs optional navigation and per-action dispatch
    only after :attr:`identification_journaled` is emitted.
    """

    # Args: (action_id, observation_id, suggested_taxon). ``suggested_taxon``
    # is a :class:`StudyTaxon` describing the identification the user just
    # journaled, letting the parent optimistically reflect it before the
    # confirmed refresh round-trip completes.
    identification_journaled = Signal(int, int, object)
    pending_actions_requested = Signal()

    def __init__(
        self,
        *,
        client: INatClient,
        action_manager: IdentifyActionManager,
        observation_id: int,
        observation_uuid: str,
        opened_login: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(f"Add identification — observation #{int(observation_id)}")
        self.setMinimumWidth(480)
        self._client = client
        self._action_manager = action_manager
        self._observation_id = int(observation_id)
        self._observation_uuid = _valid_uuid(observation_uuid)
        self._opened_login = str(opened_login or "").strip()
        self._closed = False
        self._busy = False
        self._authentication_generation = 0
        # A sticky status message reports the outcome of a submission attempt
        # and must not be overwritten by background autocomplete progress.
        self._status_locked = False
        # A local nonce distinct from the manager's UUID request ID: it marks
        # whether *this* dialog currently considers a submission active, so a
        # stray late result cannot be mistaken for the current attempt even
        # if a request ID were ever reused.
        self._submission_generation = 0
        self._active_submission_generation: int | None = None
        self._resolution_request_id: int | None = None
        # The submission generation captured when the active UUID request
        # started.  It must equal ``_active_submission_generation`` for the
        # request's result to be treated as belonging to the active attempt.
        self._resolution_request_generation: int | None = None
        self._submission_observation_id: int | None = None
        self._submission_authentication_generation: int | None = None
        self._submission_taxon_id: int | None = None
        self._submission_taxon: StudyTaxon | None = None
        self._submission_body = ""
        self._submission_disagreement = False
        self._submit_eligibility_snapshot: (
            tuple[bool, bool, int | None, bool] | None
        ) = None

        self._build()
        self._action_manager.observation_uuid_resolved.connect(self._uuid_resolved)
        self._action_manager.authentication_context_changed.connect(
            self._authentication_changed
        )
        self._refresh_auth_status()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        intro = QLabel(
            f"Add a normal identification to observation #{self._observation_id}.", self
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        self._auth_status = QLabel(self)
        self._auth_status.setWordWrap(True)
        self._auth_status.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self._auth_status)

        form = QFormLayout()
        self._taxon_field = TaxonAutocompleteField(self._client, self)
        self._taxon_field.selection_changed.connect(self._taxon_selection_changed)
        self._taxon_field.search_status_changed.connect(
            self._taxon_search_status_changed
        )
        self._taxon_field.line_edit.installEventFilter(self)
        form.addRow("Taxon", self._taxon_field)

        self._body_edit = QPlainTextEdit(self)
        self._body_edit.setPlaceholderText("Optional identification body")
        self._body_edit.setFixedHeight(92)
        # Tab moves focus onward (to the disagree checkbox, then Submit) rather
        # than inserting a literal tab into the identification body.
        self._body_edit.setTabChangesFocus(True)
        self._body_edit.installEventFilter(self)
        form.addRow("Body", self._body_edit)
        outer.addLayout(form)

        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self._status)

        self._view_pending_button = QPushButton("View pending Identify actions…", self)
        self._view_pending_button.clicked.connect(self._open_pending_actions)
        self._view_pending_button.hide()
        outer.addWidget(self._view_pending_button)

        self._disagree_check = QCheckBox("Disagree with current ID", self)
        self._disagree_check.setChecked(False)
        # Keep the checkbox out of the Tab chain so Tab in the body jumps
        # straight to Submit; it stays toggleable by mouse.
        self._disagree_check.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self._disagree_check.setToolTip(
            "Post this identification with iNaturalist's explicit disagreement "
            "flag, typically knocking the community taxon back to the coarser "
            "rank you are proposing (e.g. genus)."
        )

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self._submit_button = buttons.addButton(
            "Submit", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._submit_button.setAutoDefault(False)
        self._submit_button.clicked.connect(self._submit)
        buttons.rejected.connect(self.reject)
        # Keep the disagree checkbox anchored to the lower-left, with the
        # Cancel/Submit buttons on the right of the same row.
        button_row = QHBoxLayout()
        button_row.addWidget(self._disagree_check)
        button_row.addStretch(1)
        button_row.addWidget(buttons)
        outer.addLayout(button_row)
        self._refresh_submit_enabled()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not isinstance(event, QKeyEvent):
            return super().eventFilter(watched, event)
        if event.key() not in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            return super().eventFilter(watched, event)
        if watched is self._body_edit:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if self._can_submit():
                    self._submit()
                return True
            return super().eventFilter(watched, event)
        if (
            watched is self._taxon_field.line_edit
            and not self._taxon_field.popup_visible
        ):
            if self._can_submit():
                self._submit()
                return True
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._taxon_field.focus_editor()

    def _set_status(self, message: str, *, sticky: bool = False) -> None:
        """Set the status line; ``sticky`` protects submission outcomes.

        A sticky message survives until the user changes the taxon selection
        or submits again, so background autocomplete progress can never scroll
        away the explanation of what just happened to a submission attempt.
        """
        self._status.setText(message)
        self._status_locked = sticky

    def _taxon_selection_changed(self, _selection: object) -> None:
        if not self._busy:
            self._set_status("")
        self._refresh_submit_enabled()

    def _taxon_search_status_changed(self, message: str) -> None:
        if not self._busy and not self._status_locked:
            self._status.setText(message)

    def _authentication_changed(self) -> None:
        self._authentication_generation += 1
        if self._closed:
            return
        was_busy = self._busy
        if was_busy:
            # Release immediately rather than waiting for the stale UUID read
            # to return.  The manager's in-flight worker is left alone; its
            # result becomes harmless once _resolution_request_id is cleared.
            self._reset_after_submission_attempt()
        self._refresh_auth_status()
        if was_busy:
            self._set_status(
                "Authentication changed. The pending identification was cancelled "
                "before any local action was created. Submit again.",
                sticky=True,
            )
        elif not self._matches_opened_account():
            self._set_status(
                "Authentication changed. Authenticate as the original account or reopen this dialog.",
                sticky=True,
            )

    def _refresh_auth_status(self) -> None:
        snapshot = self._action_manager.current_authentication()
        if (
            self._same_login(snapshot.login, self._opened_login)
            and snapshot.authenticated
        ):
            self._auth_status.setText(f"Authenticated as {snapshot.login}.")
        elif snapshot.authenticated:
            self._auth_status.setText(
                f"Authenticated as {snapshot.login}; this dialog was opened for {self._opened_login}."
            )
        else:
            self._auth_status.setText(
                f"Authentication is required for the account that opened this dialog ({self._opened_login})."
            )
        self._refresh_submit_enabled()

    def _matches_opened_account(self) -> bool:
        snapshot = self._action_manager.current_authentication()
        return bool(
            self._opened_login
            and snapshot.authenticated
            and self._same_login(snapshot.login, self._opened_login)
        )

    @staticmethod
    def _same_login(left: str, right: str) -> bool:
        return str(left or "").strip().casefold() == str(right or "").strip().casefold()

    def _can_submit(self) -> bool:
        return bool(
            not self._closed
            and not self._busy
            and self._taxon_field.has_selection
            and self._matches_opened_account()
        )

    def _refresh_submit_enabled(self) -> None:
        if hasattr(self, "_submit_button"):
            selected = self._taxon_field.selected_item
            selected_taxon_id = selected.taxon_id if selected is not None else None
            account_matches = self._matches_opened_account()
            enabled = bool(
                not self._closed
                and not self._busy
                and selected_taxon_id is not None
                and account_matches
            )
            self._submit_button.setEnabled(enabled)
            snapshot = (
                self._closed,
                self._busy,
                selected_taxon_id,
                account_matches,
            )
            if snapshot != self._submit_eligibility_snapshot:
                self._submit_eligibility_snapshot = snapshot
                log.debug(
                    "Add identification submit enabled=%s closed=%s busy=%s "
                    "selected_taxon_id=%s account_matches=%s",
                    enabled,
                    self._closed,
                    self._busy,
                    selected_taxon_id,
                    account_matches,
                )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._taxon_field.setEnabled(not busy)
        self._body_edit.setEnabled(not busy)
        self._disagree_check.setEnabled(not busy)
        self._refresh_submit_enabled()

    def _submit(self) -> None:
        if self._closed or self._busy:
            return
        if not self._matches_opened_account():
            self._refresh_auth_status()
            self._set_status(
                "Authenticate as the original account or reopen this dialog before submitting.",
                sticky=True,
            )
            return
        selected = self._taxon_field.selected_item
        if selected is None or selected.taxon_id <= 0:
            self._set_status(
                "Select a taxon from the autocomplete suggestions before submitting.",
                sticky=True,
            )
            self._refresh_submit_enabled()
            return

        # Capture all user intent before asynchronous UUID resolution.  Qt's
        # plain-text editor supplies the exact text currently entered, without
        # trimming or normalizing it in this UI layer.
        self._submission_generation += 1
        self._active_submission_generation = self._submission_generation
        self._submission_observation_id = self._observation_id
        self._submission_authentication_generation = self._authentication_generation
        self._submission_taxon_id = selected.taxon_id
        self._submission_taxon = StudyTaxon(
            taxon_id=selected.taxon_id,
            name=selected.scientific_name,
            common_name=selected.preferred_common_name,
            rank=selected.rank,
        )
        self._submission_body = self._body_edit.toPlainText()
        self._submission_disagreement = self._disagree_check.isChecked()
        self._set_busy(True)
        self._set_status("Resolving the observation identity…")
        try:
            self._resolution_request_id = self._action_manager.resolve_observation_uuid(
                self._observation_id,
                self._observation_uuid,
            )
            # Retain the generation associated with this exact UUID request so
            # _uuid_resolved can require it to still equal the active
            # submission generation, rather than merely being non-None.
            self._resolution_request_generation = self._active_submission_generation
        except Exception:
            self._reset_after_submission_attempt()
            self._set_status(
                "The observation identity could not be prepared. "
                "No local action was created or authorized.",
                sticky=True,
            )

    def _uuid_resolved(self, resolution: object) -> None:
        if self._closed or not isinstance(resolution, ObservationUUIDResolution):
            return
        if (
            self._resolution_request_id is None
            or self._active_submission_generation is None
        ):
            return
        if (
            resolution.request_id != self._resolution_request_id
            or resolution.observation_id != self._observation_id
            or self._resolution_request_generation != self._active_submission_generation
        ):
            return
        if (
            self._submission_authentication_generation
            != self._authentication_generation
        ):
            self._reset_after_submission_attempt()
            self._refresh_auth_status()
            self._set_status(
                "Authentication changed while the observation identity was being resolved. "
                "No local action was created or authorized; submit again.",
                sticky=True,
            )
            return
        if not resolution.resolved:
            self._reset_after_submission_attempt()
            self._set_status(
                "Unable to resolve the observation identity. "
                "No local action was created or authorized.",
                sticky=True,
            )
            return
        if not self._submission_is_ready_to_journal(resolution):
            self._reset_after_submission_attempt()
            if not self._matches_opened_account():
                self._refresh_auth_status()
                self._set_status(
                    "Authentication is no longer valid for this dialog. "
                    "Authenticate as the original account or reopen it before submitting.",
                    sticky=True,
                )
            else:
                self._set_status(
                    "Select a taxon from the autocomplete suggestions before submitting.",
                    sticky=True,
                )
            return
        try:
            result = self._action_manager.queue_identification(
                account_login=self._opened_login,
                observation_id=self._observation_id,
                observation_uuid=resolution.observation_uuid,
                taxon_id=self._submission_taxon_id,
                body=self._submission_body,
                disagreement=self._submission_disagreement or None,
            )
        except Exception:
            self._reset_after_submission_attempt()
            self._set_status(
                "The identification could not be saved locally. "
                "No local action was created or authorized.",
                sticky=True,
            )
            return

        if result.inserted_action_id is not None:
            action_id = int(result.inserted_action_id)
            # The journal transaction has committed.  Close before notifying
            # the parent so navigation/dispatch cannot leave this entry UI in
            # an ambiguous state.
            suggested_taxon = self._submission_taxon
            self.accept()
            self.identification_journaled.emit(
                action_id, self._observation_id, suggested_taxon
            )
            return

        if result.duplicate_action_id is not None:
            self._reset_after_submission_attempt()
            self._view_pending_button.show()
            self._set_status(
                "An equivalent unresolved identification already exists locally "
                f"as action #{int(result.duplicate_action_id)}. "
                "No new action was created or authorized.",
                sticky=True,
            )
            return

        self._reset_after_submission_attempt()
        self._set_status(
            "The identification could not be saved locally. "
            "No local action was created or authorized.",
            sticky=True,
        )

    def _submission_is_ready_to_journal(
        self,
        resolution: ObservationUUIDResolution,
    ) -> bool:
        """Require the still-current captured context immediately before journaling."""
        selected = self._taxon_field.selected_item
        snapshot = self._action_manager.current_authentication()
        return bool(
            not self._closed
            and self._busy
            and self._active_submission_generation is not None
            and self._resolution_request_generation
            == self._active_submission_generation
            and self._resolution_request_id == resolution.request_id
            and resolution.observation_id == self._observation_id
            and self._submission_observation_id == self._observation_id
            and self._submission_authentication_generation
            == self._authentication_generation
            and snapshot.authenticated
            and self._same_login(snapshot.login, self._opened_login)
            and self._submission_taxon_id is not None
            and selected is not None
            and selected.taxon_id == self._submission_taxon_id
            and selected.taxon_id > 0
        )

    def _reset_after_submission_attempt(self) -> None:
        """Release the entry immediately; the widget contents are untouched."""
        self._resolution_request_id = None
        self._resolution_request_generation = None
        self._active_submission_generation = None
        self._submission_observation_id = None
        self._submission_authentication_generation = None
        self._submission_taxon_id = None
        self._submission_taxon = None
        self._submission_body = ""
        self._submission_disagreement = False
        self._set_busy(False)

    def _open_pending_actions(self) -> None:
        # This is a duplicate-only recovery route.  It neither alters the
        # durable row nor grants it dispatch authorization.
        self.reject()
        self.pending_actions_requested.emit()

    def _deactivate(self) -> None:
        """Make all late local callbacks harmless without cancelling work."""
        if self._closed:
            return
        self._closed = True
        self._taxon_field.shutdown()
        try:
            self._action_manager.observation_uuid_resolved.disconnect(
                self._uuid_resolved
            )
            self._action_manager.authentication_context_changed.disconnect(
                self._authentication_changed
            )
        except (RuntimeError, TypeError):
            pass

    def done(self, result: int) -> None:
        self._deactivate()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._deactivate()
        super().closeEvent(event)


def _valid_uuid(value: object) -> str:
    try:
        return str(UUID(str(value or "").strip()))
    except (AttributeError, TypeError, ValueError):
        return ""
