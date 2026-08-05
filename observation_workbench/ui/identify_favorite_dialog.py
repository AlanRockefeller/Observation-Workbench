"""Focused, journal-first Favorite entry for one captured observation.

Favorite always presents an explicit Add/Remove choice.  Unlike Reviewed
(one-way, always ``desired_state=True``), this dialog never infers which
direction is intended: the user must deliberately select one radio button
before Submit becomes available, and neither choice is preselected.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.services.identify_action_presentation import (
    describe_cancelled_opposite_actions,
)
from observation_workbench.services.identify_actions import (
    IdentifyActionManager,
    ObservationUUIDResolution,
)
from observation_workbench.storage.cache_db import IdentifyEnqueueResult
from observation_workbench.ui.identify_journal_dialog import IdentifyJournalDialog


class IdentifyFavoriteDialog(IdentifyJournalDialog):
    """Collect one explicit Add/Remove Favorite choice without an API write.

    The dialog owns only its captured observation/account context and an
    explicitly selected desired state.  Exactly like
    :class:`~observation_workbench.ui.identify_comment_dialog.IdentifyCommentDialog`,
    it asks the application-scoped action manager to resolve identity and
    journal the action; its parent performs per-action dispatch only after
    :attr:`favorite_journaled` is emitted.  Favorite never navigates the
    parent window and never auto-advances.
    """

    favorite_journaled = Signal(int, int, bool, object)

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
        self.setWindowTitle(f"Favorite — observation #{self._observation_id}")
        self.setMinimumWidth(420)
        self._build()
        self._connect_journal_signals()

    def _build(self) -> None:
        outer = QVBoxLayout(self)

        self._auth_status = QLabel(self)
        self._auth_status.setWordWrap(True)
        self._auth_status.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self._auth_status)

        self._add_radio = QRadioButton("Add this observation to favorites", self)
        self._remove_radio = QRadioButton("Remove this observation from favorites", self)
        self._add_radio.installEventFilter(self)
        self._remove_radio.installEventFilter(self)
        self._add_radio.toggled.connect(self._selection_changed)
        self._remove_radio.toggled.connect(self._selection_changed)
        outer.addWidget(self._add_radio)
        outer.addWidget(self._remove_radio)

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
        if watched in (self._add_radio, self._remove_radio) and event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
        ):
            # Enter must never fall through to QDialog's implicit accept: it
            # may submit only once a state has been explicitly selected, and
            # otherwise this swallows the key rather than closing the dialog.
            if self._can_submit():
                self._submit()
            return True
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._add_radio.setFocus()

    def _selected_desired_state(self) -> bool | None:
        if self._add_radio.isChecked():
            return True
        if self._remove_radio.isChecked():
            return False
        return None

    def _selection_changed(self, _checked: bool = False) -> None:
        if not self._busy:
            self._status.setText("")
        self._refresh_submit_enabled()

    def _can_submit(self) -> bool:
        return bool(
            not self._closed
            and not self._busy
            and self._selected_desired_state() is not None
            and self._matches_opened_account()
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._add_radio.setEnabled(not busy)
        self._remove_radio.setEnabled(not busy)
        self._refresh_submit_enabled()

    # -- IdentifyJournalDialog hooks --------------------------------------

    def _selected_value(self) -> bool | None:
        return self._selected_desired_state()

    def _value_is_selected(self, value: object) -> bool:
        return isinstance(value, bool)

    @property
    def _missing_selection_message(self) -> str:
        return "Choose Add or Remove before submitting."

    @property
    def _action_noun(self) -> str:
        return "Favorite change"

    def _queue_action(
        self, resolution: ObservationUUIDResolution, value: object
    ) -> IdentifyEnqueueResult:
        return self._action_manager.queue_desired_state(
            account_login=self._opened_login,
            observation_id=self._observation_id,
            observation_uuid=resolution.observation_uuid,
            action_type="favorite",
            desired_state=value,
        )

    def _emit_journaled(
        self, action_id: int, value: object, cancelled_ids: tuple[int, ...]
    ) -> None:
        self.favorite_journaled.emit(
            action_id, self._observation_id, bool(value), cancelled_ids
        )

    def _build_duplicate_message(
        self, result: IdentifyEnqueueResult, value: object
    ) -> str:
        # Only reached when the base class has already confirmed
        # duplicate_action_id is not None.
        assert result.duplicate_action_id is not None
        verb = "addition" if value else "removal"
        message = (
            f"An equivalent unresolved Favorite {verb} already exists locally as action "
            f"#{result.duplicate_action_id}. No new action was created or authorized."
        )
        cancelled_note = describe_cancelled_opposite_actions(
            "Favorite", tuple(int(a) for a in result.cancelled_action_ids)
        )
        if cancelled_note:
            message = f"{message} {cancelled_note}"
        return message
