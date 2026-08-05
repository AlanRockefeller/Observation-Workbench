"""Dialogs for supervised provisional-name agreement workflow."""
from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.observation_url import (
    ObservationURLParseError,
    ObservationURLQuery,
    parse_observations_url,
)
from observation_workbench.services.bulk_disagree import BulkDisagreeCandidate
from observation_workbench.services.bulk_identification import (
    BulkAgreeCandidate,
    BulkAgreePlanStats,
)
from observation_workbench.services.image_cache import ImageCache
from observation_workbench.ui.bulk_disagree_dialogs import (
    BulkDisagreePhotoBrowserDialog,
    _bool_default,
    _int_default,
)
from observation_workbench.ui.external_links import open_external_url_silently
from observation_workbench.ui.table_sort import (
    SortableTableWidgetItem,
    enable_click_sorting,
    sorting_suspended,
)


def _browser_candidate(candidate: BulkAgreeCandidate) -> BulkDisagreeCandidate:
    """Adapt an agreement candidate to the shape the photo browser renders.

    The browser and its alternate-ID posting path are written against
    :class:`BulkDisagreeCandidate`. Agreement has no source taxon to check, so
    the source fields stay empty and the safety re-check is disabled by the
    caller; the provisional identification being agreed with is the target.
    """
    obs = candidate.observation
    return BulkDisagreeCandidate(
        observation=obs,
        source_taxon_id=0,
        source_taxon_name="",
        target_taxon_id=candidate.target.taxon_id,
        target_taxon_name=candidate.target.taxon_name,
        target_taxon_rank="",
        current_observation_taxon_name=obs.taxon.name if obs.taxon else "",
        community_taxon_name=obs.community_taxon.name if obs.community_taxon else "",
        has_dna_barcode_its=bool((obs.dna_barcode_its or "").strip()),
        dna_barcode_its_value=obs.dna_barcode_its or "",
        user_current_taxon=candidate.user_current_taxon,
        dqa_vote_planned=False,
        explicit_disagreement=False,
    )


