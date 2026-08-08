"""Separate, read-only local iNaturalist Identify window."""

from __future__ import annotations

import html
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from PySide6.QtCore import QEvent, QObject, QRunnable, QSize, QThreadPool, Qt, Signal
from PySide6.QtGui import QAction, QFont, QIcon, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStyle,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.client import INatClient
from observation_workbench.api.parsers import parse_observation
from observation_workbench.models import StudyTaxon
from observation_workbench.services.identify_session import IdentifySession
from observation_workbench.services.image_cache import ImageCache
from observation_workbench.services.identify_actions import (
    IdentifyActionManager,
    ObservationUUIDResolution,
)
from observation_workbench.services.identify_action_presentation import (
    compact_observation_action_text,
    describe_cancelled_conflicting_actions,
    describe_cancelled_opposite_actions,
    present_identify_action,
)
from observation_workbench.services.prefetcher import (
    ImageFailure,
    ImageLoadDiagnostics,
    ImagePrefetcher,
    ImageRequestMode,
)
from observation_workbench.storage.settings import AppSettings
from observation_workbench.ui.identify_add_id_dialog import IdentifyAddIDDialog
from observation_workbench.ui.identify_captive_dialog import IdentifyCaptiveDialog
from observation_workbench.ui.identify_comment_dialog import IdentifyCommentDialog
from observation_workbench.ui.identify_favorite_dialog import IdentifyFavoriteDialog
from observation_workbench.ui.identify_info_tab import IdentifyInfoTab
from observation_workbench.ui.identify_photo_panel import IdentifyPhotoPanel
from observation_workbench.ui.identify_read_error import (
    format_read_failure,
    show_safe_read_failure,
)

log = logging.getLogger(__name__)

# Journal states in which an optimistic Add ID has definitively not been
# applied (or cannot be confirmed and will not be retried automatically), so
# the optimistic header entry is reverted and the user is notified.  The two
# cancellation states belong here as well: a cancelled row will never be
# dispatched and never produces a confirmed refresh, so without them the
# header would keep advertising a taxon the user explicitly abandoned.
_OPTIMISTIC_REVERT_STATES = frozenset(
    {
        "failed_terminal",
        "failed_retryable",
        "ambiguous",
        "cancelled",
        "tracking_cancelled",
    }
)

_DETAIL_RETRY_COOLDOWN_SECONDS = 45.0
_MISSING_DETAIL_RETRY_COOLDOWN_SECONDS = 90.0
_MAX_AUTOMATIC_DETAIL_ATTEMPTS = 2


class _DetailsSignals(QObject):
    loaded = Signal(object)
    failed = Signal(object)


