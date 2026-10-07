"""Setup, progress, and results UI for the DNA-driven vote review scan.

This workflow is read-only: it finds DNA-barcoded observations whose
identifications suggest the reviewer's own vote (or the community consensus)
is worth revisiting, and hands them off to iNaturalist in a browser. Nothing
here writes to iNaturalist.
"""

from __future__ import annotations

from dataclasses import replace
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.client import INatAPIError, INatClient
from observation_workbench.api.observation_url import parse_observations_url
from observation_workbench.services.dna_vote_review import (
    KIND_LABELS,
    DNAVoteReview,
    VoteReviewCancelled,
    VoteReviewConfig,
    VoteReviewFinding,
    VoteReviewProgress,
    VoteReviewResult,
)
from observation_workbench.storage.settings import AppSettings
from observation_workbench.ui.external_links import open_external_url_silently
from observation_workbench.ui.table_sort import (
    SortableTableWidgetItem,
    enable_click_sorting,
    sorting_suspended,
)

ALL_KINDS = "All findings"


class _ReviewSignals(QObject):
    progress = Signal(object)
    ready = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class _ReviewWorker(QRunnable):
    def __init__(
        self, client: INatClient, config: VoteReviewConfig, cancel: threading.Event
    ) -> None:
        super().__init__()
        self.client, self.config, self.cancel = client, config, cancel
        self.signals = _ReviewSignals()

    def run(self) -> None:
        try:
            review = DNAVoteReview(self.client)
            config = self.config
            if config.login:
                # Fail early and clearly on a typo rather than silently
                # returning an empty scan, and keep the numeric id: the index
                # filter is keyed by account id, not by login.
                user = self.client.get_user(config.login)
                if user is None:
                    raise ValueError(
                        f"No iNaturalist account has the username "
                        f"{config.login!r}."
                    )
                try:
                    user_id = int(user.get("id") or 0)
                except (TypeError, ValueError):
                    user_id = 0
                if user_id:
                    config = replace(config, login_user_id=user_id)
            result = review.scan(
                config,
                is_cancelled=self.cancel.is_set,
                progress=self.signals.progress.emit,
            )
        except VoteReviewCancelled:
            self.signals.cancelled.emit()
        except (INatAPIError, ValueError) as exc:
            self.signals.failed.emit(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            self.signals.failed.emit(f"The vote review scan failed: {exc}")
        else:
            self.signals.ready.emit(result)


class DNAVoteReviewSetupDialog(QDialog):
    """Collect the username, optional source URL, and scan depth."""

    def __init__(
        self,
        settings: AppSettings,
        default_login: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.config: Optional[VoteReviewConfig] = None
        self.setWindowTitle("Review DNA-contested identifications")
        self.resize(780, 400)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Finds observations carrying a DNA Barcode ITS sequence whose current "
            "identifications suggest a vote is worth revisiting. iNaturalist does not "
            "record when an observation field was added, so this assumes that a "
            "disagreement on a barcoded observation was probably driven by the "
            "sequence.\n\n"
            "With a username, the scan reports observations where that account holds a "
            "current identification and other identifiers went a different way. Leave "
            "the username blank to instead report barcoded observations by anyone "
            "whose consensus a vote could improve. This scan only reads; it never "
            "votes."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.login = QLineEdit(
            settings.dna_vote_review_login
            if settings.dna_vote_review_login_configured
            else default_login
        )
        self.login.setPlaceholderText("Blank scans observations by anyone")
        self.login.setToolTip(
            "The account whose votes are checked. Defaults to the logged-in "
            "iNaturalist user."
        )
        form.addRow("Username:", self.login)

        self.url = QLineEdit(settings.dna_vote_review_url)
        self.url.setPlaceholderText(
            "Optional — https://www.inaturalist.org/observations?place_id=…"
        )
        self.url.setToolTip(
            "Optional iNaturalist observations URL used only to narrow the population "
            "(taxon, place, dates). Its own field and identifier filters are ignored."
        )
        form.addRow("Restrict to URL (optional):", self.url)

        self.max_observations = QSpinBox()
        self.max_observations.setRange(1, 10000)
        self.max_observations.setSingleStep(200)
        self.max_observations.setValue(settings.dna_vote_review_max_observations)
        self.max_observations.setToolTip(
            "Newest observation IDs are scanned first. iNaturalist refuses to page "
            "past 10,000 results for one search."
        )
        form.addRow("Observations to scan:", self.max_observations)
        layout.addLayout(form)

        self.include_refinements = QCheckBox(
            "Also list observations where others agreed with your branch but named it "
            "more precisely"
        )
        self.include_refinements.setChecked(
            settings.dna_vote_review_include_refinements
        )
        layout.addWidget(self.include_refinements)

        self.estimate = QLabel()
        self.estimate.setWordWrap(True)
        layout.addWidget(self.estimate)
        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Scan")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.max_observations.valueChanged.connect(self._update_estimate)
        self.login.textChanged.connect(self._update_estimate)
        self._update_estimate()

    def _update_estimate(self) -> None:
        count = self.max_observations.value()
        pages = max(1, (count + 199) // 200)
        # The search pages plus one field lookup, and one username lookup when a
        # username is given. Sequences are read straight off the search pages.
        lookup = 1 if self.login.text().strip() else 0
        calls = pages + 1 + lookup
        self.estimate.setText(
            f"About {calls} API calls / {calls // 60}m {calls % 60}s at one request "
            "per second. Each page of 200 observations carries its own field values, "
            "so sequences are confirmed without extra reads."
        )

    def _accept(self) -> None:
        source_params: tuple[tuple[str, str], ...] = ()
        source_url = self.url.text().strip()
        if source_url:
            try:
                query = parse_observations_url(source_url)
                if query is None:
                    raise ValueError(
                        "Enter an iNaturalist observations URL, or leave the field "
                        "blank."
                    )
            except Exception as exc:
                QMessageBox.warning(self, "Invalid observations URL", str(exc))
                return
            source_params = tuple(
                (str(key), str(value))
                for (key, value), origin in zip(query.params, query.parameter_sources)
                if origin != "Identify default"
            )
        self.settings.dna_vote_review_login = self.login.text()
        self.settings.dna_vote_review_url = source_url
        self.settings.dna_vote_review_max_observations = self.max_observations.value()
        self.settings.dna_vote_review_include_refinements = (
            self.include_refinements.isChecked()
        )
        self.settings.sync()
        self.config = VoteReviewConfig(
            login=self.login.text().strip(),
            source_url=source_url,
            source_params=source_params,
            max_observations=self.max_observations.value(),
            include_refinements=self.include_refinements.isChecked(),
        )
        self.accept()


class VoteReviewProgressDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Scanning DNA-barcoded observations")
        self.setModal(True)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        self.label = QLabel("Starting the scan…")
        self.label.setWordWrap(True)
        self.label.setMinimumHeight(3 * self.label.fontMetrics().lineSpacing())
        layout.addWidget(self.label, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        layout.addWidget(self.bar)
        self.cancel_button = QPushButton("Cancel")
        layout.addWidget(self.cancel_button)

    def update_progress(self, progress: VoteReviewProgress) -> None:
        if progress.observations_total > 0:
            self.bar.setRange(0, progress.observations_total)
            self.bar.setValue(
                min(progress.observations_scanned, progress.observations_total)
            )
        self.label.setText(
            f"{progress.message}\n"
            f"Observations scanned: {progress.observations_scanned}"
            f" / {progress.observations_total or '?'} · "
            f"findings {progress.findings} · API calls {progress.calls_made}"
        )

    def closeEvent(self, event) -> None:
        self.cancel_button.click()
        event.ignore()


class DNAVoteReviewResultsDialog(QDialog):
    """Sortable list of findings with a detail pane and browser hand-off."""

    COLUMNS = (
        "Score",
        "Finding",
        "Observation",
        "Observer",
        "Quality",
        "Your identification",
        "Community taxon",
        "ITS bp",
        "Why",
    )

    def __init__(
        self, result: VoteReviewResult, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.result = result
        self._visible: list[VoteReviewFinding] = []
        subject = result.login or "anyone"
        self.setWindowTitle(f"DNA-contested identifications — {subject}")
        self.resize(1180, 640)
        layout = QVBoxLayout(self)

        self.summary = QLabel(self._summary_text())
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Show:"))
        self.kind_filter = QComboBox()
        self.kind_filter.addItem(ALL_KINDS, "")
        for kind, label in KIND_LABELS.items():
            if any(finding.kind == kind for finding in result.findings):
                count = sum(1 for f in result.findings if f.kind == kind)
                self.kind_filter.addItem(f"{label} ({count})", kind)
        self.kind_filter.currentIndexChanged.connect(self._repopulate)
        controls.addWidget(self.kind_filter)
        controls.addStretch(1)
        self.open_button = QPushButton("Open in browser")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_selected)
        controls.addWidget(self.open_button)
        self.copy_button = QPushButton("Copy visible URLs")
        self.copy_button.clicked.connect(self._copy_urls)
        controls.addWidget(self.copy_button)
        layout.addLayout(controls)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # One line per row: the full explanation lives in the tooltip and the
        # detail pane, so wrapping here would only make the table hard to scan.
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Fixed
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # The trailing explanation column absorbs the leftover width.
        header.setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda _item: self._open_selected())
        enable_click_sorting(self.table)
        layout.addWidget(self.table, 3)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(120)
        layout.addWidget(self.detail, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._repopulate()

    # -- population -----------------------------------------------------

    def _summary_text(self) -> str:
        result = self.result
        who = (
            f"identifications by {result.login}"
            if result.login
            else "observations by any observer"
        )
        parts = [
            f"Scanned {result.observations_scanned} DNA-barcoded {who}"
            f" and found {len(result.findings)} worth a second look."
        ]
        if result.truncated:
            parts.append(
                f"{result.observations_available} observations matched the filter; "
                "raise the scan limit or narrow the URL to cover the rest."
            )
        if result.dropped_unverified:
            parts.append(
                f"{result.dropped_unverified} candidate(s) were dropped because a "
                "qualifying ITS sequence could not be confirmed on re-read."
            )
        return " ".join(parts)

    def _repopulate(self) -> None:
        kind = self.kind_filter.currentData() or ""
        self._visible = [
            finding
            for finding in self.result.findings
            if not kind or finding.kind == kind
        ]
        with sorting_suspended(self.table):
            self.table.setRowCount(len(self._visible))
            for row, finding in enumerate(self._visible):
                cells = (
                    str(finding.score),
                    finding.kind_label,
                    str(finding.observation_id),
                    finding.observer,
                    finding.quality_grade.replace("_", " "),
                    finding.subject.display if finding.subject else "—",
                    finding.community_taxon_name or "—",
                    str(finding.sequence_length or "—"),
                    finding.reason,
                )
                for column, text in enumerate(cells):
                    item = SortableTableWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, finding.observation_id)
                    item.setToolTip(finding.reason)
                    self.table.setItem(row, column, item)
        # Findings arrive best-first; make the sort indicator say so.
        self.table.sortItems(0, Qt.SortOrder.DescendingOrder)
        self.table.resizeColumnsToContents()
        last = len(self.COLUMNS) - 1
        self.table.setColumnWidth(last, max(240, self.table.columnWidth(last)))
        self.table.resizeRowsToContents()
        self.detail.clear()
        self.open_button.setEnabled(False)
        self.copy_button.setEnabled(bool(self._visible))

    # -- interaction ----------------------------------------------------

    def _selected_finding(self) -> Optional[VoteReviewFinding]:
        items = self.table.selectedItems()
        if not items:
            return None
        observation_id = items[0].data(Qt.ItemDataRole.UserRole)
        for finding in self._visible:
            if finding.observation_id == observation_id:
                return finding
        return None

    def _selection_changed(self) -> None:
        finding = self._selected_finding()
        self.open_button.setEnabled(finding is not None)
        if finding is None:
            self.detail.clear()
            return
        lines = [
            f"{finding.kind_label} — score {finding.score}",
            finding.reason,
            "",
            f"Observation {finding.observation_id} by {finding.observer or 'unknown'}"
            + (f", observed {finding.observed_on}" if finding.observed_on else "")
            + (f", {finding.place_guess}" if finding.place_guess else ""),
            f"Displayed taxon: {finding.observation_taxon_name or '—'}",
            f"Community taxon: {finding.community_taxon_name or '—'}",
            f"Quality grade: {finding.quality_grade.replace('_', ' ') or '—'}",
        ]
        if finding.sequence_length:
            lines.append(
                f"DNA Barcode ITS: {finding.sequence_length} characters "
                "(sequence itself is never stored or logged)"
            )
        if finding.subject is not None:
            lines.append(f"Your current identification: {finding.subject.display}")
        if finding.opposing:
            lines.append("")
            lines.append("Conflicting current identifications:")
            lines.extend(
                f"  · {view.login}: {view.display}" for view in finding.opposing
            )
        if finding.finer:
            lines.append("")
            lines.append("More specific current identifications:")
            lines.extend(f"  · {view.login}: {view.display}" for view in finding.finer)
        if finding.agreeing:
            lines.append("")
            lines.append(
                "Agreeing identifiers: "
                + ", ".join(view.login for view in finding.agreeing)
            )
        lines.extend(["", finding.url])
        self.detail.setPlainText("\n".join(lines))

    def _open_selected(self) -> None:
        finding = self._selected_finding()
        if finding is not None:
            open_external_url_silently(finding.url)

    def _copy_urls(self) -> None:
        if not self._visible:
            return
        text = "\n".join(finding.url for finding in self._visible)
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        self.summary.setText(
            f"Copied {len(self._visible)} observation URLs to the clipboard. "
            + self._summary_text()
        )


class DNAVoteReviewController(QObject):
    """Owns the scan worker and its dialogs for the lifetime of the window."""

    def __init__(
        self,
        client: INatClient,
        settings: AppSettings,
        default_login_provider: Callable[[], str],
        pool: QThreadPool,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self.settings = settings
        self.default_login_provider = default_login_provider
        self.pool = pool
        self.parent_widget = parent
        self._signals: set[QObject] = set()
        self._progress: Optional[VoteReviewProgressDialog] = None
        self._cancel: Optional[threading.Event] = None
        self._results: set[QDialog] = set()

    def start(self) -> None:
        setup = DNAVoteReviewSetupDialog(
            self.settings, self.default_login_provider(), self.parent_widget
        )
        if setup.exec() != QDialog.DialogCode.Accepted or setup.config is None:
            return
        progress = VoteReviewProgressDialog(self.parent_widget)
        cancel = threading.Event()
        progress.cancel_button.clicked.connect(cancel.set)
        worker = _ReviewWorker(self.client, setup.config, cancel)
        signals = worker.signals
        self._signals.add(signals)
        self._progress, self._cancel = progress, cancel
        signals.progress.connect(progress.update_progress)
        signals.ready.connect(lambda result, s=signals: self._ready(s, result))
        signals.failed.connect(lambda message, s=signals: self._failed(s, message))
        signals.cancelled.connect(lambda s=signals: self._cancelled(s))
        self.pool.start(worker)
        progress.show()

    def _ready(self, signals: QObject, result: VoteReviewResult) -> None:
        self._finish_progress(signals)
        if not result.findings:
            QMessageBox.information(
                self.parent_widget,
                "DNA vote review",
                f"Scanned {result.observations_scanned} DNA-barcoded observations and "
                "found none whose identifications look worth revisiting.",
            )
            return
        dialog = DNAVoteReviewResultsDialog(result, self.parent_widget)
        self._results.add(dialog)
        dialog.finished.connect(
            lambda _code, target=dialog: self._results.discard(target)
        )
        dialog.show()

    def _failed(self, signals: QObject, message: str) -> None:
        self._finish_progress(signals)
        QMessageBox.warning(self.parent_widget, "DNA vote review failed", message)

    def _cancelled(self, signals: QObject) -> None:
        self._finish_progress(signals)
        QMessageBox.information(
            self.parent_widget,
            "DNA vote review cancelled",
            "The scan stopped. Nothing was written to iNaturalist.",
        )

    def _finish_progress(self, signals: QObject) -> None:
        self._signals.discard(signals)
        if self._progress is not None:
            self._progress.accept()
        self._progress, self._cancel = None, None

    def shutdown(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
        for dialog in list(self._results):
            dialog.close()
