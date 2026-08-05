"""
Filter bar widget: username, place autocomplete, taxon autocomplete,
checkboxes, optional date range, Load/Cancel buttons.

Autocomplete uses a background worker so API calls don't freeze typing.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from PySide6.QtCore import (
    QDate, QEvent, QObject, QRunnable, QStringListModel, QThreadPool, Qt, QTimer, Signal, Slot,
)
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDateEdit, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from observation_workbench.api.client import INatClient
from observation_workbench.api.observation_url import is_probable_url_input
from observation_workbench.models import StudyTaxon
from observation_workbench.ui.taxon_autocomplete import (
    TaxonAutocompleteItem,
    TaxonAutocompleteResult,
    TaxonAutocompleteWorker,
)

log = logging.getLogger(__name__)

# Rank combo items ordered most-specific-first (lowest iNat level first).
# Each tuple is (display_name, level).
RANK_COMBO_ITEMS: List[Tuple[str, int]] = [
    ("Infrahybrid", 5),
    ("Form", 5),
    ("Variety", 5),
    ("Subspecies", 5),
    ("Species", 10),
    ("Hybrid", 10),
    ("Complex", 11),
    ("Subsection", 12),
    ("Section", 13),
    ("Subgenus", 15),
    ("Genus", 20),
    ("Subtribe", 24),
    ("Tribe", 25),
    ("Supertribe", 26),
    ("Subfamily", 27),
    ("Family", 30),
    ("Epifamily", 32),
    ("Superfamily", 33),
    ("Infraorder", 35),
    ("Suborder", 37),
    ("Order", 40),
    ("Superorder", 43),
    ("Infraclass", 45),
    ("Subclass", 47),
    ("Class", 50),
    ("Superclass", 53),
    ("Subphylum", 57),
    ("Phylum", 60),
    ("Kingdom", 70),
    ("Stateofmatter", 100),
]

# Default rank: "Species" (level 10) — index in RANK_COMBO_ITEMS
_DEFAULT_RANK_INDEX = next(
    i for i, (name, _) in enumerate(RANK_COMBO_ITEMS) if name == "Species"
)


class _AutocompleteSignals(QObject):
    results = Signal(list)  # list of (name, id) tuples
    error = Signal(str)


class _PlaceAutocompleteWorker(QRunnable):
    def __init__(self, client: INatClient, query: str, generation: int, get_gen) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.query = query
        self.generation = generation
        self.get_gen = get_gen
        self.signals = _AutocompleteSignals()

    def run(self) -> None:
        if self.get_gen() != self.generation:
            return
        try:
            raw = self.client.get_places_autocomplete(self.query, per_page=7)
            results = raw.get("results") or []
            items = []
            for r in results:
                name = r.get("display_name") or r.get("name") or ""
                pid = r.get("id")
                if name and pid:
                    items.append((name, int(pid)))
            if self.get_gen() == self.generation:
                self.signals.results.emit(items)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(str(exc))


class _UserValidateSignals(QObject):
    found = Signal(str)   # canonical login
    not_found = Signal()
    error = Signal(str)


class _UserValidateWorker(QRunnable):
    def __init__(self, client: INatClient, login: str, generation: int, get_gen) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.client = client
        self.login = login
        self.generation = generation
        self.get_gen = get_gen
        self.signals = _UserValidateSignals()

    def run(self) -> None:
        if self.get_gen() != self.generation:
            return
        try:
            user = self.client.get_user(self.login)
            if self.get_gen() == self.generation:
                if user:
                    self.signals.found.emit(user.get("login", self.login))
                else:
                    self.signals.not_found.emit()
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(str(exc))


class FilterBar(QWidget):
    """
    Emits load_requested(filters_dict) when Load is clicked.
    Emits cancel_requested() when Cancel is clicked.
    """

    load_requested = Signal(dict)
    cancel_requested = Signal()
    provisional_filter_changed = Signal(bool)
    navigation_repeat_rate_changed = Signal(float)

    def __init__(self, client: INatClient, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._client = client
        self._pool = QThreadPool.globalInstance()

        # Resolved IDs for place/taxon
        self._place_id: Optional[int] = None
        self._place_name: str = ""
        self._taxon_id: Optional[int] = None
        self._taxon_name: str = ""

        # Autocomplete / validation generation tokens
        self._username_gen = 0
        self._place_gen = 0
        self._taxon_gen = 0
        self._live_ac_signals: set = set()  # prevent GC of autocomplete signal objects

        # Debounce timers
        self._username_timer = QTimer(self)
        self._username_timer.setSingleShot(True)
        self._username_timer.timeout.connect(self._fetch_username_validate)
        self._place_timer = QTimer(self)
        self._place_timer.setSingleShot(True)
        self._place_timer.timeout.connect(self._fetch_place_autocomplete)
        self._taxon_timer = QTimer(self)
        self._taxon_timer.setSingleShot(True)
        self._taxon_timer.timeout.connect(self._fetch_taxon_autocomplete)

        # Autocomplete models
        self._place_model = QStringListModel(self)
        self._taxon_model = QStringListModel(self)
        self._place_data: List[Tuple[str, int]] = []
        self._taxon_data: List[TaxonAutocompleteItem] = []

        # Username history
        self._username_history: List[str] = []
        self._username_history_model = QStringListModel(self)

        self._build_ui()

    def _build_ui(self) -> None:
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(4, 2, 4, 2)
        vbox.setSpacing(2)

        # ── Row 1: identifier / place / taxon / filter controls ──────────
        row1 = QHBoxLayout()
        row1.setSpacing(4)

        row1.addWidget(QLabel("Identifier / URL:"))
        self._username_edit = QLineEdit()
        self._username_edit.setPlaceholderText("e.g. deniszabin or an iNaturalist observations URL")
        self._username_edit.setMinimumWidth(80)
        self._username_edit.setToolTip(
            "iNaturalist username, or an iNaturalist observations URL to study its returned observations"
        )
        self._username_edit.textEdited.connect(self._on_username_text_changed)
        history_completer = QCompleter(self._username_history_model, self)
        history_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        history_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        history_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        history_completer.activated.connect(self._on_username_history_selected)
        self._username_edit.setCompleter(history_completer)
        self._username_completer = history_completer
        self._username_edit.installEventFilter(self)
        row1.addWidget(self._username_edit)
        self._username_check = QLabel("✓")
        self._username_check.setStyleSheet("color: #2ecc71; font-weight: bold;")
        self._username_check.setFixedWidth(16)
        self._username_check.setVisible(False)
        self._username_check.setToolTip("Username found on iNaturalist")
        row1.addWidget(self._username_check)

        row1.addSpacing(4)
        row1.addWidget(QLabel("Place:"))
        self._place_edit = QLineEdit()
        self._place_edit.setPlaceholderText("Type to search…")
        self._place_edit.setMinimumWidth(140)
        self._place_edit.setToolTip("Filter by place (optional)")
        place_completer = QCompleter(self._place_model, self)
        place_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        place_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        place_completer.setMaxVisibleItems(12)
        place_completer.activated.connect(self._on_place_selected)
        self._place_edit.setCompleter(place_completer)
        self._place_completer = place_completer
        self._place_edit.textEdited.connect(self._on_place_text_changed)
        row1.addWidget(self._place_edit)
        self._place_check = QLabel("✓")
        self._place_check.setStyleSheet("color: #2ecc71; font-weight: bold;")
        self._place_check.setFixedWidth(16)
        self._place_check.setVisible(False)
        self._place_check.setToolTip("Place found on iNaturalist")
        row1.addWidget(self._place_check)

        row1.addSpacing(4)
        row1.addWidget(QLabel("Taxon:"))
        self._taxon_edit = QLineEdit()
        self._taxon_edit.setPlaceholderText("Type to search…")
        self._taxon_edit.setMinimumWidth(120)
        self._taxon_edit.setToolTip("Filter by taxon (includes descendants by default)")
        taxon_completer = QCompleter(self._taxon_model, self)
        taxon_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        taxon_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        taxon_completer.setMaxVisibleItems(12)
        taxon_completer.activated[str].connect(self._on_taxon_selected)
        self._taxon_edit.setCompleter(taxon_completer)
        self._taxon_completer = taxon_completer
        self._taxon_edit.textEdited.connect(self._on_taxon_text_changed)
        row1.addWidget(self._taxon_edit)

        row1.addSpacing(8)
        self._leading_cb = QCheckBox("Leading only")
        self._leading_cb.setToolTip("Only show identifications that are currently leading the community ID")
        row1.addWidget(self._leading_cb)

        row1.addSpacing(8)
        self._rank_filter_cb = QCheckBox("Min rank:")
        self._rank_filter_cb.setChecked(True)
        self._rank_filter_cb.setToolTip("Filter identifications to a minimum taxonomic rank")
        row1.addWidget(self._rank_filter_cb)
        self._rank_combo = QComboBox()
        for name, level in RANK_COMBO_ITEMS:
            self._rank_combo.addItem(name, level)
        self._rank_combo.setCurrentIndex(_DEFAULT_RANK_INDEX)
        self._rank_combo.setMinimumWidth(90)
        self._rank_combo.setToolTip("Minimum rank to include (and all more specific ranks)")
        row1.addWidget(self._rank_combo)
        self._exact_rank_cb = QCheckBox("Exact")
        self._exact_rank_cb.setChecked(False)
        self._exact_rank_cb.setToolTip("Only show identifications at exactly this rank")
        row1.addWidget(self._exact_rank_cb)
        self._provisional_cb = QCheckBox("Provisional Name")
        self._provisional_cb.setToolTip("Only show observations with taxon names containing an apostrophe")
        self._provisional_cb.toggled.connect(self.provisional_filter_changed)
        row1.addWidget(self._provisional_cb)
        self._rank_filter_cb.toggled.connect(self._on_rank_filter_toggled)

        row1.addStretch()
        vbox.addLayout(row1)

        # ── Row 2: date range + load / cancel ────────────────────────────
        row2 = QHBoxLayout()
        row2.setSpacing(4)

        row2.addWidget(QLabel("From:"))
        self._d1_edit = QDateEdit()
        self._d1_edit.setDisplayFormat("yyyy-MM-dd")
        self._d1_edit.setCalendarPopup(True)
        self._d1_edit.setSpecialValueText("(any)")
        self._d1_edit.setMinimumDate(QDate(2008, 1, 1))
        self._d1_edit.setDate(QDate(2008, 1, 1))
        self._d1_edit.setMinimumWidth(100)
        self._d1_edit.setToolTip("Start date (optional)")
        row2.addWidget(self._d1_edit)

        row2.addSpacing(4)
        row2.addWidget(QLabel("To:"))
        self._d2_edit = QDateEdit()
        self._d2_edit.setDisplayFormat("yyyy-MM-dd")
        self._d2_edit.setCalendarPopup(True)
        self._d2_edit.setSpecialValueText("(any)")
        self._d2_edit.setMinimumDate(QDate(2008, 1, 1))
        # Start at the minimum so the field reads "(any)", like From: the
        # default load carries no end-date filter at all.
        self._d2_edit.setDate(self._d2_edit.minimumDate())
        self._d2_edit.setMinimumWidth(100)
        self._d2_edit.setToolTip("End date (optional)")
        row2.addWidget(self._d2_edit)

        row2.addSpacing(8)
        row2.addWidget(QLabel("Arrow rate:"))
        self._navigation_rate_combo = QComboBox()
        for label, rate in (
            ("0.5 / sec", 0.5),
            ("1 / sec", 1.0),
            ("2 / sec", 2.0),
            ("3 / sec", 3.0),
            ("4 / sec", 4.0),
            ("5 / sec", 5.0),
            ("8 / sec", 8.0),
            ("10 / sec", 10.0),
        ):
            self._navigation_rate_combo.addItem(label, rate)
        self._navigation_rate_combo.setMinimumWidth(82)
        self._navigation_rate_combo.setToolTip(
            "Observation changes per second while the Left or Right arrow is held"
        )
        self._navigation_rate_combo.currentIndexChanged.connect(
            self._emit_navigation_repeat_rate
        )
        row2.addWidget(self._navigation_rate_combo)

        row2.addStretch()

        self._load_btn = QPushButton("Load")
        self._load_btn.setDefault(True)
        self._load_btn.setMinimumWidth(64)
        self._load_btn.setToolTip("Fetch results (R)")
        self._load_btn.clicked.connect(self._on_load_clicked)
        row2.addWidget(self._load_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setMinimumWidth(64)
        self._cancel_btn.setToolTip("Cancel in-progress load")
        self._cancel_btn.clicked.connect(self.cancel_requested)
        row2.addWidget(self._cancel_btn)

        vbox.addLayout(row2)

        # Fixed height — filter bar should never stretch vertically
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def focus_username(self) -> None:
        self._username_edit.setFocus()
        self._username_edit.selectAll()

    def set_loading(self, is_loading: bool) -> None:
        self._load_btn.setEnabled(not is_loading)
        self._cancel_btn.setEnabled(is_loading)

    def get_filters(self) -> dict:
        d1_date = self._d1_edit.date()
        d2_date = self._d2_edit.date()
        # Treat the minimum sentinel date as "no filter" — that is the value the
        # "(any)" special text stands for in BOTH edits, so both must compare
        # against their own minimum. Today's date is a real end date.
        d1 = (
            d1_date.toString("yyyy-MM-dd")
            if d1_date != self._d1_edit.minimumDate()
            else None
        )
        d2 = (
            d2_date.toString("yyyy-MM-dd")
            if d2_date != self._d2_edit.minimumDate()
            else None
        )
        taxon_id, taxon_name = self._resolved_taxon_filter()

        return {
            "username": self._source_text(),
            "place_id": self._place_id,
            "place_name": self._place_name or self._place_edit.text().strip(),
            "taxon_id": taxon_id,
            "taxon_name": taxon_name,
            "leading_only": self._leading_cb.isChecked(),
            "d1": d1,
            "d2": d2,
            "rank_level": self._rank_combo.currentData() if self._rank_filter_cb.isChecked() else None,
            "rank_name": self._rank_combo.currentText().lower() if self._rank_filter_cb.isChecked() else None,
            "exact_rank": self._exact_rank_cb.isChecked() if self._rank_filter_cb.isChecked() else False,
            "provisional_name_only": self._provisional_cb.isChecked(),
        }

    def restore_state(self, s) -> None:
        """Restore filter bar from AppSettings."""
        if s.last_username:
            self._username_edit.setText(s.last_username)
            if len(s.last_username) >= 2 and not is_probable_url_input(s.last_username):
                self._username_timer.start(200)
        if s.last_place_name:
            self._place_edit.setText(s.last_place_name)
            self._place_id = s.last_place_id
            self._place_name = s.last_place_name
            if s.last_place_id:
                self._place_check.setVisible(True)
        if s.last_taxon_name and s.last_taxon_id is not None:
            # Strip common-name prefix if a previous session saved "Common (Sci)" format
            taxon_name = s.last_taxon_name
            if taxon_name.endswith(")") and " (" in taxon_name:
                taxon_name = taxon_name[taxon_name.rfind(" (") + 2:-1]
            self._taxon_edit.setText(taxon_name)
            self._taxon_id = s.last_taxon_id
            self._taxon_name = taxon_name
        # Username history
        self._username_history = s.username_history
        self._username_history_model.setStringList(self._username_history)
        # Rank filter
        enabled = s.rank_filter_enabled
        self._rank_filter_cb.setChecked(enabled)
        idx = self._rank_combo.findText(s.rank_filter_name)
        if idx >= 0:
            self._rank_combo.setCurrentIndex(idx)
        self._exact_rank_cb.setChecked(s.rank_exact)
        self._rank_combo.setEnabled(enabled)
        self._exact_rank_cb.setEnabled(enabled)
        self._provisional_cb.setChecked(s.provisional_name_only)
        repeat_rate = float(s.navigation_repeat_rate)
        best_index = min(
            range(self._navigation_rate_combo.count()),
            key=lambda index: abs(
                float(self._navigation_rate_combo.itemData(index)) - repeat_rate
            ),
        )
        self._navigation_rate_combo.setCurrentIndex(best_index)

    def save_state(self, s) -> None:
        """Persist filter bar to AppSettings."""
        s.last_username = self._username_edit.text().strip()
        s.last_place_id = self._place_id
        s.last_place_name = self._place_name or self._place_edit.text().strip()
        s.last_taxon_id = self._taxon_id
        s.last_taxon_name = self._taxon_name if self._taxon_id is not None else ""
        s.username_history = self._username_history
        s.rank_filter_enabled = self._rank_filter_cb.isChecked()
        s.rank_filter_name = self._rank_combo.currentText()
        s.rank_exact = self._exact_rank_cb.isChecked()
        s.provisional_name_only = self._provisional_cb.isChecked()

    def _emit_navigation_repeat_rate(self, _index: int) -> None:
        self.navigation_repeat_rate_changed.emit(
            float(self._navigation_rate_combo.currentData())
        )

    def _resolved_taxon_filter(self) -> Tuple[Optional[int], str]:
        taxon_text = self._taxon_edit.text().strip()
        if self._taxon_id is not None or not taxon_text:
            return self._taxon_id, self._taxon_name or taxon_text

        match = taxon_text.casefold()
        for item in self._taxon_data:
            label = item.display_name(StudyTaxon.show_common_names)
            if match in {item.scientific_name.casefold(), label.casefold()}:
                self._taxon_id = item.taxon_id
                self._taxon_name = item.scientific_name
                self._taxon_edit.setText(item.scientific_name)
                self._taxon_edit.setToolTip(
                    f"Taxon: {item.scientific_name}  (id={item.taxon_id})"
                )
                return item.taxon_id, item.scientific_name

        return None, taxon_text

    # ------------------------------------------------------------------
    # Event filter (username history popup on click)
    # ------------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._username_edit and event.type() == QEvent.Type.MouseButtonPress:
            if not self._username_edit.text() and self._username_history:
                QTimer.singleShot(0, self._username_completer.complete)
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Internal slots
    # ------------------------------------------------------------------

    def _on_rank_filter_toggled(self, checked: bool) -> None:
        self._rank_combo.setEnabled(checked)
        self._exact_rank_cb.setEnabled(checked)

    def _on_load_clicked(self) -> None:
        filters = self.get_filters()
        if not filters["username"]:
            return
        self.load_requested.emit(filters)

    def _on_username_text_changed(self, text: str) -> None:
        self._username_check.setVisible(False)
        if is_probable_url_input(text):
            self._username_timer.stop()
            return
        if len(text.strip()) >= 2:
            self._username_timer.start(500)
        else:
            self._username_timer.stop()

    def _on_place_text_changed(self, text: str) -> None:
        # Clear resolved ID when user edits manually and reset tooltip indicator
        self._place_id = None
        self._place_name = ""
        self._place_check.setVisible(False)
        self._place_edit.setToolTip("Filter by place (optional)")
        if len(text) >= 2:
            self._place_timer.start(400)  # debounce 400ms

    def _on_taxon_text_changed(self, text: str) -> None:
        self._taxon_gen += 1
        self._taxon_timer.stop()
        self._taxon_id = None
        self._taxon_name = ""
        self._taxon_edit.setToolTip("Filter by taxon (includes descendants by default)")
        if len(text) >= 2:
            self._taxon_timer.start(400)

    def _fetch_place_autocomplete(self) -> None:
        q = self._place_edit.text().strip()
        if not q:
            return
        self._place_gen += 1
        gen = self._place_gen
        worker = _PlaceAutocompleteWorker(self._client, q, gen, lambda: self._place_gen)
        sigs = worker.signals
        self._live_ac_signals.add(sigs)
        sigs.results.connect(lambda items, s=sigs: (self._live_ac_signals.discard(s), self._on_place_results(items)))
        sigs.error.connect(lambda _e, s=sigs: self._live_ac_signals.discard(s))
        self._pool.start(worker)

    def _fetch_taxon_autocomplete(self) -> None:
        q = self._taxon_edit.text().strip()
        if not q:
            return
        gen = self._taxon_gen
        worker = TaxonAutocompleteWorker(self._client, q, gen)
        sigs = worker.signals
        self._live_ac_signals.add(sigs)
        # The worker emits from a pool thread.  A bound QObject slot ensures
        # model updates and completer state changes happen in the GUI thread.
        sigs.finished.connect(self._taxon_autocomplete_finished)
        self._pool.start(worker)

    @Slot(list)
    def _on_place_results(self, items: List[Tuple[str, int]]) -> None:
        self._place_data = items
        self._place_model.setStringList([name for name, _ in items])
        log.debug("Place autocomplete returned %s usable result(s)", len(items))
        if items and self._place_edit.hasFocus() and self._place_edit.isVisible():
            self._place_completer.setCompletionPrefix(self._place_edit.text().strip())
            self._place_completer.popup().setCurrentIndex(
                self._place_completer.completionModel().index(0, 0)
            )
            self._place_completer.complete(self._place_edit.rect())
            log.debug("Showing %s place autocomplete suggestion(s)", len(items))

    @Slot(object)
    def _taxon_autocomplete_finished(self, result: object) -> None:
        signals = self.sender()
        if isinstance(signals, QObject):
            self._live_ac_signals.discard(signals)
        if not isinstance(result, TaxonAutocompleteResult):
            log.debug("Ignoring an invalid filter-bar taxon autocomplete callback")
            return
        if result.generation != self._taxon_gen:
            log.debug(
                "Ignoring stale filter-bar taxon autocomplete generation %s (current=%s)",
                result.generation,
                self._taxon_gen,
            )
            return
        if self._taxon_edit.text().strip() != result.query:
            log.debug(
                "Ignoring filter-bar taxon autocomplete generation %s because the query changed",
                result.generation,
            )
            return
        if result.diagnostic:
            self._taxon_data = []
            self._taxon_model.setStringList([])
            log.debug(
                "Filter-bar taxon autocomplete generation %s ended with %s",
                result.generation,
                result.diagnostic,
            )
            return
        self._on_taxon_results(list(result.items))

    @Slot(list)
    def _on_taxon_results(self, items: List[TaxonAutocompleteItem]) -> None:
        self._taxon_data = items
        self._taxon_model.setStringList(
            [item.display_name(StudyTaxon.show_common_names) for item in items]
        )
        log.debug("Filter-bar taxon autocomplete returned %s usable result(s)", len(items))
        if items and self._taxon_edit.hasFocus() and self._taxon_edit.isVisible():
            self._taxon_completer.setCompletionPrefix(self._taxon_edit.text().strip())
            self._taxon_completer.popup().setCurrentIndex(
                self._taxon_completer.completionModel().index(0, 0)
            )
            self._taxon_completer.complete(self._taxon_edit.rect())
            log.debug("Showing %s filter-bar taxon autocomplete suggestion(s)", len(items))

    def _fetch_username_validate(self) -> None:
        if is_probable_url_input(self._username_edit.text()):
            return
        login = self._username_edit.text().strip().replace(' ', '_')
        if not login:
            return
        self._username_gen += 1
        gen = self._username_gen
        worker = _UserValidateWorker(self._client, login, gen, lambda: self._username_gen)
        sigs = worker.signals
        self._live_ac_signals.add(sigs)
        sigs.found.connect(lambda name, s=sigs: (self._live_ac_signals.discard(s), self._on_username_validated(name)))
        sigs.not_found.connect(lambda s=sigs: self._live_ac_signals.discard(s))
        sigs.error.connect(lambda _e, s=sigs: self._live_ac_signals.discard(s))
        self._pool.start(worker)

    @Slot(str)
    def _on_username_validated(self, login: str) -> None:
        self._username_check.setVisible(True)
        self._add_to_username_history(login)
        current = self._username_edit.text()
        if current != login and current.strip().replace(' ', '_').lower() == login.lower():
            self._username_edit.setText(login)
        log.debug("Validated username: %s", login)

    def _add_to_username_history(self, login: str) -> None:
        if login in self._username_history:
            self._username_history.remove(login)
        self._username_history.insert(0, login)
        self._username_history = self._username_history[:20]
        self._username_history_model.setStringList(self._username_history)

    @Slot(str)
    def _on_username_history_selected(self, text: str) -> None:
        self._username_check.setVisible(False)
        if len(text.strip()) >= 2 and not is_probable_url_input(text):
            self._username_timer.start(200)

    @Slot(str)
    def _on_place_selected(self, text: str) -> None:
        for name, pid in self._place_data:
            if name == text:
                self._place_id = pid
                self._place_name = name
                self._place_edit.setText(name)
                self._place_edit.setToolTip(f"Place: {name}  (id={pid})")
                self._place_check.setVisible(True)
                log.debug("Selected place: %s (id=%s)", name, pid)
                break

    @Slot(str)
    def _on_taxon_selected(self, text: str) -> None:
        for item in self._taxon_data:
            label = item.display_name(StudyTaxon.show_common_names)
            if label == text:
                self._taxon_id = item.taxon_id
                self._taxon_name = item.scientific_name  # always store scientific name
                self._taxon_edit.setText(item.scientific_name)
                self._taxon_edit.setToolTip(
                    f"Taxon: {item.scientific_name}  (id={item.taxon_id})"
                )
                log.debug("Selected taxon: %s (id=%s)", item.scientific_name, item.taxon_id)
                break

    def _source_text(self) -> str:
        raw = self._username_edit.text().strip()
        if is_probable_url_input(raw):
            return raw
        return raw.replace(' ', '_')
