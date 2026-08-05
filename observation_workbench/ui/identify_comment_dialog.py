"""Focused, journal-first Comment entry for one captured observation."""
from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.services.identify_actions import (
    IdentifyActionManager,
    ObservationUUIDResolution,
)


class IdentifyCommentDialog(QDialog):
    """Collect one comment body without performing an API write.

    The dialog owns only its captured observation/account context.  It asks
    the application-scoped action manager to resolve identity and journal the
    action; its parent performs per-action dispatch only after
    :attr:`comment_journaled` is emitted.  Comment never navigates the parent
    window and never auto-advances.
    """

    comment_journaled = Signal(int, int)
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
        self.setWindowTitle(f"Comment — observation #{int(observation_id)}")
        self.setMinimumWidth(480)
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
        self._active_submission_body = ""
        self._resolution_request_id: int | None = None
        # The submission generation captured when the active UUID request
        # started.  It must equal ``_active_submission_generation`` for the
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

        self._body_edit = QPlainTextEdit(self)
        self._body_edit.setPlaceholderText("Write a comment for this observation…")
        self._body_edit.setMinimumHeight(160)
        self._body_edit.installEventFilter(self)
        self._body_edit.textChanged.connect(self._body_changed)
        outer.addWidget(self._body_edit, 1)

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
        if watched is self._body_edit and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if self._can_submit():
                    self._submit()
                return True
            # Ordinary Enter falls through to QPlainTextEdit's own handling,
            # which inserts a newline.
            return super().eventFilter(watched, event)
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._body_edit.setFocus()

    def _body_changed(self) -> None:
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
            # to return.  The manager's in-flight worker is left alone; its
            # result becomes harmless once _resolution_request_id is cleared.
            self._reset_after_submission_attempt()
        self._refresh_auth_status()
        if was_busy:
            self._status.setText(
                "Authentication changed. The pending comment was cancelled before "
                "any local action was created. Submit again."
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
            and self._body_edit.toPlainText().strip()
            and self._matches_opened_account()
        )

    def _refresh_submit_enabled(self) -> None:
        if hasattr(self, "_submit_button"):
            self._submit_button.setEnabled(self._can_submit())

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._body_edit.setEnabled(not busy)
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
        body = self._body_edit.toPlainText()
        if not body.strip():
            self._status.setText("Write a comment before submitting.")
            self._refresh_submit_enabled()
            return

        # Capture all user intent before asynchronous UUID resolution.  The
        # body is preserved exactly as entered; .strip() above is used only
        # to decide whether meaningful content exists, never to normalize
        # what gets journaled.
        self._submission_generation += 1
        self._active_submission_generation = self._submission_generation
        self._active_submission_authentication_generation = self._authentication_generation
        self._active_submission_body = body
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
            self._reset_after_submission_attempt()
            if not self._matches_opened_account():
                self._refresh_auth_status()
                self._status.setText(
                    "Authentication is no longer valid for this dialog. "
                    "Authenticate as the original account or reopen it before submitting."
                )
            else:
                self._status.setText("Write a comment before submitting.")
            return

        body = self._active_submission_body
        try:
            result = self._action_manager.queue_comment(
                account_login=self._opened_login,
                observation_id=self._observation_id,
                observation_uuid=resolution.observation_uuid,
                body=body,
            )
        except Exception:
            self._reset_after_submission_attempt()
            self._status.setText(
                "The comment could not be saved locally. "
                "No local action was created or authorized."
            )
            return

        if result.inserted_action_id is not None:
            action_id = int(result.inserted_action_id)
            # The journal transaction has committed.  Close before notifying
            # the parent so dispatch cannot leave this entry UI ambiguous.
            self.accept()
            self.comment_journaled.emit(action_id, self._observation_id)
            return

        if result.duplicate_action_id is not None:
            self._reset_after_submission_attempt()
            self._view_pending_button.show()
            self._status.setText(
                "An equivalent unresolved comment already exists locally as action "
                f"#{int(result.duplicate_action_id)}. No new action was created or authorized."
            )
            return

        self._reset_after_submission_attempt()
        self._status.setText(
            "The comment could not be saved locally. "
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
            and bool(self._active_submission_body.strip())
        )

    def _reset_after_submission_attempt(self) -> None:
        self._resolution_request_id = None
        self._resolution_request_generation = None
        self._active_submission_generation = None
        self._active_submission_authentication_generation = None
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
