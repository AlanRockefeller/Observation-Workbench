"""Credential-free pending Identify action management dialogs."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.services.identify_action_presentation import (
    IdentifyActionPresentation,
    format_journal_time,
    present_identify_action,
)
from observation_workbench.services.identify_actions import IdentifyActionManager
from observation_workbench.ui.external_links import open_external_url_silently


class IdentifyActionDetailsDialog(QDialog):
    """Explicit, plaintext-only view of one journal action."""

    def __init__(self, action: IdentifyActionPresentation, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Identify action #{action.local_action_id} details")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._add(form, "Local action ID", f"#{action.local_action_id}")
        self._add(form, "Parent action ID", _action_id_text(action.parent_action_id))
        self._add(form, "Observation ID", str(action.observation_id))
        self._add(form, "Observation UUID", action.observation_uuid or "—")
        self._add(form, "Account", action.account_login or "—")
        self._add(form, "Action type", action.action_type or "—")
        self._add(form, "State", action.state_text)
        self._add(form, "Desired state", _desired_state_text(action.desired_state))
        self._add(form, "Created", action.created_text)
        self._add(form, "Updated", action.updated_text)
        self._add(form, "Confirmed", format_journal_time(action.confirmed_at))
        self._add(form, "Unsafe attempt count", str(action.attempt_count))
        self._add(form, "Verification attempt count", str(action.verification_attempt_count))
        self._add(form, "Last operation phase", action.last_operation_phase or "—")
        self._add(form, "Outcome unknown", "Yes" if action.outcome_unknown else "No")
        self._add(form, "Server response ID", action.server_object_id or "—")
        self._add(form, "Server response UUID", action.server_object_uuid or "—")
        self._add(form, "Verification status", action.verification_status or "—")
        self._add(form, "Verification diagnostic", action.verification_diagnostic or "—")
        self._add(form, "Last error diagnostic", action.error_summary or "—")
        if action.retry_lineage_text:
            self._add(form, "Retry lineage", action.retry_lineage_text)
        if action.action_type == "identification":
            self._add(form, "Taxon ID", str(action.taxon_id) if action.taxon_id else "—")
        layout.addLayout(form)

        if action.action_type in {"identification", "comment"}:
            body_label = QLabel(
                "Identification body" if action.action_type == "identification" else "Comment body",
                self,
            )
            body_label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(body_label)
            body = QPlainTextEdit(self)
            body.setReadOnly(True)
            body.setPlainText(action.payload_body)
            body.setMinimumHeight(130)
            layout.addWidget(body)

        button_row = QHBoxLayout()
        open_button = QPushButton("Open observation", self)
        open_button.setEnabled(action.can_open_observation)
        open_button.clicked.connect(lambda: _open_observation(action.observation_id))
        button_row.addWidget(open_button)
        button_row.addStretch()
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.close)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)
        self.resize(620, 620)

    def _add(self, form: QFormLayout, title: str, value: str) -> None:
        label = QLabel(value, self)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(title, label)


class PendingIdentifyActionsDialog(QDialog):
    """One modeless action center backed entirely by IdentifyActionManager."""

    _COLUMNS = (
        "Local ID",
        "Observation",
        "Account",
        "Type",
        "Intended action",
        "State",
        "Created",
        "Attempts",
        "Last status / error",
        "Lineage",
    )

    def __init__(self, manager: IdentifyActionManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pending Identify actions")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._manager = manager
        self._closed = False
        self._actions: list[IdentifyActionPresentation] = []

        layout = QVBoxLayout(self)
        self._notice = QLabel("Queued actions remain paused until you explicitly resume them.", self)
        self._notice.setWordWrap(True)
        layout.addWidget(self._notice)

        self._table = QTableWidget(0, len(self._COLUMNS), self)
        self._table.setHorizontalHeaderLabels(self._COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.itemSelectionChanged.connect(self._update_controls)
        self._table.itemDoubleClicked.connect(lambda _item: self._show_details())
        layout.addWidget(self._table, 1)

        self._selection_hint = QLabel(self)
        self._selection_hint.setWordWrap(True)
        self._selection_hint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._selection_hint)

        commands = QGridLayout()
        # QPushButton.clicked carries a ``checked`` bool, and PySide6 passes it
        # to any slot that accepts an argument.  Wrapping keeps that bool out of
        # refresh()'s selected_action_id, which would otherwise become False and
        # silently discard the user's row selection on every manual Refresh.
        self._refresh_button = self._button("Refresh", lambda: self.refresh())
        self._resume_button = self._button("Resume queued actions", self._resume_queued)
        self._submit_button = self._button("Submit selected", self._submit_selected)
        self._pause_button = self._button("Pause", self._pause)
        self._cancel_button = self._button("Cancel queued action", self._cancel_selected)
        self._verify_button = self._button("Verify again", self._verify_selected)
        self._retry_button = self._button("Retry (re-queue)", self._retry_definite_failure)
        self._retry_anyway_button = self._button("Retry anyway", self._retry_anyway)
        self._open_button = self._button("Open observation", self._open_selected)
        self._details_button = self._button("Details", self._show_details)
        for index, button in enumerate(
            (
                self._refresh_button,
                self._resume_button,
                self._submit_button,
                self._pause_button,
                self._cancel_button,
                self._verify_button,
                self._retry_button,
                self._retry_anyway_button,
                self._open_button,
                self._details_button,
            )
        ):
            commands.addWidget(button, index // 3, index % 3)
        layout.addLayout(commands)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        button_box.rejected.connect(self.close)
        layout.addWidget(button_box)

        manager.action_changed.connect(self._manager_changed)
        manager.summary_changed.connect(self._summary_changed)
        manager.running_changed.connect(self._running_changed)
        manager.paused.connect(self._paused)
        manager.authentication_context_changed.connect(self._authentication_changed)
        self.refresh()
        self.resize(1240, 560)

    def _button(self, text: str, slot) -> QPushButton:
        button = QPushButton(text, self)
        button.clicked.connect(slot)
        return button

    def refresh(self, selected_action_id: int | None = None) -> None:
        if self._closed:
            return
        if selected_action_id is None:
            selected = self._selected_action()
            selected_action_id = selected.local_action_id if selected else None
        self._actions = [present_identify_action(action) for action in self._manager.list_actions()]
        self._table.setRowCount(len(self._actions))
        selected_row = -1
        for row, action in enumerate(self._actions):
            values = (
                f"#{action.local_action_id}",
                str(action.observation_id),
                action.account_login,
                action.action_type,
                action.intended_summary,
                action.state_text,
                action.created_text,
                str(action.attempt_count),
                action.error_summary or action.verification_diagnostic or action.verification_status,
                action.retry_lineage_text,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, action.local_action_id)
                item.setToolTip(value)
                self._table.setItem(row, column, item)
            if action.local_action_id == selected_action_id:
                selected_row = row
        self._table.resizeColumnsToContents()
        if selected_row >= 0:
            self._table.selectRow(selected_row)
        self._update_notice()
        self._update_controls()

    def _selected_action(self) -> IdentifyActionPresentation | None:
        items = self._table.selectedItems()
        if not items:
            return None
        action_id = items[0].data(Qt.ItemDataRole.UserRole)
        for action in self._actions:
            if action.local_action_id == action_id:
                return action
        return None

    def _update_notice(self) -> None:
        queue = self._manager.queue_summary()
        eligible = queue.eligible_for_current_account
        other = queue.queued_for_other_accounts
        account_text = f" for {queue.current_login}" if queue.current_login else ""
        details: list[str] = []
        if eligible:
            details.append(f"{eligible} queued{account_text}")
        if other:
            owners = ", ".join(queue.other_account_logins)
            owner_text = f" ({owners})" if owners else ""
            details.append(f"{other} queued for other account(s){owner_text}")
        queue_text = " · ".join(details) or "no queued actions"
        if self._manager.is_running:
            active_id = self._manager.active_action_id
            self._notice.setText(
                f"Identify action manager is working on action #{active_id or '…'} · {queue_text}."
            )
        elif self._manager.is_paused:
            suffix = ""
            if not eligible and other and queue.other_account_logins:
                suffix = f" Sign in as {queue.other_account_logins[0]} to submit its rows."
            self._notice.setText(f"Identify action manager is paused · {queue_text}.{suffix}")
        else:
            self._notice.setText(f"Identify action manager has authorized dispatch · {queue_text}.")

    def _update_controls(self) -> None:
        action = self._selected_action()
        queue = self._manager.queue_summary()
        self._resume_button.setEnabled(bool(queue.eligible_for_current_account))
        can_submit = False
        if action is not None:
            can_submit, _reason = self._manager.can_request_dispatch(action.local_action_id)
        self._submit_button.setEnabled(can_submit)
        self._pause_button.setEnabled(not self._manager.is_paused)
        self._cancel_button.setEnabled(bool(action and action.can_cancel))
        self._cancel_button.setText(action.cancel_label if action else "Cancel queued action")
        self._verify_button.setEnabled(bool(action and action.can_verify and not self._manager.is_running))
        self._verify_button.setText("Check iNaturalist again" if action and action.can_verify else "Verify again")
        self._retry_button.setEnabled(bool(action and action.can_retry_definite_failure))
        self._retry_anyway_button.setEnabled(bool(action and action.can_retry_anyway))
        self._open_button.setEnabled(bool(action and action.can_open_observation))
        self._details_button.setEnabled(action is not None)
        self._selection_hint.setText(self._next_step_text(action, can_submit))

    def _next_step_text(
        self,
        action: IdentifyActionPresentation | None,
        can_submit: bool,
    ) -> str:
        """Explain the intentionally conservative controls for the selected row."""
        if action is None:
            return "Select an action to see its safe next step."
        if action.state == "queued":
            if can_submit:
                return (
                    "This action has not been sent. Use Submit selected, or Resume queued "
                    "actions to send the current account's queued rows. Retry is only for a "
                    "write that definitely failed."
                )
            allowed, reason = self._manager.can_request_dispatch(action.local_action_id)
            return (
                "This action has not been sent. "
                + (reason if not allowed and reason else "Authenticate as the action's account to submit it.")
            )
        if action.state == "failed_retryable":
            return (
                "iNaturalist definitely rejected this action before applying it. Retry "
                "(re-queue) makes it queued again; then explicitly submit or resume it."
            )
        if action.state == "submitted_unverified":
            return (
                "The write may already exist on iNaturalist, but its result was not verified. "
                "Use Check iNaturalist again. Retrying is disabled to avoid creating a duplicate."
            )
        if action.state == "ambiguous":
            return (
                "The connection failed after the write may have reached iNaturalist. Check "
                "iNaturalist again, or use Retry anyway only if you accept duplicate-ID risk."
            )
        if action.state == "failed_terminal":
            return (
                "This action was rejected because its saved details need correction. Open Details, "
                "then create a corrected new action rather than retrying the same request."
            )
        if action.state == "submitting":
            return "This action is currently being submitted. Its controls will update when that finishes."
        return "This action is no longer pending. Open Details to review its local history."

    def _resume_queued(self) -> None:
        allowed, required = self._manager.can_resume_queued_actions()
        if not allowed:
            message = (
                f"Authenticate as {required} before resuming queued actions."
                if required
                else "No queued actions belong to the currently authenticated account."
            )
            QMessageBox.information(
                self,
                "Identify actions remain paused",
                message,
            )
            return
        self._manager.resume_queued_actions()
        self._update_notice()
        self._update_controls()

    def _submit_selected(self) -> None:
        action = self._selected_action()
        if action is None:
            return
        if self._manager.request_dispatch(action.local_action_id):
            self._update_notice()
            self._update_controls()
            return
        _allowed, reason = self._manager.can_request_dispatch(action.local_action_id)
        QMessageBox.information(
            self,
            "Identify action remains queued",
            reason or "The selected action could not be submitted.",
        )

    def _pause(self) -> None:
        self._manager.pause("Identify actions paused by the user.")
        self._update_notice()
        self._update_controls()

    def _cancel_selected(self) -> None:
        action = self._selected_action()
        if action is None or not action.can_cancel:
            return
        if action.state in {"ambiguous", "submitted_unverified"}:
            message = (
                "This action may already exist on iNaturalist. Stopping local tracking does not "
                "undo anything on iNaturalist."
            )
            title = "Stop tracking locally?"
            confirm = "Stop tracking locally"
        else:
            message = "This removes the queued local action. Nothing will be sent to iNaturalist."
            title = "Cancel queued action?"
            confirm = "Cancel queued action"
        box = QMessageBox(QMessageBox.Icon.Warning, title, message, parent=self)
        proceed = box.addButton(confirm, QMessageBox.ButtonRole.DestructiveRole)
        keep = box.addButton("Keep pending", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() is proceed:
            self._manager.cancel(action.local_action_id)

    def _verify_selected(self) -> None:
        action = self._selected_action()
        if action is not None:
            self._manager.verify_again(action.local_action_id)

    def _retry_definite_failure(self) -> None:
        action = self._selected_action()
        if action is not None:
            self._manager.retry_definite_failure(action.local_action_id)

    def _retry_anyway(self) -> None:
        action = self._selected_action()
        if action is None or not action.can_retry_anyway:
            return
        noun = "identification" if action.action_type == "identification" else "comment" if action.action_type == "comment" else "action"
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "Retry anyway despite duplicate risk?",
            f"The original {noun} action #{action.local_action_id} is ambiguous and may already "
            f"have succeeded on iNaturalist. Retrying can create a duplicate {noun}.\n\n"
            "A separate linked queued action will be recorded and will remain paused until you "
            "explicitly submit it or resume queued actions.",
            parent=self,
        )
        retry = box.addButton("Retry anyway", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is retry:
            retry_id = self._manager.retry_anyway(action.local_action_id, duplicate_risk_confirmed=True)
            if retry_id is not None:
                self.refresh(retry_id)

    def _open_selected(self) -> None:
        action = self._selected_action()
        if action is not None:
            _open_observation(action.observation_id)

    def _show_details(self) -> None:
        action = self._selected_action()
        if action is None:
            return
        dialog = IdentifyActionDetailsDialog(action, self)
        dialog.exec()

    def _manager_changed(self, _action: object) -> None:
        self.refresh()

    def _summary_changed(self, _summary: object) -> None:
        self._update_notice()
        self._update_controls()

    def _running_changed(self, _running: bool) -> None:
        self._update_notice()
        self._update_controls()

    def _paused(self, _reason: str) -> None:
        self._update_notice()
        self._update_controls()

    def _authentication_changed(self) -> None:
        self._update_notice()
        self._update_controls()

    def closeEvent(self, event) -> None:
        self._closed = True
        super().closeEvent(event)


def _open_observation(observation_id: int) -> None:
    if observation_id > 0:
        open_external_url_silently(f"https://www.inaturalist.org/observations/{observation_id}")


def _action_id_text(value: int | None) -> str:
    return f"#{value}" if value is not None else "—"


def _desired_state_text(value: bool | None) -> str:
    if value is None:
        return "—"
    return "Enabled" if value else "Disabled"