class BulkAgreeSetupDialog(QDialog):
    """Choose the observation source and safety options before planning.

    Mirrors :class:`BulkDisagreeSetupDialog`: an observations URL is pasted and
    validated here, so the workflow no longer depends on whatever query happens
    to be loaded in the viewer.
    """

    def __init__(
        self,
        *,
        prefill_url: str = "",
        defaults: Optional[dict] = None,
        current_query_description: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Agree to Provisional IDs")
        self.resize(760, 460)
        self._observation_query: Optional[ObservationURLQuery] = None
        self._current_query_available = bool(current_query_description)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        intro = QLabel(
            "Paste an iNaturalist observations URL. Every result is scanned for a "
            "current provisional identification made by somebody else that you have "
            "not agreed with yet. Nothing is written until you review the preview and "
            "start the run."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        layout.addLayout(grid)

        self._url_radio = QRadioButton("iNaturalist observations URL:")
        self._url_radio.setChecked(True)
        grid.addWidget(self._url_radio, 0, 0)
        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText(
            "https://www.inaturalist.org/observations?user_id=…&taxon_id=…"
        )
        grid.addWidget(self._url_edit, 0, 1)

        self._current_radio = QRadioButton("Use the query loaded in the viewer:")
        self._current_radio.setEnabled(self._current_query_available)
        grid.addWidget(self._current_radio, 1, 0)
        self._current_label = QLabel(
            current_query_description
            or "No query is loaded in the viewer yet."
        )
        self._current_label.setWordWrap(True)
        grid.addWidget(self._current_label, 1, 1)

        self._url_radio.toggled.connect(self._on_source_mode_changed)

        self._require_dna_cb = QCheckBox(
            "Only agree when the observation has a DNA Barcode ITS observation field"
        )
        self._require_dna_cb.setChecked(True)
        self._require_dna_cb.setToolTip(
            "Observations without a DNA Barcode ITS field are skipped during planning."
        )
        layout.addWidget(self._require_dna_cb)

        self._only_if_needed_cb = QCheckBox(
            "Only agree when needed (skip observations already Research Grade for the proposed taxon)"
        )
        self._only_if_needed_cb.setChecked(True)
        self._only_if_needed_cb.setToolTip(
            "Skip an observation when it is already Research Grade and its current "
            "community taxon matches the provisional name being proposed."
        )
        layout.addWidget(self._only_if_needed_cb)

        options_row = QHBoxLayout()
        options_row.addWidget(QLabel("Maximum observations to scan:"))
        self._max_spin = QSpinBox()
        self._max_spin.setRange(1, 999_999)
        self._max_spin.setValue(100)
        options_row.addWidget(self._max_spin)
        options_row.addSpacing(16)
        options_row.addWidget(QLabel("Delay:"))
        self._delay_min_spin = QSpinBox()
        self._delay_min_spin.setRange(0, 3600)
        self._delay_min_spin.setSuffix("s min")
        self._delay_min_spin.setValue(10)
        self._delay_max_spin = QSpinBox()
        self._delay_max_spin.setRange(0, 3600)
        self._delay_max_spin.setSuffix("s max")
        self._delay_max_spin.setValue(30)
        self._delay_min_spin.valueChanged.connect(self._on_delay_min_changed)
        self._delay_max_spin.valueChanged.connect(self._on_delay_max_changed)
        options_row.addWidget(self._delay_min_spin)
        options_row.addWidget(self._delay_max_spin)
        options_row.addStretch(1)
        layout.addLayout(options_row)

        self._dry_run_cb = QCheckBox("Preview only / dry run")
        self._dry_run_cb.setChecked(False)
        self._dry_run_cb.setToolTip(
            "Run the whole workflow without posting any identification."
        )
        layout.addWidget(self._dry_run_cb)

        self._validation_label = QLabel("")
        self._validation_label.setWordWrap(True)
        self._validation_label.setStyleSheet("QLabel { color: #b00020; }")
        layout.addWidget(self._validation_label)

        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._plan_btn = QPushButton("Plan")
        self._plan_btn.setEnabled(False)
        buttons.addButton(self._plan_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self._plan_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)

        self._url_timer = QTimer(self)
        self._url_timer.setSingleShot(True)
        self._url_timer.timeout.connect(self._validate_url)
        self._url_edit.textChanged.connect(lambda _text: self._url_timer.start(350))

        self._apply_defaults(defaults or {}, prefill_url)

    # -- accessors ------------------------------------------------------

    def use_current_query(self) -> bool:
        return self._current_radio.isChecked()

    @property
    def observation_query(self) -> Optional[ObservationURLQuery]:
        """Parsed URL query, or None when the viewer's query is used instead."""
        return None if self.use_current_query() else self._observation_query

    def source_url(self) -> str:
        return self._url_edit.text().strip()

    def require_dna_barcode_its(self) -> bool:
        return self._require_dna_cb.isChecked()

    def only_if_needed(self) -> bool:
        return self._only_if_needed_cb.isChecked()

    def max_observations(self) -> int:
        return self._max_spin.value()

    def delay_min_seconds(self) -> int:
        return self._delay_min_spin.value()

    def delay_max_seconds(self) -> int:
        return self._delay_max_spin.value()

    def dry_run(self) -> bool:
        return self._dry_run_cb.isChecked()

    # -- internals ------------------------------------------------------

    def _apply_defaults(self, defaults: dict, prefill_url: str) -> None:
        url = prefill_url.strip() or str(defaults.get("url") or "").strip()
        self._require_dna_cb.setChecked(
            _bool_default(defaults.get("require_dna_barcode_its"), True)
        )
        self._only_if_needed_cb.setChecked(
            _bool_default(defaults.get("only_if_needed"), True)
        )
        self._max_spin.setValue(_int_default(defaults.get("max_observations"), 100))
        delay_min = max(0, _int_default(defaults.get("delay_min_seconds"), 10))
        delay_max = max(delay_min, _int_default(defaults.get("delay_max_seconds"), 30))
        self._delay_min_spin.setValue(delay_min)
        self._delay_max_spin.setValue(delay_max)
        self._dry_run_cb.setChecked(_bool_default(defaults.get("dry_run"), False))

        if (
            str(defaults.get("source_mode") or "url") == "current"
            and self._current_query_available
        ):
            self._current_radio.setChecked(True)

        if url:
            self._url_edit.setText(url)
            QTimer.singleShot(0, self._validate_url)
        else:
            self._update_plan_enabled()

    def _on_source_mode_changed(self, _checked: bool = False) -> None:
        self._url_edit.setEnabled(self._url_radio.isChecked())
        if self._url_radio.isChecked():
            self._validate_url()
        else:
            self._set_validation("")
            self._update_plan_enabled()

    def _validate_url(self) -> None:
        self._observation_query = None
        if not self._url_radio.isChecked():
            self._set_validation("")
            self._update_plan_enabled()
            return
        text = self._url_edit.text().strip()
        if not text:
            self._set_validation("Paste an iNaturalist observations URL.")
            self._update_plan_enabled()
            return
        try:
            query = parse_observations_url(text)
        except ObservationURLParseError as exc:
            self._set_validation(str(exc))
            self._update_plan_enabled()
            return
        if query is None:
            self._set_validation("Paste an iNaturalist observations URL.")
            self._update_plan_enabled()
            return
        if query.source_kind == "identify":
            self._set_validation(
                "iNaturalist /observations/identify URLs cannot be scanned by this "
                "workflow. Use an /observations URL with the same filters."
            )
            self._update_plan_enabled()
            return
        self._observation_query = query
        self._set_validation("")
        self._update_plan_enabled()

    def _update_plan_enabled(self) -> None:
        ready = (
            self._current_query_available
            if self.use_current_query()
            else self._observation_query is not None
        )
        self._plan_btn.setEnabled(bool(ready))

    def _set_validation(self, text: str) -> None:
        self._validation_label.setText(text)

    def _on_delay_min_changed(self, value: int) -> None:
        if value > self._delay_max_spin.value():
            self._delay_max_spin.blockSignals(True)
            self._delay_max_spin.setValue(value)
            self._delay_max_spin.blockSignals(False)

    def _on_delay_max_changed(self, value: int) -> None:
        if value < self._delay_min_spin.value():
            self._delay_min_spin.blockSignals(True)
            self._delay_min_spin.setValue(value)
            self._delay_min_spin.blockSignals(False)


def format_agree_plan_stats(stats: BulkAgreePlanStats) -> str:
    return (
        f"Scanned results: {stats.total_results_scanned}\n"
        f"Candidates: {stats.candidate_count}\n"
        f"Skipped, no current provisional ID by somebody else: {stats.skipped_no_provisional_id}\n"
        f"Skipped, no DNA Barcode ITS field: {stats.skipped_no_dna_barcode_its}\n"
        f"Skipped, you already agreed: {stats.skipped_already_agreed}\n"
        f"Skipped, already Research Grade for proposed taxon: {stats.skipped_already_research_grade}\n"
        f"Skipped, you previously withdrew that ID: {stats.skipped_previously_withdrew}\n"
        f"Skipped due to permanent skip list: {stats.skipped_permanent}"
    )


class BulkAgreePreviewDialog(QDialog):
    """Review planned agreements, browse their photos, then start or go back."""

    def __init__(
        self,
        candidates: List[BulkAgreeCandidate],
        stats: Optional[BulkAgreePlanStats] = None,
        *,
        client=None,
        disk_cache: Optional[ImageCache] = None,
        api_token: str = "",
        login: str = "",
        on_skip_forever: Optional[Callable[[BulkAgreeCandidate], None]] = None,
        on_unskip_forever: Optional[Callable[[BulkAgreeCandidate], None]] = None,
        request_reauthentication: Optional[
            Callable[
                [Callable[[str, str], None], Callable[[str], None]],
                None,
            ]
        ] = None,
        dry_run: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preview Provisional Agreements")
        self.resize(1100, 580)
        self._candidates = list(candidates)
        self._stats = stats
        self._back_requested = False
        self._client = client
        self._disk_cache = disk_cache
        self._api_token = api_token
        self._login = login
        self._on_skip_forever = on_skip_forever
        self._on_unskip_forever = on_unskip_forever
        self._request_reauthentication = request_reauthentication
        self._dry_run = dry_run

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        warning = QLabel(
            "This will write identifications to iNaturalist. Only proceed if you can "
            "independently verify each proposed ID. Each observation will be refreshed "
            "again immediately before posting, and you can skip or cancel during execution."
        )
        if dry_run:
            warning.setText(warning.text() + " Dry run is enabled, so no writes will be posted.")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        if stats is not None:
            stats_label = QLabel(format_agree_plan_stats(stats))
            stats_label.setWordWrap(True)
            layout.addWidget(stats_label)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            [
                "Observation ID",
                "URL",
                "Observer",
                "Target taxon",
                "Source identifier",
                "ID date",
                "Your current ID",
                "DNA Barcode ITS present?",
            ]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        enable_click_sorting(self._table)
        layout.addWidget(self._table, 1)

        self._agree_label = QLabel("Type <b>AGREE</b> in the box below to enable the Start button:")
        layout.addWidget(self._agree_label)

        self._agree_edit = QLineEdit()
        self._agree_edit.setPlaceholderText("Type AGREE here to enable Start")
        layout.addWidget(self._agree_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._back_btn = QPushButton("Back")
        self._back_btn.setToolTip("Return to the provisional agreement setup dialog.")
        self._browse_btn = QPushButton("Browse photos...")
        self._browse_btn.clicked.connect(self._browse_photos)
        self._start_btn = QPushButton("Start")
        buttons.addButton(self._back_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self._browse_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self._start_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self._back_btn.clicked.connect(self._go_back)
        self._start_btn.clicked.connect(self.accept)
        self._agree_edit.textChanged.connect(lambda _text: self._update_start_enabled())
        layout.addWidget(buttons)
        self._start_btn.setDefault(False)
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setDefault(True)
        self._populate_table()
        self._update_start_enabled()

    def candidates(self) -> List[BulkAgreeCandidate]:
        return list(self._candidates)

    def back_requested(self) -> bool:
        return self._back_requested

    def _go_back(self) -> None:
        self._back_requested = True
        self.reject()

    def _populate_table(self) -> None:
        with sorting_suspended(self._table):
            self._table.setRowCount(len(self._candidates))
            for row, candidate in enumerate(self._candidates):
                obs = candidate.observation
                values = [
                    str(obs.obs_id),
                    obs.url,
                    obs.observer_login,
                    candidate.target.taxon_name,
                    candidate.target.source_login,
                    candidate.target.source_created_at[:19],
                    candidate.user_current_taxon or "",
                    "Yes" if (obs.dna_barcode_its or "").strip() else "No",
                ]
                for col, value in enumerate(values):
                    self._table.setItem(row, col, SortableTableWidgetItem(value))
        self._table.resizeColumnsToContents()

    def _browse_photos(self) -> None:
        if not self._client or not self._disk_cache:
            return
        by_obs_id = {c.observation.obs_id: c for c in self._candidates}
        browser_candidates = [_browser_candidate(c) for c in self._candidates]
        dlg = BulkDisagreePhotoBrowserDialog(
            browser_candidates,
            client=self._client,
            disk_cache=self._disk_cache,
            api_token=self._api_token,
            login=self._login,
            # Agreement has no source taxon from a URL, so there is nothing for
            # the alternate-ID path to re-check.
            require_source_taxon_match=False,
            default_comment="",
            dry_run=self._dry_run,
            on_skip_forever=self._skip_forever_adapter(by_obs_id, self._on_skip_forever),
            on_unskip_forever=self._skip_forever_adapter(by_obs_id, self._on_unskip_forever),
            request_reauthentication=(
                self._request_photo_browser_reauthentication
                if self._request_reauthentication is not None
                else None
            ),
            window_title="Browse Provisional Agreement Photos",
            parent=self,
        )
        dlg.exec()
        kept_ids = [c.observation.obs_id for c in dlg.candidates()]
        self._candidates = [by_obs_id[obs_id] for obs_id in kept_ids if obs_id in by_obs_id]
        self._populate_table()
        self._update_start_enabled()

    @staticmethod
    def _skip_forever_adapter(
        by_obs_id: dict,
        callback: Optional[Callable[[BulkAgreeCandidate], None]],
    ) -> Optional[Callable[[BulkDisagreeCandidate], None]]:
        """Route the browser's skip callbacks back to the agreement candidate."""
        if callback is None:
            return None

        def handler(browser_candidate: BulkDisagreeCandidate) -> None:
            candidate = by_obs_id.get(browser_candidate.observation.obs_id)
            if candidate is not None:
                callback(candidate)

        return handler

    def _request_photo_browser_reauthentication(
        self,
        on_success: Callable[[str, str], None],
        on_failure: Callable[[str], None],
    ) -> None:
        if self._request_reauthentication is None:
            on_failure("Authentication is unavailable from this window.")
            return

        def authenticated(api_token: str, login: str) -> None:
            self._api_token = api_token
            self._login = login
            on_success(api_token, login)

        def authentication_failed(msg: str) -> None:
            self._api_token = ""
            self._login = ""
            on_failure(msg)

        self._request_reauthentication(authenticated, authentication_failed)

    def _update_start_enabled(self) -> None:
        count = len(self._candidates)
        self._browse_btn.setEnabled(bool(self._client and self._disk_cache and count))
        needs_confirmation = count > 5
        self._agree_label.setVisible(needs_confirmation)
        self._agree_edit.setVisible(needs_confirmation)
        confirmed = (
            self._agree_edit.text().strip() == "AGREE"
            if needs_confirmation
            else True
        )
        self._start_btn.setEnabled(count > 0 and confirmed)


class BulkAgreeProgressDialog(QDialog):
    cancel_requested = Signal()
    skip_requested = Signal()
    skip_forever_requested = Signal()
    skip_delay_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()
    delay_changed = Signal(int, int)  # min_seconds, max_seconds
    nav_key_pressed = Signal(object, object)
    open_all_review_requested = Signal()

    _NAV_PASSTHROUGH_KEYS = {
        Qt.Key.Key_Up,
        Qt.Key.Key_Down,
        Qt.Key.Key_BracketLeft,
        Qt.Key.Key_BracketRight,
    }

    def __init__(
        self,
        parent=None,
        *,
        delay_min_seconds: int = 10,
        delay_max_seconds: int = 30,
        dry_run: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Agree to Provisional IDs")
        self.resize(640, 460)
        self.setModal(False)
        self._finished = False
        self._paused = False
        self._observation_url = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        if dry_run:
            dry_run_label = QLabel(
                "DRY RUN — every candidate is refreshed and checked, but no "
                "identification is posted."
            )
            dry_run_label.setWordWrap(True)
            dry_run_label.setStyleSheet("QLabel { color: #b36b00; font-weight: 700; }")
            layout.addWidget(dry_run_label)

        pause_label = QLabel("Move to end for human review when:")
        layout.addWidget(pause_label)
        self._pause_recent_cb = QCheckBox("Most recent identification is not provisional")
        self._pause_recent_cb.setChecked(True)
        layout.addWidget(self._pause_recent_cb)
        self._pause_comments_cb = QCheckBox("Comments have been added since the provisional name was proposed")
        self._pause_comments_cb.setChecked(True)
        layout.addWidget(self._pause_comments_cb)

        delay_row = QHBoxLayout()
        self._delay_label = QLabel("Delay:")
        self._delay_min_spin = QSpinBox()
        self._delay_min_spin.setRange(0, 3600)
        self._delay_min_spin.setSuffix("s min")
        self._delay_min_spin.setValue(max(0, int(delay_min_seconds)))
        self._delay_max_spin = QSpinBox()
        self._delay_max_spin.setRange(0, 3600)
        self._delay_max_spin.setSuffix("s max")
        self._delay_max_spin.setValue(max(int(delay_min_seconds), int(delay_max_seconds)))
        self._delay_min_spin.valueChanged.connect(self._on_delay_min_changed)
        self._delay_max_spin.valueChanged.connect(self._on_delay_max_changed)
        delay_row.addWidget(self._delay_label)
        delay_row.addWidget(self._delay_min_spin)
        delay_row.addWidget(self._delay_max_spin)
        delay_row.addStretch(1)
        layout.addLayout(delay_row)

        self._title = QLabel("Ready")
        self._title.setWordWrap(True)
        layout.addWidget(self._title)

        self._taxon_label = QLabel("")
        taxon_font = QFont()
        taxon_font.setPointSize(16)
        taxon_font.setBold(True)
        self._taxon_label.setFont(taxon_font)
        self._taxon_label.setWordWrap(True)
        layout.addWidget(self._taxon_label)

        self._details = QTextEdit()
        self._details.setReadOnly(True)
        self._details.setAcceptRichText(False)
        layout.addWidget(self._details, 1)

        self._review_banner = QLabel("")
        self._review_banner.setWordWrap(True)
        self._review_banner.setVisible(False)
        self._review_banner.setStyleSheet(
            "QLabel {"
            "  color: #8dff9a;"
            "  background-color: #143a1d;"
            "  border: 2px solid #2f9e44;"
            "  border-radius: 4px;"
            "  padding: 10px 12px;"
            "  font-size: 15px;"
            "  font-weight: 700;"
            "}"
        )
        layout.addWidget(self._review_banner)

        self._review_actions = QHBoxLayout()
        self._copy_review_url_btn = QPushButton("Copy observation URL")
        self._copy_review_url_btn.setToolTip(
            "Copy the observation URL to the clipboard for human review."
        )
        self._copy_review_url_btn.clicked.connect(self._copy_review_url)
        self._open_review_obs_btn = QPushButton("Open observation in browser")
        self._open_review_obs_btn.setToolTip(
            "Open this observation in your browser for human review."
        )
        self._open_review_obs_btn.clicked.connect(self._open_review_observation)
        self._open_all_review_btn = QPushButton("Open all needing review in browser")
        self._open_all_review_btn.setToolTip(
            "Open every remaining observation that needs human review in your "
            "browser, and skip all of them here (you'll handle them yourself)."
        )
        self._open_all_review_btn.clicked.connect(self.open_all_review_requested)
        self._review_actions.addWidget(self._copy_review_url_btn)
        self._review_actions.addWidget(self._open_review_obs_btn)
        self._review_actions.addWidget(self._open_all_review_btn)
        self._review_actions.addStretch(1)
        layout.addLayout(self._review_actions)
        self._set_review_actions_visible(False)

        comment_label = QLabel("Comment to post with this identification (edit as needed):")
        layout.addWidget(comment_label)

        self._comment_edit = QTextEdit()
        self._comment_edit.setAcceptRichText(False)
        self._comment_edit.setFixedHeight(70)
        layout.addWidget(self._comment_edit)

        self._countdown = QLabel("")
        layout.addWidget(self._countdown)

        row = QHBoxLayout()
        self._cancel_btn = QPushButton("Cancel")
        self._pause_btn = QPushButton("Pause")
        self._skip_btn = QPushButton("Skip this ID")
        self._skip_forever_btn = QPushButton("Skip forever")
        self._skip_forever_btn.setToolTip(
            "Skip this observation now and remember it — it will be excluded from all future bulk-agree runs."
        )
        self._skip_delay_btn = QPushButton("Post now / skip delay")
        self._cancel_btn.clicked.connect(self.cancel_requested)
        self._pause_btn.clicked.connect(self._on_pause_clicked)
        self._skip_btn.clicked.connect(self.skip_requested)
        self._skip_forever_btn.clicked.connect(self.skip_forever_requested)
        self._skip_delay_btn.clicked.connect(self.skip_delay_requested)
        row.addWidget(self._cancel_btn)
        row.addWidget(self._pause_btn)
        row.addWidget(self._skip_btn)
        row.addWidget(self._skip_forever_btn)
        row.addWidget(self._skip_delay_btn)
        layout.addLayout(row)
        self._install_nav_passthrough_filter()

    def _install_nav_passthrough_filter(self) -> None:
        """Let the viewer's navigation keys work while this modeless dialog has focus."""
        self.installEventFilter(self)
        for child in self.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if self._forward_nav_key_if_allowed(event):
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._forward_nav_key_if_allowed(event):
            return
        super().keyPressEvent(event)

    def _forward_nav_key_if_allowed(self, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not isinstance(event, QKeyEvent):
            return False
        if event.key() not in self._NAV_PASSTHROUGH_KEYS:
            return False
        if not self._focus_allows_nav_passthrough():
            return False
        self.nav_key_pressed.emit(event.key(), event.modifiers())
        event.accept()
        return True

    def _focus_allows_nav_passthrough(self) -> bool:
        focus = self.focusWidget()
        if focus is None:
            return True
        if focus is self._comment_edit or self._comment_edit.isAncestorOf(focus):
            return False
        cls_name = type(focus).__name__
        if "SpinBox" in cls_name or "ComboBox" in cls_name:
            return False
        if cls_name in {"QLineEdit", "QPlainTextEdit"}:
            return False
        is_read_only = getattr(focus, "isReadOnly", None)
        return not (callable(is_read_only) and not is_read_only())

    def _on_pause_clicked(self) -> None:
        self._paused = not self._paused
        if self._paused:
            self._pause_btn.setText("Resume")
            self.pause_requested.emit()
        else:
            self._pause_btn.setText("Pause")
            self.resume_requested.emit()

    def _on_delay_min_changed(self, value: int) -> None:
        if value > self._delay_max_spin.value():
            self._delay_max_spin.blockSignals(True)
            self._delay_max_spin.setValue(value)
            self._delay_max_spin.blockSignals(False)
        self.delay_changed.emit(self.delay_min_seconds(), self.delay_max_seconds())

    def _on_delay_max_changed(self, value: int) -> None:
        if value < self._delay_min_spin.value():
            self._delay_min_spin.blockSignals(True)
            self._delay_min_spin.setValue(value)
            self._delay_min_spin.blockSignals(False)
        self.delay_changed.emit(self.delay_min_seconds(), self.delay_max_seconds())

    def delay_min_seconds(self) -> int:
        return self._delay_min_spin.value()

    def delay_max_seconds(self) -> int:
        return self._delay_max_spin.value()

    def show_candidate(self, index: int, total: int, candidate: BulkAgreeCandidate, *, comment: str = "") -> None:
        obs = candidate.observation
        self._observation_url = obs.url
        self._set_review_actions_visible(False)
        self._title.setText(f"Item {index + 1} of {total}: observation {obs.obs_id}")
        self._taxon_label.setText(candidate.target.taxon_name)
        self._details.setPlainText(
            "\n".join(
                [
                    f"Observation: {obs.url}",
                    f"Observer: {obs.observer_login}",
                    f"Source identifier: {candidate.target.source_login}",
                    f"Source ID date: {candidate.target.source_created_at}",
                    "",
                    "Status: ready",
                ]
            )
        )
        self._comment_edit.setPlainText(comment)
        self._countdown.setText("")
        self._paused = False
        self._pause_btn.setText("Pause")

    def get_comment(self) -> str:
        return self._comment_edit.toPlainText().strip()

    def set_status(self, status: str) -> None:
        self._details.setPlainText(self._details.toPlainText().split("\nStatus:")[0] + f"\nStatus: {status}")

    def set_countdown(self, seconds: int) -> None:
        if seconds <= 0:
            self._countdown.setText("Posting as soon as the API allows.")
            return
        self._countdown.setText(f"Posting this ID automatically in {seconds}s — click 'Post now / skip delay' to post immediately, or 'Skip this ID' to pass.")

    def set_posting(self) -> None:
        """Disable interactive controls while a write request is in flight."""
        self._skip_btn.setEnabled(False)
        self._skip_forever_btn.setEnabled(False)
        self._skip_delay_btn.setEnabled(False)
        self._pause_btn.setEnabled(False)
        self._comment_edit.setEnabled(False)
        self._countdown.setText("")

    def set_waiting(self) -> None:
        """Re-enable interactive controls when back in the review/delay phase."""
        self._skip_btn.setEnabled(True)
        self._skip_forever_btn.setEnabled(True)
        self._skip_delay_btn.setEnabled(True)
        self._pause_btn.setEnabled(True)
        self._comment_edit.setEnabled(True)
        self._review_banner.setVisible(False)
        self._set_review_actions_visible(False)

    def pause_on_recent_not_provisional(self) -> bool:
        return self._pause_recent_cb.isChecked()

    def pause_on_comments_since(self) -> bool:
        return self._pause_comments_cb.isChecked()

    def set_paused_for_review(self, reason: str) -> None:
        """Enter an indefinite human-review pause and show the reason."""
        self._paused = True
        self._pause_btn.setText("Resume")
        self.set_waiting()
        self._review_banner.setText(
            f"HUMAN REVIEW REQUIRED\n{reason}"
        )
        self._review_banner.setVisible(True)
        self._set_review_actions_visible(True)
        self.set_status(f"Paused for human review: {reason}")
        self._countdown.setText(
            "Human review required — click Resume to start countdown, or Skip to pass this observation."
        )

    def set_summary(
        self,
        posted: int,
        skipped: int,
        failed: int,
        cancelled: bool,
        *,
        skipped_no_dna: int = 0,
        skipped_previously_withdrew: int = 0,
    ) -> None:
        state = "Cancelled" if cancelled else "Complete"
        self._finished = True
        self._title.setText(state)
        lines = [f"{state}", "", f"Posted: {posted}", f"Skipped: {skipped}", f"Failed: {failed}"]
        if skipped_no_dna:
            lines.append(f"Skipped (no DNA Barcode ITS): {skipped_no_dna}")
        if skipped_previously_withdrew:
            lines.append(
                f"Skipped (previously withdrew provisional ID): {skipped_previously_withdrew}"
            )
        self._details.setPlainText("\n".join(lines))
        self._countdown.setText("")
        self._taxon_label.setText("")
        self._comment_edit.setPlainText("")
        self._comment_edit.setEnabled(False)
        self._cancel_btn.setText("Close")
        self._cancel_btn.clicked.disconnect(self.cancel_requested)
        self._cancel_btn.clicked.connect(self.accept)
        self._pause_btn.setVisible(False)
        self._skip_btn.setVisible(False)
        self._skip_forever_btn.setVisible(False)
        self._skip_delay_btn.setVisible(False)
        self._set_review_actions_visible(False)
        self._delay_label.setVisible(False)
        self._delay_min_spin.setVisible(False)
        self._delay_max_spin.setVisible(False)

    def closeEvent(self, event) -> None:
        if not self._finished:
            self.cancel_requested.emit()
        super().closeEvent(event)

    def _set_review_actions_visible(self, visible: bool) -> None:
        self._copy_review_url_btn.setVisible(visible)
        self._open_review_obs_btn.setVisible(visible)
        self._open_all_review_btn.setVisible(visible)

    def _copy_review_url(self) -> None:
        if self._observation_url:
            QApplication.clipboard().setText(self._observation_url)

    def _open_review_observation(self) -> None:
        if self._observation_url:
            open_external_url_silently(self._observation_url)
