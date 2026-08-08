"""
Result list panel: left-side list of study results with thumbnail,
taxon name, date, and status indicators.

Uses QListWidget with a custom QStyledItemDelegate. All layout metrics
are derived from QFontMetrics so the row scales correctly at any text size.
Thumbnails load asynchronously and are stored as item data roles.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from PySide6.QtCore import (
    QObject,
    QPoint,
    QRect,
    QRunnable,
    QSize,
    QThreadPool,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.api.client import INatClient
from observation_workbench.models import StudyObservation
from observation_workbench.services.image_cache import ImageCache

log = logging.getLogger(__name__)

THUMB_SIZE = 60

_OBS_ROLE = Qt.ItemDataRole.UserRole

_PAD = 6  # pixels of padding around thumbnail and at row edges
_GAP = 2  # vertical gap between text lines


# ---------------------------------------------------------------------------
# Background thumbnail loader (unchanged)
# ---------------------------------------------------------------------------


class _ThumbSignals(QObject):
    loaded = Signal(int, QPixmap)  # obs_id, pixmap


class _ThumbWorker(QRunnable):
    """Loads a thumbnail for the result list."""

    def __init__(
        self,
        obs_id: int,
        photo_id: int,
        url_square: str,
        client: INatClient,
        disk_cache: ImageCache,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.obs_id = obs_id
        self.photo_id = photo_id
        self.url_square = url_square
        self.client = client
        self.disk_cache = disk_cache
        self.signals = _ThumbSignals()

    def run(self) -> None:
        try:
            cached = self.disk_cache.get(self.photo_id, "thumb")
            if cached is None:
                cached = self.disk_cache.get(self.photo_id, "square")
            if cached is None:
                data = self.client.download_image(self.url_square)
                ext = "jpg" if data[:2] == b"\xff\xd8" else "png"
                self.disk_cache.put(self.photo_id, "square", data, ext)
                cached = data

            img = QPixmap()
            img.loadFromData(cached)
            if not img.isNull():
                thumb = img.scaled(
                    THUMB_SIZE,
                    THUMB_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.signals.loaded.emit(self.obs_id, thumb)
        except Exception as exc:
            log.debug("Thumb load failed obs=%d: %s", self.obs_id, exc)


# ---------------------------------------------------------------------------
# Delegate
# ---------------------------------------------------------------------------


class ResultItemDelegate(QStyledItemDelegate):
    """
    Draws each row: thumbnail on the left, text block on the right.

    All vertical positions are computed from QFontMetrics so the layout
    stays correct at any font scale.  Species names are elided with
    ElideRight if they would overflow the available width.
    """

    def __init__(
        self, font_scale: float = 1.0, parent: Optional[QObject] = None
    ) -> None:
        super().__init__(parent)
        self._font_scale = font_scale
        self._thumbnails: Dict[int, QPixmap] = {}  # obs_id → pixmap

    def set_font_scale(self, scale: float) -> None:
        self._font_scale = scale

    def set_thumbnail(self, obs_id: int, pixmap: QPixmap) -> None:
        self._thumbnails[obs_id] = pixmap

    def clear_thumbnails(self) -> None:
        self._thumbnails.clear()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_fonts(self) -> tuple[QFont, QFont, QFont]:
        app_pt = QApplication.font().pointSize()
        if app_pt <= 0:
            app_pt = 10
        title_f = QFont()
        title_f.setPointSize(max(6, round(app_pt * self._font_scale)))
        title_f.setBold(True)
        meta_f = QFont()
        meta_f.setPointSize(max(6, round((app_pt - 1) * self._font_scale)))
        status_f = QFont()
        status_f.setPointSize(max(6, round((app_pt - 2) * self._font_scale)))
        return title_f, meta_f, status_f

    @staticmethod
    def _indicators(obs: StudyObservation) -> List[str]:
        result: List[str] = []
        ident = obs.target_identification
        if ident:
            if ident.is_leading:
                result.append("★ leading")
            if ident.is_disagreement:
                result.append("⚡ disagrees")
        if obs.quality_grade == "research":
            result.append("✓ RG")
        if obs.obscured:
            result.append("🔒 obscured")
        return result

    def _text_block_height(
        self,
        obs: StudyObservation,
        title_fm: QFontMetrics,
        meta_fm: QFontMetrics,
        status_fm: QFontMetrics,
    ) -> int:
        h = title_fm.height() + _GAP + meta_fm.height()
        if obs.provisional_species_name:
            h += _GAP + meta_fm.height()
        if self._indicators(obs):
            h += _GAP + status_fm.height()
        return h

    # ------------------------------------------------------------------
    # QStyledItemDelegate interface
    # ------------------------------------------------------------------

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        obs: Optional[StudyObservation] = index.data(_OBS_ROLE)
        if obs is None:
            return QSize(200, THUMB_SIZE + 2 * _PAD)
        title_f, meta_f, status_f = self._make_fonts()
        text_h = self._text_block_height(
            obs,
            QFontMetrics(title_f),
            QFontMetrics(meta_f),
            QFontMetrics(status_f),
        )
        row_h = max(THUMB_SIZE, text_h) + 2 * _PAD
        w = option.rect.width() if option.rect.isValid() else 200
        return QSize(w, row_h)

    def paint(self, painter, option: QStyleOptionViewItem, index) -> None:
        obs: Optional[StudyObservation] = index.data(_OBS_ROLE)
        if obs is None:
            return

        painter.save()
        r = option.rect

        # Selection background
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(r, option.palette.highlight())
            default_color = option.palette.highlightedText().color()
        else:
            default_color = option.palette.text().color()

        title_f, meta_f, status_f = self._make_fonts()
        title_fm = QFontMetrics(title_f)
        meta_fm = QFontMetrics(meta_f)
        status_fm = QFontMetrics(status_f)

        # --- Thumbnail (top-aligned to row top + padding) ---
        thumb_rect = QRect(r.left() + _PAD, r.top() + _PAD, THUMB_SIZE, THUMB_SIZE)
        pixmap: Optional[QPixmap] = self._thumbnails.get(obs.obs_id)
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                THUMB_SIZE,
                THUMB_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            # Center the (possibly non-square) scaled image within thumb_rect
            dx = (THUMB_SIZE - scaled.width()) // 2
            dy = (THUMB_SIZE - scaled.height()) // 2
            painter.drawPixmap(thumb_rect.left() + dx, thumb_rect.top() + dy, scaled)
        else:
            painter.setPen(QColor("#444"))
            painter.drawRect(thumb_rect)
            painter.setPen(QColor("#888"))
            painter.drawText(thumb_rect, Qt.AlignmentFlag.AlignCenter, "…")

        # --- Text block (vertically centered within the row) ---
        text_x = r.left() + _PAD + THUMB_SIZE + _PAD
        text_w = r.right() - text_x - _PAD
        text_h = self._text_block_height(obs, title_fm, meta_fm, status_fm)
        text_top = r.top() + (r.height() - text_h) // 2

        # Title line (bold, elided)
        taxon = obs.display_taxon
        title = taxon.display_name if taxon else "Unknown"
        painter.setFont(title_f)
        painter.setPen(default_color)
        painter.drawText(
            text_x,
            text_top + title_fm.ascent(),
            title_fm.elidedText(title, Qt.TextElideMode.ElideRight, text_w),
        )

        y = text_top + title_fm.height() + _GAP

        # Provisional species name line
        if obs.provisional_species_name:
            painter.setFont(meta_f)
            painter.setPen(QColor("#ccaa55"))
            painter.drawText(
                text_x,
                y + meta_fm.ascent(),
                meta_fm.elidedText(
                    obs.provisional_species_name,
                    Qt.TextElideMode.ElideRight,
                    text_w,
                ),
            )
            y += meta_fm.height() + _GAP

        # Date / observer line
        meta_text = f"{obs.display_date}  ·  {obs.observer_login}"
        painter.setFont(meta_f)
        painter.setPen(QColor("#aaa"))
        painter.drawText(
            text_x,
            y + meta_fm.ascent(),
            meta_fm.elidedText(meta_text, Qt.TextElideMode.ElideRight, text_w),
        )

        # Status indicators line
        indicators = self._indicators(obs)
        if indicators:
            y2 = y + meta_fm.height() + _GAP
            painter.setFont(status_f)
            painter.setPen(QColor("#66aa66"))
            painter.drawText(text_x, y2 + status_fm.ascent(), "  ".join(indicators))

        painter.restore()


# ---------------------------------------------------------------------------
# Result list widget
# ---------------------------------------------------------------------------


class ResultList(QWidget):
    """
    Left panel showing all loaded observations.
    Emits selection_changed(obs_index) when user selects an item.
    """

    selection_changed = Signal(int)
    near_bottom_reached = Signal(int)

    def __init__(
        self,
        client: INatClient,
        disk_cache: ImageCache,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._disk_cache = disk_cache
        self._font_scale: float = 1.0
        # Dedicated pool with 2 threads so thumbnail loading doesn't compete
        # with the global pool used for page fetching and image prefetching.
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(2)
        self._observations: List[StudyObservation] = []
        self._suppress_selection = False
        self._live_thumb_signals: set = set()  # prevent GC of signal objects
        self._obs_id_to_row: Dict[int, int] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._list = QListWidget()
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setUniformItemSizes(False)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._delegate = ResultItemDelegate(parent=self._list)
        self._list.setItemDelegate(self._delegate)
        layout.addWidget(self._list)

    @Slot(int)
    def _on_scroll(self, value: int) -> None:
        count = self._list.count()
        if count == 0:
            return

        viewport_rect = self._list.viewport().rect()
        cx = viewport_rect.width() // 2
        bottom_y = viewport_rect.bottom()

        bottom_index = self._list.indexAt(QPoint(cx, bottom_y))

        if not bottom_index.isValid():
            last_item = self._list.item(count - 1)
            if last_item:
                rect = self._list.visualItemRect(last_item)
                if not rect.isEmpty() and rect.top() <= bottom_y:
                    last_visible_row = count - 1
                else:
                    return
            else:
                return
        else:
            last_visible_row = bottom_index.row()

        if count - 1 - last_visible_row <= 15:
            self.near_bottom_reached.emit(count)

    def set_font_scale(self, scale: float) -> None:
        self._font_scale = scale
        self._delegate.set_font_scale(scale)
        self._list.viewport().update()

    def set_observations(self, observations: List[StudyObservation]) -> None:
        self._suppress_selection = True
        self._list.clear()
        self._delegate.clear_thumbnails()
        self._observations = list(observations)  # own copy
        self._obs_id_to_row = {}

        for row, obs in enumerate(observations):
            item = QListWidgetItem()
            item.setData(_OBS_ROLE, obs)
            self._list.addItem(item)
            self._obs_id_to_row[obs.obs_id] = row
            self._load_thumbnail(obs)

        if observations:
            self._list.setCurrentRow(0)
        self._suppress_selection = False

    def refresh_display(self) -> None:
        """Force layout recalculation and repaint without reloading data/thumbnails."""
        model = self._list.model()
        if model:
            model.layoutChanged.emit()

    def _load_thumbnail(self, obs: StudyObservation) -> None:
        if not obs.photos:
            return
        photo = obs.photos[0]
        if not photo.url_square:
            return
        worker = _ThumbWorker(
            obs_id=obs.obs_id,
            photo_id=photo.photo_id,
            url_square=photo.url_square,
            client=self._client,
            disk_cache=self._disk_cache,
        )
        sigs = worker.signals
        self._live_thumb_signals.add(sigs)
        sigs.loaded.connect(
            lambda oid, px, s=sigs: (
                self._live_thumb_signals.discard(s),
                self._on_thumb_loaded(oid, px),
            )
        )
        self._pool.start(worker)

    def _on_thumb_loaded(self, obs_id: int, pixmap: QPixmap) -> None:
        if obs_id not in self._obs_id_to_row:
            return
        self._delegate.set_thumbnail(obs_id, pixmap)
        self._list.viewport().update()

    def append_observations(self, new_obs: List[StudyObservation]) -> None:
        """Append more items (for pagination 'Load more')."""
        self._suppress_selection = True
        for obs in new_obs:
            row = self._list.count()
            self._observations.append(obs)
            item = QListWidgetItem()
            item.setData(_OBS_ROLE, obs)
            self._list.addItem(item)
            self._obs_id_to_row[obs.obs_id] = row
            self._load_thumbnail(obs)
        self._suppress_selection = False

    def replace_observation(self, idx: int, obs: StudyObservation) -> None:
        """Replace one row's observation data after an authenticated refresh/write."""
        if not (0 <= idx < len(self._observations) and idx < self._list.count()):
            return
        old_id = self._observations[idx].obs_id
        self._observations[idx] = obs
        item = self._list.item(idx)
        if item:
            item.setData(_OBS_ROLE, obs)
        self._obs_id_to_row.pop(old_id, None)
        self._obs_id_to_row[obs.obs_id] = idx
        self.refresh_display()

    def set_current_index(self, idx: int) -> None:
        if 0 <= idx < self._list.count():
            self._suppress_selection = True
            self._list.setCurrentRow(idx)
            self._list.scrollToItem(self._list.currentItem())
            self._suppress_selection = False

    def current_index(self) -> int:
        return self._list.currentRow()

    def count(self) -> int:
        return self._list.count()

    @Slot(int)
    def _on_row_changed(self, row: int) -> None:
        if not self._suppress_selection and row >= 0:
            self.selection_changed.emit(row)
