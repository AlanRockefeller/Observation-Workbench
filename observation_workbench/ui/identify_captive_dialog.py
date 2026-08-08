"""Focused, journal-first Captive/Cultivated entry for one captured observation.

Captive/Cultivated is not an observation-field update. It is the
authenticated user's vote on the iNaturalist Data Quality Assessment "wild"
metric: Mark Captive/Cultivated votes ``agree=false``, Vote Wild votes
``agree=true``, and Remove my Wild/Captive vote deletes the authenticated
user's vote. Exactly like :class:`~observation_workbench.ui.identify_favorite_dialog.IdentifyFavoriteDialog`,
this dialog never infers which of the three explicit operations is intended
-- neither from ``StudyObservation.captive``, quality grade, nor prior local
rows -- the user must deliberately select one radio button before Submit
becomes available.
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
    describe_cancelled_conflicting_actions,
)
from observation_workbench.services.identify_actions import (
    IdentifyActionManager,
    ObservationUUIDResolution,
)
from observation_workbench.storage.cache_db import IdentifyEnqueueResult
from observation_workbench.ui.identify_journal_dialog import IdentifyJournalDialog

_VOTE_NOUN = {
    "disagree": "Captive/Cultivated vote",
    "agree": "Wild vote",
    "remove": "Wild/Captive vote removal",
}


class IdentifyCaptiveDialog(IdentifyJournalDialog):
    """Collect one explicit Captive/Cultivated Data Quality vote.

    The dialog owns only its captured observation/account context and an
    explicitly selected vote operation. Exactly like
    :class:`~observation_workbench.ui.identify_favorite_dialog.IdentifyFavoriteDialog`,
    it asks the application-scoped action manager to resolve identity and
    journal the action; its parent performs per-action dispatch only after
    :attr:`quality_metric_journaled` is emitted. Captive/Cultivated never
    navigates the parent window and never auto-advances.
    """

    quality_metric_journaled = Signal(int, int, str, object)

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
        self.setWindowTitle(f"Captive/Cultivated — observation #{self._observation_id}")
        self.setMinimumWidth(460)
        self._build()
        self._connect_journal_signals()

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

    def _can_submit(self) -> bool:
        return bool(
            not self._closed
            and not self._busy
            and self._selected_vote() is not None
            and self._matches_opened_account()
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for radio in self._vote_radios.values():
            radio.setEnabled(not busy)
        self._refresh_submit_enabled()

    # -- IdentifyJournalDialog hooks --------------------------------------

    def _selected_value(self) -> str | None:
        return self._selected_vote()

    def _value_is_selected(self, value: object) -> bool:
        return value in {"agree", "disagree", "remove"}

    @property
    def _missing_selection_message(self) -> str:
        return "Choose an option before submitting."

    @property
    def _action_noun(self) -> str:
        return "Captive/Cultivated vote"

    def _queue_action(
        self, resolution: ObservationUUIDResolution, value: object
    ) -> IdentifyEnqueueResult:
        return self._action_manager.queue_quality_metric(
            account_login=self._opened_login,
            observation_id=self._observation_id,
            observation_uuid=resolution.observation_uuid,
            metric="wild",
            vote=str(value),
        )

    def _emit_journaled(
        self, action_id: int, value: object, cancelled_ids: tuple[int, ...]
    ) -> None:
        self.quality_metric_journaled.emit(
            action_id, self._observation_id, str(value), cancelled_ids
        )

    def _build_duplicate_message(
        self, result: IdentifyEnqueueResult, value: object
    ) -> str:
        # Only reached when the base class has already confirmed
        # duplicate_action_id is not None.
        assert result.duplicate_action_id is not None
        noun = _VOTE_NOUN.get(str(value), "Captive/Cultivated vote")
        message = (
            f"An equivalent unresolved {noun} already exists locally as action "
            f"#{result.duplicate_action_id}. No new action was created or authorized."
        )
        cancelled_note = describe_cancelled_conflicting_actions(
            "Captive/Cultivated", tuple(int(a) for a in result.cancelled_action_ids)
        )
        if cancelled_note:
            message = f"{message} {cancelled_note}"
        return message
