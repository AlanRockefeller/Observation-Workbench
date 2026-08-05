"""Lifecycle-safe planning dialog for a read-only Identify session."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.client import INatClient
from observation_workbench.api.observation_url import ObservationURLParseError, parse_observations_url
from observation_workbench.services.identify_session import (
    IdentifyQueryPlan,
    IdentifySession,
    IdentifySessionCancelled,
    load_identify_session,
    query_requires_authentication,
    resolve_viewer_scoped_params,
)
from observation_workbench.storage.settings import AppSettings
from observation_workbench.ui.identify_read_error import show_safe_read_failure

_ROW_TOKEN_ROLE = Qt.ItemDataRole.UserRole
_RESOLUTION_TOKEN_ROLE = Qt.ItemDataRole.UserRole + 1
log = logging.getLogger(__name__)


class _ReadSignals(QObject):
    result = Signal(object)
    error = Signal(object)


class _ReadWorker(QRunnable):
    """Run one safe read away from the Qt UI thread."""

    def __init__(self, read: Callable[[], object]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._read = read
        self.signals = _ReadSignals()

    def run(self) -> None:
        try:
            self.signals.result.emit(self._read())
        except Exception as exc:
            self.signals.error.emit(exc)


@dataclass(frozen=True)
class _ResolutionSubscriber:
    row_token: int
    parse_generation: int
    resolution_token: int
    parameter_name: str
    api_value: str
    entity_kind: str
    entity_ids: tuple[int, ...]


class IdentifySetupDialog(QDialog):
    """Construct one immutable query plan, without stale UI callbacks."""

    session_ready = Signal(object)

    def __init__(
        self,
        settings: AppSettings,
        client: INatClient,
        api_token: str,
        parent: QWidget | None = None,
        metadata_client: INatClient | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._client = client
        # Friendly-name (taxon/place) resolution is read-only metadata that
        # shouldn't compete with the count preview / session build for the
        # shared client's 1 req/s budget; default to the same client so
        # existing callers that don't pass one keep working unchanged.
        self._metadata_client = metadata_client if metadata_client is not None else client
        self._api_token = api_token
        self._dialog_state = "open"
        self._parse_generation = 0
        self._count_generation = 0
        self._execution_generation = 0
        self._resolution_generation = 0
        self._row_token_counter = 0
        self._source_kind = "identify"
        self._display_url = ""
        self._parsed_url_text = ""
        self._has_parsed_query = False
        self._execution_active = False
        self._table_updates = False
        self._live_signals: set[_ReadSignals] = set()
        self._name_cache: dict[tuple[str, int], str | None] = {}
        self._name_errors: dict[tuple[str, int], Exception] = {}
        self._name_inflight: dict[tuple[str, int], set[_ResolutionSubscriber]] = {}

        self.setWindowTitle("Identify observations")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._build()
        self.resize(1_040, 720)
        if self._url_edit.text().strip():
            self.parse_url()

    def _build(self) -> None:
        outer = QVBoxLayout(self)

        url_row = QHBoxLayout()
        self._url_edit = QLineEdit(self._settings.identify_last_url, self)
        self._url_edit.textEdited.connect(self._on_url_edited)
        self._parse_button = QPushButton("Parse / reload", self)
        self._parse_button.clicked.connect(self.parse_url)
        url_row.addWidget(self._url_edit, 1)
        url_row.addWidget(self._parse_button)
        outer.addLayout(url_row)

        self._summary = QLabel("Paste an iNaturalist observations or Identify URL.", self)
        self._summary.setWordWrap(True)
        outer.addWidget(self._summary)

        self._table = QTableWidget(0, 4, self)
        self._table.setHorizontalHeaderLabels(
            ["Parameter", "API value", "Resolved meaning", "Source"]
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMinimumHeight(320)
        self._table.cellChanged.connect(self._on_table_changed)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_table_context_menu)
        outer.addWidget(self._table, 1)

        table_buttons = QHBoxLayout()
        self._add_parameter_button = QPushButton("Add advanced parameter", self)
        self._remove_parameter_button = QPushButton("Remove selected", self)
        self._add_parameter_button.clicked.connect(self.add_parameter)
        self._remove_parameter_button.clicked.connect(self._remove_selected_parameter)
        table_buttons.addWidget(self._add_parameter_button)
        table_buttons.addWidget(self._remove_parameter_button)
        table_buttons.addStretch()
        outer.addLayout(table_buttons)

        form = QFormLayout()
        self._limit_spin = QSpinBox(self)
        self._limit_spin.setRange(1, 999_999)
        self._limit_spin.setValue(self._settings.identify_session_limit)
        self._radius_spin = QSpinBox(self)
        self._radius_spin.setRange(0, 20)
        self._radius_spin.setValue(self._settings.identify_prefetch_radius)
        self._count_label = QLabel("Not loaded", self)
        self._auth_notice = QLabel("", self)
        self._auth_notice.setWordWrap(True)
        form.addRow("Session limit", self._limit_spin)
        form.addRow("Prefetch radius", self._radius_spin)
        form.addRow("Result count", self._count_label)
        form.addRow("", self._auth_notice)
        outer.addLayout(form)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self._execute_button = button_box.addButton(
            "Execute", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._execute_button.clicked.connect(self.execute)
        button_box.rejected.connect(self.reject)
        outer.addWidget(button_box)
        self._update_execute_enabled()

    def add_parameter(self) -> None:
        if not self._is_open() or self._execution_active:
            return
        self._add_row("", "", "", "User edit")
        self._schedule_count_preview()

    def parse_url(self) -> None:
        if not self._is_open() or self._execution_active:
            return
        try:
            query = parse_observations_url(self._url_edit.text())
            if query is None:
                raise ObservationURLParseError("Enter an iNaturalist observations URL.")
        except Exception as exc:
            if self._url_edit.text().strip() != self._parsed_url_text:
                self._mark_url_stale()
            QMessageBox.warning(self, "Invalid URL", str(exc))
            return

        self._parse_generation += 1
        self._count_generation += 1
        self._execution_generation += 1
        self._resolution_generation += 1
        parse_generation = self._parse_generation
        self._source_kind = query.source_kind
        self._display_url = query.display_url
        self._parsed_url_text = self._url_edit.text().strip()
        self._has_parsed_query = True

        sources = list(query.parameter_sources)
        if len(sources) != len(query.params):
            sources = ["URL"] * len(query.params)
        self._table_updates = True
        try:
            self._table.setRowCount(0)
            for (name, value), source in zip(query.params, sources):
                self._add_row(name, value, "", source)
        finally:
            self._table_updates = False

        self._summary.setText(
            f"{query.source_kind.title()} query with {len(query.params)} parameter(s)."
        )
        self._start_count_preview(self._record_snapshot(), parse_generation)
        for row in range(self._table.rowCount()):
            self._start_row_resolution(row)
        self._update_execute_enabled()

    def execute(self) -> None:
        if not self._is_open() or self._execution_active:
            return
        records = self._record_snapshot()
        params = tuple((name, value) for name, value, _ in records)
        sources = tuple(source for _, _, source in records)
        if query_requires_authentication(params) and not self._api_token:
            QMessageBox.warning(
                self,
                "Authentication required",
                "This query contains reviewed filtering, which is user-specific. "
                "Authenticate in the main window before building this Identify session.",
            )
            return

        plan = IdentifyQueryPlan(
            params=params,
            sources=sources,
            session_limit=self._limit_spin.value(),
            prefetch_radius=self._radius_spin.value(),
            source_kind=self._source_kind,
            display_url=self._display_url,
        )
        self._settings.identify_last_url = self._url_edit.text()
        self._settings.identify_session_limit = plan.session_limit
        self._settings.identify_prefetch_radius = plan.prefetch_radius
        self._settings.sync()
        self._start_session_build(plan, self._parse_generation)

    def reject(self) -> None:
        if self._is_open():
            self._invalidate_callbacks("rejected")
        super().reject()

    def accept(self) -> None:
        if self._is_open():
            self._invalidate_callbacks("accepted")
        super().accept()

    def closeEvent(self, event) -> None:
        if self._is_open():
            self._invalidate_callbacks("closed")
        super().closeEvent(event)

    def _remove_selected_parameter(self) -> None:
        row = self._table.currentRow()
        if row < 0 or self._execution_active:
            return
        self._table.removeRow(row)
        self._resolution_generation += 1
        self._schedule_count_preview()

    def _add_row(self, name: str, value: str, meaning: str, source: str) -> None:
        self._row_token_counter += 1
        row = self._table.rowCount()
        self._table.insertRow(row)
        name_item = QTableWidgetItem(name)
        name_item.setData(_ROW_TOKEN_ROLE, self._row_token_counter)
        name_item.setData(_RESOLUTION_TOKEN_ROLE, 0)
        self._table.setItem(row, 0, name_item)
        self._table.setItem(row, 1, QTableWidgetItem(value))
        meaning_item = QTableWidgetItem(meaning)
        meaning_item.setFlags(meaning_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(row, 2, meaning_item)
        source_item = QTableWidgetItem(source or "User edit")
        source_item.setFlags(source_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self._table.setItem(row, 3, source_item)

    def _record_snapshot(self) -> tuple[tuple[str, str, str], ...]:
        records: list[tuple[str, str, str]] = []
        for row in range(self._table.rowCount()):
            name = self._item_text(row, 0).strip()
            if not name:
                continue
            value = self._item_text(row, 1)
            source = self._item_text(row, 3) or "User edit"
            records.append((name, value, source))
        return tuple(records)

    def _on_table_changed(self, row: int, column: int) -> None:
        if self._table_updates or not self._is_open() or self._execution_active:
            return
        if column not in (0, 1):
            return
        self._table_updates = True
        try:
            source_item = self._table.item(row, 3)
            if source_item is not None:
                source_item.setText("User edit")
        finally:
            self._table_updates = False
        self._resolution_generation += 1
        self._start_row_resolution(row)
        self._schedule_count_preview()

    def _show_table_context_menu(self, position) -> None:
        row = self._table.indexAt(position).row()
        if row < 0 or self._execution_active:
            return
        menu = QMenu(self)
        retry_action = menu.addAction("Retry friendly-name resolution")
        retry_action.triggered.connect(lambda: self._start_row_resolution(row, explicit=True))
        menu.exec(self._table.viewport().mapToGlobal(position))

    def _on_url_edited(self, text: str) -> None:
        """A typed URL must never silently reuse an older parsed table."""
        del text
        if self._has_parsed_query or self._parsed_url_text:
            self._mark_url_stale()

    def _mark_url_stale(self) -> None:
        if not self._is_open() or self._execution_active:
            return
        self._parse_generation += 1
        self._count_generation += 1
        self._execution_generation += 1
        self._resolution_generation += 1
        self._has_parsed_query = False
        self._parsed_url_text = ""
        self._source_kind = "identify"
        self._display_url = ""
        self._table_updates = True
        try:
            self._table.setRowCount(0)
        finally:
            self._table_updates = False
        self._count_label.setText("URL changed — parse again")
        self._auth_notice.clear()
        self._summary.setText("Click Parse / reload to apply this URL.")
        self._update_execute_enabled()

    def _schedule_count_preview(self) -> None:
        if not self._is_open() or self._execution_active or not self._has_parsed_query:
            return
        self._count_generation += 1
        scheduled_count_generation = self._count_generation
        parse_generation = self._parse_generation

        def start_if_current() -> None:
            if not self._is_callback_current(parse_generation):
                return
            if scheduled_count_generation != self._count_generation:
                return
            self._start_count_preview(self._record_snapshot(), parse_generation)

        QTimer.singleShot(250, start_if_current)

    def _start_count_preview(
        self,
        records: tuple[tuple[str, str, str], ...],
        parse_generation: int,
    ) -> None:
        if not self._is_callback_current(parse_generation) or self._execution_active:
            return
        self._count_generation += 1
        count_generation = self._count_generation
        params = tuple((name, value) for name, value, _ in records)
        if query_requires_authentication(params) and not self._api_token:
            self._count_label.setText("Authentication required")
            self._auth_notice.setText(
                "Authenticate to preview or execute reviewed filtering faithfully."
            )
            self._update_execute_enabled()
            return

        self._auth_notice.clear()
        self._count_label.setText("Loading…")
        self._update_execute_enabled()

        def is_current() -> bool:
            return (
                self._is_callback_current(parse_generation)
                and count_generation == self._count_generation
                and not self._execution_active
            )

        def success(raw: object) -> None:
            if not is_current():
                return
            total = raw.get("total_results", "Unknown") if isinstance(raw, dict) else "Unknown"
            self._count_label.setText(str(total))

        def failure(exc: Exception) -> None:
            if not is_current():
                return
            self._count_label.setText("Count unavailable")
            self._update_execute_enabled()
            show_safe_read_failure(
                self,
                "Preview Identify result count",
                exc,
                lambda: self._start_count_preview(records, parse_generation),
            )

        # Match _start_session_build's unconditional token so the previewed
        # count reflects the same auth the built session will actually use, and
        # resolve `viewer_id` exactly as load_identify_session does -- otherwise
        # the previewed total is computed without the reviewed filter the
        # session applies.
        token = self._api_token

        def read_count() -> object:
            return self._client.get_observations(
                resolve_viewer_scoped_params(self._client, params, token),
                page=1,
                per_page=1,
                api_token=token,
            )

        self._start_read(
            "Preview Identify result count",
            read_count,
            success,
            failure,
        )

    def _start_session_build(
        self,
        plan: IdentifyQueryPlan,
        parse_generation: int,
    ) -> None:
        if not self._is_callback_current(parse_generation) or self._execution_active:
            return
        self._execution_generation += 1
        execution_generation = self._execution_generation
        self._execution_active = True
        self._set_query_controls_enabled(False)
        self._count_label.setText("Building fixed session queue…")
        self._update_execute_enabled()

        def is_current() -> bool:
            return (
                self._is_callback_current(parse_generation)
                and execution_generation == self._execution_generation
            )

        def cancelled() -> bool:
            return not is_current()

        def success(session: object) -> None:
            if not is_current() or not isinstance(session, IdentifySession):
                return
            self._execution_active = False
            self._set_query_controls_enabled(True)
            self._update_execute_enabled()
            if not session.items:
                QMessageBox.information(
                    self,
                    "No observations",
                    "This query returned no usable observations.",
                )
                return
            self.session_ready.emit(session)
            self.accept()

        def failure(exc: Exception) -> None:
            if not is_current():
                return
            self._execution_active = False
            self._set_query_controls_enabled(True)
            self._update_execute_enabled()
            if isinstance(exc, IdentifySessionCancelled):
                self._count_label.setText("Session construction cancelled")
                return
            self._count_label.setText("Session build failed")
            show_safe_read_failure(
                self,
                "Build Identify session queue",
                exc,
                lambda: self._start_session_build(plan, parse_generation),
            )

        self._start_read(
            "Build Identify session queue",
            lambda: load_identify_session(
                self._client,
                plan,
                self._api_token,
                is_cancelled=cancelled,
            ),
            success,
            failure,
        )

    # TODO(review): entity_ids are resolved one lookup per id below; batching
    # multiple pending taxon_ids into a single comma-separated /taxa lookup
    # (matching results back to each subscriber) would cut request volume
    # for queries with many ids, but needs a new batched client method and
    # careful result-matching, so it's deferred rather than done inline here.
    def _start_row_resolution(self, row: int, explicit: bool = False) -> None:
        if not self._is_open() or not 0 <= row < self._table.rowCount():
            return
        name_item = self._table.item(row, 0)
        if name_item is None:
            return
        row_token = name_item.data(_ROW_TOKEN_ROLE)
        if not isinstance(row_token, int):
            return
        parameter_name = self._item_text(row, 0).strip()
        api_value = self._item_text(row, 1)
        entity_kind, entity_ids = _resolution_target(parameter_name, api_value)
        self._resolution_generation += 1
        resolution_token = self._resolution_generation
        # Changing item data emits cellChanged just like a user edit.  Keep
        # this internal freshness marker from recursively starting another
        # row resolution through _on_table_changed.
        self._table_updates = True
        try:
            name_item.setData(_RESOLUTION_TOKEN_ROLE, resolution_token)
        finally:
            self._table_updates = False

        if entity_kind is None:
            self._set_resolution_text(row, "")
            return
        if not entity_ids:
            self._set_resolution_text(row, "Unresolved")
            return

        subscriber = _ResolutionSubscriber(
            row_token=row_token,
            parse_generation=self._parse_generation,
            resolution_token=resolution_token,
            parameter_name=parameter_name,
            api_value=api_value,
            entity_kind=entity_kind,
            entity_ids=entity_ids,
        )
        if explicit:
            for entity_id in entity_ids:
                self._name_errors.pop((entity_kind, entity_id), None)
        self._render_resolution(subscriber)

        for entity_id in entity_ids:
            key = (entity_kind, entity_id)
            if key in self._name_cache:
                continue
            if key in self._name_errors and not explicit:
                continue
            subscribers = self._name_inflight.get(key)
            if subscribers is not None:
                subscribers.add(subscriber)
                continue
            self._name_inflight[key] = {subscriber}
            self._start_name_lookup(key, subscriber, explicit)

    def _start_name_lookup(
        self,
        key: tuple[str, int],
        retry_subscriber: _ResolutionSubscriber,
        explicit: bool,
    ) -> None:
        entity_kind, entity_id = key

        def success(raw: object) -> None:
            name = _friendly_name(entity_kind, raw)
            self._complete_name_lookup(key, name)

        def failure(exc: Exception) -> None:
            self._complete_name_lookup(key, None, exc)
            if explicit and self._subscriber_is_current(retry_subscriber):
                show_safe_read_failure(
                    self,
                    f"Resolve {entity_kind} name",
                    exc,
                    lambda: self._retry_resolution_subscriber(retry_subscriber),
                )

        read: Callable[[], object]
        if entity_kind == "taxon":
            read = lambda: self._metadata_client.get_taxon_by_id(entity_id)
        else:
            read = lambda: self._metadata_client.get_place_by_id(entity_id)
        self._start_read(f"Resolve {entity_kind} name", read, success, failure)

    def _complete_name_lookup(
        self,
        key: tuple[str, int],
        name: str | None,
        error: Exception | None = None,
    ) -> None:
        subscribers = self._name_inflight.pop(key, set())
        if error is None:
            self._name_cache[key] = name
            self._name_errors.pop(key, None)
        else:
            self._name_errors[key] = error
        if not self._is_open():
            return
        for subscriber in subscribers:
            if self._subscriber_is_current(subscriber):
                self._render_resolution(subscriber)

    def _retry_resolution_subscriber(self, subscriber: _ResolutionSubscriber) -> None:
        row = self._find_row_by_token(subscriber.row_token)
        if row is not None and self._subscriber_is_current(subscriber):
            self._start_row_resolution(row, explicit=True)

    def _render_resolution(self, subscriber: _ResolutionSubscriber) -> None:
        row = self._find_row_by_token(subscriber.row_token)
        if row is None or not self._subscriber_is_current(subscriber):
            return
        keys = [(subscriber.entity_kind, entity_id) for entity_id in subscriber.entity_ids]
        errors = [self._name_errors[key] for key in keys if key in self._name_errors]
        if errors:
            error = errors[0]
            self._set_resolution_text(row, f"Failed: {type(error).__name__}: {error}")
            return
        if any(key not in self._name_cache for key in keys):
            self._set_resolution_text(row, "Resolving…")
            return
        names = [self._name_cache[key] for key in keys]
        if any(not name for name in names):
            self._set_resolution_text(row, "Unresolved")
            return
        self._set_resolution_text(row, ", ".join(name for name in names if name))

    def _subscriber_is_current(self, subscriber: _ResolutionSubscriber) -> bool:
        if not self._is_callback_current(subscriber.parse_generation):
            return False
        row = self._find_row_by_token(subscriber.row_token)
        if row is None:
            return False
        name_item = self._table.item(row, 0)
        return bool(
            name_item is not None
            and self._item_text(row, 0).strip() == subscriber.parameter_name
            and self._item_text(row, 1) == subscriber.api_value
            and name_item.data(_RESOLUTION_TOKEN_ROLE) == subscriber.resolution_token
        )

    def _find_row_by_token(self, row_token: int) -> int | None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None and item.data(_ROW_TOKEN_ROLE) == row_token:
                return row
        return None

    def _set_resolution_text(self, row: int, value: str) -> None:
        item = self._table.item(row, 2)
        if item is not None:
            item.setText(value)

    def _item_text(self, row: int, column: int) -> str:
        item = self._table.item(row, column)
        return item.text() if item is not None else ""

    def _start_read(
        self,
        operation: str,
        read: Callable[[], object],
        success: Callable[[object], None],
        failure: Callable[[Exception], None],
    ) -> None:
        log.debug("Starting Identify read: %s", operation)
        worker = _ReadWorker(read)
        signals = worker.signals
        self._live_signals.add(signals)

        def on_success(value: object) -> None:
            self._live_signals.discard(signals)
            success(value)

        def on_failure(exc: Exception) -> None:
            self._live_signals.discard(signals)
            failure(exc)

        signals.result.connect(on_success)
        signals.error.connect(on_failure)
        QThreadPool.globalInstance().start(worker)

    def _set_query_controls_enabled(self, enabled: bool) -> None:
        self._url_edit.setEnabled(enabled)
        self._parse_button.setEnabled(enabled)
        self._table.setEnabled(enabled)
        self._add_parameter_button.setEnabled(enabled)
        self._remove_parameter_button.setEnabled(enabled)
        self._limit_spin.setEnabled(enabled)
        self._radius_spin.setEnabled(enabled)

    def _update_execute_enabled(self) -> None:
        params = tuple((name, value) for name, value, _ in self._record_snapshot())
        requires_auth = query_requires_authentication(params)
        enabled = (
            self._is_open()
            and self._has_parsed_query
            and self._url_edit.text().strip() == self._parsed_url_text
            and not self._execution_active
            and not (requires_auth and not self._api_token)
        )
        self._execute_button.setEnabled(enabled)
        if requires_auth and not self._api_token:
            self._execute_button.setToolTip("Authentication is required for reviewed filtering.")
        else:
            self._execute_button.setToolTip("")

    def _is_open(self) -> bool:
        return self._dialog_state == "open"

    def _is_callback_current(self, parse_generation: int) -> bool:
        return self._is_open() and parse_generation == self._parse_generation

    def _invalidate_callbacks(self, state: str) -> None:
        self._dialog_state = state
        self._parse_generation += 1
        self._count_generation += 1
        self._execution_generation += 1
        self._resolution_generation += 1
        self._execution_active = False


def _resolution_target(parameter_name: str, api_value: str) -> tuple[str | None, tuple[int, ...]]:
    normalized_name = parameter_name.strip().casefold()
    if normalized_name == "place_id":
        value = api_value.strip()
        return "place", (int(value),) if value.isdigit() else ()
    if normalized_name in {"taxon_id", "without_taxon_id"}:
        values = tuple(part.strip() for part in api_value.split(","))
        if not values or any(not value.isdigit() for value in values):
            return "taxon", ()
        return "taxon", tuple(int(value) for value in values)
    return None, ()


def _friendly_name(entity_kind: str, raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    results = raw.get("results") or []
    first = results[0] if isinstance(results, list) and results else {}
    if not isinstance(first, dict):
        return None
    if entity_kind == "place":
        return first.get("display_name") or first.get("name") or None
    return first.get("name") or None
