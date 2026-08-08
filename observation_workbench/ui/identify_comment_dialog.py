"""Focused, journal-first Comment entry for one captured observation."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
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
from observation_workbench.storage.cache_db import IdentifyEnqueueResult
from observation_workbench.ui.identify_journal_dialog import IdentifyJournalDialog


class IdentifyCommentDialog(IdentifyJournalDialog):
    """Collect one comment body without performing an API write.

    The dialog owns only its captured observation/account context.  It asks
    the application-scoped action manager to resolve identity and journal the
    action; its parent performs per-action dispatch only after
    :attr:`comment_journaled` is emitted.  Comment never navigates the parent
    window and never auto-advances.
    """

    comment_journaled = Signal(int, int)

    def __init__(
        self,
        *,
        action_manager: IdentifyActionManager,
        observation_id: int,
        observation_uuid: str,
        opened_login: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            action_manager=action_manager,
            observation_id=observation_id,
            observation_uuid=observation_uuid,
            opened_login=opened_login,
            parent=parent,
        )
        self.setWindowTitle(f"Comment — observation #{self._observation_id}")
        self.setMinimumWidth(480)
        self._build()
        self._connect_journal_signals()

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

    def _can_submit(self) -> bool:
        return bool(
            not self._closed
            and not self._busy
            and self._body_edit.toPlainText().strip()
            and self._matches_opened_account()
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._body_edit.setEnabled(not busy)
        self._refresh_submit_enabled()

    # -- IdentifyJournalDialog hooks --------------------------------------

    def _selected_value(self) -> str:
        # Preserved exactly as entered; .strip() is used only in
        # _value_is_selected to decide whether meaningful content exists,
        # never to normalize what gets journaled.
        return self._body_edit.toPlainText()

    def _value_is_selected(self, value: object) -> bool:
        return bool(str(value or "").strip())

    @property
    def _missing_selection_message(self) -> str:
        return "Write a comment before submitting."

    @property
    def _action_noun(self) -> str:
        return "comment"

    def _queue_action(
        self, resolution: ObservationUUIDResolution, value: object
    ) -> IdentifyEnqueueResult:
        return self._action_manager.queue_comment(
            account_login=self._opened_login,
            observation_id=self._observation_id,
            observation_uuid=resolution.observation_uuid,
            body=value,
        )

    def _emit_journaled(
        self, action_id: int, value: object, cancelled_ids: tuple[int, ...]
    ) -> None:
        self.comment_journaled.emit(action_id, self._observation_id)

    def _build_duplicate_message(
        self, result: IdentifyEnqueueResult, value: object
    ) -> str:
        # Only reached when the base class has already confirmed
        # duplicate_action_id is not None.
        assert result.duplicate_action_id is not None
        return (
            "An equivalent unresolved comment already exists locally as action "
            f"#{result.duplicate_action_id}. No new action was created or authorized."
        )
