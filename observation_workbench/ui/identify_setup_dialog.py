"""Lifecycle-safe planning dialog for a read-only Identify session."""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.client import INatClient
from observation_workbench.api.observation_url import (
    ObservationURLParseError,
    parse_observations_url,
)
from observation_workbench.services.identify_presets import (
    PresetCancelled,
    describe_query,
    find_dna_disagreement_ids,
    find_maverick_observation_ids,
    fungi_near_url,
    id_list_url,
    query_params_url,
    static_meaning,
)
from observation_workbench.services.identify_session import (
    ID_LIST_CHUNK,
    IdentifyQueryPlan,
    IdentifySession,
    IdentifySessionCancelled,
    load_identify_session,
    query_requires_authentication,
    resolve_viewer_scoped_params,
    split_id_list,
)
from observation_workbench.storage.settings import AppSettings
from observation_workbench.ui.identify_read_error import show_safe_read_failure

_ROW_TOKEN_ROLE = Qt.ItemDataRole.UserRole
_RESOLUTION_TOKEN_ROLE = Qt.ItemDataRole.UserRole + 1
log = logging.getLogger(__name__)

# Shared across dialog openings: taxon/place names (persisted in settings) and
# recent result counts (memory only; shown instantly, then refreshed).
_shared_names: dict[tuple[str, int], str] | None = None
_count_cache: dict[tuple, tuple[float, str]] = {}
_COUNT_CACHE_TTL_SECONDS = 15 * 60

# Dynamic presets: their observation list is looked up afresh every time.
PRESET_MAVERICK = "maverick"
PRESET_DNA_DISAGREE = "dna_disagree"
_PRESET_TITLES = {
    PRESET_MAVERICK: "My maverick identifications",
    PRESET_DNA_DISAGREE: "DNA-barcoded observations where others disagree with my ID",
}

_QUICK_BUTTON_STYLE = """
QPushButton[recent="true"] {
    text-align: left;
    padding: 6px 10px;
    border: 1px solid palette(mid);
    border-radius: 6px;
    background: palette(base);
}
QPushButton[recent="true"]:hover, QPushButton[recent="true"]:focus {
    border: 1px solid palette(highlight);
}
"""


def _load_shared_names(settings: AppSettings) -> dict[tuple[str, int], str]:
    global _shared_names
    if _shared_names is None:
        _shared_names = {}
        for key, name in settings.identify_name_cache.items():
            kind, _, raw_id = key.partition(":")
            if kind in {"taxon", "place"} and raw_id.isdigit():
                _shared_names[(kind, int(raw_id))] = name
    return _shared_names


