"""
Taxon Summary Tree panel — learning feature.

Shows species counts for the current filter context, sorted by count.
Clicking a taxon queues it into the taxon filter field.

How it's populated:
  - Calls /observations/species_counts?ident_user_login=...&place_id=...&taxon_id=...
    - This returns counts of distinct taxa in observations where the target user has identified
  - For observations URL mode, calls /observations/species_counts with the URL query params
  - Results grouped by taxon, sorted by count descending
  - See services/taxon_summary.py for caching details

How to extend for compare mode:
  - Add a second user's TaxonSummary alongside the first
  - Compute per-taxon agreement/disagreement rates
  - Add columns: User A count, User B count, overlap, disagreement %
  - The tree structure already supports multi-column display via QTreeWidget
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional

from PySide6.QtCore import (
    QObject, QRunnable, QThreadPool, Qt, Signal, Slot,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)


class _CountItem(QTreeWidgetItem):
    """QTreeWidgetItem that sorts the count column (1) numerically."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        tree = self.treeWidget()
        if tree and tree.sortColumn() == 1:
            try:
                return int(self.text(1)) < int(other.text(1))
            except (ValueError, TypeError):
                pass
        return super().__lt__(other)

from observation_workbench.api.client import INatClient
from observation_workbench.models import TaxonSummary
from observation_workbench.services.taxon_summary import TaxonSummaryService
from observation_workbench.storage.cache_db import CacheDB

log = logging.getLogger(__name__)


class _SummarySignals(QObject):
    finished = Signal(object)  # TaxonSummary
    error = Signal(str)


class _SummaryWorker(QRunnable):
    def __init__(self, service: TaxonSummaryService, kwargs: dict, generation: int, get_gen) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.service = service
        self.kwargs = kwargs
        self.generation = generation
        self.get_gen = get_gen
        self.signals = _SummarySignals()

    def run(self) -> None:
        if self.get_gen() != self.generation:
            return
        try:
            if "observation_source_key" in self.kwargs:
                summary = self.service.fetch_observation_query(
                    source_key=self.kwargs["observation_source_key"],
                    query_params=self.kwargs["observation_params"],
                )
            else:
                summary = self.service.fetch(**self.kwargs)
            if self.get_gen() == self.generation:
                self.signals.finished.emit(summary)
        except Exception as exc:
            if self.get_gen() == self.generation:
                self.signals.error.emit(str(exc))


class TaxonTreePanel(QWidget):
    """
    Collapsible taxon summary panel.
    Emits taxon_selected(taxon_id, taxon_name) when user clicks a taxon.
    """

    taxon_selected = Signal(int, str)   # taxon_id, display_name
    summary_finished = Signal()         # emitted on success or error

    def __init__(
        self,
        client: INatClient,
        db: CacheDB,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = TaxonSummaryService(client, db)
        self._pool = QThreadPool.globalInstance()
        self._generation = 0
        self._live_summary_signals: set = set()  # prevent GC of signal objects
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Header row
        header = QHBoxLayout()
        title = QLabel("<b>Taxon Summary</b>")
        header.addWidget(title)
        header.addStretch()
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setFixedWidth(70)
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.clicked.connect(self._reload)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        self._progress.setFixedHeight(4)
        layout.addWidget(self._progress)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Taxon", "Count"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSortingEnabled(True)
        self._tree.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.setToolTip("Double-click a taxon to filter by it")
        layout.addWidget(self._tree)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #888;")
        layout.addWidget(self._status_label)

    def load_summary(
        self,
        username: str,
        taxon_id=None,
        place_id=None,
        d1=None,
        d2=None,
    ) -> None:
        """Fetch and display taxon summary for the given filters."""
        self._generation += 1
        gen = self._generation
        self._progress.setVisible(True)
        self._refresh_btn.setEnabled(False)
        self._current_kwargs = dict(
            username=username, taxon_id=taxon_id,
            place_id=place_id, d1=d1, d2=d2,
        )
        self._start_worker(self._current_kwargs, gen)

    def load_observation_summary(
        self,
        source_key: str,
        query_params: list,
    ) -> None:
        """Fetch and display taxon summary for an observations URL query."""
        self._generation += 1
        gen = self._generation
        self._progress.setVisible(True)
        self._refresh_btn.setEnabled(False)
        self._current_kwargs = dict(
            observation_source_key=source_key,
            observation_params=query_params,
        )
        self._start_worker(self._current_kwargs, gen)

    def _start_worker(self, kwargs: dict, gen: int) -> None:
        worker = _SummaryWorker(
            service=self._service,
            kwargs=kwargs,
            generation=gen,
            get_gen=lambda: self._generation,
        )
        sigs = worker.signals
        self._live_summary_signals.add(sigs)
        sigs.finished.connect(lambda r, s=sigs, g=gen: (self._live_summary_signals.discard(s), self._on_summary_loaded(r, g)))
        sigs.error.connect(lambda e, s=sigs, g=gen: (self._live_summary_signals.discard(s), self._on_summary_error(e, g)))
        self._pool.start(worker)

    def _on_summary_loaded(self, summary: TaxonSummary, generation: int) -> None:
        if generation != self._generation:
            return
        self._progress.setVisible(False)
        self._refresh_btn.setEnabled(True)
        self._populate(summary)
        self.summary_finished.emit()

    def _on_summary_error(self, msg: str, generation: int) -> None:
        if generation != self._generation:
            return
        self._progress.setVisible(False)
        self._refresh_btn.setEnabled(True)
        self._status_label.setText(f"Error: {msg[:80]}")
        log.warning("Taxon summary error: %s", msg)
        self.summary_finished.emit()

    def _populate(self, summary: TaxonSummary) -> None:
        self._tree.clear()
        for tc in summary.counts:
            item = _CountItem([
                tc.taxon.display_name,
                str(tc.count),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, tc.taxon.taxon_id)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, tc.taxon.display_name)
            # Right-align the count column
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._tree.addTopLevelItem(item)

        n = self._tree.topLevelItemCount()
        self._status_label.setText(
            f"{n} taxa · {summary.total} observations"
        )

    def cancel_pending(self) -> None:
        """Cancel any in-flight summary fetch and reset UI to waiting state."""
        self._generation += 1
        self._progress.setVisible(False)
        self._refresh_btn.setEnabled(False)
        self._status_label.setText("Waiting for results…")

    def _reload(self) -> None:
        kwargs = getattr(self, "_current_kwargs", None)
        if not kwargs:
            return
        self._generation += 1
        gen = self._generation
        self._progress.setVisible(True)
        self._refresh_btn.setEnabled(False)
        self._start_worker(dict(kwargs), gen)

    @Slot(QTreeWidgetItem, int)
    def _on_item_double_clicked(self, item: QTreeWidgetItem, col: int) -> None:
        taxon_id = item.data(0, Qt.ItemDataRole.UserRole)
        taxon_name = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if taxon_id:
            self.taxon_selected.emit(int(taxon_id), str(taxon_name))