class _DetailsWorker(QRunnable):
    def __init__(
        self, client: INatClient, observation_ids: tuple[int, ...], token: str
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._client = client
        self._observation_ids = observation_ids
        self._token = token
        self.signals = _DetailsSignals()

    def run(self) -> None:
        try:
            self.signals.loaded.emit(
                self._client.get_observations_by_ids(self._observation_ids, self._token)
            )
        except Exception as exc:
            self.signals.failed.emit(exc)


class _CurrentUserSignals(QObject):
    loaded = Signal(int)
    failed = Signal(object)


class _CurrentUserWorker(QRunnable):
    """Resolve the authenticated account's numeric ID for reviewed-state filtering."""

    def __init__(self, client: INatClient, token: str) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._client = client
        self._token = token
        self.signals = _CurrentUserSignals()

    def run(self) -> None:
        try:
            user_id = _extract_current_user_id(
                self._client.get_current_user(self._token)
            )
            if user_id is None:
                raise ValueError("Current user response had no numeric id")
            self.signals.loaded.emit(user_id)
        except Exception as exc:
            self.signals.failed.emit(exc)


@dataclass
class _DetailState:
    status: str = "not_requested"
    last_attempt: float = 0.0
    last_attempt_at: str = ""
    retry_after: float = 0.0
    automatic_attempts: int = 0
    diagnostic: str = ""


@dataclass(frozen=True)
class _DetailRequest:
    generation: int
    authentication_generation: int
    requested_ids: frozenset[int]
    observation_versions: tuple[tuple[int, int], ...]
    explicit: bool
    operation: str

    def version_for(self, observation_id: int) -> int:
        for candidate_id, version in self.observation_versions:
            if candidate_id == observation_id:
                return version
        return -1


@dataclass(frozen=True)
class _AgreeOperation:
    """Immutable Agree intent owned by one Identify window.

    UUID resolution may finish after navigation, another Agree attempt, or an
    authentication transition.  Every value needed for the journal payload is
    consequently captured here before the asynchronous safe read begins.
    """

    generation: int
    authentication_generation: int
    observation_id: int
    observation_uuid: str
    taxon_id: int
    taxon_display_name: str
    account_login: str


@dataclass
class _OptimisticTaxon:
    """A locally-suggested identification reflected in the header before the
    centralized confirmed refresh round-trip lands.

    ``action_id`` ties the optimistic taxon to the exact journaled Add ID
    action so that a later *failure* of that action reverts the header and
    notifies the user, while a *success* is superseded silently by the real
    confirmed refresh (or, transiently, marked as saved).
    """

    action_id: int
    taxon: StudyTaxon
    confirmed: bool = False


@dataclass(frozen=True)
class _ReviewedOperation:
    """Immutable Reviewed intent owned by one Identify window.

    Reviewed is one-way (``desired_state=True``) and carries no taxon.  Every
    value the later journal call needs is captured here before the safe UUID
    read begins, exactly as :class:`_AgreeOperation` does for Agree.
    """

    generation: int
    authentication_generation: int
    observation_id: int
    observation_uuid: str
    account_login: str


class _IdentifyShortcutFilter(QObject):
    """Route read-only Identify keys before focused child widgets consume them."""

    def __init__(self, window: "IdentifyWindow") -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        del watched
        if event.type() not in {QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress}:
            return False
        if QApplication.activeWindow() is not self._window:
            return False
        if self._window._shortcut_should_be_suppressed():
            return False
        key_event = event
        if not isinstance(key_event, QKeyEvent):
            return False
        if not self._window._handle_identify_shortcut(
            key_event,
            execute=event.type() == QEvent.Type.KeyPress,
        ):
            return False
        event.accept()
        return True


class IdentifyWindow(QMainWindow):
    """One fixed, read-only queue with identity-safe image/detail enrichment."""

    pending_actions_requested = Signal()

    def __init__(
        self,
        session: IdentifySession,
        settings: AppSettings,
        client: INatClient,
        cache: ImageCache,
        read_auth_provider: Callable[[], str],
        parent: QWidget | None = None,
        action_manager: IdentifyActionManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._session = session
        self._settings = settings
        self._client = client
        self._read_auth_provider = read_auth_provider
        self._action_manager = action_manager
        self._closed = False
        self._generation = 0
        self._detail_authentication_generation = 0
        self._identify_authentication_generation = 0
        self._observation_index = 0
        self._photo_index = 0
        self._selected_photo_id: int | None = None
        self._brightness = 0
        self._navigation_direction = 0
        self._detail_states: dict[int, _DetailState] = {
            item.observation_id: _DetailState() for item in session.items
        }
        self._observation_refresh_versions: dict[int, int] = {
            item.observation_id: 0 for item in session.items
        }
        self._detail_live_signals: set[_DetailsSignals] = set()
        self._detail_requests: dict[_DetailsSignals, _DetailRequest] = {}
        self._recent_failures: deque[tuple[str, str]] = deque(maxlen=30)
        self._background_image_failure_ids: set[int] = set()
        self._active_image_photo_ids: frozenset[int] = frozenset()
        self._thumbnail_buttons: dict[int, QPushButton] = {}
        self._thumbnail_photo_ids: tuple[int, ...] = ()
        self._optimistic_taxa: dict[int, _OptimisticTaxon] = {}
        self._add_id_dialog: IdentifyAddIDDialog | None = None
        self._comment_dialog: IdentifyCommentDialog | None = None
        self._favorite_dialog: IdentifyFavoriteDialog | None = None
        self._captive_dialog: IdentifyCaptiveDialog | None = None
        self._agree_operation_generation = 0
        self._agree_operation: _AgreeOperation | None = None
        self._agree_resolution_request_id: int | None = None
        self._reviewed_operation_generation = 0
        self._reviewed_operation: _ReviewedOperation | None = None
        self._reviewed_resolution_request_id: int | None = None
        self._current_user_id: int | None = None
        self._current_user_live_signals: set[_CurrentUserSignals] = set()

        self.setWindowTitle("iNaturalist Identify")
        self._prefetch = ImagePrefetcher(
            client,
            cache,
            settings.memory_cache_max_mb * 1024 * 1024,
            self,
        )
        self._prefetch.set_observations(session.observations)
        self._prefetch.image_ready_detailed.connect(self._image_ready)
        self._prefetch.image_failed_detailed.connect(self._image_failed)
        self._prefetch.image_diagnostics_changed.connect(
            self._image_diagnostics_changed
        )
        self._prefetch.request_state_changed.connect(self._image_request_state_changed)

        self._shortcut_filter = _IdentifyShortcutFilter(self)
        QApplication.instance().installEventFilter(self._shortcut_filter)
        self.destroyed.connect(lambda *_args: self._remove_shortcut_filter())

        self._build()
        if self._action_manager is not None:
            self._action_manager.action_changed.connect(self._action_manager_changed)
            self._action_manager.authentication_context_changed.connect(
                self._action_manager_authentication_changed
            )
            self._action_manager.observation_uuid_resolved.connect(
                self._agree_uuid_resolved
            )
            self._action_manager.observation_uuid_resolved.connect(
                self._reviewed_uuid_resolved
            )
        self._restore_window_state()
        self._observation_index = self._initial_observation_index()
        self._show_current_observation()
        self._start_current_user_lookup()

    @property
    def should_open_maximized(self) -> bool:
        """MainWindow uses this before showing this top-level window once."""
        return not _has_saved_value(self._settings.identify_window_geometry)

    def _build(self) -> None:
        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        image_side = QWidget(self._splitter)
        image_layout = QVBoxLayout(image_side)
        image_layout.setContentsMargins(3, 3, 3, 3)

        header_row = QHBoxLayout()
        self._header = QLabel(self)
        self._header.setWordWrap(True)
        self._header.setTextFormat(Qt.TextFormat.RichText)
        header_font = QFont(self._header.font())
        header_font.setPointSize(header_font.pointSize() + 7)
        header_font.setBold(True)
        self._header.setFont(header_font)
        header_row.addWidget(self._header, 1)
        self._show_reviewed_checkbox = QCheckBox("Show reviewed", self)
        self._show_reviewed_checkbox.setChecked(self._settings.identify_show_reviewed)
        self._show_reviewed_checkbox.toggled.connect(self._on_show_reviewed_toggled)
        header_row.addWidget(self._show_reviewed_checkbox, 0, Qt.AlignmentFlag.AlignTop)
        image_layout.addLayout(header_row)

        self._pending_actions_panel = QWidget(self)
        pending_layout = QHBoxLayout(self._pending_actions_panel)
        pending_layout.setContentsMargins(4, 2, 4, 2)
        self._pending_actions_label = QLabel(self._pending_actions_panel)
        self._pending_actions_label.setWordWrap(True)
        self._pending_actions_label.setTextFormat(Qt.TextFormat.PlainText)
        pending_layout.addWidget(self._pending_actions_label, 1)
        self._view_pending_actions_button = QPushButton(
            "View pending actions…", self._pending_actions_panel
        )
        self._view_pending_actions_button.clicked.connect(
            self.pending_actions_requested.emit
        )
        pending_layout.addWidget(self._view_pending_actions_button)
        self._pending_actions_panel.hide()
        image_layout.addWidget(self._pending_actions_panel)

        self._photo_panel = IdentifyPhotoPanel(self)
        self._photo_panel.retry_requested.connect(self._retry_image)
        self._photo_panel.details_requested.connect(self._show_image_details)
        image_layout.addWidget(self._photo_panel, 1)

        self._thumbnail_scroll = QScrollArea(self)
        self._thumbnail_scroll.setWidgetResizable(False)
        self._thumbnail_scroll.setFixedHeight(82)
        self._thumbnail_body = QWidget(self._thumbnail_scroll)
        self._thumbnail_layout = QHBoxLayout(self._thumbnail_body)
        self._thumbnail_layout.setContentsMargins(2, 2, 2, 2)
        self._thumbnail_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._thumbnail_scroll.setWidget(self._thumbnail_body)
        image_layout.addWidget(self._thumbnail_scroll)

        captive_controls = QHBoxLayout()
        self._captive_button = QPushButton("Captive/Cultivated", self)
        self._captive_button.clicked.connect(self._open_captive)
        captive_controls.addWidget(self._captive_button)
        captive_controls.addStretch()
        image_layout.addLayout(captive_controls)

        self._splitter.addWidget(image_side)
        self._tabs = QTabWidget(self._splitter)
        current_login = ""
        if self._action_manager is not None:
            snapshot = self._action_manager.current_authentication()
            current_login = snapshot.login if snapshot.authenticated else ""
        self._info_tab = IdentifyInfoTab(current_login, self._tabs)
        self._tabs.addTab(self._info_tab, "Info")
        for title in ("Suggestions", "Annotations", "Data Quality"):
            placeholder = QWidget(self._tabs)
            placeholder_layout = QVBoxLayout(placeholder)
            placeholder_layout.addWidget(
                QLabel("Not implemented in this gate.", placeholder)
            )
            self._tabs.addTab(placeholder, title)
        self._refresh_button = QToolButton(self._tabs)
        self._refresh_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self._refresh_button.setAutoRaise(True)
        self._refresh_button.setToolTip(
            "Refresh this observation from iNaturalist (picks up changes made in a web browser)"
        )
        self._refresh_button.clicked.connect(self._refresh_current_observation)
        self._tabs.setCornerWidget(self._refresh_button, Qt.Corner.TopRightCorner)
        self._splitter.addWidget(self._tabs)
        self._splitter.setSizes([900, 300])
        self.setCentralWidget(self._splitter)

        toolbar = QToolBar(self)
        toolbar.setObjectName("identify_main_toolbar")
        self.addToolBar(toolbar)
        self._add_id_action = QAction("Add ID", self)
        self._add_id_action.setToolTip("Add a normal identification (I)")
        self._add_id_action.triggered.connect(self._open_add_id)
        toolbar.addAction(self._add_id_action)
        self._agree_action = QAction("Agree", self)
        self._agree_action.setToolTip("Agree with the observation taxon (A)")
        self._agree_action.triggered.connect(self._start_agree)
        toolbar.addAction(self._agree_action)
        self._comment_action = QAction("Comment", self)
        self._comment_action.setToolTip("Add a comment (C)")
        self._comment_action.triggered.connect(self._open_comment)
        toolbar.addAction(self._comment_action)
        self._reviewed_action = QAction("Reviewed", self)
        self._reviewed_action.triggered.connect(self._mark_reviewed)
        toolbar.addAction(self._reviewed_action)
        self._favorite_action = QAction("Favorite", self)
        self._favorite_action.triggered.connect(self._open_favorite)
        toolbar.addAction(self._favorite_action)
        self._refresh_action_entry_availability()

        self._network_label = QLabel("Network: normal", self)
        self._network_details_button = QPushButton("Details…", self)
        self._network_details_button.clicked.connect(self._show_network_details)
        self._detail_retry_button = QPushButton("Retry details", self)
        self._detail_retry_button.clicked.connect(self._retry_current_details)
        self._detail_retry_button.hide()
        self.statusBar().addPermanentWidget(self._network_label)
        self.statusBar().addPermanentWidget(self._detail_retry_button)
        self.statusBar().addPermanentWidget(self._network_details_button)

    def _restore_window_state(self) -> None:
        geometry = self._settings.identify_window_geometry
        window_state = self._settings.identify_window_state
        splitter_state = self._settings.identify_splitter_state
        if _has_saved_value(geometry):
            self.restoreGeometry(geometry)
        if _has_saved_value(window_state):
            self.restoreState(window_state)
        if _has_saved_value(splitter_state):
            self._splitter.restoreState(splitter_state)
        active_tab = max(
            0, min(self._settings.identify_active_tab, self._tabs.count() - 1)
        )
        self._tabs.setCurrentIndex(active_tab)

    def _show_current_observation(self, direction: int = 0) -> None:
        if self._closed or not self._session.items:
            return
        self._navigation_direction = direction
        self._observation_index = max(
            0,
            min(self._observation_index, len(self._session.items) - 1),
        )
        observation = self._current_observation()
        self._photo_index = _clamp_photo_index(self._photo_index, observation.photos)
        self._selected_photo_id = (
            observation.photos[self._photo_index].photo_id
            if observation.photos
            else None
        )
        self._brightness = 0
        self._update_header()
        self._info_tab.set_observation(observation)
        self._refresh_action_entry_availability()
        self._update_pending_action_status()
        self._ensure_thumbnail_strip(observation)
        self._update_thumbnail_selection(scroll_selected=True)
        self._display_selected_photo(preserve_view=False)
        self._prefetch.prefetch_identify_position(
            self._observation_index,
            self._photo_index,
            self._session.plan.prefetch_radius,
            direction=direction,
        )
        self._update_selected_loading_state()
        self._load_nearby_details()
        self._derive_network_status()

    def _display_selected_photo(self, *, preserve_view: bool) -> None:
        observation = self._current_observation()
        if not observation.photos or self._selected_photo_id is None:
            self._photo_panel.show_no_photo()
            return
        photo = self._current_photo()
        if photo is None:
            self._photo_panel.show_no_photo()
            return
        self._photo_panel.begin_photo(photo.photo_id)
        cached = self._prefetch.get_best_cached(photo.photo_id)
        if cached is not None:
            pixmap, size = cached
            self._photo_panel.set_photo(
                photo.photo_id,
                pixmap,
                size,
                preserve_view=preserve_view,
            )
        failure = self._prefetch.failure_for_photo(photo.photo_id)
        if failure is not None:
            self._photo_panel.show_failure(photo.photo_id, failure)
        elif cached is None:
            self._photo_panel.show_loading(photo.photo_id)
        diagnostics = self._prefetch.diagnostics_for_photo(photo.photo_id)
        if diagnostics is not None and diagnostics.partial_failures:
            self._photo_panel.show_partial_details(photo.photo_id)

    def _update_selected_loading_state(self) -> None:
        photo = self._current_photo()
        if photo is not None and self._prefetch.is_request_in_flight(photo.photo_id):
            if (
                self._prefetch.active_request_mode(photo.photo_id)
                == ImageRequestMode.EXPLICIT_RETRY
            ):
                self._photo_panel.show_retrying(photo.photo_id)
            else:
                self._photo_panel.show_loading(photo.photo_id)

    def _update_header(self) -> None:
        observation = self._current_observation()
        optimistic = self._optimistic_taxa.get(observation.obs_id)
        if optimistic is not None:
            taxon = optimistic.taxon.display_name or "Unknown"
            marker = " · <i>saved</i>" if optimistic.confirmed else " · <i>saving…</i>"
        else:
            taxon = (
                observation.display_taxon.display_name
                if observation.display_taxon
                else "Unknown"
            )
            marker = ""
        self._header.setText(
            f"{html.escape(taxon)}{marker} · {_quality_grade_html(observation.quality_grade)} · "
            f"{self._observation_index + 1} / {len(self._session.items)} · #{observation.obs_id}"
        )

    def _ensure_thumbnail_strip(self, observation) -> None:
        photo_ids = tuple(photo.photo_id for photo in observation.photos)
        if photo_ids == self._thumbnail_photo_ids:
            return
        horizontal_scroll = self._thumbnail_scroll.horizontalScrollBar().value()
        while self._thumbnail_layout.count():
            item = self._thumbnail_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._thumbnail_buttons.clear()
        self._thumbnail_photo_ids = photo_ids
        for photo_index, photo in enumerate(observation.photos):
            button = QPushButton(str(photo_index + 1), self._thumbnail_body)
            button.setFixedSize(70, 70)
            button.setIconSize(QSize(64, 64))
            button.setToolTip(f"Photo {photo_index + 1} of {len(observation.photos)}")
            button.clicked.connect(
                lambda _checked=False, photo_id=photo.photo_id: self._select_photo_id(
                    photo_id
                )
            )
            self._thumbnail_buttons[photo.photo_id] = button
            cached = self._prefetch.get_best_cached(photo.photo_id)
            if cached is not None:
                self._set_thumbnail_pixmap(photo.photo_id, cached[0])
            self._thumbnail_layout.addWidget(button)
        self._thumbnail_body.adjustSize()
        self._thumbnail_body.setMinimumWidth(self._thumbnail_body.sizeHint().width())
        self._thumbnail_scroll.horizontalScrollBar().setValue(horizontal_scroll)

    def _update_thumbnail_selection(self, *, scroll_selected: bool = False) -> None:
        for photo_id, button in self._thumbnail_buttons.items():
            if photo_id == self._selected_photo_id:
                button.setStyleSheet("border: 2px solid #4a90e2")
                if scroll_selected:
                    self._thumbnail_scroll.ensureWidgetVisible(button, 8, 0)
            else:
                button.setStyleSheet("")

    def _set_thumbnail_pixmap(self, photo_id: int, pixmap: QPixmap) -> None:
        button = self._thumbnail_buttons.get(photo_id)
        if button is None or pixmap.isNull():
            return
        icon_pixmap = pixmap.scaled(
            64,
            64,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        button.setIcon(QIcon(icon_pixmap))
        button.setIconSize(icon_pixmap.size())

    def _select_photo_id(self, photo_id: int) -> None:
        observation = self._current_observation()
        for index, photo in enumerate(observation.photos):
            if photo.photo_id == photo_id:
                self._photo_index = index
                self._selected_photo_id = photo_id
                self._brightness = 0
                self._display_selected_photo(preserve_view=False)
                self._prefetch.prefetch_identify_position(
                    self._observation_index,
                    self._photo_index,
                    self._session.plan.prefetch_radius,
                    direction=self._navigation_direction,
                )
                self._update_selected_loading_state()
                self._update_thumbnail_selection(scroll_selected=True)
                self._derive_network_status()
                return

    def move_observation(self, delta: int) -> None:
        if not self._session.items or delta == 0:
            return
        step = 1 if delta > 0 else -1
        next_index = self._observation_index
        for _ in range(abs(delta)):
            candidate = self._next_visible_index(next_index, step)
            if candidate is None:
                break
            next_index = candidate
        if next_index == self._observation_index:
            return
        self._observation_index = next_index
        self._photo_index = 0
        self._selected_photo_id = None
        self._show_current_observation(direction=step)

    def _is_observation_reviewed(self, observation) -> bool:
        return (
            self._current_user_id is not None
            and self._current_user_id in observation.reviewed_by
        )

    def _is_index_visible(self, index: int) -> bool:
        if self._show_reviewed_checkbox.isChecked():
            return True
        return not self._is_observation_reviewed(self._session.items[index].observation)

    def _next_visible_index(self, start: int, step: int) -> int | None:
        index = start + step
        while 0 <= index < len(self._session.items):
            if self._is_index_visible(index):
                return index
            index += step
        return None

    def _initial_observation_index(self) -> int:
        if not self._session.items:
            return 0
        if self._is_index_visible(0):
            return 0
        # If nothing from index 1 onward is visible either, every item in the
        # session is reviewed; fall back to showing the first one anyway.
        candidate = self._next_visible_index(0, 1)
        return candidate if candidate is not None else 0

    def _on_show_reviewed_toggled(self, checked: bool) -> None:
        self._settings.identify_show_reviewed = checked
        self._settings.sync()
        self._update_reviewed_availability()
        if (
            checked
            or not self._session.items
            or self._is_index_visible(self._observation_index)
        ):
            return
        candidate = self._next_visible_index(self._observation_index, 1)
        if candidate is None:
            candidate = self._next_visible_index(self._observation_index, -1)
        if candidate is None:
            return
        self._observation_index = candidate
        self._photo_index = 0
        self._selected_photo_id = None
        self._show_current_observation()

    def _start_current_user_lookup(self) -> None:
        token = self._current_detail_read_token()
        if not token:
            return
        authentication_generation = self._identify_authentication_generation
        worker = _CurrentUserWorker(self._client, token)
        signals = worker.signals
        self._current_user_live_signals.add(signals)

        def is_current() -> bool:
            return (
                not self._closed
                and authentication_generation
                == self._identify_authentication_generation
                and token == self._current_detail_read_token()
            )

        def loaded(user_id: int) -> None:
            self._current_user_live_signals.discard(signals)
            if is_current():
                self._current_user_id_resolved(user_id)

        def failed(exc: object) -> None:
            self._current_user_live_signals.discard(signals)
            if is_current():
                log.debug("Identify current-user lookup failed: %s", exc)

        signals.loaded.connect(loaded)
        signals.failed.connect(failed)
        QThreadPool.globalInstance().start(worker)

    def _current_user_id_resolved(self, user_id: int) -> None:
        if self._closed:
            return
        self._current_user_id = user_id
        if not self._session.items or self._is_index_visible(self._observation_index):
            return
        candidate = self._next_visible_index(self._observation_index, 1)
        if candidate is None:
            candidate = self._next_visible_index(self._observation_index, -1)
        if candidate is None:
            return
        self._observation_index = candidate
        self._photo_index = 0
        self._selected_photo_id = None
        self._show_current_observation()

    def move_photo(self, delta: int) -> None:
        observation = self._current_observation()
        if not observation.photos:
            return
        next_index = (self._photo_index + delta) % len(observation.photos)
        self._navigation_direction = 1 if delta > 0 else -1
        self._select_photo_id(observation.photos[next_index].photo_id)

    def _image_ready(
        self,
        observation_index: int,
        photo_index: int,
        observation_id: int,
        photo_id: int,
        size: str,
        pixmap: QPixmap,
    ) -> None:
        del observation_index, photo_index
        if self._closed or observation_id != self._current_observation().obs_id:
            return
        self._background_image_failure_ids.discard(photo_id)
        self._set_thumbnail_pixmap(photo_id, pixmap)
        if photo_id != self._selected_photo_id:
            self._derive_network_status()
            return
        self._photo_panel.set_photo(
            photo_id,
            pixmap,
            size,
            preserve_view=self._photo_panel.current_photo_id == photo_id
            and self._photo_panel.has_pixmap,
        )
        diagnostics = self._prefetch.diagnostics_for_photo(photo_id)
        if diagnostics is not None and diagnostics.partial_failures:
            self._photo_panel.show_partial_details(photo_id)
        self._derive_network_status()

    def _image_failed(self, failure: ImageFailure) -> None:
        if self._closed:
            return
        self._recent_failures.append(("Image", _format_image_failure(failure)))
        if failure.photo_id == self._selected_photo_id:
            self._photo_panel.show_failure(failure.photo_id, failure)
        else:
            self._background_image_failure_ids.add(failure.photo_id)
        self._derive_network_status()

    def _retry_image(self) -> None:
        photo = self._current_photo()
        observation = self._current_observation()
        if photo is None or not self._prefetch.can_explicit_retry(photo.photo_id):
            return
        started = self._prefetch.request_photo(
            photo,
            observation.obs_id,
            self._observation_index,
            self._photo_index,
            priority=120,
            request_mode=ImageRequestMode.EXPLICIT_RETRY,
        )
        if started:
            self._photo_panel.show_retrying(photo.photo_id)
        self._derive_network_status()

    def _show_image_details(self) -> None:
        photo = self._current_photo()
        if photo is None:
            return
        diagnostics = self._prefetch.diagnostics_for_photo(photo.photo_id)
        text = _format_image_diagnostics(diagnostics)
        QMessageBox.information(self, "Image details", text)

    def _image_request_state_changed(
        self, active_count: int, active_photo_ids: object
    ) -> None:
        del active_count
        if self._closed:
            return
        self._active_image_photo_ids = (
            frozenset(
                photo_id for photo_id in active_photo_ids if isinstance(photo_id, int)
            )
            if isinstance(active_photo_ids, tuple)
            else frozenset()
        )
        self._update_selected_loading_state()
        self._derive_network_status()

    def _image_diagnostics_changed(self, photo_id: int) -> None:
        if self._closed or photo_id != self._selected_photo_id:
            return
        diagnostics = self._prefetch.diagnostics_for_photo(photo_id)
        if diagnostics is not None and diagnostics.partial_failures:
            self._photo_panel.show_partial_details(photo_id)
        self._derive_network_status()

    def _load_nearby_details(self) -> None:
        if not self._session.items:
            return
        start = max(0, self._observation_index - self._session.plan.prefetch_radius)
        end = min(
            len(self._session.items),
            self._observation_index + self._session.plan.prefetch_radius + 1,
        )
        now = time.monotonic()
        ids = [
            self._session.items[index].observation_id
            for index in range(start, end)
            if self._detail_is_eligible(self._session.items[index].observation_id, now)
        ]
        if ids:
            self._start_detail_request(
                ids, explicit=False, operation="Load Identify observation details"
            )

    def _detail_is_eligible(self, observation_id: int, now: float) -> bool:
        state = self._detail_states.setdefault(observation_id, _DetailState())
        if state.status == "not_requested":
            return True
        if state.status in {"complete", "in_flight"}:
            return False
        return (
            state.automatic_attempts < _MAX_AUTOMATIC_DETAIL_ATTEMPTS
            and now >= state.retry_after
        )

    def _start_detail_request(
        self,
        observation_ids: list[int],
        *,
        explicit: bool,
        operation: str,
    ) -> None:
        if self._closed:
            return
        requested_ids = frozenset(
            observation_id
            for observation_id in observation_ids
            if self._detail_states.setdefault(observation_id, _DetailState()).status
            != "in_flight"
        )
        if not requested_ids:
            return
        token = self._current_detail_read_token()
        now = time.monotonic()
        for observation_id in requested_ids:
            state = self._detail_states[observation_id]
            state.status = "in_flight"
            state.last_attempt = now
            state.last_attempt_at = datetime.now(timezone.utc).isoformat()
            state.diagnostic = ""
            if not explicit:
                state.automatic_attempts += 1

        request = _DetailRequest(
            generation=self._generation,
            authentication_generation=self._detail_authentication_generation,
            requested_ids=requested_ids,
            observation_versions=tuple(
                (
                    observation_id,
                    self._observation_refresh_versions.get(observation_id, 0),
                )
                for observation_id in sorted(requested_ids)
            ),
            explicit=explicit,
            operation=operation,
        )
        worker = _DetailsWorker(self._client, tuple(sorted(requested_ids)), token)
        signals = worker.signals
        self._detail_live_signals.add(signals)
        self._detail_requests[signals] = request

        def loaded(raw: object) -> None:
            self._detail_live_signals.discard(signals)
            self._detail_requests.pop(signals, None)
            self._details_loaded(request, raw)

        def failed(exc: Exception) -> None:
            self._detail_live_signals.discard(signals)
            self._detail_requests.pop(signals, None)
            self._details_failed(request, exc)

        signals.loaded.connect(loaded)
        signals.failed.connect(failed)
        QThreadPool.globalInstance().start(worker)
        self._derive_network_status()

    def _details_loaded(self, request: _DetailRequest, raw: object) -> None:
        if not self._is_current_detail_request(request):
            return
        records = raw.get("results") if isinstance(raw, dict) else []
        returned_ids: set[int] = set()
        current_id = self._current_observation().obs_id
        previous_photo_id = self._selected_photo_id

        for record in records or []:
            observation = (
                parse_observation(record) if isinstance(record, dict) else None
            )
            if observation is None or observation.obs_id not in request.requested_ids:
                continue
            if request.version_for(
                observation.obs_id
            ) != self._observation_refresh_versions.get(
                observation.obs_id,
                0,
            ):
                continue
            if not self._session.replace(observation):
                continue
            returned_ids.add(observation.obs_id)
            self._prefetch.replace_observation(observation)
            state = self._detail_states[observation.obs_id]
            state.status = "complete"
            state.retry_after = 0.0
            state.automatic_attempts = 0
            state.diagnostic = ""

        now = time.monotonic()
        for observation_id in request.requested_ids - returned_ids:
            if request.version_for(
                observation_id
            ) != self._observation_refresh_versions.get(
                observation_id,
                0,
            ):
                continue
            state = self._detail_states[observation_id]
            state.status = "missing"
            state.retry_after = now + _MISSING_DETAIL_RETRY_COOLDOWN_SECONDS
            state.diagnostic = (
                f"Attempted at: {state.last_attempt_at}\n"
                "The detail response did not include this observation."
            )
            self._recent_failures.append(("Observation details", state.diagnostic))

        if current_id in returned_ids:
            self._reconcile_current_after_detail(previous_photo_id)
        self._derive_network_status()

    def _details_failed(self, request: _DetailRequest, exc: Exception) -> None:
        if not self._is_current_detail_request(request):
            return
        now = time.monotonic()
        affected_ids: list[int] = []
        for observation_id in request.requested_ids:
            if request.version_for(
                observation_id
            ) != self._observation_refresh_versions.get(
                observation_id,
                0,
            ):
                continue
            state = self._detail_states[observation_id]
            state.status = "failed"
            state.retry_after = now + _DETAIL_RETRY_COOLDOWN_SECONDS
            state.diagnostic = (
                f"Attempted at: {state.last_attempt_at}\n{format_read_failure(exc)}"
            )
            affected_ids.append(observation_id)
        if not affected_ids:
            self._derive_network_status()
            return
        diagnostic = self._detail_states[affected_ids[0]].diagnostic
        self._recent_failures.append((request.operation, diagnostic))
        current_id = self._current_observation().obs_id
        self._derive_network_status()
        if request.explicit and current_id in request.requested_ids:

            def retry_same_detail_read() -> None:
                self._retry_detail_request(
                    tuple(request.requested_ids), request.operation
                )

            show_safe_read_failure(
                self,
                request.operation,
                exc,
                retry_same_detail_read,
            )
        else:
            log.warning(
                "Identify detail read failed ids=%s: %s", request.requested_ids, exc
            )

    def _reconcile_current_after_detail(self, previous_photo_id: int | None) -> None:
        observation = self._current_observation()
        photo_ids = tuple(photo.photo_id for photo in observation.photos)
        collection_changed = photo_ids != self._thumbnail_photo_ids
        if previous_photo_id in photo_ids:
            self._selected_photo_id = previous_photo_id
            self._photo_index = photo_ids.index(previous_photo_id)
            selected_photo_changed = False
        elif photo_ids:
            self._photo_index = _clamp_photo_index(
                self._photo_index, observation.photos
            )
            self._selected_photo_id = photo_ids[self._photo_index]
            selected_photo_changed = True
        else:
            self._photo_index = 0
            self._selected_photo_id = None
            selected_photo_changed = previous_photo_id is not None

        self._update_header()
        self._info_tab.set_observation(observation)
        self._refresh_action_entry_availability()
        self._update_pending_action_status()
        self._ensure_thumbnail_strip(observation)
        self._update_thumbnail_selection(scroll_selected=selected_photo_changed)
        if selected_photo_changed:
            self._brightness = 0
            self._display_selected_photo(preserve_view=False)
            self._prefetch.prefetch_identify_position(
                self._observation_index,
                self._photo_index,
                self._session.plan.prefetch_radius,
                direction=self._navigation_direction,
            )
            self._update_selected_loading_state()
        elif self._selected_photo_id is None:
            self._photo_panel.show_no_photo()
        if collection_changed and self._selected_photo_id is not None:
            self._prefetch.prefetch_identify_position(
                self._observation_index,
                self._photo_index,
                self._session.plan.prefetch_radius,
                direction=self._navigation_direction,
            )
            self._update_selected_loading_state()
        # Same selected identity intentionally leaves photo/zoom/pan/brightness intact.

    def _refresh_current_observation(self) -> None:
        """Force an authoritative re-fetch of the current observation.

        Useful when the observation was changed elsewhere (e.g. in a web
        browser). Bumping the refresh version discards any in-flight stale
        result and marks this explicit fetch as authoritative; resetting the
        detail state ensures the request is issued even when the observation
        was already complete.
        """
        if self._closed or not self._session.items:
            return
        observation_id = self._current_observation().obs_id
        self._observation_refresh_versions[observation_id] = (
            self._observation_refresh_versions.get(observation_id, 0) + 1
        )
        state = self._detail_states.setdefault(observation_id, _DetailState())
        state.status = "not_requested"
        state.automatic_attempts = 0
        state.retry_after = 0.0
        state.diagnostic = ""
        self._start_detail_request(
            [observation_id],
            explicit=True,
            operation=f"Refresh observation {observation_id}",
        )
        self._derive_network_status()

    def _retry_current_details(self) -> None:
        if self._closed or not self._session.items:
            return
        observation_id = self._current_observation().obs_id
        state = self._detail_states.setdefault(observation_id, _DetailState())
        if state.status == "in_flight":
            return
        self._retry_detail_request(
            (observation_id,),
            f"Retry details for observation {observation_id}",
        )

    def _retry_detail_request(
        self,
        observation_ids: tuple[int, ...],
        operation: str,
    ) -> None:
        if self._closed:
            return
        self._start_detail_request(
            list(observation_ids), explicit=True, operation=operation
        )

    def _derive_network_status(self) -> None:
        if self._closed or not self._session.items:
            return
        observation = self._current_observation()
        photo = self._current_photo()
        current_image_failure = (
            self._prefetch.failure_for_photo(photo.photo_id)
            if photo is not None
            else None
        )
        detail_state = self._detail_states.setdefault(
            observation.obs_id, _DetailState()
        )
        current_detail_failure = detail_state.status in {"failed", "missing"}
        current_image_loading = (
            photo is not None and self._prefetch.is_request_in_flight(photo.photo_id)
        )
        current_image_retrying = (
            photo is not None
            and self._prefetch.active_request_mode(photo.photo_id)
            == ImageRequestMode.EXPLICIT_RETRY
        )
        background_image_loading = bool(
            self._active_image_photo_ids
            - ({photo.photo_id} if photo is not None else set())
        )
        current_detail_loading = detail_state.status == "in_flight"
        background_detail_failures = any(
            state.status in {"failed", "missing"}
            and observation_id != observation.obs_id
            for observation_id, state in self._detail_states.items()
        )
        background_detail_loading = any(
            state.status == "in_flight" and observation_id != observation.obs_id
            for observation_id, state in self._detail_states.items()
        )

        if current_image_retrying:
            status = "Network: retrying current image"
        elif current_image_failure is not None:
            status = "Network: current image failed"
        elif current_detail_failure:
            status = "Network: current observation details need attention"
        elif current_image_loading or current_detail_loading:
            status = "Network: loading current observation"
        elif self._background_image_failure_ids or background_detail_failures:
            status = "Network: background failures"
        elif background_image_loading and background_detail_loading:
            status = "Network: loading nearby images and details"
        elif background_image_loading:
            status = "Network: loading nearby images"
        elif background_detail_loading:
            status = "Network: loading nearby details"
        else:
            status = "Network: normal"
        self._network_label.setText(status)
        self._detail_retry_button.setVisible(current_detail_failure)

    def _show_network_details(self) -> None:
        if not self._recent_failures:
            QMessageBox.information(
                self, "Network details", "No recent network failures."
            )
            return
        text = "\n\n".join(
            f"{label}\n{detail}" for label, detail in self._recent_failures
        )
        QMessageBox.information(self, "Recent network failures", text)

    def _handle_identify_shortcut(self, event: QKeyEvent, *, execute: bool) -> bool:
        """Recognize one Identify shortcut for ShortcutOverride or KeyPress."""
        key = event.key()
        modifiers = event.modifiers()
        has_alt_or_meta = bool(
            modifiers
            & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
        )
        has_shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        if (
            event.text() == "?"
            or key == Qt.Key.Key_Question
            or (has_shift and key == Qt.Key.Key_Slash)
        ):
            action = self._show_shortcuts
        elif has_shift and key == Qt.Key.Key_Left:
            action = lambda: self._tabs.setCurrentIndex(
                (self._tabs.currentIndex() - 1) % self._tabs.count()
            )
        elif has_shift and key == Qt.Key.Key_Right:
            action = lambda: self._tabs.setCurrentIndex(
                (self._tabs.currentIndex() + 1) % self._tabs.count()
            )
        elif has_alt_or_meta and key == Qt.Key.Key_Left:
            action = lambda: self.move_photo(-1)
        elif has_alt_or_meta and key == Qt.Key.Key_Right:
            action = lambda: self.move_photo(1)
        elif has_alt_or_meta and key == Qt.Key.Key_Up:
            action = lambda: self._adjust_brightness(1)
        elif has_alt_or_meta and key == Qt.Key.Key_Down:
            action = lambda: self._adjust_brightness(-1)
        elif key == Qt.Key.Key_Left:
            action = lambda: self.move_observation(-1)
        elif key == Qt.Key.Key_Right:
            action = lambda: self.move_observation(1)
        elif key == Qt.Key.Key_Up:
            action = lambda: self.move_photo(-1)
        elif key == Qt.Key.Key_Down:
            action = lambda: self.move_photo(1)
        elif key == Qt.Key.Key_Z and not has_alt_or_meta:
            action = self._photo_panel.toggle_zoom
        elif (
            key == Qt.Key.Key_I
            and not has_alt_or_meta
            and not has_shift
            and not bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        ):
            if not self._can_open_add_id():
                return False
            action = self._open_add_id
        elif (
            key == Qt.Key.Key_A
            and not has_alt_or_meta
            and not has_shift
            and not bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        ):
            if not self._can_agree():
                return False
            action = self._start_agree
        elif (
            key == Qt.Key.Key_C
            and not has_alt_or_meta
            and not has_shift
            and not bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        ):
            if not self._can_open_comment():
                return False
            action = self._open_comment
        elif (
            key == Qt.Key.Key_R
            and not has_alt_or_meta
            and not has_shift
            and not bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        ):
            if not self._can_mark_reviewed():
                return False
            action = self._mark_reviewed
        elif (
            key == Qt.Key.Key_F
            and not has_alt_or_meta
            and not has_shift
            and not bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        ):
            if not self._can_open_favorite():
                return False
            action = self._open_favorite
        elif (
            key == Qt.Key.Key_X
            and not has_alt_or_meta
            and not has_shift
            and not bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        ):
            if not self._can_open_captive():
                return False
            action = self._open_captive
        else:
            return False
        if execute:
            action()
        return True

    def _shortcut_should_be_suppressed(self) -> bool:
        if self._closed:
            return True
        modal = QApplication.activeModalWidget()
        if modal is not None and modal is not self:
            return True
        if self._add_id_dialog is not None and self._add_id_dialog.isVisible():
            return True
        if self._comment_dialog is not None and self._comment_dialog.isVisible():
            return True
        if self._favorite_dialog is not None and self._favorite_dialog.isVisible():
            return True
        if self._captive_dialog is not None and self._captive_dialog.isVisible():
            return True
        active_popup = getattr(QApplication, "activePopupWidget", lambda: None)()
        if active_popup is not None:
            return True
        focus = QApplication.focusWidget()
        if focus is None:
            return False
        if isinstance(
            focus,
            (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QAbstractItemView),
        ):
            return True
        combo = focus.parent()
        while combo is not None:
            if isinstance(combo, QComboBox) and combo.isEditable():
                return True
            combo = combo.parent()
        return False

    def _adjust_brightness(self, delta: int) -> None:
        if self._current_photo() is None:
            return
        self._brightness = max(-10, min(10, self._brightness + delta))
        self._photo_panel.set_brightness(self._brightness)
        sign = "+" if self._brightness > 0 else ""
        self.statusBar().showMessage(f"Brightness: {sign}{self._brightness}", 1500)

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "Identify shortcuts",
            "Left / Right: previous / next observation\n"
            "Up / Down: previous / next photo\n"
            "Alt or Cmd + Left / Right: previous / next photo\n"
            "Shift + Left / Right: previous / next tab\n"
            "I: add an identification\n"
            "A: agree with the observation taxon\n"
            "C: add a comment\n"
            "R: mark the observation reviewed; go to the next observation when "
            "reviewed observations are hidden\n"
            "F: add or remove favorite\n"
            "X: change your Captive/Cultivated Data Quality vote\n"
            "Z: toggle fit / 1:1\n"
            "Alt or Cmd + Up / Down: adjust brightness\n"
            "?: show this help",
        )

    def apply_confirmed_observation_refresh(self, observation) -> bool:
        """Apply MainWindow's one centralized safe refresh by stable ID.

        This changes no session order. When the selected photo remains present,
        :meth:`_reconcile_current_after_detail` deliberately preserves its view
        state, including zoom, pan, and brightness.
        """
        if self._closed or not self._session.replace(observation):
            return False
        # The authoritative observation now reflects the confirmed write, so
        # the optimistic placeholder is retired without any further notice.
        self._optimistic_taxa.pop(observation.obs_id, None)
        self._observation_refresh_versions[observation.obs_id] = (
            self._observation_refresh_versions.get(observation.obs_id, 0) + 1
        )
        self._prefetch.replace_observation(observation)
        state = self._detail_states.setdefault(observation.obs_id, _DetailState())
        state.status = "complete"
        state.retry_after = 0.0
        state.automatic_attempts = 0
        state.diagnostic = ""
        if observation.obs_id == self._current_observation().obs_id:
            self._reconcile_current_after_detail(self._selected_photo_id)
        return True

    def _action_manager_changed(self, action: object) -> None:
        if self._closed:
            return
        self._reconcile_optimistic_taxon(action)
        self._update_pending_action_status()

    def _reconcile_optimistic_taxon(self, action: object) -> None:
        """Resolve an optimistic header entry when its journaled action changes.

        Success is left in place (the confirmed refresh replaces it with real
        data); a definite failure, an unretryable ambiguity, or a rejection
        reverts the header to the authoritative taxon and notifies the user.
        """
        if not isinstance(action, dict):
            return
        action_id = _coerce_int(action.get("local_action_id"))
        observation_id = _coerce_int(action.get("observation_id"))
        if action_id is None or observation_id is None:
            return
        optimistic = self._optimistic_taxa.get(observation_id)
        if optimistic is None or optimistic.action_id != action_id:
            return
        state = str(action.get("state") or "")
        if state == "confirmed":
            # The write reached iNaturalist; keep showing the suggested taxon
            # until the centralized refresh swaps in authoritative data, but
            # drop the in-flight marker so the header no longer reads "saving".
            if not optimistic.confirmed:
                optimistic.confirmed = True
                if (
                    self._session.items
                    and self._current_observation().obs_id == observation_id
                ):
                    self._update_header()
            return
        if state in _OPTIMISTIC_REVERT_STATES:
            self._optimistic_taxa.pop(observation_id, None)
            if (
                self._session.items
                and self._current_observation().obs_id == observation_id
            ):
                self._update_header()
            self._notify_optimistic_reverted(observation_id, optimistic.taxon, state)

    def _notify_optimistic_reverted(
        self, observation_id: int, taxon: StudyTaxon, state: str
    ) -> None:
        taxon_name = (taxon.display_name or "your identification").strip()
        if state == "tracking_cancelled":
            # Local tracking was stopped for a row whose write may already
            # exist on iNaturalist, so this must not claim it was not applied.
            self.statusBar().showMessage(
                f"Displayed taxon reverted: local tracking of your ID of {taxon_name} "
                f"on observation #{observation_id} was cancelled, so it can no longer "
                "be confirmed here. It may or may not exist on iNaturalist.",
                12000,
            )
            return
        if state == "cancelled":
            reason = "you cancelled it before it was sent"
        elif state == "ambiguous":
            reason = (
                "the submission outcome could not be confirmed; it may or may "
                "not have been recorded"
            )
        elif state == "failed_terminal":
            reason = "iNaturalist rejected it"
        else:  # failed_retryable
            reason = "it could not be submitted (for example, a network issue)"
        self.statusBar().showMessage(
            f"Displayed taxon reverted: your ID of {taxon_name} on observation "
            f"#{observation_id} was not applied because {reason}. "
            "See Pending Identify Actions.",
            12000,
        )

    def _action_manager_authentication_changed(self) -> None:
        # Action banners and the current-ID marker never retain the login that
        # happened to be supplied when this read-only window was constructed.
        self._identify_authentication_generation += 1
        if self._closed:
            return
        if self._agree_operation is not None:
            self._cancel_agree_for_authentication_change()
        if self._reviewed_operation is not None:
            self._cancel_reviewed_for_authentication_change()
        self._current_user_id = None
        self._start_current_user_lookup()
        self._invalidate_detail_authentication_context()
        if self._action_manager is not None:
            snapshot = self._action_manager.current_authentication()
            self._info_tab.set_login(snapshot.login if snapshot.authenticated else "")
        self._refresh_action_entry_availability()
        if self._session.items:
            self._info_tab.set_observation(self._current_observation())
        self._update_pending_action_status()
        # Any invalidated details in the current prefetch range restart under
        # the current account, or unauthenticated after logout.
        self._load_nearby_details()
        self._derive_network_status()

    def _action_entry_busy(self) -> bool:
        """Return whether Add ID, Agree, Comment, Reviewed, Favorite, or Captive/Cultivated is collecting intent.

        At most one of these six may actively collect or prepare user
        intent in a given Identify window at a time.  This governs only the
        entry phase: an already-journaled action still being dispatched by
        the application-scoped manager does not count as busy here.
        """
        return bool(
            self._add_id_dialog is not None
            or self._comment_dialog is not None
            or self._agree_operation is not None
            or self._reviewed_operation is not None
            or self._favorite_dialog is not None
            or self._captive_dialog is not None
        )

    def _refresh_action_entry_availability(self) -> None:
        """Recompute Add ID, Agree, Comment, Reviewed, Favorite, and Captive/Cultivated availability together."""
        self._update_add_id_availability()
        self._update_agree_availability()
        self._update_comment_availability()
        self._update_reviewed_availability()
        self._update_favorite_availability()
        self._update_captive_availability()

    def _update_add_id_availability(self) -> None:
        if not hasattr(self, "_add_id_action"):
            return
        if self._closed:
            self._add_id_action.setEnabled(False)
            self._add_id_action.setToolTip("Identify window is closing.")
            return
        if self._action_manager is None:
            self._add_id_action.setEnabled(False)
            self._add_id_action.setToolTip("Identify action management is unavailable.")
            return
        if not self._session.items:
            self._add_id_action.setEnabled(False)
            self._add_id_action.setToolTip("No observation is displayed.")
            return
        observation = self._current_observation()
        if self._positive_numeric_id(observation.obs_id) is None:
            self._add_id_action.setEnabled(False)
            self._add_id_action.setToolTip("This observation has no stable numeric ID.")
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._add_id_action.setEnabled(False)
            self._add_id_action.setToolTip("Authenticate to add an identification.")
            return
        if self._add_id_dialog is not None:
            self._add_id_action.setEnabled(False)
            self._add_id_action.setToolTip("Add ID is already open.")
            return
        if self._agree_operation is not None:
            self._add_id_action.setEnabled(False)
            self._add_id_action.setToolTip("Agree is being prepared locally.")
            return
        if self._comment_dialog is not None:
            self._add_id_action.setEnabled(False)
            self._add_id_action.setToolTip("Comment is currently open.")
            return
        if self._reviewed_operation is not None:
            self._add_id_action.setEnabled(False)
            self._add_id_action.setToolTip("Reviewed is being prepared locally.")
            return
        if self._favorite_dialog is not None:
            self._add_id_action.setEnabled(False)
            self._add_id_action.setToolTip("Favorite is currently open.")
            return
        if self._captive_dialog is not None:
            self._add_id_action.setEnabled(False)
            self._add_id_action.setToolTip("Captive/Cultivated is currently open.")
            return
        self._add_id_action.setEnabled(self._can_open_add_id())
        self._add_id_action.setToolTip(
            f"Add a normal identification as {snapshot.login} (I)"
        )

    def _can_open_add_id(self) -> bool:
        """Return whether this window can safely open the Add ID dialog.

        A tracked ``_add_id_dialog`` pointer means Add ID still owns the
        action-entry slot even when ``isVisible()`` is false during close or
        deferred deletion, so a hidden/closing dialog blocks entry exactly
        like a visible one until its ``destroyed`` callback clears it.
        """
        if (
            self._closed
            or self._action_manager is None
            or not self._session.items
            or self._action_entry_busy()
        ):
            return False
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            return False
        observation = self._current_observation()
        return self._positive_numeric_id(observation.obs_id) is not None

    def _can_agree(self) -> bool:
        """Return whether this window can safely capture an Agree operation."""
        if (
            self._closed
            or self._action_manager is None
            or not self._session.items
            or self._action_entry_busy()
        ):
            return False
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            return False
        observation = self._current_observation()
        if self._positive_numeric_id(observation.obs_id) is None:
            return False
        taxon = observation.taxon
        return (
            taxon is not None and self._positive_numeric_id(taxon.taxon_id) is not None
        )

    def _can_open_comment(self) -> bool:
        """Return whether this window can safely open the Comment dialog."""
        if (
            self._closed
            or self._action_manager is None
            or not self._session.items
            or self._action_entry_busy()
        ):
            return False
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            return False
        observation = self._current_observation()
        return self._positive_numeric_id(observation.obs_id) is not None

    def _update_agree_availability(self) -> None:
        if not hasattr(self, "_agree_action"):
            return
        if self._closed:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip("Identify window is closing.")
            return
        if self._action_manager is None:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip("Identify action management is unavailable.")
            return
        if self._agree_operation is not None:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip("Agree is being prepared locally.")
            return
        if self._add_id_dialog is not None:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip("Add ID is currently open.")
            return
        if self._comment_dialog is not None:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip("Comment is currently open.")
            return
        if self._reviewed_operation is not None:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip("Reviewed is being prepared locally.")
            return
        if self._favorite_dialog is not None:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip("Favorite is currently open.")
            return
        if self._captive_dialog is not None:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip("Captive/Cultivated is currently open.")
            return
        if not self._session.items:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip("No observation is displayed.")
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip(
                "Authenticate to agree with the observation taxon."
            )
            return
        observation = self._current_observation()
        if self._positive_numeric_id(observation.obs_id) is None:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip("This observation has no stable numeric ID.")
            return
        taxon = observation.taxon
        if taxon is None:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip(
                "This observation has no current taxon to agree with."
            )
            return
        if self._positive_numeric_id(taxon.taxon_id) is None:
            self._agree_action.setEnabled(False)
            self._agree_action.setToolTip(
                "The observation taxon has no valid numeric ID to agree with."
            )
            return
        taxon_name = str(
            taxon.display_name or taxon.name or "observation taxon"
        ).strip()
        self._agree_action.setEnabled(True)
        self._agree_action.setToolTip(
            f"Agree with {taxon_name} as {snapshot.login} (A)"
        )

    def _update_comment_availability(self) -> None:
        if not hasattr(self, "_comment_action"):
            return
        if self._closed:
            self._comment_action.setEnabled(False)
            self._comment_action.setToolTip("Identify window is closing.")
            return
        if self._action_manager is None:
            self._comment_action.setEnabled(False)
            self._comment_action.setToolTip(
                "Identify action management is unavailable."
            )
            return
        if self._comment_dialog is not None:
            self._comment_action.setEnabled(False)
            self._comment_action.setToolTip("Comment is already open.")
            return
        if self._add_id_dialog is not None:
            self._comment_action.setEnabled(False)
            self._comment_action.setToolTip("Add ID is currently open.")
            return
        if self._agree_operation is not None:
            self._comment_action.setEnabled(False)
            self._comment_action.setToolTip("Agree is being prepared locally.")
            return
        if self._reviewed_operation is not None:
            self._comment_action.setEnabled(False)
            self._comment_action.setToolTip("Reviewed is being prepared locally.")
            return
        if self._favorite_dialog is not None:
            self._comment_action.setEnabled(False)
            self._comment_action.setToolTip("Favorite is currently open.")
            return
        if self._captive_dialog is not None:
            self._comment_action.setEnabled(False)
            self._comment_action.setToolTip("Captive/Cultivated is currently open.")
            return
        if not self._session.items:
            self._comment_action.setEnabled(False)
            self._comment_action.setToolTip("No observation is displayed.")
            return
        observation = self._current_observation()
        if self._positive_numeric_id(observation.obs_id) is None:
            self._comment_action.setEnabled(False)
            self._comment_action.setToolTip(
                "This observation has no stable numeric ID."
            )
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._comment_action.setEnabled(False)
            self._comment_action.setToolTip("Authenticate to add a comment.")
            return
        self._comment_action.setEnabled(True)
        self._comment_action.setToolTip(f"Add a comment as {snapshot.login} (C)")

    def _can_mark_reviewed(self) -> bool:
        """Return whether this window can safely capture a Reviewed operation."""
        if (
            self._closed
            or self._action_manager is None
            or not self._session.items
            or self._action_entry_busy()
        ):
            return False
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            return False
        observation = self._current_observation()
        return self._positive_numeric_id(observation.obs_id) is not None

    def _update_reviewed_availability(self) -> None:
        if not hasattr(self, "_reviewed_action"):
            return
        if self._closed:
            self._reviewed_action.setEnabled(False)
            self._reviewed_action.setToolTip("Identify window is closing.")
            return
        if self._action_manager is None:
            self._reviewed_action.setEnabled(False)
            self._reviewed_action.setToolTip(
                "Identify action management is unavailable."
            )
            return
        if self._reviewed_operation is not None:
            self._reviewed_action.setEnabled(False)
            self._reviewed_action.setToolTip("Reviewed is being prepared locally.")
            return
        if self._add_id_dialog is not None:
            self._reviewed_action.setEnabled(False)
            self._reviewed_action.setToolTip("Add ID is currently open.")
            return
        if self._comment_dialog is not None:
            self._reviewed_action.setEnabled(False)
            self._reviewed_action.setToolTip("Comment is currently open.")
            return
        if self._agree_operation is not None:
            self._reviewed_action.setEnabled(False)
            self._reviewed_action.setToolTip("Agree is being prepared locally.")
            return
        if self._favorite_dialog is not None:
            self._reviewed_action.setEnabled(False)
            self._reviewed_action.setToolTip("Favorite is currently open.")
            return
        if self._captive_dialog is not None:
            self._reviewed_action.setEnabled(False)
            self._reviewed_action.setToolTip("Captive/Cultivated is currently open.")
            return
        if not self._session.items:
            self._reviewed_action.setEnabled(False)
            self._reviewed_action.setToolTip("No observation is displayed.")
            return
        observation = self._current_observation()
        if self._positive_numeric_id(observation.obs_id) is None:
            self._reviewed_action.setEnabled(False)
            self._reviewed_action.setToolTip(
                "This observation has no stable numeric ID."
            )
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._reviewed_action.setEnabled(False)
            self._reviewed_action.setToolTip(
                "Authenticate to mark this observation reviewed."
            )
            return
        self._reviewed_action.setEnabled(True)
        tooltip = f"Mark this observation reviewed as {snapshot.login} (R)"
        if not self._show_reviewed_checkbox.isChecked():
            tooltip = f"{tooltip}; then go to the next observation"
        self._reviewed_action.setToolTip(tooltip)

    def _can_open_favorite(self) -> bool:
        """Return whether this window can safely open the Favorite dialog."""
        if (
            self._closed
            or self._action_manager is None
            or not self._session.items
            or self._action_entry_busy()
        ):
            return False
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            return False
        observation = self._current_observation()
        return self._positive_numeric_id(observation.obs_id) is not None

    def _update_favorite_availability(self) -> None:
        if not hasattr(self, "_favorite_action"):
            return
        if self._closed:
            self._favorite_action.setEnabled(False)
            self._favorite_action.setToolTip("Identify window is closing.")
            return
        if self._action_manager is None:
            self._favorite_action.setEnabled(False)
            self._favorite_action.setToolTip(
                "Identify action management is unavailable."
            )
            return
        if self._favorite_dialog is not None:
            self._favorite_action.setEnabled(False)
            self._favorite_action.setToolTip("Favorite is already open.")
            return
        if self._add_id_dialog is not None:
            self._favorite_action.setEnabled(False)
            self._favorite_action.setToolTip("Add ID is currently open.")
            return
        if self._comment_dialog is not None:
            self._favorite_action.setEnabled(False)
            self._favorite_action.setToolTip("Comment is currently open.")
            return
        if self._agree_operation is not None:
            self._favorite_action.setEnabled(False)
            self._favorite_action.setToolTip("Agree is being prepared locally.")
            return
        if self._reviewed_operation is not None:
            self._favorite_action.setEnabled(False)
            self._favorite_action.setToolTip("Reviewed is being prepared locally.")
            return
        if self._captive_dialog is not None:
            self._favorite_action.setEnabled(False)
            self._favorite_action.setToolTip("Captive/Cultivated is currently open.")
            return
        if not self._session.items:
            self._favorite_action.setEnabled(False)
            self._favorite_action.setToolTip("No observation is displayed.")
            return
        observation = self._current_observation()
        if self._positive_numeric_id(observation.obs_id) is None:
            self._favorite_action.setEnabled(False)
            self._favorite_action.setToolTip(
                "This observation has no stable numeric ID."
            )
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._favorite_action.setEnabled(False)
            self._favorite_action.setToolTip(
                "Authenticate to add or remove this observation from favorites."
            )
            return
        self._favorite_action.setEnabled(True)
        self._favorite_action.setToolTip(
            f"Add or remove this observation from favorites as {snapshot.login} (F)"
        )

    def _can_open_captive(self) -> bool:
        """Return whether this window can safely open the Captive/Cultivated dialog."""
        if (
            self._closed
            or self._action_manager is None
            or not self._session.items
            or self._action_entry_busy()
        ):
            return False
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            return False
        observation = self._current_observation()
        return self._positive_numeric_id(observation.obs_id) is not None

    def _update_captive_availability(self) -> None:
        if not hasattr(self, "_captive_button"):
            return
        if self._closed:
            self._captive_button.setEnabled(False)
            self._captive_button.setToolTip("Identify window is closing.")
            return
        if self._action_manager is None:
            self._captive_button.setEnabled(False)
            self._captive_button.setToolTip(
                "Identify action management is unavailable."
            )
            return
        if self._captive_dialog is not None:
            self._captive_button.setEnabled(False)
            self._captive_button.setToolTip("Captive/Cultivated is already open.")
            return
        if self._add_id_dialog is not None:
            self._captive_button.setEnabled(False)
            self._captive_button.setToolTip("Add ID is currently open.")
            return
        if self._comment_dialog is not None:
            self._captive_button.setEnabled(False)
            self._captive_button.setToolTip("Comment is currently open.")
            return
        if self._agree_operation is not None:
            self._captive_button.setEnabled(False)
            self._captive_button.setToolTip("Agree is being prepared locally.")
            return
        if self._reviewed_operation is not None:
            self._captive_button.setEnabled(False)
            self._captive_button.setToolTip("Reviewed is being prepared locally.")
            return
        if self._favorite_dialog is not None:
            self._captive_button.setEnabled(False)
            self._captive_button.setToolTip("Favorite is currently open.")
            return
        if not self._session.items:
            self._captive_button.setEnabled(False)
            self._captive_button.setToolTip("No observation is displayed.")
            return
        observation = self._current_observation()
        if self._positive_numeric_id(observation.obs_id) is None:
            self._captive_button.setEnabled(False)
            self._captive_button.setToolTip(
                "This observation has no stable numeric ID."
            )
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._captive_button.setEnabled(False)
            self._captive_button.setToolTip(
                "Authenticate to change your Captive/Cultivated vote."
            )
            return
        self._captive_button.setEnabled(True)
        self._captive_button.setToolTip(
            f"Change your Captive/Cultivated Data Quality vote as {snapshot.login} (X)"
        )

    def _start_agree(self) -> None:
        """Capture Agree intent and begin only the safe UUID-resolution read."""
        if self._closed or self._action_manager is None or not self._session.items:
            self._update_agree_availability()
            return
        if self._action_entry_busy():
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._update_agree_availability()
            self.statusBar().showMessage(
                "Authenticate to agree with the observation taxon. No local action was created.",
                6000,
            )
            return
        observation = self._current_observation()
        observation_id = self._positive_numeric_id(observation.obs_id)
        if observation_id is None:
            self._update_agree_availability()
            self.statusBar().showMessage(
                "This observation has no stable numeric ID. No local Agree action was created.",
                6000,
            )
            return
        taxon = observation.taxon
        if taxon is None:
            self._update_agree_availability()
            self.statusBar().showMessage(
                "This observation has no current taxon to agree with. No local action was created.",
                6000,
            )
            return
        taxon_id = self._positive_numeric_id(taxon.taxon_id)
        if taxon_id is None:
            self._update_agree_availability()
            self.statusBar().showMessage(
                "The observation taxon has no valid numeric ID. No local action was created.",
                6000,
            )
            return

        # Capture every value used by the later journal operation now.  In
        # particular, neither UUID completion nor navigation ever reads a
        # later observation or derives taxon identity from presentation text.
        self._agree_operation_generation += 1
        operation = _AgreeOperation(
            generation=self._agree_operation_generation,
            authentication_generation=self._identify_authentication_generation,
            observation_id=observation_id,
            observation_uuid=str(observation.uuid or ""),
            taxon_id=taxon_id,
            taxon_display_name=str(taxon.display_name or taxon.name or "").strip(),
            account_login=str(snapshot.login or "").strip(),
        )
        self._agree_operation = operation
        self._agree_resolution_request_id = None
        self._refresh_action_entry_availability()
        self.statusBar().showMessage(
            "Resolving the observation identity before saving Agree locally…"
        )
        try:
            request_id = self._action_manager.resolve_observation_uuid(
                operation.observation_id,
                operation.observation_uuid,
            )
        except Exception:
            if self._is_active_agree_operation(operation):
                self._clear_agree_operation()
                self.statusBar().showMessage(
                    "The observation identity could not be prepared. "
                    "No local action was created or authorized.",
                    8000,
                )
            return
        if self._is_active_agree_operation(operation):
            self._agree_resolution_request_id = int(request_id)

    def _agree_uuid_resolved(self, resolution: object) -> None:
        """Journal the immutable Agree intent only for its exact UUID result."""
        if self._closed or self._action_manager is None:
            return
        operation = self._agree_operation
        request_id = self._agree_resolution_request_id
        if operation is None or request_id is None:
            return
        if not isinstance(resolution, ObservationUUIDResolution):
            return
        if (
            not self._is_active_agree_operation(operation)
            or resolution.request_id != request_id
            or resolution.observation_id != operation.observation_id
        ):
            return
        if (
            operation.authentication_generation
            != self._identify_authentication_generation
        ):
            self._cancel_agree_for_authentication_change()
            return
        if not resolution.resolved:
            self._clear_agree_operation()
            self.statusBar().showMessage(
                "Unable to resolve the observation identity. "
                "No local action was created or authorized.",
                8000,
            )
            return
        if not self._agree_is_ready_to_journal(operation, resolution):
            # The only mutable context required by Agree is authentication;
            # explain that race specifically and leave the window usable.
            self._cancel_agree_for_authentication_change()
            return
        try:
            result = self._action_manager.queue_identification(
                account_login=operation.account_login,
                observation_id=operation.observation_id,
                observation_uuid=resolution.observation_uuid,
                taxon_id=operation.taxon_id,
                body="",
            )
        except Exception:
            self._clear_agree_operation()
            self.statusBar().showMessage(
                "Agree could not be saved locally. No local action was created or authorized.",
                8000,
            )
            return

        if result.inserted_action_id is not None:
            action_id = int(result.inserted_action_id)
            self._clear_agree_operation()
            self._agree_journaled(
                action_id, operation.observation_id, operation.taxon_display_name
            )
            return
        if result.duplicate_action_id is not None:
            duplicate_id = int(result.duplicate_action_id)
            self._clear_agree_operation()
            self._show_pending_actions_access(
                f"An equivalent unresolved identification exists locally as action #{duplicate_id}."
            )
            self.statusBar().showMessage(
                "An equivalent unresolved identification already exists locally "
                f"as action #{duplicate_id}. No new action was created or authorized.",
                10000,
            )
            return
        self._clear_agree_operation()
        self.statusBar().showMessage(
            "Agree could not be saved locally. No local action was created or authorized.",
            8000,
        )

    def _agree_is_ready_to_journal(
        self,
        operation: _AgreeOperation,
        resolution: ObservationUUIDResolution,
    ) -> bool:
        """Check immutable operation correlation and live authentication at commit."""
        snapshot = (
            self._action_manager.current_authentication()
            if self._action_manager
            else None
        )
        return bool(
            self._is_active_agree_operation(operation)
            and self._agree_resolution_request_id == resolution.request_id
            and resolution.observation_id == operation.observation_id
            and operation.authentication_generation
            == self._identify_authentication_generation
            and snapshot is not None
            and snapshot.authenticated
            and self._same_login(snapshot.login, operation.account_login)
            and operation.observation_id > 0
            and operation.taxon_id > 0
        )

    def _agree_journaled(
        self,
        action_id: int,
        observation_id: int,
        taxon_display_name: str = "",
    ) -> None:
        """Advance once after commit, then authorize only this new local row."""
        if self._closed or self._action_manager is None:
            return
        if (
            self._settings.identify_advance_after_identification
            and self._session.items
            and self._current_observation().obs_id == int(observation_id)
            and self._observation_index < len(self._session.items) - 1
        ):
            self.move_observation(1)
        if self._action_manager.request_dispatch(int(action_id)):
            label = f" with {taxon_display_name}" if taxon_display_name else ""
            self.statusBar().showMessage(
                f"Agree{label} saved locally as action #{int(action_id)}; authorized for submission.",
                5000,
            )
            return
        self._show_pending_actions_access(
            f"Agree action #{int(action_id)} was saved locally but remains paused."
        )
        self.statusBar().showMessage(
            f"Agree action #{int(action_id)} was saved locally but remains paused. "
            "Review Pending Identify Actions if needed.",
            10000,
        )

    def _cancel_agree_for_authentication_change(self) -> None:
        if self._agree_operation is None:
            return
        self._clear_agree_operation()
        self.statusBar().showMessage(
            "Agree was cancelled before journaling because authentication changed. "
            "No local action was created or authorized.",
            10000,
        )

    def _clear_agree_operation(self) -> None:
        self._agree_operation = None
        self._agree_resolution_request_id = None
        self._refresh_action_entry_availability()

    def _is_active_agree_operation(self, operation: _AgreeOperation) -> bool:
        return bool(
            not self._closed
            and self._agree_operation == operation
            and self._agree_operation_generation == operation.generation
        )

    def _mark_reviewed(self) -> None:
        """Capture Reviewed intent and begin only the safe UUID-resolution read.

        Reviewed is one-way (``desired_state=True``, empty payload). When
        reviewed observations are hidden, advance as soon as the immutable
        intent is captured and UUID resolution has started, without waiting
        for local journaling or network submission.
        """
        if self._closed or self._action_manager is None or not self._session.items:
            self._update_reviewed_availability()
            return
        if self._action_entry_busy():
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._update_reviewed_availability()
            self.statusBar().showMessage(
                "Authenticate to mark this observation reviewed. No local action was created.",
                6000,
            )
            return
        observation = self._current_observation()
        observation_id = self._positive_numeric_id(observation.obs_id)
        if observation_id is None:
            self._update_reviewed_availability()
            self.statusBar().showMessage(
                "This observation has no stable numeric ID. No local Reviewed action was created.",
                6000,
            )
            return

        # Capture every value used by the later journal operation now, exactly
        # as Agree does: neither UUID completion nor navigation ever reads a
        # later observation or a later authenticated login.
        self._reviewed_operation_generation += 1
        operation = _ReviewedOperation(
            generation=self._reviewed_operation_generation,
            authentication_generation=self._identify_authentication_generation,
            observation_id=observation_id,
            observation_uuid=str(observation.uuid or ""),
            account_login=str(snapshot.login or "").strip(),
        )
        self._reviewed_operation = operation
        self._reviewed_resolution_request_id = None
        self._refresh_action_entry_availability()
        self.statusBar().showMessage(
            "Resolving the observation identity before saving Reviewed locally…"
        )
        try:
            request_id = self._action_manager.resolve_observation_uuid(
                operation.observation_id,
                operation.observation_uuid,
            )
        except Exception:
            if self._is_active_reviewed_operation(operation):
                self._clear_reviewed_operation()
                self.statusBar().showMessage(
                    "The observation identity could not be prepared. "
                    "No local action was created or authorized.",
                    8000,
                )
            return
        if self._is_active_reviewed_operation(operation):
            self._reviewed_resolution_request_id = int(request_id)
            if not self._show_reviewed_checkbox.isChecked():
                self.move_observation(1)

    def _reviewed_uuid_resolved(self, resolution: object) -> None:
        """Journal the immutable Reviewed intent only for its exact UUID result."""
        if self._closed or self._action_manager is None:
            return
        operation = self._reviewed_operation
        request_id = self._reviewed_resolution_request_id
        if operation is None or request_id is None:
            return
        if not isinstance(resolution, ObservationUUIDResolution):
            return
        if (
            not self._is_active_reviewed_operation(operation)
            or resolution.request_id != request_id
            or resolution.observation_id != operation.observation_id
        ):
            return
        if (
            operation.authentication_generation
            != self._identify_authentication_generation
        ):
            self._cancel_reviewed_for_authentication_change()
            return
        if not resolution.resolved:
            self._clear_reviewed_operation()
            self.statusBar().showMessage(
                "Unable to resolve the observation identity. "
                "No local action was created or authorized.",
                8000,
            )
            return
        if not self._reviewed_is_ready_to_journal(operation, resolution):
            # The only mutable context required by Reviewed is authentication;
            # explain that race specifically and leave the window usable.
            self._cancel_reviewed_for_authentication_change()
            return
        try:
            result = self._action_manager.queue_desired_state(
                account_login=operation.account_login,
                observation_id=operation.observation_id,
                observation_uuid=resolution.observation_uuid,
                action_type="reviewed",
                desired_state=True,
            )
        except Exception:
            self._clear_reviewed_operation()
            self.statusBar().showMessage(
                "Reviewed could not be saved locally. No local action was created or authorized.",
                8000,
            )
            return

        if result.inserted_action_id is not None:
            action_id = int(result.inserted_action_id)
            self._clear_reviewed_operation()
            self._reviewed_journaled(action_id, result.cancelled_action_ids)
            return
        if result.duplicate_action_id is not None:
            duplicate_id = int(result.duplicate_action_id)
            self._clear_reviewed_operation()
            cancelled_note = describe_cancelled_opposite_actions(
                "Reviewed", tuple(int(a) for a in result.cancelled_action_ids)
            )
            pending_message = f"An equivalent unresolved Reviewed action already exists locally as action #{duplicate_id}."
            status_message = (
                "An equivalent unresolved Reviewed action already exists locally "
                f"as action #{duplicate_id}. No new action was created or authorized."
            )
            if cancelled_note:
                pending_message = f"{pending_message} {cancelled_note}"
                status_message = f"{status_message} {cancelled_note}"
            self._show_pending_actions_access(pending_message)
            self.statusBar().showMessage(status_message, 10000)
            return
        self._clear_reviewed_operation()
        self.statusBar().showMessage(
            "Reviewed could not be saved locally. No local action was created or authorized.",
            8000,
        )

    def _reviewed_is_ready_to_journal(
        self,
        operation: _ReviewedOperation,
        resolution: ObservationUUIDResolution,
    ) -> bool:
        """Check immutable operation correlation and live authentication at commit."""
        snapshot = (
            self._action_manager.current_authentication()
            if self._action_manager
            else None
        )
        return bool(
            self._is_active_reviewed_operation(operation)
            and self._reviewed_resolution_request_id == resolution.request_id
            and resolution.observation_id == operation.observation_id
            and operation.authentication_generation
            == self._identify_authentication_generation
            and snapshot is not None
            and snapshot.authenticated
            and self._same_login(snapshot.login, operation.account_login)
            and operation.observation_id > 0
        )

    def _reviewed_journaled(
        self,
        action_id: int,
        cancelled_action_ids: tuple[int, ...] = (),
    ) -> None:
        """Authorize only this exact new row; navigation occurred at capture."""
        if self._closed or self._action_manager is None:
            return
        cancelled_note = describe_cancelled_opposite_actions(
            "Reviewed", tuple(int(a) for a in cancelled_action_ids)
        )
        if self._action_manager.request_dispatch(int(action_id)):
            message = f"Reviewed saved locally as action #{int(action_id)}; authorized for submission."
            if cancelled_note:
                message = f"{message} {cancelled_note}"
            self.statusBar().showMessage(message, 5000)
            return
        self._show_pending_actions_access(
            f"Reviewed action #{int(action_id)} was saved locally but remains paused."
        )
        message = (
            f"Reviewed action #{int(action_id)} was saved locally but remains paused. "
            "Review Pending Identify Actions if needed."
        )
        if cancelled_note:
            message = f"{message} {cancelled_note}"
        self.statusBar().showMessage(message, 10000)
        self._update_pending_action_status()

    def _cancel_reviewed_for_authentication_change(self) -> None:
        if self._reviewed_operation is None:
            return
        self._clear_reviewed_operation()
        self.statusBar().showMessage(
            "Reviewed was cancelled before journaling because authentication changed. "
            "No local action was created or authorized.",
            10000,
        )

    def _clear_reviewed_operation(self) -> None:
        self._reviewed_operation = None
        self._reviewed_resolution_request_id = None
        self._refresh_action_entry_availability()

    def _is_active_reviewed_operation(self, operation: _ReviewedOperation) -> bool:
        return bool(
            not self._closed
            and self._reviewed_operation == operation
            and self._reviewed_operation_generation == operation.generation
        )

    def _show_pending_actions_access(self, message: str) -> None:
        """Keep the existing pending-action route visible after a local outcome."""
        if self._closed:
            return
        self._pending_actions_label.setText(f"Local action status: {message}")
        self._pending_actions_panel.show()

    @staticmethod
    def _same_login(left: str, right: str) -> bool:
        return str(left or "").strip().casefold() == str(right or "").strip().casefold()

    @staticmethod
    def _positive_numeric_id(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            numeric_id = int(value)
        except (TypeError, ValueError):
            return None
        return numeric_id if numeric_id > 0 else None

    def _open_add_id(self) -> None:
        if self._closed or self._action_manager is None or not self._session.items:
            return
        existing = self._add_id_dialog
        if existing is not None:
            # A tracked pointer still owns the action-entry slot even once
            # isVisible() goes false during close/deferred deletion; never
            # create a replacement, wait for the destroyed callback instead.
            if existing.isVisible():
                existing.raise_()
                existing.activateWindow()
            return
        if (
            self._agree_operation is not None
            or self._comment_dialog is not None
            or self._reviewed_operation is not None
            or self._favorite_dialog is not None
            or self._captive_dialog is not None
        ):
            self._refresh_action_entry_availability()
            self.statusBar().showMessage(
                "Finish or cancel Agree, Comment, Reviewed, Favorite, or Captive/Cultivated "
                "before adding an identification.",
                6000,
            )
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._update_add_id_availability()
            self.statusBar().showMessage(
                "Authenticate to add an identification. No local action was created.",
                6000,
            )
            return
        observation = self._current_observation()
        if self._positive_numeric_id(observation.obs_id) is None:
            self.statusBar().showMessage(
                "This observation has no stable numeric ID. No local action was created.",
                6000,
            )
            return
        dialog = IdentifyAddIDDialog(
            client=self._client,
            action_manager=self._action_manager,
            observation_id=observation.obs_id,
            observation_uuid=observation.uuid,
            opened_login=snapshot.login,
            parent=self,
        )
        dialog.identification_journaled.connect(self._add_id_journaled)
        dialog.pending_actions_requested.connect(self.pending_actions_requested.emit)
        dialog.destroyed.connect(
            lambda _object=None, tracked=dialog: self._add_id_dialog_destroyed(tracked)
        )
        self._add_id_dialog = dialog
        self._refresh_action_entry_availability()
        dialog.show()

    def _add_id_dialog_destroyed(self, dialog: IdentifyAddIDDialog) -> None:
        if self._add_id_dialog is dialog:
            self._add_id_dialog = None
            if not self._closed:
                self._refresh_action_entry_availability()

    def _set_optimistic_taxon(
        self, observation_id: int, action_id: int, taxon: object
    ) -> None:
        """Record a locally-suggested taxon so the header updates immediately.

        The entry is skipped when the suggestion already matches the taxon the
        header shows, since that identification changes nothing to display.
        """
        if not isinstance(taxon, StudyTaxon) or taxon.taxon_id <= 0:
            return
        observation = self._observation_by_id(observation_id)
        current = observation.display_taxon if observation is not None else None
        if current is not None and current.taxon_id == taxon.taxon_id:
            return
        self._optimistic_taxa[observation_id] = _OptimisticTaxon(action_id, taxon)
        if self._session.items and self._current_observation().obs_id == observation_id:
            self._update_header()

    def _observation_by_id(self, observation_id: int):
        for item in self._session.items:
            if item.observation_id == observation_id:
                return item.observation
        return None

    def _add_id_journaled(
        self, action_id: int, observation_id: int, suggested_taxon: object = None
    ) -> None:
        """Advance only after durable enqueue, then authorize this exact row."""
        if self._closed or self._action_manager is None:
            return
        # Reflect the suggested taxon in the header immediately, before any
        # advance.  It is keyed by observation ID, so it stays correct whether
        # or not Add ID advances to the next observation.
        self._set_optimistic_taxon(int(observation_id), int(action_id), suggested_taxon)
        if (
            self._settings.identify_advance_after_identification
            and self._session.items
            and self._current_observation().obs_id == int(observation_id)
        ):
            # move_observation clamps at the last item, so Add ID never wraps.
            self.move_observation(1)
        if self._action_manager.request_dispatch(int(action_id)):
            self.statusBar().showMessage(
                f"Identification saved locally as action #{int(action_id)}; authorized for submission.",
                5000,
            )
            return
        # The durable row remains visible in the per-observation local-action
        # presentation and action center.  Never broaden this to a queue-wide
        # resume merely because one new row was journaled.
        self.statusBar().showMessage(
            f"Identification saved locally as action #{int(action_id)}; it remains paused. "
            "Review Pending Identify Actions if needed.",
            8000,
        )
        self._update_pending_action_status()

    def _open_comment(self) -> None:
        if self._closed or self._action_manager is None or not self._session.items:
            return
        existing = self._comment_dialog
        if existing is not None:
            # A tracked pointer still owns the action-entry slot even once
            # isVisible() goes false during close/deferred deletion; never
            # create a replacement, wait for the destroyed callback instead.
            if existing.isVisible():
                existing.raise_()
                existing.activateWindow()
            return
        if (
            self._agree_operation is not None
            or self._add_id_dialog is not None
            or self._reviewed_operation is not None
            or self._favorite_dialog is not None
            or self._captive_dialog is not None
        ):
            self._refresh_action_entry_availability()
            self.statusBar().showMessage(
                "Finish or cancel Add ID, Agree, Reviewed, Favorite, or Captive/Cultivated "
                "before adding a comment.",
                6000,
            )
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._update_comment_availability()
            self.statusBar().showMessage(
                "Authenticate to add a comment. No local action was created.",
                6000,
            )
            return
        observation = self._current_observation()
        if self._positive_numeric_id(observation.obs_id) is None:
            self.statusBar().showMessage(
                "This observation has no stable numeric ID. No local action was created.",
                6000,
            )
            return
        dialog = IdentifyCommentDialog(
            action_manager=self._action_manager,
            observation_id=observation.obs_id,
            observation_uuid=observation.uuid,
            opened_login=snapshot.login,
            parent=self,
        )
        dialog.comment_journaled.connect(self._comment_journaled)
        dialog.pending_actions_requested.connect(self.pending_actions_requested.emit)
        dialog.destroyed.connect(
            lambda _object=None, tracked=dialog: self._comment_dialog_destroyed(tracked)
        )
        self._comment_dialog = dialog
        self._refresh_action_entry_availability()
        dialog.show()

    def _comment_dialog_destroyed(self, dialog: IdentifyCommentDialog) -> None:
        if self._comment_dialog is dialog:
            self._comment_dialog = None
            if not self._closed:
                self._refresh_action_entry_availability()

    def _comment_journaled(self, action_id: int, observation_id: int) -> None:
        """Authorize only the exact inserted row; Comment never navigates."""
        del observation_id
        if self._closed or self._action_manager is None:
            return
        if self._action_manager.request_dispatch(int(action_id)):
            self.statusBar().showMessage(
                f"Comment saved locally as action #{int(action_id)}; authorized for submission.",
                5000,
            )
            return
        self._show_pending_actions_access(
            f"Comment action #{int(action_id)} was saved locally but remains paused."
        )
        self.statusBar().showMessage(
            f"Comment action #{int(action_id)} was saved locally but remains paused. "
            "Review Pending Identify Actions if needed.",
            8000,
        )
        self._update_pending_action_status()

    def _open_favorite(self) -> None:
        if self._closed or self._action_manager is None or not self._session.items:
            return
        existing = self._favorite_dialog
        if existing is not None:
            # A tracked pointer still owns the action-entry slot even once
            # isVisible() goes false during close/deferred deletion; never
            # create a replacement, wait for the destroyed callback instead.
            if existing.isVisible():
                existing.raise_()
                existing.activateWindow()
            return
        if (
            self._add_id_dialog is not None
            or self._comment_dialog is not None
            or self._agree_operation is not None
            or self._reviewed_operation is not None
            or self._captive_dialog is not None
        ):
            self._refresh_action_entry_availability()
            self.statusBar().showMessage(
                "Finish or cancel Add ID, Agree, Comment, Reviewed, or Captive/Cultivated "
                "before changing Favorite.",
                6000,
            )
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._update_favorite_availability()
            self.statusBar().showMessage(
                "Authenticate to add or remove this observation from favorites. "
                "No local action was created.",
                6000,
            )
            return
        observation = self._current_observation()
        if self._positive_numeric_id(observation.obs_id) is None:
            self.statusBar().showMessage(
                "This observation has no stable numeric ID. No local action was created.",
                6000,
            )
            return
        dialog = IdentifyFavoriteDialog(
            action_manager=self._action_manager,
            observation_id=observation.obs_id,
            observation_uuid=observation.uuid,
            opened_login=snapshot.login,
            parent=self,
        )
        dialog.favorite_journaled.connect(self._favorite_journaled)
        dialog.pending_actions_requested.connect(self.pending_actions_requested.emit)
        dialog.destroyed.connect(
            lambda _object=None, tracked=dialog: self._favorite_dialog_destroyed(
                tracked
            )
        )
        self._favorite_dialog = dialog
        self._refresh_action_entry_availability()
        dialog.show()

    def _favorite_dialog_destroyed(self, dialog: IdentifyFavoriteDialog) -> None:
        if self._favorite_dialog is dialog:
            self._favorite_dialog = None
            if not self._closed:
                self._refresh_action_entry_availability()

    def _favorite_journaled(
        self,
        action_id: int,
        observation_id: int,
        desired_state: bool,
        cancelled_action_ids: object,
    ) -> None:
        """Never advance; authorize only this exact new local row."""
        del observation_id
        if self._closed or self._action_manager is None:
            return
        cancelled_ids = (
            tuple(int(a) for a in cancelled_action_ids)
            if isinstance(cancelled_action_ids, (tuple, list))
            else ()
        )
        cancelled_note = describe_cancelled_opposite_actions("Favorite", cancelled_ids)
        verb = "addition" if desired_state else "removal"
        if self._action_manager.request_dispatch(int(action_id)):
            message = (
                f"Favorite {verb} saved locally as action #{int(action_id)}; "
                "authorized for submission."
            )
            if cancelled_note:
                message = f"{message} {cancelled_note}"
            self.statusBar().showMessage(message, 5000)
            return
        self._show_pending_actions_access(
            f"Favorite action #{int(action_id)} was saved locally but remains paused."
        )
        message = (
            f"Favorite action #{int(action_id)} remains paused. "
            "Review Pending Identify Actions if needed."
        )
        if cancelled_note:
            message = f"{message} {cancelled_note}"
        self.statusBar().showMessage(message, 10000)
        self._update_pending_action_status()

    def _open_captive(self) -> None:
        if self._closed or self._action_manager is None or not self._session.items:
            return
        existing = self._captive_dialog
        if existing is not None:
            # A tracked pointer still owns the action-entry slot even once
            # isVisible() goes false during close/deferred deletion; never
            # create a replacement, wait for the destroyed callback instead.
            if existing.isVisible():
                existing.raise_()
                existing.activateWindow()
            return
        if (
            self._add_id_dialog is not None
            or self._comment_dialog is not None
            or self._agree_operation is not None
            or self._reviewed_operation is not None
            or self._favorite_dialog is not None
        ):
            self._refresh_action_entry_availability()
            self.statusBar().showMessage(
                "Finish or cancel Add ID, Agree, Comment, Reviewed, or Favorite "
                "before changing your Captive/Cultivated vote.",
                6000,
            )
            return
        snapshot = self._action_manager.current_authentication()
        if not snapshot.authenticated:
            self._update_captive_availability()
            self.statusBar().showMessage(
                "Authenticate to change your Captive/Cultivated vote. No local action was created.",
                6000,
            )
            return
        observation = self._current_observation()
        if self._positive_numeric_id(observation.obs_id) is None:
            self.statusBar().showMessage(
                "This observation has no stable numeric ID. No local action was created.",
                6000,
            )
            return
        dialog = IdentifyCaptiveDialog(
            action_manager=self._action_manager,
            observation_id=observation.obs_id,
            observation_uuid=observation.uuid,
            opened_login=snapshot.login,
            parent=self,
        )
        dialog.quality_metric_journaled.connect(self._captive_journaled)
        dialog.pending_actions_requested.connect(self.pending_actions_requested.emit)
        dialog.destroyed.connect(
            lambda _object=None, tracked=dialog: self._captive_dialog_destroyed(tracked)
        )
        self._captive_dialog = dialog
        self._refresh_action_entry_availability()
        dialog.show()

    def _captive_dialog_destroyed(self, dialog: IdentifyCaptiveDialog) -> None:
        if self._captive_dialog is dialog:
            self._captive_dialog = None
            if not self._closed:
                self._refresh_action_entry_availability()

    def _captive_journaled(
        self,
        action_id: int,
        observation_id: int,
        vote: str,
        cancelled_action_ids: object,
    ) -> None:
        """Never advance; authorize only this exact new local row."""
        del observation_id
        if self._closed or self._action_manager is None:
            return
        cancelled_ids = (
            tuple(int(a) for a in cancelled_action_ids)
            if isinstance(cancelled_action_ids, (tuple, list))
            else ()
        )
        cancelled_note = describe_cancelled_conflicting_actions(
            "Captive/Cultivated", cancelled_ids
        )
        label = {
            "disagree": "Captive/Cultivated vote",
            "agree": "Wild vote",
            "remove": "Wild/Captive vote removal",
        }.get(vote, "Captive/Cultivated vote")
        if self._action_manager.request_dispatch(int(action_id)):
            message = f"{label} saved locally as action #{int(action_id)}; authorized for submission."
            if cancelled_note:
                message = f"{message} {cancelled_note}"
            self.statusBar().showMessage(message, 5000)
            return
        self._show_pending_actions_access(
            f"{label} action #{int(action_id)} was saved locally but remains paused."
        )
        message = (
            f"{label} action #{int(action_id)} remains paused. "
            "Review Pending Identify Actions if needed."
        )
        if cancelled_note:
            message = f"{message} {cancelled_note}"
        self.statusBar().showMessage(message, 10000)
        self._update_pending_action_status()

    def _current_detail_read_token(self) -> str:
        """Get the current in-memory token only when starting a safe read."""
        return str(self._read_auth_provider() or "").strip()

    def _is_current_detail_request(self, request: _DetailRequest) -> bool:
        return (
            not self._closed
            and request.generation == self._generation
            and request.authentication_generation
            == self._detail_authentication_generation
        )

    def _invalidate_detail_authentication_context(self) -> None:
        """Reject old-auth callbacks and make their detail states retryable."""
        self._detail_authentication_generation += 1
        for state in self._detail_states.values():
            if state.status != "in_flight":
                continue
            state.status = "not_requested"
            state.last_attempt = 0.0
            state.last_attempt_at = ""
            state.retry_after = 0.0
            state.automatic_attempts = 0
            state.diagnostic = ""

    def _update_pending_action_status(self) -> None:
        if self._closed or self._action_manager is None or not self._session.items:
            self._pending_actions_panel.hide()
            return
        observation_id = self._current_observation().obs_id
        actions = [
            present_identify_action(action)
            for action in self._action_manager.actions_for_observation(observation_id)
        ]
        self._info_tab.set_pending_actions(
            [
                action
                for action in actions
                if action.is_active or action.requires_attention
            ]
        )
        text = compact_observation_action_text(actions)
        if text:
            self._pending_actions_label.setText(f"Local action status: {text}")
            self._pending_actions_panel.show()
        else:
            self._pending_actions_panel.hide()

    def _current_observation(self):
        return self._session.items[self._observation_index].observation

    def _current_photo(self):
        observation = self._current_observation()
        if not observation.photos or self._selected_photo_id is None:
            return None
        self._photo_index = _clamp_photo_index(self._photo_index, observation.photos)
        photo = observation.photos[self._photo_index]
        if photo.photo_id == self._selected_photo_id:
            return photo
        for index, candidate in enumerate(observation.photos):
            if candidate.photo_id == self._selected_photo_id:
                self._photo_index = index
                return candidate
        self._selected_photo_id = photo.photo_id
        return photo

    def closeEvent(self, event) -> None:
        self._closed = True
        self._generation += 1
        self._agree_operation = None
        self._agree_resolution_request_id = None
        self._reviewed_operation = None
        self._reviewed_resolution_request_id = None
        if self._add_id_dialog is not None:
            self._add_id_dialog.close()
            self._add_id_dialog = None
        if self._comment_dialog is not None:
            self._comment_dialog.close()
            self._comment_dialog = None
        if self._favorite_dialog is not None:
            self._favorite_dialog.close()
            self._favorite_dialog = None
        if self._captive_dialog is not None:
            self._captive_dialog.close()
            self._captive_dialog = None
        self._remove_shortcut_filter()
        self._prefetch.clear()
        self._settings.identify_window_geometry = self.saveGeometry()
        self._settings.identify_window_state = self.saveState()
        self._settings.identify_splitter_state = self._splitter.saveState()
        self._settings.identify_active_tab = self._tabs.currentIndex()
        self._settings.sync()
        if self._action_manager is not None:
            try:
                self._action_manager.action_changed.disconnect(
                    self._action_manager_changed
                )
                self._action_manager.authentication_context_changed.disconnect(
                    self._action_manager_authentication_changed
                )
                self._action_manager.observation_uuid_resolved.disconnect(
                    self._agree_uuid_resolved
                )
                self._action_manager.observation_uuid_resolved.disconnect(
                    self._reviewed_uuid_resolved
                )
            except (RuntimeError, TypeError):
                pass
        super().closeEvent(event)

    def _remove_shortcut_filter(self) -> None:
        application = QApplication.instance()
        if application is not None:
            application.removeEventFilter(self._shortcut_filter)


def _clamp_photo_index(photo_index: int, photos: list) -> int:
    if not photos:
        return 0
    return max(0, min(photo_index, len(photos) - 1))


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _extract_current_user_id(raw: object) -> int | None:
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("results"), list):
        results = raw["results"]
        raw_user = results[0] if results else {}
    else:
        raw_user = raw.get("user") if isinstance(raw.get("user"), dict) else raw
    if not isinstance(raw_user, dict):
        return None
    try:
        value = raw_user.get("id")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_QUALITY_GRADE_LABELS = {
    "research": "Research Grade",
    "needs_id": "Needs ID",
    "casual": "Casual",
}


def _quality_grade_html(quality_grade: str) -> str:
    grade = (quality_grade or "").strip().casefold()
    label = _QUALITY_GRADE_LABELS.get(grade, "Unknown Grade")
    if grade == "research":
        return f"<span style='color:#66aa66'>{label}</span>"
    return label


def _format_image_failure(failure: ImageFailure) -> str:
    if not failure.attempts:
        return "No waterfall candidate diagnostics were available."
    chunks = []
    for attempt in failure.attempts:
        chunks.append(
            "\n".join(
                (
                    f"Candidate: {attempt.candidate_number}",
                    f"Size: {attempt.size}",
                    f"URL: {attempt.url}",
                    f"HTTP status: {attempt.status_code if attempt.status_code is not None else 'Unavailable'}",
                    f"Exception: {attempt.exception_type}",
                    f"Message: {attempt.message}",
                    f"Timestamp: {attempt.timestamp}",
                )
            )
        )
    return "\n\n".join(chunks)


def _format_image_diagnostics(diagnostics: ImageLoadDiagnostics | None) -> str:
    if diagnostics is None:
        return "No image failure details available."
    sections: list[str] = []
    if diagnostics.loaded_size:
        sections.append(f"Loaded size: {diagnostics.loaded_size.title()}")
    if diagnostics.partial_failures:
        sections.append(
            "Higher-quality attempts that failed:\n"
            + _format_image_failure(
                ImageFailure(diagnostics.photo_id, diagnostics.partial_failures)
            )
        )
        sections.append(
            "Automatic original-upgrade retry is delayed until the configured cooldown."
        )
    if diagnostics.complete_failure is not None:
        sections.append(_format_image_failure(diagnostics.complete_failure))
    return "\n\n".join(sections) or "No image failure details available."


def _has_saved_value(value: object) -> bool:
    if value is None:
        return False
    is_empty = getattr(value, "isEmpty", None)
    return not bool(is_empty()) if callable(is_empty) else bool(value)
