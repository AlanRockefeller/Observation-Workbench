"""Dialogs for the supervised "apply autovalidated identifications" workflow.

The preview, photo browser, and progress dialogs are shared with the bulk
disagree workflow; only the setup step and the unresolved-name report are
specific to this one.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
)

from observation_workbench.api.observation_url import (
    ObservationURLParseError,
    ObservationURLQuery,
    parse_observations_url,
)
from observation_workbench.services.autovalidated_ids import (
    AUTOVALIDATOR_LOGIN,
    AutovalidatedPlanStats,
)
from observation_workbench.ui.bulk_disagree_dialogs import (
    _bool_default,
    _int_default,
)
from observation_workbench.ui.table_sort import (
    SortableTableWidgetItem,
    enable_click_sorting,
    sorting_suspended,
)

log = logging.getLogger(__name__)

# The full corpus of autovalidated observations is tens of thousands of records,
# and each scanned page costs an API request, so the scan is always bounded.
MAX_SCAN_LIMIT = 50000


class AutovalidatedIdSetupDialog(QDialog):
    """Collect the scan bounds, comment, and posting options for the workflow."""

    def __init__(
        self,
        *,
        defaults: Optional[dict] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Apply Autovalidated Identifications")
        self.resize(720, 620)
        self._observation_query: Optional[ObservationURLQuery] = None
        self._url_error = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        intro = QLabel(
            "Finds observations that carry a DNA Barcode ITS sequence and an "
            f"automated identification comment from @{AUTOVALIDATOR_LOGIN}, but whose "
            "community consensus is not yet the autovalidated name. The name is read "
            "from the observation's Provisional Species Name field, falling back to "
            "Species Name Override, and is matched to an iNaturalist taxon by exact "
            "name only — a near miss is never accepted. Observations whose observer "
            'set "ID Update Needed" to "Yes" are excluded, and you review every '
            "observation and its photos before anything is posted."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(
            QLabel("Optional iNaturalist observations URL to narrow the search:")
        )
        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText(
            "Leave blank to scan all autovalidated observations, or paste e.g. "
            "https://www.inaturalist.org/observations?place_id=1"
        )
        self._url_edit.setToolTip(
            "Filters from this URL narrow the search. The autovalidation filters are "
            "always applied on top, so a URL can never widen the search."
        )
        self._url_edit.textChanged.connect(self._validate_url)
        layout.addWidget(self._url_edit)

        self._url_status = QLabel("")
        self._url_status.setWordWrap(True)
        layout.addWidget(self._url_status)

        self._scan_mode = QComboBox()
        self._scan_mode.addItem(
            "Continue next batch (and restore pending reviews)", "continue"
        )
        self._scan_mode.addItem(
            "Retry pending reviews and unresolved names only", "retry"
        )
        self._scan_mode.addItem(
            "Check for new or changed observations (restart scan)", "rescan"
        )
        layout.addWidget(self._scan_mode)
        resume_help = QLabel(
            "Progress is saved separately for your account and search filters. "
            "Continue scans the next batch in observation-ID order, oldest first, "
            "and restores a bounded batch of unfinished reviews. Retry reads only "
            "pending observations. Check for new or changed observations restarts "
            "the scan; continue subsequent batches to revisit the full search. "
            "Closing a preview or using dry run keeps reviews pending."
        )
        resume_help.setWordWrap(True)
        layout.addWidget(resume_help)

        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel("Batch size:"))
        self._max_spin = QSpinBox()
        self._max_spin.setRange(1, MAX_SCAN_LIMIT)
        self._max_spin.setSingleStep(100)
        self._max_spin.setValue(200)
        self._max_spin.setSuffix(" observations")
        self._max_spin.setToolTip(
            "Maximum new observations to scan, plus up to this many pending "
            "observations to retry. Keep the same size for each successive batch."
        )
        scan_row.addWidget(self._max_spin)
        scan_row.addStretch(1)
        layout.addLayout(scan_row)

        self._skip_unresolved_cb = QCheckBox(
            "Skip observations whose autovalidated name is not on iNaturalist yet"
        )
        self._skip_unresolved_cb.setChecked(True)
        self._skip_unresolved_cb.setToolTip(
            "A provisional name that has not been created on iNaturalist cannot be "
            "identified as anything, so these observations are never posted to. "
            "Unchecked, they are listed after the scan so the missing names can be "
            "created."
        )
        layout.addWidget(self._skip_unresolved_cb)

        layout.addWidget(QLabel("Comment to post with each identification (optional):"))
        self._comment_edit = QTextEdit()
        self._comment_edit.setAcceptRichText(False)
        self._comment_edit.setPlaceholderText(
            "Leave blank to post the identification with no comment."
        )
        self._comment_edit.setFixedHeight(80)
        layout.addWidget(self._comment_edit)

        delay_row = QHBoxLayout()
        delay_row.addWidget(QLabel("Delay between posts:"))
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
        delay_row.addWidget(self._delay_min_spin)
        delay_row.addWidget(self._delay_max_spin)
        delay_row.addStretch(1)
        layout.addLayout(delay_row)

        self._tag_others_cb = QCheckBox(
            "Tag users who proposed a different identification"
        )
        self._tag_others_cb.setChecked(False)
        self._tag_others_cb.setToolTip(
            "Append a blank line and @-mentions of users whose current ID differs "
            "from the autovalidated name, e.g. @scottostuni @johnplischke."
        )
        layout.addWidget(self._tag_others_cb)

        self._dry_run_cb = QCheckBox("Preview only / dry run")
        self._dry_run_cb.setChecked(False)
        layout.addWidget(self._dry_run_cb)

        self._validation_label = QLabel("")
        self._validation_label.setWordWrap(True)
        self._validation_label.setStyleSheet("QLabel { color: #b00020; }")
        layout.addWidget(self._validation_label)

        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._plan_btn = QPushButton("Plan")
        buttons.addButton(self._plan_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        self._plan_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)

        self._apply_defaults(defaults or {})
        self._validate_url()

    # -- public accessors --------------------------------------------------

    def observation_query(self) -> Optional[ObservationURLQuery]:
        """The parsed narrowing URL, or None when the search is unrestricted."""
        return self._observation_query

    def narrowing_url(self) -> str:
        return self._url_edit.text().strip()

    def scan_mode(self) -> str:
        return str(self._scan_mode.currentData())

    def max_observations(self) -> int:
        return self._max_spin.value()

    def skip_unresolved_names(self) -> bool:
        return self._skip_unresolved_cb.isChecked()

    def comment(self) -> str:
        return self._comment_edit.toPlainText().strip()

    def delay_min_seconds(self) -> int:
        return self._delay_min_spin.value()

    def delay_max_seconds(self) -> int:
        return self._delay_max_spin.value()

    def dry_run(self) -> bool:
        return self._dry_run_cb.isChecked()

    def tag_other_identifiers(self) -> bool:
        return self._tag_others_cb.isChecked()

    # -- defaults ----------------------------------------------------------

    def _apply_defaults(self, defaults: dict) -> None:
        self._url_edit.setText(str(defaults.get("url") or "").strip())
        self._max_spin.setValue(
            max(
                1,
                min(
                    MAX_SCAN_LIMIT, _int_default(defaults.get("max_observations"), 200)
                ),
            )
        )
        self._skip_unresolved_cb.setChecked(
            _bool_default(defaults.get("skip_unresolved_names"), True)
        )
        self._comment_edit.setPlainText(str(defaults.get("comment") or ""))
        delay_min = max(0, _int_default(defaults.get("delay_min_seconds"), 10))
        delay_max = max(delay_min, _int_default(defaults.get("delay_max_seconds"), 30))
        self._delay_min_spin.setValue(delay_min)
        self._delay_max_spin.setValue(delay_max)
        # Dry run always starts unchecked; remember the tagging preference.
        self._dry_run_cb.setChecked(False)
        self._tag_others_cb.setChecked(
            _bool_default(defaults.get("tag_other_identifiers"), False)
        )

    # -- validation --------------------------------------------------------

    def _validate_url(self) -> None:
        text = self._url_edit.text().strip()
        self._observation_query = None
        self._url_error = ""
        if not text:
            self._url_status.setText(
                "No URL: every autovalidated observation is in scope, oldest ID first."
            )
        else:
            try:
                query = parse_observations_url(text)
            except (ObservationURLParseError, ValueError) as exc:
                self._url_error = str(exc)
                query = None
            if self._url_error:
                self._url_status.setText("")
            elif query is None:
                self._url_error = (
                    "Paste an iNaturalist observations URL, or leave the field blank."
                )
                self._url_status.setText("")
            elif query.source_kind == "identify":
                self._url_error = (
                    "iNaturalist /observations/identify URLs cannot be scanned here; "
                    "use an /observations URL."
                )
                self._url_status.setText("")
            else:
                self._observation_query = query
                self._url_status.setText(
                    f"Search narrowed by {len(query.params)} filter(s) from this URL."
                )
        self._validation_label.setText(self._url_error)
        self._plan_btn.setEnabled(not self._url_error)

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


class UnresolvedAutovalidatedNamesDialog(QDialog):
    """List autovalidated names that have no iNaturalist taxon yet.

    These are the observations the workflow cannot post to: there is nothing to
    identify them as until somebody creates the name. Shown only when the user
    unchecked the skip option, so the names can be created and the scan re-run.
    """

    def __init__(
        self,
        unresolved: List[Tuple[int, str]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Autovalidated Names Not on iNaturalist")
        self.resize(720, 480)
        self._unresolved = list(unresolved)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)

        distinct = sorted({name for _, name in self._unresolved}, key=str.casefold)
        intro = QLabel(
            f"{len(self._unresolved)} observation(s) carry {len(distinct)} name(s) that "
            "no active iNaturalist taxon matches exactly. No identification can be "
            "posted to these until the names are created."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["Observation ID", "URL", "Autovalidated name"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        enable_click_sorting(table)
        with sorting_suspended(table):
            table.setRowCount(len(self._unresolved))
            for row, (obs_id, name) in enumerate(self._unresolved):
                values = [
                    str(obs_id),
                    f"https://www.inaturalist.org/observations/{obs_id}",
                    name,
                ]
                for col, value in enumerate(values):
                    table.setItem(row, col, SortableTableWidgetItem(value))
        table.resizeColumnsToContents()
        layout.addWidget(table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy_btn = QPushButton("Copy names")
        copy_btn.setToolTip("Copy the distinct names to the clipboard, one per line.")
        copy_btn.clicked.connect(lambda: self._copy_names(distinct))
        buttons.addButton(copy_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _copy_names(self, distinct: List[str]) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText("\n".join(distinct))


def format_autovalidated_stats(stats: AutovalidatedPlanStats) -> str:
    """Multi-line scan summary for the message box shown after planning."""
    return (
        f"New observations scanned: {stats.total_url_results_scanned}\n"
        f"Pending observations rechecked: {stats.pending_rechecked}\n"
        + (
            "Reached the end of this search. Use Check for new or changed observations "
            "to restart it.\n"
            if stats.scan_exhausted
            else ""
        )
        + f"Candidates: {stats.candidate_count}\n"
        f"Skipped because the consensus already matches: "
        f"{stats.skipped_consensus_already_matches}\n"
        f"Skipped because the name is not on iNaturalist: "
        f"{stats.skipped_unresolved_name}\n"
        f"Skipped because you already have that ID: {stats.skipped_already_target}\n"
        f"Skipped because no autovalidation comment was found: "
        f"{stats.skipped_not_autovalidated}\n"
        f"Skipped because no autovalidated name was recorded: "
        f"{stats.skipped_no_suggested_name}\n"
        f"Skipped due to missing DNA Barcode ITS: "
        f"{stats.skipped_missing_dna_barcode_its}\n"
        f"Skipped due to permanent skip list: {stats.skipped_permanent}\n"
        f"Skipped due to refresh failure: {stats.skipped_refresh_failure}"
    )
