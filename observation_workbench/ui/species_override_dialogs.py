"""Dialogs for planning species-name observation field updates."""

from __future__ import annotations

import re

from PySide6.QtCore import QRect, QThreadPool, Qt, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.client import INatClient
from observation_workbench.models import StudyPhoto
from observation_workbench.services.image_cache import ImageCache
from observation_workbench.services.species_override import (
    SPECIES_NAME_OVERRIDE_FIELD_NAME,
    SpeciesOverridePlan,
    SpeciesOverridePlanRow,
)
from observation_workbench.ui.bulk_disagree_dialogs import (
    _GalleryImageWorker,
    _HoldToZoomLabel,
    _photo_fetch_target,
)
from observation_workbench.ui.external_links import open_external_url_silently
from observation_workbench.ui.table_sort import (
    SortableCheckItem,
    SortableTableWidgetItem,
    enable_click_sorting,
    sorting_suspended,
)

_OBS_URL_ID_RE = re.compile(r"/observations/(\d+)")

# Matches ImagePrefetcher._MAX_CONCURRENT_BACKGROUND_PREFETCH so this dialog's
# photo downloads share the same modest, bounded concurrency budget.
_MAX_CONCURRENT_PHOTO_LOADS = 3


class SpeciesOverridePhotoBrowserDialog(QDialog):
    """Visual review of the observations selected for an override update."""

    def __init__(
        self,
        rows: list[SpeciesOverridePlanRow],
        *,
        client: INatClient,
        disk_cache: ImageCache,
        target_field_name: str = SPECIES_NAME_OVERRIDE_FIELD_NAME,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Browse {target_field_name} Photos")
        self.resize(1120, 780)
        self._rows = list(rows)
        self._client = client
        self._disk_cache = disk_cache
        self._pool = QThreadPool.globalInstance()
        self._ignored_ids: set[int] = set()
        self._cards: dict[int, QFrame] = {}
        self._photo_labels: dict[tuple[int, int], QLabel] = {}
        self._live_image_signals: set[object] = set()
        self._undo_stack: list[int] = []
        self._card_pending_photos: dict[int, list[StudyPhoto]] = {}
        self._cards_started: set[int] = set()
        self._photo_queue: list[tuple[int, StudyPhoto]] = []
        self._queued_photo_keys: set[tuple[int, int]] = set()
        self._in_flight_loads = 0

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Review every observation that is currently selected for the "
            f"{target_field_name} update. Choose Keep after confirming the species, "
            "or Ignore this update to remove an observation from the planned update."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self._count_label = QLabel("")
        layout.addWidget(self._count_label)
        self._undo_btn = QPushButton("Undo last ignore")
        self._undo_btn.setEnabled(False)
        self._undo_btn.clicked.connect(self._undo_last_ignore)
        tools = QHBoxLayout()
        tools.addWidget(self._undo_btn)
        tools.addStretch(1)
        layout.addLayout(tools)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        for row in self._rows:
            card = self._make_card(row)
            self._cards[row.observation_id] = card
            content_layout.addWidget(card)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self._scroll = scroll
        scroll.verticalScrollBar().valueChanged.connect(self._check_visible_cards)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn:
            close_btn.setText("Done")
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)
        self._update_count()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._check_visible_cards()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._check_visible_cards()

    def included_observation_ids(self) -> list[int]:
        return [
            row.observation_id
            for row in self._rows
            if row.observation_id not in self._ignored_ids
        ]

    def _make_card(self, row: SpeciesOverridePlanRow) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setStyleSheet("QFrame { background: #ffffff; } QLabel { color: #000000; }")
        layout = QVBoxLayout(card)
        title = QLabel(
            f"<b>Observation {row.observation_id}</b> by {row.observer_login}"
        )
        layout.addWidget(title)
        details = QLabel(
            f"Current: {row.consensus_name or '(none)'} | "
            f"Provisional: {row.provisional_name or '(none)'}"
        )
        details.setWordWrap(True)
        layout.addWidget(details)
        actions = QHBoxLayout()
        keep_btn = QPushButton("Keep")
        ignore_btn = QPushButton("Ignore this update")
        open_btn = QPushButton("Open observation")
        keep_btn.clicked.connect(lambda _checked=False, c=card: c.setVisible(False))
        ignore_btn.clicked.connect(
            lambda _checked=False, obs_id=row.observation_id: self._ignore(obs_id)
        )
        open_btn.clicked.connect(
            lambda _checked=False, url=row.observation_url: open_external_url_silently(
                url
            )
        )
        for button in (keep_btn, ignore_btn, open_btn):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        if not row.photos:
            no_photo = QLabel("No photos on this observation.")
            no_photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            no_photo.setMinimumHeight(120)
            layout.addWidget(no_photo)
        else:
            grid = QGridLayout()
            columns = 2 if len(row.photos) > 1 else 1
            for index, photo in enumerate(row.photos):
                label = _HoldToZoomLabel(
                    f"Loading photo {index + 1} of {len(row.photos)}..."
                )
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setMinimumHeight(240)
                label.setStyleSheet("QLabel { background: #111; color: #ddd; }")
                grid.addWidget(label, index // columns, index % columns)
                self._photo_labels[(row.observation_id, photo.photo_id)] = label
            layout.addLayout(grid)
            self._card_pending_photos[row.observation_id] = list(row.photos)
        return card

    def _check_visible_cards(self) -> None:
        if not self._card_pending_photos:
            return
        viewport = self._scroll.viewport()
        viewport_rect = viewport.rect()
        for obs_id in list(self._card_pending_photos):
            if obs_id in self._cards_started:
                continue
            card = self._cards.get(obs_id)
            if card is None or not card.isVisible():
                continue
            top_left = card.mapTo(viewport, card.rect().topLeft())
            card_rect = QRect(top_left, card.size())
            if not card_rect.intersects(viewport_rect):
                continue
            self._cards_started.add(obs_id)
            for photo in self._card_pending_photos.pop(obs_id):
                self._enqueue_photo(obs_id, photo)
        self._pump_photo_queue()

    def _enqueue_photo(self, obs_id: int, photo) -> None:
        key = (obs_id, photo.photo_id)
        if key in self._queued_photo_keys:
            return
        self._queued_photo_keys.add(key)
        self._photo_queue.append((obs_id, photo))

    def _pump_photo_queue(self) -> None:
        while self._photo_queue and self._in_flight_loads < _MAX_CONCURRENT_PHOTO_LOADS:
            obs_id, photo = self._photo_queue.pop(0)
            self._queued_photo_keys.discard((obs_id, photo.photo_id))
            self._in_flight_loads += 1
            self._load_photo(obs_id, photo)

    def _photo_load_finished(self) -> None:
        self._in_flight_loads = max(0, self._in_flight_loads - 1)
        self._pump_photo_queue()

    def _load_photo(self, obs_id: int, photo) -> None:
        target = _photo_fetch_target(photo, "large")
        if target is None:
            self._photo_load_finished()
            return
        size, url = target
        worker = _GalleryImageWorker(
            obs_id=obs_id,
            photo_id=photo.photo_id,
            image_url=url,
            client=self._client,
            disk_cache=self._disk_cache,
            size=size,
        )
        signals = worker.signals
        self._live_image_signals.add(signals)
        signals.loaded.connect(
            lambda loaded_obs_id, photo_id, data, s=signals: (
                self._live_image_signals.discard(s),
                self._on_photo_loaded(loaded_obs_id, photo_id, data),
            )
        )
        signals.failed.connect(
            lambda loaded_obs_id, photo_id, message, s=signals: (
                self._live_image_signals.discard(s),
                self._on_photo_failed(loaded_obs_id, photo_id, message),
            )
        )
        self._pool.start(worker)

    @Slot(int, int, object)
    def _on_photo_loaded(self, obs_id: int, photo_id: int, data: bytes) -> None:
        self._photo_load_finished()
        label = self._photo_labels.get((obs_id, photo_id))
        if label is None:
            return
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        if pixmap.isNull():
            label.setText("Could not decode photo.")
            return
        if isinstance(label, _HoldToZoomLabel):
            label.set_full_pixmap(pixmap)
        scaled = pixmap.scaled(
            520,
            540,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(scaled)
        label.setMinimumHeight(max(180, scaled.height()))

    def _on_photo_failed(self, obs_id: int, photo_id: int, message: str) -> None:
        self._photo_load_finished()
        label = self._photo_labels.get((obs_id, photo_id))
        if label is not None:
            label.setText(f"Could not load photo: {message}")

    def _ignore(self, obs_id: int) -> None:
        if obs_id in self._ignored_ids:
            return
        self._ignored_ids.add(obs_id)
        self._undo_stack.append(obs_id)
        card = self._cards.get(obs_id)
        if card is not None:
            card.setVisible(False)
        self._update_count()

    def _undo_last_ignore(self) -> None:
        while self._undo_stack:
            obs_id = self._undo_stack.pop()
            if obs_id not in self._ignored_ids:
                continue
            self._ignored_ids.remove(obs_id)
            card = self._cards.get(obs_id)
            if card is not None:
                card.setVisible(True)
            break
        self._update_count()

    def _update_count(self) -> None:
        included = len(self._rows) - len(self._ignored_ids)
        self._count_label.setText(
            f"{included} observation(s) included; {len(self._ignored_ids)} ignored."
        )
        self._undo_btn.setEnabled(bool(self._undo_stack))


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
