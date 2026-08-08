"""
Image viewer panel: displays the current photo with fit-to-window / 1:1 zoom.

Uses QGraphicsView + QGraphicsScene for proper zoom/pan and smooth scaling.
Loading indicator shown while image is fetching.

Waterfall loading: original → large → medium (via ImagePrefetcher).
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

_CLICK_THRESHOLD = (
    5  # max manhattan distance (px) between press and release to count as a click
)


class _ClickableGraphicsView(QGraphicsView):
    """QGraphicsView that emits `clicked` when the user clicks without dragging."""

    clicked = Signal()

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self._press_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._press_pos is not None
            and (event.pos() - self._press_pos).manhattanLength() < _CLICK_THRESHOLD
        ):
            self.clicked.emit()
        self._press_pos = None
        super().mouseReleaseEvent(event)


class ViewerPanel(QWidget):
    """
    Displays a single photo.
    Modes:
      - Fit to window (default)
      - 1:1 pixel zoom (toggle with L)
    """

    zoom_toggled = Signal(bool)  # True = 1:1 zoom

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._fit_mode = True
        self._pixmap: Optional[QPixmap] = None
        self._photo_idx: int = 0
        self._photo_total: int = 0
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(QColor(30, 30, 30))

        self._view = _ClickableGraphicsView(self._scene)
        self._view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._view.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self._view.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._view.clicked.connect(self.toggle_zoom)
        layout.addWidget(self._view)

        # Status overlay at bottom
        self._status_label = QLabel("No image loaded")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet("color: #888; background: transparent;")
        layout.addWidget(self._status_label)

        self._pixmap_item = QGraphicsPixmapItem()
        self._pixmap_item.setTransformationMode(
            Qt.TransformationMode.SmoothTransformation
        )
        self._scene.addItem(self._pixmap_item)

    def set_photo_info(self, idx: int, total: int) -> None:
        """Call before set_pixmap/set_loading so the status line shows photo N/M."""
        self._photo_idx = idx
        self._photo_total = total

    def set_loading(self, loading: bool) -> None:
        if loading:
            self._pixmap = None
            self._pixmap_item.setPixmap(QPixmap())
            self._scene.setSceneRect(QRectF())
            self._status_label.setText(f"Loading…{self._photo_suffix()}")
        else:
            self._update_status()

    def set_pixmap(self, pixmap: QPixmap, size_label: str = "") -> None:
        """Display a new pixmap. size_label is e.g. 'original', 'large'."""
        self._pixmap = pixmap
        self._pixmap_item.setPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self._apply_zoom()
        sz = f"{pixmap.width()}×{pixmap.height()}"
        sl = f" ({size_label})" if size_label else ""
        self._status_label.setText(f"{sz}{sl}{self._photo_suffix()}")

    def clear(self) -> None:
        self._pixmap = None
        self._pixmap_item.setPixmap(QPixmap())
        self._scene.setSceneRect(QRectF())
        self._status_label.setText("No image")

    def toggle_zoom(self) -> None:
        self._fit_mode = not self._fit_mode
        self._apply_zoom()
        self.zoom_toggled.emit(not self._fit_mode)

    def set_fit_mode(self, fit: bool) -> None:
        self._fit_mode = fit
        self._apply_zoom()

    @property
    def is_fit_mode(self) -> bool:
        return self._fit_mode

    def _apply_zoom(self) -> None:
        if not self._pixmap or self._pixmap.isNull():
            return
        if self._fit_mode:
            self._view.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._view.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        else:
            self._view.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            self._view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self._view.resetTransform()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit_mode and self._pixmap and not self._pixmap.isNull():
            self._view.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._fit_mode and self._pixmap and not self._pixmap.isNull():
            self._view.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    def _photo_suffix(self) -> str:
        """Returns '  ·  photo N/M' when the observation has more than one photo."""
        if self._photo_total > 1:
            return f"  ·  photo {self._photo_idx + 1}/{self._photo_total}"
        return ""

    def _update_status(self) -> None:
        if self._pixmap and not self._pixmap.isNull():
            sz = f"{self._pixmap.width()}×{self._pixmap.height()}"
            mode = "fit" if self._fit_mode else "1:1"
            self._status_label.setText(f"{sz}  [{mode}]{self._photo_suffix()}")
        else:
            self._status_label.setText("No image")