def _save_shared_names(settings: AppSettings) -> None:
    if _shared_names is not None:
        settings.identify_name_cache = {
            f"{kind}:{entity_id}": name
            for (kind, entity_id), name in _shared_names.items()
        }


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
        login: str = "",
        user_id: int = 0,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._client = client
        self._login = login.strip()
        self._user_id = int(user_id or 0)
        self._preset_generation = 0
        self._preset_busy = False
        # (kind, title) when the current table came from a dynamic preset, so
        # history replays the lookup rather than a frozen observation list.
        self._active_preset: tuple[str, str] | None = None
        # Parameters exactly as parsed from the URL field, to tell whether the
        # table was edited before execution (history must replay what ran).
        self._parsed_params: tuple[tuple[str, str], ...] = ()
        self._quick_buttons: list[QPushButton] = []
        # Friendly-name (taxon/place) resolution is read-only metadata that
        # shouldn't compete with the count preview / session build for the
        # shared client's 1 req/s budget; default to the same client so
        # existing callers that don't pass one keep working unchanged.
        self._metadata_client = (
            metadata_client if metadata_client is not None else client
        )
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
        self._pending_reads: dict[
            _ReadSignals, tuple[Callable[[object], None], Callable[[Exception], None]]
        ] = {}
        self._name_cache: dict[tuple[str, int], str | None] = dict(
            _load_shared_names(settings)
        )
        self._name_errors: dict[tuple[str, int], Exception] = {}
        self._name_inflight: dict[tuple[str, int], set[_ResolutionSubscriber]] = {}

        self.setWindowTitle("Identify observations")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._build()
        self.resize(900, 760)
        if self._url_edit.text().strip():
            self.parse_url()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setSpacing(8)
        from observation_workbench.ui.home_page import CARD_STYLE

        self.setStyleSheet(CARD_STYLE + _QUICK_BUTTON_STYLE)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(scroll)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(4, 4, 8, 4)
        body_layout.setSpacing(10)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        heading = QLabel("What would you like to identify?", body)
        heading_font = heading.font()
        heading_font.setPointSizeF(heading_font.pointSizeF() * 1.5)
        heading_font.setBold(True)
        heading.setFont(heading_font)
        body_layout.addWidget(heading)

        if not self._api_token:
            signed_out = QLabel(
                "You are not signed in. You can still look through observations; "
                "sign in from the main window to add IDs, and to use the "
                "“My …” buttons, which need to know who you are.",
                body,
            )
            signed_out.setWordWrap(True)
            body_layout.addWidget(signed_out)

        # Quick start ---------------------------------------------------
        area_row = QHBoxLayout()
        self._area_label = QLabel("", body)
        area_row.addWidget(self._area_label, 1)
        self._change_area_button = QPushButton("Change area…", body)
        self._change_area_button.clicked.connect(self._choose_home_place)
        area_row.addWidget(self._change_area_button)
        body_layout.addLayout(area_row)

        quick = QGridLayout()
        quick.setHorizontalSpacing(10)
        quick.setVerticalSpacing(10)
        self._fungi_button = self._quick_button(
            "", "Fungi observations still at “Needs ID”", body
        )
        self._fungi_button.clicked.connect(
            lambda _checked=False: self._load_fungi_preset(only_kingdom=False)
        )
        self._fungi_kingdom_button = self._quick_button(
            "", "Nobody has got further than “Fungi” — a good place to help", body
        )
        self._fungi_kingdom_button.clicked.connect(
            lambda _checked=False: self._load_fungi_preset(only_kingdom=True)
        )
        self._maverick_button = self._quick_button(
            _PRESET_TITLES[PRESET_MAVERICK],
            "Where your current ID disagrees with the community — worth a re-look",
            body,
        )
        self._maverick_button.clicked.connect(
            lambda _checked=False: self._run_dynamic_preset(PRESET_MAVERICK)
        )
        self._dna_button = self._quick_button(
            "DNA-barcoded observations where others disagree with me",
            "Sequenced observations where your ID is outvoted or contested",
            body,
        )
        self._dna_button.clicked.connect(
            lambda _checked=False: self._run_dynamic_preset(PRESET_DNA_DISAGREE)
        )
        for button in (self._maverick_button, self._dna_button):
            if not self._preset_available(button):
                button.setEnabled(False)
                button.setToolTip(
                    "Sign in from the main window first."
                    if not self._login
                    else "Sign in again from the main window so the program "
                    "learns your account number."
                )
        quick.addWidget(self._fungi_button, 0, 0)
        quick.addWidget(self._fungi_kingdom_button, 0, 1)
        quick.addWidget(self._maverick_button, 1, 0)
        quick.addWidget(self._dna_button, 1, 1)
        quick.setColumnStretch(0, 1)
        quick.setColumnStretch(1, 1)
        body_layout.addLayout(quick)
        self._refresh_area_text()

        # Recent searches -------------------------------------------------
        self._recent_heading = QLabel("<b>Recent searches</b>", body)
        body_layout.addWidget(self._recent_heading)
        self._recent_container = QWidget(body)
        self._recent_layout = QVBoxLayout(self._recent_container)
        self._recent_layout.setContentsMargins(0, 0, 0, 0)
        self._recent_layout.setSpacing(4)
        body_layout.addWidget(self._recent_container)
        self._rebuild_recent()

        # Link (kept visible, but secondary) ------------------------------
        link_label = QLabel(
            "Or paste an iNaturalist observations or Identify link:", body
        )
        small = link_label.font()
        small.setPointSizeF(small.pointSizeF() * 0.9)
        link_label.setFont(small)
        link_label.setEnabled(False)  # dimmed, de-emphasised
        body_layout.addWidget(link_label)
        url_row = QHBoxLayout()
        self._url_edit = QLineEdit(self._settings.identify_last_url, body)
        self._url_edit.setFont(small)
        self._url_edit.setPlaceholderText(
            "https://www.inaturalist.org/observations/identify?…"
        )
        self._url_edit.textEdited.connect(self._on_url_edited)
        self._url_edit.returnPressed.connect(self.parse_url)
        self._parse_button = QPushButton("Load link", body)
        self._parse_button.setFont(small)
        self._parse_button.clicked.connect(self.parse_url)
        url_row.addWidget(self._url_edit, 1)
        url_row.addWidget(self._parse_button)
        body_layout.addLayout(url_row)

        # Query details (advanced) ----------------------------------------
        self._details_toggle = QToolButton(body)
        self._details_toggle.setCheckable(True)
        self._details_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._details_toggle.setText("Show query details and options")
        self._details_toggle.setStyleSheet("QToolButton { border: none; }")
        self._details_toggle.toggled.connect(self._set_details_visible)
        body_layout.addWidget(self._details_toggle)

        self._details = QWidget(body)
        details_layout = QVBoxLayout(self._details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self._table = QTableWidget(0, 4, self._details)
        self._table.setHorizontalHeaderLabels(
            ["Parameter", "API value", "Meaning", "Source"]
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setColumnWidth(1, 160)
        self._table.setMinimumHeight(220)
        self._table.cellChanged.connect(self._on_table_changed)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_table_context_menu)
        details_layout.addWidget(self._table, 1)

        table_buttons = QHBoxLayout()
        self._add_parameter_button = QPushButton(
            "Add advanced parameter", self._details
        )
        self._remove_parameter_button = QPushButton("Remove selected", self._details)
        self._add_parameter_button.clicked.connect(self.add_parameter)
        self._remove_parameter_button.clicked.connect(self._remove_selected_parameter)
        table_buttons.addWidget(self._add_parameter_button)
        table_buttons.addWidget(self._remove_parameter_button)
        table_buttons.addStretch()
        details_layout.addLayout(table_buttons)

        form = QFormLayout()
        self._limit_spin = QSpinBox(self._details)
        self._limit_spin.setRange(1, 999_999)
        self._limit_spin.setValue(self._settings.identify_session_limit)
        self._limit_spin.setToolTip("The most observations one session will hold.")
        self._radius_spin = QSpinBox(self._details)
        self._radius_spin.setRange(0, 20)
        self._radius_spin.setValue(self._settings.identify_prefetch_radius)
        self._radius_spin.setToolTip(
            "How many observations ahead (and behind) to load photos for in advance."
        )
        form.addRow("Session limit", self._limit_spin)
        form.addRow("Load photos ahead", self._radius_spin)
        details_layout.addLayout(form)
        self._details.setVisible(False)
        body_layout.addWidget(self._details)
        body_layout.addStretch(1)

        # Selection summary + start (always visible) ----------------------
        selection = QFrame(self)
        selection.setFrameShape(QFrame.Shape.StyledPanel)
        selection_layout = QVBoxLayout(selection)
        selection_layout.setContentsMargins(10, 8, 10, 8)
        self._summary = QLabel("Choose a search above, or paste a link.", selection)
        self._summary.setWordWrap(True)
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        selection_layout.addWidget(self._summary)
        count_row = QHBoxLayout()
        self._count_label = QLabel("Not loaded", selection)
        count_font = self._count_label.font()
        count_font.setBold(True)
        self._count_label.setFont(count_font)
        count_row.addWidget(self._count_label)
        self._auth_notice = QLabel("", selection)
        self._auth_notice.setWordWrap(True)
        count_row.addWidget(self._auth_notice, 1)
        selection_layout.addLayout(count_row)
        outer.addWidget(selection)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self._execute_button = button_box.addButton(
            "Start identifying", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._execute_button.setDefault(True)
        self._execute_button.clicked.connect(self.execute)
        button_box.rejected.connect(self.reject)
        outer.addWidget(button_box)
        self._update_execute_enabled()

    # ------------------------------------------------------------------
    # Friendly entry points

    def _quick_button(self, title: str, subtitle: str, parent: QWidget) -> QPushButton:
        from observation_workbench.ui.home_page import CardButton

        button = CardButton(title, "", html.escape(subtitle), parent=parent)
        self._quick_buttons.append(button)
        return button

    @staticmethod
    def _set_quick_text(button: QPushButton, title: str) -> None:
        button.set_title(title)  # type: ignore[attr-defined]

    def _refresh_area_text(self) -> None:
        place_id, name = self._settings.identify_home_place
        area = name or "your area"
        self._area_label.setText(
            f"<b>Quick start</b> &nbsp;·&nbsp; your area: {html.escape(area)}"
            if place_id
            else "<b>Quick start</b> &nbsp;·&nbsp; set your area to use the "
            "fungi buttons"
        )
        self._change_area_button.setText("Change area…" if place_id else "Set area…")
        short = (name.split(",")[0] if name else "my area") or "my area"
        self._set_quick_text(self._fungi_button, f"Fungi near {short} that need ID")
        self._set_quick_text(
            self._fungi_kingdom_button, f"Fungi near {short} identified only as “Fungi”"
        )

    def _choose_home_place(self) -> bool:
        dialog = _PlacePickerDialog(
            self._metadata_client,
            suggestion=(self._settings.last_place_id, self._settings.last_place_name),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.place is None:
            return False
        place_id, name = dialog.place
        self._settings.set_identify_home_place(place_id, name)
        self._settings.sync()
        self._name_cache[("place", place_id)] = name
        _load_shared_names(self._settings)[("place", place_id)] = name
        self._refresh_area_text()
        return True

    def _load_fungi_preset(self, *, only_kingdom: bool) -> None:
        if not self._is_open() or self._execution_active or self._preset_busy:
            return
        place_id, _name = self._settings.identify_home_place
        if place_id is None:
            if not self._choose_home_place():
                return
            place_id, _name = self._settings.identify_home_place
        if place_id is None:
            return
        self._load_url(
            fungi_near_url(
                place_id, only_kingdom=only_kingdom, signed_in=bool(self._api_token)
            )
        )

    def _load_url(self, url: str, preset: tuple[str, str] | None = None) -> None:
        self._url_edit.setText(url)
        self.parse_url()
        if self._has_parsed_query and self._parsed_url_text == url.strip():
            self._active_preset = preset
            self._refresh_summary()

    def _run_dynamic_preset(self, kind: str) -> None:
        if not self._is_open() or self._execution_active or self._preset_busy:
            return
        if kind == PRESET_DNA_DISAGREE and self._login and self._user_id <= 0:
            QMessageBox.information(
                self,
                "Sign in again",
                "This scan needs your numeric iNaturalist account number, which "
                "this sign-in did not record. Sign in again from the main window, "
                "then open Identify again.",
            )
            return
        if not self._login:
            QMessageBox.information(
                self,
                "Sign in first",
                "This button looks up your own identifications, so the program "
                "needs to know who you are. Sign in from the main window, then "
                "open Identify again.",
            )
            return
        self._preset_generation += 1
        generation = self._preset_generation
        title = _PRESET_TITLES[kind]
        login, user_id = self._login, self._user_id
        limit = self._limit_spin.value()

        def cancelled() -> bool:
            return not self._is_open() or generation != self._preset_generation

        if kind == PRESET_MAVERICK:
            message = "Finding your maverick identifications…"

            def read() -> object:
                return find_maverick_observation_ids(
                    self._client, login, limit=limit, is_cancelled=cancelled
                )

        else:
            message = (
                "Scanning DNA-barcoded observations you identified "
                "(this can take a minute)…"
            )

            def read() -> object:
                return find_dna_disagreement_ids(
                    self._client, login, user_id, is_cancelled=cancelled
                )

        self._set_preset_busy(True)
        self._count_label.setText(message)

        def success(ids: object) -> None:
            if cancelled():
                return
            self._set_preset_busy(False)
            if not isinstance(ids, list) or not ids:
                self._count_label.setText("Nothing found")
                QMessageBox.information(
                    self, title, "No matching observations were found. Nice work!"
                )
                return
            self._load_url(id_list_url(ids), preset=(kind, title))

        def failure(exc: Exception) -> None:
            if cancelled():
                return
            self._set_preset_busy(False)
            if isinstance(exc, PresetCancelled):
                return
            self._count_label.setText("Lookup failed")
            show_safe_read_failure(
                self, title, exc, lambda: self._run_dynamic_preset(kind)
            )

        self._start_read(title, read, success, failure)

    def _preset_available(self, button: QPushButton) -> bool:
        if button is self._maverick_button:
            return bool(self._login)
        if button is self._dna_button:
            # The DNA scan filters by numeric account id; sign-ins saved by
            # older builds have a login but no id.
            return bool(self._login) and self._user_id > 0
        return True

    def _supersede_pending_preset(self) -> None:
        """A newer selection or edit makes any running preset lookup stale."""
        if self._preset_busy:
            self._preset_generation += 1
            self._set_preset_busy(False)
            self._count_label.setText("Not loaded")

    def _set_preset_busy(self, busy: bool) -> None:
        self._preset_busy = busy
        for button in self._quick_buttons:
            button.setEnabled(not busy and self._preset_available(button))
        for button in self._recent_container.findChildren(QPushButton):
            button.setEnabled(not busy)
        self._update_execute_enabled()

    def _rebuild_recent(self) -> None:
        while self._recent_layout.count():
            item = self._recent_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        history = self._settings.identify_history
        self._recent_heading.setVisible(bool(history))
        self._recent_container.setVisible(bool(history))
        for entry in history:
            kind, url, label = entry["kind"], entry["url"], entry["label"]
            if kind != "url":
                text = _PRESET_TITLES.get(kind, label or kind)
            else:
                text = label or _label_from_url(url)
            row = QWidget(self._recent_container)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(4)
            button = _ElidedButton(text, row)
            button.setProperty("recent", True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(
                f"{text}\n\n{url}"
                if kind == "url"
                else f"{text}\n\nLooks up the list afresh"
            )
            if kind == "url":
                button.clicked.connect(lambda _checked=False, u=url: self._load_url(u))
            elif kind in _PRESET_TITLES:
                button.clicked.connect(
                    lambda _checked=False, k=kind: self._run_dynamic_preset(k)
                )
                button.setEnabled(
                    bool(self._login)
                    and (kind != PRESET_DNA_DISAGREE or self._user_id > 0)
                )
            row_layout.addWidget(button, 1)
            forget = QToolButton(row)
            forget.setText("✕")
            forget.setAutoRaise(True)
            forget.setToolTip("Remove from recent searches")
            forget.clicked.connect(
                lambda _checked=False, k=kind, u=url: self._forget_recent(k, u)
            )
            row_layout.addWidget(forget)
            self._recent_layout.addWidget(row)

    def _forget_recent(self, kind: str, url: str) -> None:
        self._settings.forget_identify_search(kind, url)
        self._settings.sync()
        self._rebuild_recent()

    def _set_details_visible(self, visible: bool) -> None:
        self._details.setVisible(visible)
        self._details_toggle.setArrowType(
            Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow
        )
        self._details_toggle.setText(
            "Hide query details and options"
            if visible
            else "Show query details and options"
        )

    def _current_description(self) -> str:
        records = [
            (self._item_text(row, 0), self._item_text(row, 1), self._item_text(row, 2))
            for row in range(self._table.rowCount())
        ]
        return describe_query(records)

    def _refresh_summary(self) -> None:
        if not self._has_parsed_query:
            return
        if self._active_preset is not None:
            title = self._active_preset[1]
        else:
            title = self._current_description()
        self._summary.setText(f"Selected: <b>{html.escape(title)}</b>")

    def add_parameter(self) -> None:
        if not self._is_open() or self._execution_active:
            return
        self._supersede_pending_preset()
        self._active_preset = None
        self._add_row("", "", "", "User edit")
        self._schedule_count_preview()
        self._refresh_summary()

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

        self._supersede_pending_preset()
        self._parse_generation += 1
        self._count_generation += 1
        self._execution_generation += 1
        self._resolution_generation += 1
        parse_generation = self._parse_generation
        self._source_kind = query.source_kind
        self._display_url = query.display_url
        self._parsed_url_text = self._url_edit.text().strip()
        self._parsed_params = tuple((str(k), str(v)) for k, v in query.params)
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

        self._active_preset = None
        self._start_count_preview(self._record_snapshot(), parse_generation)
        for row in range(self._table.rowCount()):
            self._start_row_resolution(row)
        self._refresh_summary()
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
        if self._active_preset is not None:
            kind, title = self._active_preset
            self._settings.remember_identify_search(kind, "", title)
        else:
            # An edited table no longer matches the link it came from; save a
            # link built from the parameters that run, so replay matches.
            replay_url = (
                self._url_edit.text()
                if params == self._parsed_params
                else query_params_url(params)
            )
            self._settings.remember_identify_search(
                "url", replay_url, self._current_description()
            )
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
        self._supersede_pending_preset()
        self._active_preset = None
        self._schedule_count_preview()
        self._refresh_summary()

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
        self._supersede_pending_preset()
        self._active_preset = None
        self._start_row_resolution(row)
        self._schedule_count_preview()
        self._refresh_summary()

    def _show_table_context_menu(self, position) -> None:
        row = self._table.indexAt(position).row()
        if row < 0 or self._execution_active:
            return
        menu = QMenu(self)
        retry_action = menu.addAction("Retry friendly-name resolution")
        retry_action.triggered.connect(
            lambda: self._start_row_resolution(row, explicit=True)
        )
        menu.exec(self._table.viewport().mapToGlobal(position))

    def _on_url_edited(self, text: str) -> None:
        """A typed URL must never silently reuse an older parsed table."""
        del text
        self._active_preset = None
        if self._has_parsed_query or self._parsed_url_text:
            self._mark_url_stale()

    def _mark_url_stale(self) -> None:
        if not self._is_open() or self._execution_active:
            return
        self._supersede_pending_preset()
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
        self._summary.setText("Press Enter or click Load link to use this link.")
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
        self._update_execute_enabled()

        id_list, _others = split_id_list(params)
        if id_list is not None and len(id_list) > ID_LIST_CHUNK:
            # Too long for one count request. Other filters may exclude some,
            # so this is an upper bound, not a count of matches.
            others = len(_others)
            self._count_label.setText(
                f"Up to {len(id_list):,} observations"
                + (" (other filters not yet applied)" if others else "")
            )
            return

        cache_key = (params, bool(self._api_token))
        cached = _count_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < _COUNT_CACHE_TTL_SECONDS:
            self._count_label.setText(f"{cached[1]} observations (checking…)")
        else:
            self._count_label.setText("Counting…")

        def is_current() -> bool:
            return (
                self._is_callback_current(parse_generation)
                and count_generation == self._count_generation
                and not self._execution_active
            )

        def success(raw: object) -> None:
            if not is_current():
                return
            total = raw.get("total_results") if isinstance(raw, dict) else None
            text = f"{total:,}" if isinstance(total, int) else "Unknown"
            if isinstance(total, int):
                _count_cache[cache_key] = (time.monotonic(), text)
            self._count_label.setText(f"{text} observations")

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
            self._set_resolution_text(row, static_meaning(parameter_name, api_value))
            self._refresh_summary()
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

        to_fetch: list[tuple[str, int]] = []
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
            to_fetch.append(key)
        if entity_kind == "taxon" and len(to_fetch) > 1:
            # One request for every taxon in the row instead of one each.
            self._start_batch_taxon_lookup(to_fetch, subscriber, explicit)
        else:
            for key in to_fetch:
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

    def _start_batch_taxon_lookup(
        self,
        keys: list[tuple[str, int]],
        retry_subscriber: _ResolutionSubscriber,
        explicit: bool,
    ) -> None:
        taxon_ids = [entity_id for _kind, entity_id in keys]

        def success(raw: object) -> None:
            names: dict[int, str] = {}
            results = raw.get("results") if isinstance(raw, dict) else None
            for record in results if isinstance(results, list) else []:
                if isinstance(record, dict):
                    try:
                        taxon_id = int(record.get("id") or 0)
                    except (TypeError, ValueError):
                        continue
                    name = _friendly_name("taxon", {"results": [record]})
                    if taxon_id and name:
                        names[taxon_id] = name
            for key in keys:
                self._complete_name_lookup(key, names.get(key[1]))

        def failure(exc: Exception) -> None:
            for key in keys:
                self._complete_name_lookup(key, None, exc)
            if explicit and self._subscriber_is_current(retry_subscriber):
                show_safe_read_failure(
                    self,
                    "Resolve taxon names",
                    exc,
                    lambda: self._retry_resolution_subscriber(retry_subscriber),
                )

        self._start_read(
            "Resolve taxon names",
            lambda: self._metadata_client.get_taxa_by_ids(taxon_ids),
            success,
            failure,
        )

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
            if name:
                _load_shared_names(self._settings)[key] = name
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
        keys = [
            (subscriber.entity_kind, entity_id) for entity_id in subscriber.entity_ids
        ]
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
        self._refresh_summary()

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
        self._pending_reads[signals] = (success, failure)

        # Connect to bound slots on this QDialog (a GUI-thread QObject), not
        # nested plain functions -- Qt's auto connection type is decided by
        # the receiver's thread affinity, which it can only detect for a
        # QObject method. A plain closure receiver would run on the worker
        # thread that emits the signal, touching widgets off the GUI thread.
        signals.result.connect(self._on_read_result)
        signals.error.connect(self._on_read_error)
        QThreadPool.globalInstance().start(worker)

    @Slot(object)
    def _on_read_result(self, value: object) -> None:
        signals = self.sender()
        pending = self._pending_reads.pop(signals, None)
        self._live_signals.discard(signals)
        if pending is None:
            return
        success, _failure = pending
        success(value)

    @Slot(object)
    def _on_read_error(self, exc: Exception) -> None:
        signals = self.sender()
        pending = self._pending_reads.pop(signals, None)
        self._live_signals.discard(signals)
        if pending is None:
            return
        _success, failure = pending
        failure(exc)

    def _set_query_controls_enabled(self, enabled: bool) -> None:
        for button in self._quick_buttons:
            button.setEnabled(enabled and self._preset_available(button))
        self._recent_container.setEnabled(enabled)
        self._change_area_button.setEnabled(enabled)
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
            and not self._preset_busy
            and not (requires_auth and not self._api_token)
        )
        self._execute_button.setEnabled(enabled)
        if requires_auth and not self._api_token:
            self._execute_button.setToolTip(
                "Authentication is required for reviewed filtering."
            )
        else:
            self._execute_button.setToolTip("")

    def _is_open(self) -> bool:
        return self._dialog_state == "open"

    def _is_callback_current(self, parse_generation: int) -> bool:
        return self._is_open() and parse_generation == self._parse_generation

    def _invalidate_callbacks(self, state: str) -> None:
        _save_shared_names(self._settings)
        self._settings.sync()
        self._dialog_state = state
        self._parse_generation += 1
        self._count_generation += 1
        self._execution_generation += 1
        self._resolution_generation += 1
        self._execution_active = False


def _resolution_target(
    parameter_name: str, api_value: str
) -> tuple[str | None, tuple[int, ...]]:
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


def _label_from_url(url: str) -> str:
    """Describe a saved URL using only names already in the shared cache."""
    try:
        query = parse_observations_url(url)
    except ObservationURLParseError:
        query = None
    if query is None:
        return url
    names = _shared_names or {}
    records = []
    for name, value in query.params:
        kind, ids = _resolution_target(name, value)
        meaning = ""
        if kind is not None and ids:
            resolved = [names.get((kind, entity_id)) for entity_id in ids]
            if all(resolved):
                meaning = ", ".join(n for n in resolved if n)
        records.append((name, value, meaning))
    return describe_query(records)


class _PlaceSearchSignals(QObject):
    done = Signal(int, object)


class _PlaceSearchWorker(QRunnable):
    def __init__(self, client: INatClient, query: str, generation: int) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._client = client
        self._query = query
        self._generation = generation
        self.signals = _PlaceSearchSignals()

    def run(self) -> None:
        try:
            raw = self._client.get_places_autocomplete(self._query, per_page=10)
        except Exception as exc:  # reported in the dialog, never raised
            log.debug("Place search failed: %s", type(exc).__name__)
            raw = None
        self.signals.done.emit(self._generation, raw)


class _PlacePickerDialog(QDialog):
    """Pick the iNaturalist place used by the "near me" Identify buttons."""

    def __init__(
        self,
        client: INatClient,
        *,
        suggestion: tuple[int | None, str] = (None, ""),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose your area")
        self.resize(480, 420)
        self._client = client
        self._generation = 0
        self._live_signals: set[_PlaceSearchSignals] = set()
        self.place: tuple[int, str] | None = None

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Type the name of a county, state, park, or country. The fungi buttons "
            "will show observations from this place. You can change it any time.",
            self,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("e.g. Mendocino County")
        self._search.textEdited.connect(self._schedule_search)
        layout.addWidget(self._search)
        self._results = QListWidget(self)
        self._results.itemSelectionChanged.connect(self._selection_changed)
        self._results.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self._results, 1)
        self._status = QLabel("", self)
        layout.addWidget(self._status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok,
            self,
        )
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText("Use this place")
        self._ok.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._start_search)

        place_id, name = suggestion
        if place_id and name:
            item = QListWidgetItem(f"{name}  (from the main window's Place filter)")
            item.setData(Qt.ItemDataRole.UserRole, (int(place_id), name))
            self._results.addItem(item)
            self._results.setCurrentItem(item)

    def _schedule_search(self, _text: str) -> None:
        # Any edit makes in-flight results stale, even if no new search starts.
        self._generation += 1
        self._results.clear()
        self.place = None
        self._ok.setEnabled(False)
        self._status.setText("")
        self._timer.start()

    def _start_search(self) -> None:
        query = self._search.text().strip()
        if len(query) < 2:
            return
        self._generation += 1
        worker = _PlaceSearchWorker(self._client, query, self._generation)
        self._live_signals.add(worker.signals)
        worker.signals.done.connect(self._on_results)
        self._status.setText("Searching…")
        QThreadPool.globalInstance().start(worker)

    @Slot(int, object)
    def _on_results(self, generation: int, raw: object) -> None:
        self._live_signals.discard(self.sender())
        if generation != self._generation:
            return
        results = raw.get("results") if isinstance(raw, dict) else None
        if not isinstance(results, list):
            self._status.setText("Search failed; check your connection and try again.")
            return
        self._results.clear()
        for record in results:
            if not isinstance(record, dict):
                continue
            try:
                place_id = int(record.get("id") or 0)
            except (TypeError, ValueError):
                continue
            name = str(record.get("display_name") or record.get("name") or "")
            if place_id and name:
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, (place_id, name))
                self._results.addItem(item)
        self._status.setText("" if self._results.count() else "No places found.")
        if self._results.count():
            self._results.setCurrentRow(0)

    def _selection_changed(self) -> None:
        item = self._results.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        self.place = tuple(data) if data else None  # type: ignore[assignment]
        self._ok.setEnabled(self.place is not None)


class _ElidedButton(QPushButton):
    """A button whose long label is shortened with "…" to fit its width."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(80)
        self.setText(text)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        available = max(20, self.width() - 28)
        self.setText(
            self.fontMetrics().elidedText(
                self._full_text, Qt.TextElideMode.ElideRight, available
            )
        )
