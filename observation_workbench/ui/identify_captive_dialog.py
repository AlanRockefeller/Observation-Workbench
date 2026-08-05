"""Focused, journal-first Captive/Cultivated entry for one captured observation.

Captive/Cultivated is not an observation-field update. It is the
authenticated user's vote on the iNaturalist Data Quality Assessment "wild"
metric: Mark Captive/Cultivated votes ``agree=false``, Vote Wild votes
``agree=true``, and Remove my Wild/Captive vote deletes the authenticated
user's vote. Exactly like :class:`IdentifyFavoriteDialog`, this dialog never
infers which of the three explicit operations is intended -- neither from
``StudyObservation.captive``, quality grade, nor prior local rows -- the user
must deliberately select one radio button before Submit becomes available.
"""
from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.services.identify_action_presentation import (
    describe_cancelled_conflicting_actions,
)
from observation_workbench.services.identify_actions import (
    IdentifyActionManager,
    ObservationUUIDResolution,
)

_VOTE_NOUN = {
    "disagree": "Captive/Cultivated vote",
    "agree": "Wild vote",
    "remove": "Wild/Captive vote removal",
}


class IdentifyCaptiveDialog(QDialog):
    """Collect one explicit Captive/Cultivated Data Quality vote.

    The dialog owns only its captured observation/account context and an
    explicitly selected vote operation. Exactly like
    :class:`IdentifyFavoriteDialog`, it asks the application-scoped action
    manager to resolve identity and journal the action; its parent performs
    per-action dispatch only after :attr:`quality_metric_journaled` is
    emitted. Captive/Cultivated never navigates the parent window and never
    auto-advances.
    """

    quality_metric_journaled = Signal(int, int, str, object)
    pending_actions_requested = Signal()

    def __init__(
        self,
        *,
        action_manager: IdentifyActionManager,
        observation_id: int,
        observation_uuid: str,
        opened_login: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(f"Captive/Cultivated — observation #{int(observation_id)}")
        self.setMinimumWidth(460)
        self._action_manager = action_manager
        self._observation_id = int(observation_id)
        self._observation_uuid = _valid_uuid(observation_uuid)
        self._opened_login = str(opened_login or "").strip()
        self._closed = False
        self._busy = False
        self._authentication_generation = 0
        # A local nonce distinct from the manager's UUID request ID: it marks
        # whether *this* dialog currently considers a submission active, so a
        # stray late result cannot be mistaken for the current attempt even
        # if a request ID were ever reused.
        self._submission_generation = 0
        self._active_submission_generation: int | None = None
        self._active_submission_authentication_generation: int | None = None
        self._active_submission_vote: str | None = None
        self._resolution_request_id: int | None = None
        # The submission generation captured when the active UUID request
        # started. It must equal ``_active_submission_generation`` for the
        # request's result to be treated as belonging to the active attempt.
        self._resolution_request_generation: int | None = None

        self._build()
        self._action_manager.observation_uuid_resolved.connect(self._uuid_resolved)
        self._action_manager.authentication_context_changed.connect(
            self._authentication_changed
        )
        self._refresh_auth_status()

    def _build(self) -> None:
        outer = QVBoxLayout(self)

        self._auth_status = QLabel(self)
        self._auth_status.setWordWrap(True)
        self._auth_status.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self._auth_status)

        self._disagree_radio = QRadioButton("Mark Captive/Cultivated", self)
        self._agree_radio = QRadioButton("Vote Wild", self)
        self._remove_radio = QRadioButton("Remove my Wild/Captive vote", self)
        self._vote_radios: dict[str, QRadioButton] = {
            "disagree": self._disagree_radio,
            "agree": self._agree_radio,
            "remove": self._remove_radio,
        }
        hints = {
            "disagree": "Vote that this organism was not wild.",
            "agree": "Vote that this organism was wild and present without human intervention.",
            "remove": "Remove your vote on this Data Quality metric.",
        }
        for key, radio in self._vote_radios.items():
            radio.installEventFilter(self)
            radio.toggled.connect(self._selection_changed)
            outer.addWidget(radio)
            hint = QLabel(hints[key], self)
            hint.setWordWrap(True)
            hint.setTextFormat(Qt.TextFormat.PlainText)
            hint.setStyleSheet("margin-left: 20px;")
            outer.addWidget(hint)

        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self._status)

        self._view_pending_button = QPushButton("View pending Identify actions…", self)
        self._view_pending_button.clicked.connect(self._open_pending_actions)
        self._view_pending_button.hide()
        outer.addWidget(self._view_pending_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self._submit_button = buttons.addButton(
            "Submit", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._submit_button.setAutoDefault(False)
        self._submit_button.clicked.connect(self._submit)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._refresh_submit_enabled()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not isinstance(event, QKeyEvent):
            return super().eventFilter(watched, event)
        if watched in self._vote_radios.values() and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            # Enter must never fall through to QDialog's implicit accept: it
            # may submit only once an operation has been explicitly
            # selected, and otherwise this swallows the key rather than
            # closing the dialog.
            if self._can_submit():
                self._submit()
            return True
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._disagree_radio.setFocus()

    def _selected_vote(self) -> str | None:
        for vote, radio in self._vote_radios.items():
            if radio.isChecked():
                return vote
        return None

    def _selection_changed(self, _checked: bool = False) -> None:
        if not self._busy:
            self._status.setText("")
        self._refresh_submit_enabled()

    def _authentication_changed(self) -> None:
        self._authentication_generation += 1
        if self._closed:
            return
        was_busy = self._busy
        if was_busy:
            # Release immediately rather than waiting for the stale UUID read
            # to return. The manager's in-flight worker is left alone; its
            # result becomes harmless once _resolution_request_id is cleared.
            # The visible selected radio option is deliberately left intact.
            self._reset_after_submission_attempt()
        self._refresh_auth_status()
        if was_busy:
            self._status.setText(
                "Authentication changed. The pending Captive/Cultivated vote was cancelled "
                "before any local action was created. Submit again."
            )
        elif not self._matches_opened_account():
            self._status.setText(
                "Authentication changed. Authenticate as the original account or reopen this dialog."
            )

    def _refresh_auth_status(self) -> None:
        snapshot = self._action_manager.current_authentication()
        if self._same_login(snapshot.login, self._opened_login) and snapshot.authenticated:
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
            and self._selected_vote() is not None
            and self._matches_opened_account()
        )

    def _refresh_submit_enabled(self) -> None:
        if hasattr(self, "_submit_button"):
            self._submit_button.setEnabled(self._can_submit())

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for radio in self._vote_radios.values():
            radio.setEnabled(not busy)
        self._refresh_submit_enabled()

    def _submit(self) -> None:
        if self._closed or self._busy:
            return
        if not self._matches_opened_account():
            self._refresh_auth_status()
            self._status.setText(
                "Authenticate as the original account or reopen this dialog before submitting."
            )
            return
        vote = self._selected_vote()
        if vote is None:
            self._status.setText("Choose an option before submitting.")
            self._refresh_submit_enabled()
            return

        # Capture all user intent before asynchronous UUID resolution.
        self._submission_generation += 1
        self._active_submission_generation = self._submission_generation
        self._active_submission_authentication_generation = self._authentication_generation
        self._active_submission_vote = vote
        self._set_busy(True)
        self._status.setText("Resolving the observation identity…")
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
            self._status.setText(
                "The observation identity could not be prepared. "
                "No local action was created or authorized."
            )

    def _uuid_resolved(self, resolution: object) -> None:
        if self._closed or not isinstance(resolution, ObservationUUIDResolution):
            return
        if self._resolution_request_id is None or self._active_submission_generation is None:
            return
        if (
            resolution.request_id != self._resolution_request_id
            or resolution.observation_id != self._observation_id
            or self._resolution_request_generation != self._active_submission_generation
        ):
            return
        if (
            self._active_submission_authentication_generation
            != self._authentication_generation
        ):
            self._reset_after_submission_attempt()
            self._refresh_auth_status()
            self._status.setText(
                "Authentication changed while the observation identity was being resolved. "
                "No local action was created or authorized; submit again."
            )
            return
        if not resolution.resolved:
            self._reset_after_submission_attempt()
            self._status.setText(
                "Unable to resolve the observation identity. "
                "No local action was created or authorized."
            )
            return
        if not self._is_ready_to_journal(resolution):
            # The only mutable context required by Captive/Cultivated is
            # authentication and the explicit selection; explain that race
            # specifically and leave the window usable.
            self._reset_after_submission_attempt()
            if not self._matches_opened_account():
                self._refresh_auth_status()
                self._status.setText(
                    "Authentication is no longer valid for this dialog. "
                    "Authenticate as the original account or reopen it before submitting."
                )
            else:
                self._status.setText("Choose an option before submitting.")
            return

        vote = self._active_submission_vote
        if vote not in {"agree", "disagree", "remove"}:
            self._reset_after_submission_attempt()
            self._status.setText("Choose an option before submitting.")
            return
        try:
            result = self._action_manager.queue_quality_metric(
                account_login=self._opened_login,
                observation_id=self._observation_id,
                observation_uuid=resolution.observation_uuid,
                metric="wild",
                vote=vote,
            )
        except Exception:
            self._reset_after_submission_attempt()
            self._status.setText(
                "The Captive/Cultivated vote could not be saved locally. "
                "No local action was created or authorized."
            )
            return

        if result.inserted_action_id is not None:
            action_id = int(result.inserted_action_id)
            cancelled_ids = tuple(int(a) for a in result.cancelled_action_ids)
            # The journal transaction has committed. Close before notifying
            # the parent so dispatch cannot leave this entry UI ambiguous.
            self.accept()
            self.quality_metric_journaled.emit(
                action_id, self._observation_id, vote, cancelled_ids
            )
            return

        if result.duplicate_action_id is not None:
            self._reset_after_submission_attempt()
            self._view_pending_button.show()
            noun = _VOTE_NOUN.get(vote, "Captive/Cultivated vote")
            message = (
                f"An equivalent unresolved {noun} already exists locally as action "
                f"#{int(result.duplicate_action_id)}. No new action was created or authorized."
            )
            cancelled_note = describe_cancelled_conflicting_actions(
                "Captive/Cultivated", tuple(int(a) for a in result.cancelled_action_ids)
            )
            if cancelled_note:
                message = f"{message} {cancelled_note}"
            self._status.setText(message)
            return

        self._reset_after_submission_attempt()
        self._status.setText(
            "The Captive/Cultivated vote could not be saved locally. "
            "No local action was created or authorized."
        )

    def _is_ready_to_journal(self, resolution: ObservationUUIDResolution) -> bool:
        """Require the still-current captured context immediately before journaling."""
        snapshot = self._action_manager.current_authentication()
        return bool(
            not self._closed
            and self._busy
            and self._active_submission_generation is not None
            and self._resolution_request_generation == self._active_submission_generation
            and self._resolution_request_id == resolution.request_id
            and resolution.observation_id == self._observation_id
            and self._active_submission_authentication_generation
            == self._authentication_generation
            and snapshot.authenticated
            and self._same_login(snapshot.login, self._opened_login)
            and self._active_submission_vote in {"agree", "disagree", "remove"}
        )

    def _reset_after_submission_attempt(self) -> None:
        self._resolution_request_id = None
        self._resolution_request_generation = None
        self._active_submission_generation = None
        self._active_submission_authentication_generation = None
        self._active_submission_vote = None
        self._set_busy(False)

    def _open_pending_actions(self) -> None:
        # This is a duplicate-only recovery route. It neither alters the
        # durable row nor grants it dispatch authorization.
        self.reject()
        self.pending_actions_requested.emit()

    def _deactivate(self) -> None:
        """Make all late local callbacks harmless without cancelling work."""
        if self._closed:
            return
        self._closed = True
        try:
            self._action_manager.observation_uuid_resolved.disconnect(self._uuid_resolved)
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
