"""Reusable, lifecycle-safe taxon autocomplete controls.

Both the study filter and Identify action entry use this small control so a
taxon is always selected from an API result carrying its numeric iNaturalist
ID.  Network work stays in a QRunnable and results are correlated with the
text generation that started them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import (
    QEvent,
    QObject,
    QRunnable,
    QThreadPool,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.client import INatClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaxonAutocompleteItem:
    """One stable taxon choice returned by the iNaturalist autocomplete API."""

    taxon_id: int
    scientific_name: str
    preferred_common_name: str = ""
    rank: str = ""

    def display_name(self, include_common_name: bool = True) -> str:
        if include_common_name and self.preferred_common_name:
            return f"{self.preferred_common_name} ({self.scientific_name})"
        return self.scientific_name

    @property
    def selection_text(self) -> str:
        details = [f"taxon #{self.taxon_id}"]
        if self.rank:
            details.append(self.rank)
        if self.preferred_common_name:
            details.append(self.preferred_common_name)
        return f"Selected: {self.scientific_name} · " + " · ".join(details)


@dataclass(frozen=True)
class TaxonAutocompleteResult:
    """Terminal result for one autocomplete generation."""

    generation: int
    items: tuple[TaxonAutocompleteItem, ...] = ()
    diagnostic: str = ""
    query: str = ""


class _TaxonAutocompleteSignals(QObject):
    finished = Signal(object)


class TaxonAutocompleteWorker(QRunnable):
    """Read taxon suggestions without touching Qt GUI objects."""

    def __init__(self, client: INatClient, query: str, generation: int) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._client = client
        self._query = query
        self._generation = generation
        self.signals = _TaxonAutocompleteSignals()

    def run(self) -> None:
        try:
            raw = self._client.get_taxa_autocomplete(self._query, per_page=7)
            records = raw.get("results") if isinstance(raw, dict) else []
            items: list[TaxonAutocompleteItem] = []
            for record in records or []:
                if not isinstance(record, dict):
                    continue
                try:
                    taxon_id = int(record.get("id"))
                except (TypeError, ValueError):
                    continue
                scientific_name = str(record.get("name") or "").strip()
                if taxon_id <= 0 or not scientific_name:
                    continue
                items.append(
                    TaxonAutocompleteItem(
                        taxon_id=taxon_id,
                        scientific_name=scientific_name,
                        preferred_common_name=str(
                            record.get("preferred_common_name") or ""
                        ).strip(),
                        rank=str(record.get("rank") or "").strip(),
                    )
                )
            result = TaxonAutocompleteResult(
                self._generation,
                tuple(items),
                query=self._query,
            )
            log.debug(
                "Taxon autocomplete generation %s returned %s usable result(s) for %r",
                self._generation,
                len(items),
                self._query,
            )
        except Exception as exc:
            # A type name is enough to distinguish a local autocomplete
            # failure without exposing private response/request diagnostics.
            result = TaxonAutocompleteResult(
                self._generation,
                diagnostic=type(exc).__name__,
                query=self._query,
            )
            log.debug(
                "Taxon autocomplete generation %s failed for %r",
                self._generation,
                self._query,
                exc_info=True,
            )
        self.signals.finished.emit(result)


class TaxonAutocompleteField(QWidget):
    """A debounced autocomplete editor that requires an API-resolved taxon."""

    selection_changed = Signal(object)
    search_status_changed = Signal(str)

    def __init__(
        self,
        client: INatClient,
        parent: QWidget | None = None,
        *,
        include_common_names: bool = True,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._include_common_names = include_common_names
        self._pool = QThreadPool.globalInstance()
        self._generation = 0
        self._closed = False
        self._items: tuple[TaxonAutocompleteItem, ...] = ()
        self._selected_item: TaxonAutocompleteItem | None = None
        self._live_signals: set[_TaxonAutocompleteSignals] = set()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.line_edit = QLineEdit(self)
        self.line_edit.setPlaceholderText("Type at least two characters to search taxa…")
        self.line_edit.textEdited.connect(self._text_edited)
        self.line_edit.installEventFilter(self)
        row.addWidget(self.line_edit)
        outer.addLayout(row)

        self._suggestions_label = QLabel("Matching taxa — click one to select:", self)
        self._suggestions_label.setTextFormat(Qt.TextFormat.PlainText)
        self._suggestions_label.hide()
        outer.addWidget(self._suggestions_label)
        self._suggestions = QListWidget(self)
        self._suggestions.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._suggestions.setUniformItemSizes(True)
        self._suggestions.setAlternatingRowColors(True)
        self._suggestions.itemClicked.connect(self._suggestion_activated)
        self._suggestions.itemActivated.connect(self._suggestion_activated)
        self._suggestions.installEventFilter(self)
        self._suggestions.hide()
        outer.addWidget(self._suggestions)

        self._selected_label = QLabel("Select a taxon from the suggestions.", self)
        self._selected_label.setTextFormat(Qt.TextFormat.PlainText)
        self._selected_label.setWordWrap(True)
        outer.addWidget(self._selected_label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start_search)

    @property
    def selected_item(self) -> TaxonAutocompleteItem | None:
        return self._selected_item

    @property
    def has_selection(self) -> bool:
        return self._selected_item is not None

    @property
    def popup_visible(self) -> bool:
        return self._suggestions.isVisible()

    def focus_editor(self) -> None:
        self.line_edit.setFocus()

    def shutdown(self) -> None:
        """Invalidate late callbacks while retaining each signal until it ends."""
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self._timer.stop()
        self._hide_suggestions()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not isinstance(event, QKeyEvent):
            return super().eventFilter(watched, event)
        if watched is self.line_edit and self._suggestions.isVisible():
            if event.key() in {Qt.Key.Key_Down, Qt.Key.Key_Up}:
                row = (
                    0
                    if event.key() == Qt.Key.Key_Down
                    else self._suggestions.count() - 1
                )
                self._suggestions.setCurrentRow(row)
                self._suggestions.setFocus()
                return True
            if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                current = self._suggestions.currentItem()
                if current is None and self._suggestions.count():
                    current = self._suggestions.item(0)
                if current is not None:
                    self._suggestion_activated(current)
                    return True
            if event.key() == Qt.Key.Key_Escape:
                self._hide_suggestions()
                return True
        if watched is self._suggestions and event.key() == Qt.Key.Key_Escape:
            self._hide_suggestions()
            self.line_edit.setFocus()
            return True
        return super().eventFilter(watched, event)

    def _text_edited(self, text: str) -> None:
        if (
            self._selected_item is not None
            and self._selected_item.scientific_name == text.strip()
        ):
            self._timer.stop()
            return
        self._generation += 1
        self._timer.stop()
        self._items = ()
        self._hide_suggestions()
        self._clear_selection()
        self.search_status_changed.emit("")
        if len(text.strip()) >= 2:
            self._timer.start(300)

    def _clear_selection(self) -> None:
        if self._selected_item is None:
            return
        self._selected_item = None
        self._selected_label.setText("Select a taxon from the suggestions.")
        self.selection_changed.emit(None)

    def _start_search(self) -> None:
        if self._closed:
            return
        query = self.line_edit.text().strip()
        if len(query) < 2:
            return
        generation = self._generation
        worker = TaxonAutocompleteWorker(self._client, query, generation)
        signals = worker.signals
        self._live_signals.add(signals)
        # Connect to a QObject slot rather than a lambda.  The worker emits
        # from a thread-pool thread; the slot then runs in this widget's GUI
        # thread before it updates the inline suggestion list.
        signals.finished.connect(self._search_finished)
        self._pool.start(worker)

    @Slot(object)
    def _search_finished(self, result: object) -> None:
        signals = self.sender()
        if isinstance(signals, _TaxonAutocompleteSignals):
            self._live_signals.discard(signals)
        if self._closed or not isinstance(result, TaxonAutocompleteResult):
            log.debug("Ignoring taxon autocomplete callback for a closed or invalid field")
            return
        if result.generation != self._generation:
            log.debug(
                "Ignoring stale taxon autocomplete generation %s (current=%s)",
                result.generation,
                self._generation,
            )
            return
        if self.line_edit.text().strip() != result.query:
            log.debug(
                "Ignoring taxon autocomplete generation %s because the query changed",
                result.generation,
            )
            return
        if result.diagnostic:
            self._items = ()
            self._hide_suggestions()
            self.search_status_changed.emit("Taxon search is temporarily unavailable. Try again.")
            log.debug(
                "Taxon autocomplete generation %s ended with %s",
                result.generation,
                result.diagnostic,
            )
            return
        self._items = result.items
        if not self._items:
            self._hide_suggestions()
            self.search_status_changed.emit("No matching taxa were found.")
            return
        # An exact scientific name identifies one concrete API result without
        # guessing among fuzzy suggestions, so it can be resolved immediately.
        normalized_query = result.query.casefold()
        exact_matches = tuple(
            item
            for item in self._items
            if item.scientific_name.casefold() == normalized_query
        )
        if len(exact_matches) == 1:
            self._select_item(exact_matches[0])
            return
        if self.isVisible():
            self._show_suggestions()
            log.debug(
                "Showing %s inline taxon autocomplete suggestion(s) for generation %s",
                len(self._items),
                result.generation,
            )
        else:
            log.debug(
                "Taxon autocomplete generation %s has %s result(s), but the field "
                "is not visible",
                result.generation,
                len(self._items),
            )

    def _show_suggestions(self) -> None:
        self._suggestions.clear()
        for item in self._items:
            row = QListWidgetItem(self._completion_text(item))
            row.setData(Qt.ItemDataRole.UserRole, item.taxon_id)
            row.setToolTip(item.selection_text)
            self._suggestions.addItem(row)
        row_height = max(
            self._suggestions.sizeHintForRow(0),
            self._suggestions.fontMetrics().height() + 8,
        )
        self._suggestions.setFixedHeight(
            row_height * min(len(self._items), 7)
            + (2 * self._suggestions.frameWidth())
            + 2
        )
        self._suggestions_label.show()
        self._suggestions.show()

    def _hide_suggestions(self) -> None:
        self._suggestions_label.hide()
        self._suggestions.hide()

    @Slot(QListWidgetItem)
    def _suggestion_activated(self, row: QListWidgetItem) -> None:
        taxon_id = row.data(Qt.ItemDataRole.UserRole)
        for item in self._items:
            if item.taxon_id == taxon_id:
                self._select_item(item)
                return

    def _select_item(self, item: TaxonAutocompleteItem) -> None:
        changed = self._selected_item != item
        self._timer.stop()
        self._selected_item = item
        self.line_edit.setText(item.scientific_name)
        self._selected_label.setText(item.selection_text)
        self._hide_suggestions()
        self.search_status_changed.emit("")
        if changed:
            self.selection_changed.emit(item)
        if changed:
            log.debug(
                "Selected taxon autocomplete result %s (%s)",
                item.scientific_name,
                item.taxon_id,
            )

    def _completion_text(self, item: TaxonAutocompleteItem) -> str:
        name = item.display_name(self._include_common_names)
        if item.rank:
            return f"{name} · {item.rank} · #{item.taxon_id}"
        return f"{name} · #{item.taxon_id}"
