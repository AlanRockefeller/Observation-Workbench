"""Dialogs for planning species-name observation field updates."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QVBoxLayout,
)

from observation_workbench.api.client import INatClient
from observation_workbench.models import StudyObservation
from observation_workbench.services.bulk_disagree import BulkDisagreeCandidate
from observation_workbench.services.image_cache import ImageCache
from observation_workbench.services.species_override import (
    SPECIES_NAME_OVERRIDE_FIELD_NAME,
    SpeciesOverridePlan,
    SpeciesOverridePlanRow,
)
from observation_workbench.ui.bulk_disagree_dialogs import (
    BulkDisagreePhotoBrowserDialog,
)
from observation_workbench.ui.external_links import open_external_url_silently
from observation_workbench.ui.table_sort import (
    SortableCheckItem,
    SortableTableWidgetItem,
    enable_click_sorting,
    sorting_suspended,
)

_OBS_URL_ID_RE = re.compile(r"/observations/(\d+)")


class SpeciesOverridePhotoBrowserDialog(BulkDisagreePhotoBrowserDialog):
    """Adapt field-update rows to the shared observation photo browser."""

    def __init__(
        self,
        rows: list[SpeciesOverridePlanRow],
        *,
        client: INatClient,
        disk_cache: ImageCache,
        target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
        proposed_species_name: str = "",
        parent=None,
    ) -> None:
        candidates = [
            BulkDisagreeCandidate(
                observation=StudyObservation(
                    obs_id=row.observation_id,
                    observer_login=row.observer_login,
                    provisional_species_name=row.provisional_name,
                    photos=list(row.photos),
                ),
                source_taxon_id=0,
                source_taxon_name="",
                target_taxon_id=0,
                target_taxon_name=proposed_species_name,
                current_observation_taxon_name=row.consensus_name,
                explicit_disagreement=False,
            )
            for row in rows
        ]
        super().__init__(
            candidates,
            client=client,
            disk_cache=disk_cache,
            api_token="",
            login="",
            require_source_taxon_match=False,
            target_field_name=target_field_name,
            window_title=f"Browse {target_field_name} Photos",
            parent=parent,
        )

    def included_observation_ids(self) -> list[int]:
        return [candidate.observation.obs_id for candidate in self.candidates()]


class SpeciesOverrideSetupDialog(QDialog):
    def __init__(
        self,
        *,
        prefill_provisional_name: str = "",
        target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._target_field_name = target_field_name
        self.setWindowTitle(f"Update {target_field_name}")
        self.resize(760, 380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        layout.addLayout(grid)

        self._provisional_radio = QRadioButton("Search by Provisional Species Name")
        self._provisional_radio.setChecked(True)
        grid.addWidget(self._provisional_radio, 0, 0, 1, 2)

        grid.addWidget(QLabel("Provisional Species Name:"), 1, 0)
        self._provisional_edit = QLineEdit()
        self._provisional_edit.setPlaceholderText("Name to search for")
        self._provisional_edit.setText(prefill_provisional_name.strip())
        grid.addWidget(self._provisional_edit, 1, 1)

        self._list_radio = QRadioButton("Use pasted observation IDs or URLs")
        grid.addWidget(self._list_radio, 2, 0, 1, 2)

        grid.addWidget(QLabel("Observations:"), 3, 0)
        self._numbers_edit = QPlainTextEdit()
        self._numbers_edit.setPlaceholderText(
            "Paste observation IDs or iNaturalist observation URLs, separated by spaces, commas, or lines"
        )
        self._numbers_edit.setFixedHeight(94)
        grid.addWidget(self._numbers_edit, 3, 1)

        self._numbers_status = QLabel("")
        self._numbers_status.setWordWrap(True)
        grid.addWidget(self._numbers_status, 4, 1)

        grid.addWidget(QLabel(f"{target_field_name}:"), 5, 0)
        self._override_edit = QLineEdit()
        self._override_edit.setPlaceholderText(f"{target_field_name} value to set")
        grid.addWidget(self._override_edit, 5, 1)

        self._genus_cb = QCheckBox("Only update observations matching genus:")
        grid.addWidget(self._genus_cb, 6, 0)
        self._genus_edit = QLineEdit()
        self._genus_edit.setPlaceholderText("Genus name")
        self._genus_edit.setEnabled(False)
        grid.addWidget(self._genus_edit, 6, 1)

        self._validation_label = QLabel("")
        self._validation_label.setWordWrap(True)
        self._validation_label.setStyleSheet("QLabel { color: #b00020; }")
        layout.addWidget(self._validation_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._plan_btn = QPushButton("Plan")
        buttons.addButton(self._plan_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self._plan_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)

        self._provisional_edit.textChanged.connect(self._update_plan_enabled)
        self._numbers_edit.textChanged.connect(self._on_numbers_changed)
        self._override_edit.textChanged.connect(self._update_plan_enabled)
        self._provisional_radio.toggled.connect(self._on_source_changed)
        self._list_radio.toggled.connect(self._on_source_changed)
        self._genus_cb.toggled.connect(self._on_genus_toggled)
        self._genus_edit.textChanged.connect(self._update_plan_enabled)
        self._observation_ids: list[int] = []
        self._invalid_tokens: list[str] = []
        self._on_source_changed()
        self._on_numbers_changed()
        self._update_plan_enabled()

    def source_mode(self) -> str:
        return "observations" if self._list_radio.isChecked() else "provisional"

    def provisional_name(self) -> str:
        return self._provisional_edit.text().strip()

    def override_name(self) -> str:
        return self._override_edit.text().strip()

    def observation_ids(self) -> list[int]:
        return list(self._observation_ids)

    def invalid_tokens(self) -> list[str]:
        return list(self._invalid_tokens)

    def genus_filter(self) -> str:
        if not self._genus_cb.isChecked():
            return ""
        return self._genus_edit.text().strip()

    def _on_source_changed(self) -> None:
        list_mode = self.source_mode() == "observations"
        self._provisional_edit.setEnabled(not list_mode)
        self._numbers_edit.setEnabled(list_mode)
        self._update_plan_enabled()

    def _on_genus_toggled(self, checked: bool) -> None:
        self._genus_edit.setEnabled(checked)
        self._update_plan_enabled()

    def _on_numbers_changed(self) -> None:
        ids, invalid = parse_observation_id_tokens(self._numbers_edit.toPlainText())
        self._observation_ids = ids
        self._invalid_tokens = invalid
        parts = [f"{len(ids)} observation number(s) recognized."]
        if invalid:
            shown = ", ".join(invalid[:5])
            if len(invalid) > 5:
                shown += ", ..."
            parts.append(f"Ignoring unrecognized: {shown}")
        self._numbers_status.setText(" ".join(parts))
        self._update_plan_enabled()

    def _update_plan_enabled(self) -> None:
        if self.source_mode() == "provisional" and not self.provisional_name():
            self._validation_label.setText("Enter a Provisional Species Name.")
            self._plan_btn.setEnabled(False)
            return
        if self.source_mode() == "observations" and not self._observation_ids:
            self._validation_label.setText("Enter at least one observation ID or URL.")
            self._plan_btn.setEnabled(False)
            return
        if not self.override_name():
            self._validation_label.setText(f"Enter a {self._target_field_name} value.")
            self._plan_btn.setEnabled(False)
            return
        if self._genus_cb.isChecked() and not self.genus_filter():
            self._validation_label.setText(
                "Enter a genus name, or clear the genus gate."
            )
            self._plan_btn.setEnabled(False)
            return
        self._validation_label.setText("")
        self._plan_btn.setEnabled(True)


class SpeciesOverridePlanDialog(QDialog):
    def __init__(
        self,
        plan: SpeciesOverridePlan,
        *,
        login: str = "",
        client: INatClient | None = None,
        disk_cache: ImageCache | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Plan {plan.target_field_name} Update")
        self.resize(1180, 580)
        self._plan = plan
        self._populating = False
        self._client = client
        self._disk_cache = disk_cache

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        summary = QLabel(self._summary_text(login))
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self._count_label = QLabel("")
        layout.addWidget(self._count_label)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            [
                "Use",
                "Status",
                "Observation ID",
                "Observer",
                "Current consensus name",
                "Provisional Species Name",
                plan.target_field_name,
                "URL",
            ]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.itemChanged.connect(self._update_start_enabled)
        self._table.itemSelectionChanged.connect(self._update_open_enabled)
        self._table.itemDoubleClicked.connect(lambda item: self._open_row(item.row()))
        enable_click_sorting(self._table)
        layout.addWidget(self._table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._check_all_btn = QPushButton("Check all")
        self._uncheck_all_btn = QPushButton("Uncheck all")
        self._open_selected_btn = QPushButton("Open selected")
        self._browse_btn = QPushButton("Browse photos...")
        self._start_btn = QPushButton("Start")
        buttons.addButton(self._check_all_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self._uncheck_all_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(
            self._open_selected_btn, QDialogButtonBox.ButtonRole.ActionRole
        )
        buttons.addButton(self._browse_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self._start_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self._check_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        self._uncheck_all_btn.clicked.connect(lambda: self._set_all_checked(False))
        self._open_selected_btn.clicked.connect(self._open_selected_observation)
        self._browse_btn.clicked.connect(self._browse_photos)
        self._start_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)

        self._populate_table()
        self._update_start_enabled()
        self._update_open_enabled()
        self._browse_btn.setEnabled(
            bool(client and disk_cache and self.selected_observation_ids())
        )

    def selected_observation_ids(self) -> list[int]:
        selected: list[int] = []
        for row in range(self._table.rowCount()):
            check_item = self._table.item(row, 0)
            obs_id_item = self._table.item(row, 2)
            if check_item is None or obs_id_item is None:
                continue
            if check_item.checkState() != Qt.CheckState.Checked:
                continue
            try:
                selected.append(int(obs_id_item.text()))
            except ValueError:
                continue
        return selected

    def _summary_text(self, login: str) -> str:
        if self._plan.source_mode == "observations":
            text = (
                f"Loaded {self._plan.total_observations} pasted observation(s). "
                f"Set {self._plan.target_field_name} to {self._plan.override_name}."
            )
        else:
            text = (
                f"Matched {self._plan.total_observations} observation(s) with "
                f"Provisional Species Name = {self._plan.provisional_name}. "
                f"Set {self._plan.target_field_name} to {self._plan.override_name}."
            )
        if self._plan.genus_filter:
            text += f" Genus gate: only rows matching {self._plan.genus_filter} are selectable."
        if login:
            text += f" The write will be made as {login}."
        if self._plan.missing_provisional_fields:
            text += (
                f" {self._plan.missing_provisional_fields} matching observation(s) "
                "could not be confirmed from the returned field values and will be skipped."
            )
        skipped = self._plan.skipped_row_count
        if skipped:
            text += f" {skipped} row(s) are not selectable; see the Status column."
        return text

    def _populate_table(self) -> None:
        self._populating = True
        try:
            with sorting_suspended(self._table):
                self._table.setRowCount(len(self._plan.rows))
                for row_index, row in enumerate(self._plan.rows):
                    check_item = SortableCheckItem("")
                    flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    if row.update_allowed:
                        flags |= Qt.ItemFlag.ItemIsUserCheckable
                    check_item.setFlags(flags)
                    check_item.setCheckState(
                        Qt.CheckState.Checked
                        if row.update_allowed
                        else Qt.CheckState.Unchecked
                    )
                    self._table.setItem(row_index, 0, check_item)

                    values = [
                        "Ready" if row.update_allowed else row.skip_reason,
                        str(row.observation_id),
                        row.observer_login,
                        row.consensus_name,
                        row.provisional_name,
                        row.override_value,
                        row.observation_url,
                    ]
                    for col, value in enumerate(values, start=1):
                        item = SortableTableWidgetItem(value)
                        item.setFlags(
                            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                        )
                        self._table.setItem(row_index, col, item)
        finally:
            self._populating = False
        self._table.resizeColumnsToContents()

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(state)
        self._update_start_enabled()

    def _update_start_enabled(self, *_args) -> None:
        if self._populating:
            return
        selected = len(self.selected_observation_ids())
        self._count_label.setText(
            f"{selected} of {self._plan.updatable_row_count} updatable observation(s) selected; "
            f"{self._plan.skipped_row_count} skipped."
        )
        self._start_btn.setEnabled(selected > 0)
        self._browse_btn.setEnabled(
            bool(self._client and self._disk_cache and selected)
        )

    def _browse_photos(self) -> None:
        if not self._client or not self._disk_cache:
            return
        selected = set(self.selected_observation_ids())
        rows = [
            row
            for row in self._plan.rows
            if row.update_allowed and row.observation_id in selected
        ]
        dlg = SpeciesOverridePhotoBrowserDialog(
            rows,
            client=self._client,
            disk_cache=self._disk_cache,
            target_field_name=self._plan.target_field_name,
            proposed_species_name=self._plan.override_name,
            parent=self,
        )
        dlg.exec()
        included = set(dlg.included_observation_ids())
        self._populating = True
        try:
            for table_row in range(self._table.rowCount()):
                check_item = self._table.item(table_row, 0)
                obs_item = self._table.item(table_row, 2)
                if check_item is None or obs_item is None:
                    continue
                if check_item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    check_item.setCheckState(
                        Qt.CheckState.Checked
                        if int(obs_item.text()) in included
                        else Qt.CheckState.Unchecked
                    )
        finally:
            self._populating = False
        self._update_start_enabled()

    def _update_open_enabled(self) -> None:
        self._open_selected_btn.setEnabled(self._current_row() is not None)

    def _current_row(self) -> int | None:
        row = self._table.currentRow()
        if row < 0 or row >= self._table.rowCount():
            return None
        return row

    def _open_selected_observation(self) -> None:
        row = self._current_row()
        if row is not None:
            self._open_row(row)

    def _open_row(self, row: int) -> None:
        if row < 0 or row >= self._table.rowCount():
            return
        item = self._table.item(row, 7)
        if item is None:
            return
        url = item.text().strip()
        if url:
            open_external_url_silently(url)


def parse_observation_id_tokens(text: str) -> tuple[list[int], list[str]]:
    """Parse bare observation IDs and pasted iNaturalist observation URLs."""
    ids: list[int] = []
    invalid: list[str] = []
    seen: set[int] = set()
    for token in re.split(r"[\s,]+", text.strip()):
        if not token:
            continue
        obs_id: int | None = None
        if token.isdigit():
            obs_id = int(token)
        else:
            match = _OBS_URL_ID_RE.search(token)
            if match:
                obs_id = int(match.group(1))
        if obs_id and obs_id > 0:
            if obs_id not in seen:
                seen.add(obs_id)
                ids.append(obs_id)
        else:
            invalid.append(token)
    return ids, invalid
