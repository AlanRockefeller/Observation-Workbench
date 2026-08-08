"""Identity-aware image panel used only by the read-only Identify window."""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.services.prefetcher import ImageFailure

PhotoPanelState = Literal[
    "no_photo",
    "loading_new_photo",
    "loaded",
    "upgrading_same_photo",
    "failed_current_photo",
]


class _PhotoView(QGraphicsView):
    clicked = Signal(object)
    double_clicked = Signal()
    zoom_requested = Signal(float)

    def __init__(self, scene: QGraphicsScene, parent: QWidget | None = None) -> None:
        super().__init__(scene, parent)
        self._press_position: QPoint | None = None
        self._suppress_next_click = False
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_position = event.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        should_emit = (
            event.button() == Qt.MouseButton.LeftButton
            and self._press_position is not None
            and (event.pos() - self._press_position).manhattanLength() < 5
            and not self._suppress_next_click
        )
        self._press_position = None
        if self._suppress_next_click:
            self._suppress_next_click = False
        super().mouseReleaseEvent(event)
        if should_emit:
            self.clicked.emit(event.pos())

    def mouseDoubleClickEvent(self, event) -> None:
        self._suppress_next_click = True
        self._press_position = None
        self.double_clicked.emit()
        event.accept()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta:
            self.zoom_requested.emit(1.18 if delta > 0 else 1 / 1.18)
        event.accept()


class IdentifyPhotoPanel(QWidget):
    retry_requested = Signal()
    details_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state: PhotoPanelState = "no_photo"
        self._photo_id: int | None = None
        self._pixmap: QPixmap | None = None
        self._is_fitted = True
        self._brightness = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(QColor(25, 25, 25))
        self._view = _PhotoView(self._scene, self)
        self._view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._view.clicked.connect(self._handle_click)
        self._view.double_clicked.connect(self.fit)
        self._view.zoom_requested.connect(self.zoom)

        self._pixmap_item = QGraphicsPixmapItem()
        self._scene.addItem(self._pixmap_item)
        self._brightness_overlay = QGraphicsRectItem()
        self._brightness_overlay.setZValue(1)
        self._scene.addItem(self._brightness_overlay)

        self._status = QLabel("No photographs")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet("color: #bbb; background: #222")

        status_row = QHBoxLayout()
        self._upgrade_retry_button = QPushButton("Retry original", self)
        self._upgrade_retry_button.clicked.connect(self.retry_requested)
        self._upgrade_retry_button.hide()
        self._image_details_button = QPushButton("Details…", self)
        self._image_details_button.clicked.connect(self.details_requested)
        self._image_details_button.hide()
        status_row.addWidget(self._status, 1)
        status_row.addWidget(self._upgrade_retry_button)
        status_row.addWidget(self._image_details_button)

        self._failure_banner = QWidget(self)
        failure_layout = QHBoxLayout(self._failure_banner)
        failure_layout.setContentsMargins(6, 2, 6, 2)
        self._failure_label = QLabel()
        self._retry_button = QPushButton("Retry")
        self._details_button = QPushButton("Details…")
        self._retry_button.clicked.connect(self.retry_requested)
        self._details_button.clicked.connect(self.details_requested)
        failure_layout.addWidget(self._failure_label, 1)
        failure_layout.addWidget(self._retry_button)
        failure_layout.addWidget(self._details_button)
        self._failure_banner.hide()

        layout.addWidget(self._view, 1)
        layout.addLayout(status_row)
        layout.addWidget(self._failure_banner)

    @property
    def current_photo_id(self) -> int | None:
        return self._photo_id

    @property
    def has_pixmap(self) -> bool:
        return self._pixmap is not None and not self._pixmap.isNull()

    @property
    def is_fitted(self) -> bool:
        return self._is_fitted

    def begin_photo(self, photo_id: int) -> None:
        """Begin a distinct photo identity; no previous image may remain visible."""
        if self._photo_id == photo_id:
            return
        self._photo_id = photo_id
        self._pixmap = None
        self._state = "loading_new_photo"
        self._reset_scene()
        self.clear_failure()
        self._status.setText("Loading…")

    def show_loading(self, photo_id: int) -> None:
        if photo_id != self._photo_id:
            self.begin_photo(photo_id)
        if self._photo_id != photo_id:
            return
        if self.has_pixmap:
            self._state = "upgrading_same_photo"
            if "Loading upgrade…" not in self._status.text():
                self._status.setText(f"{self._image_description()} · Loading upgrade…")
        else:
            self._state = "loading_new_photo"
            self._status.setText("Loading…")

    def set_photo(
        self,
        photo_id: int,
        pixmap: QPixmap,
        size: str,
        *,
        preserve_view: bool = False,
    ) -> None:
        if photo_id != self._photo_id or pixmap.isNull():
            return

        old_bounds = self._scene.sceneRect()
        old_center = self._view.mapToScene(self._view.viewport().rect().center())
        old_scale = self._view.transform().m11()
        preserve_manual_view = preserve_view and self.has_pixmap and not self._is_fitted

        self._pixmap = pixmap
        self._pixmap_item.setPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self._update_brightness_overlay()
        self.clear_failure()
        self._state = "loaded"
        self._status.setText(self._image_description(size))

        if preserve_manual_view and old_bounds.width() > 0 and old_bounds.height() > 0:
            new_bounds = self._scene.sceneRect()
            center_x = old_center.x() / old_bounds.width() * new_bounds.width()
            center_y = old_center.y() / old_bounds.height() * new_bounds.height()
            scale_ratio = new_bounds.width() / old_bounds.width()
            target_scale = _clamp_scale(old_scale / max(scale_ratio, 0.0001))
            self._view.resetTransform()
            self._view.scale(target_scale, target_scale)
            self._view.centerOn(center_x, center_y)
            self._is_fitted = False
            self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        else:
            self.fit()

    def show_failure(self, photo_id: int, failure: ImageFailure) -> None:
        if photo_id != self._photo_id:
            return
        self._state = "failed_current_photo"
        self._failure_label.setText(failure.message)
        self._retry_button.setText("Retry")
        self._retry_button.setEnabled(True)
        self._details_button.setEnabled(True)
        self._upgrade_retry_button.hide()
        self._image_details_button.hide()
        self._failure_banner.show()
        if not self.has_pixmap:
            self._status.setText("Image unavailable")

    def show_partial_details(self, photo_id: int) -> None:
        """Expose failed higher-quality attempts without treating a fallback as failed."""
        if photo_id != self._photo_id or not self.has_pixmap:
            return
        self._failure_banner.hide()
        self._retry_button.setText("Retry")
        self._retry_button.setEnabled(True)
        self._upgrade_retry_button.setEnabled(True)
        self._upgrade_retry_button.show()
        self._image_details_button.show()

    def show_retrying(self, photo_id: int) -> None:
        """Keep an existing fallback visible while an explicit retry is active."""
        if photo_id != self._photo_id:
            return
        self._state = "upgrading_same_photo" if self.has_pixmap else "loading_new_photo"
        self._retry_button.setText("Retrying image…")
        self._retry_button.setEnabled(False)
        self._details_button.setEnabled(True)
        self._upgrade_retry_button.setEnabled(False)
        self._failure_label.setText("Retrying image…")
        self._failure_banner.show()
        if not self.has_pixmap:
            self._status.setText("Retrying image…")

    def show_no_photo(self) -> None:
        """Represent a legitimate no-photo observation without a failure banner."""
        self._photo_id = None
        self._pixmap = None
        self._state = "no_photo"
        self._reset_scene()
        self.clear_failure()
        self._status.setText("No photographs")

    def clear_failure(self) -> None:
        self._failure_label.clear()
        self._failure_banner.hide()
        self._retry_button.setText("Retry")
        self._retry_button.setEnabled(True)
        self._upgrade_retry_button.hide()
        self._image_details_button.hide()

    def fit(self) -> None:
        if self.has_pixmap:
            self._view.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._is_fitted = True
        self._view.setDragMode(QGraphicsView.DragMode.NoDrag)

    def toggle_zoom(self) -> None:
        if not self.has_pixmap:
            return
        if not self._is_fitted:
            self.fit()
            return
        self._view.resetTransform()
        self._view.centerOn(self._pixmap_item.boundingRect().center())
        self._is_fitted = False
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def zoom(self, factor: float) -> None:
        if not self.has_pixmap or factor <= 0:
            return
        current_scale = self._view.transform().m11()
        target_scale = _clamp_scale(current_scale * factor)
        if current_scale <= 0:
            return
        self._view.scale(target_scale / current_scale, target_scale / current_scale)
        self._is_fitted = False
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def set_brightness(self, value: int) -> None:
        self._brightness = max(-10, min(10, int(value)))
        self._update_brightness_overlay()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._is_fitted:
            self.fit()

    def _handle_click(self, position: QPoint) -> None:
        if not self.has_pixmap:
            return
        if not self._is_fitted:
            self.fit()
            return
        point = self._view.mapToScene(position)
        self._view.resetTransform()
        self._view.centerOn(point)
        self._is_fitted = False
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def _reset_scene(self) -> None:
        self._pixmap_item.setPixmap(QPixmap())
        self._scene.setSceneRect(QRectF())
        self._brightness = 0
        self._brightness_overlay.setRect(QRectF())
        self._brightness_overlay.setBrush(Qt.BrushStyle.NoBrush)
        self._brightness_overlay.setPen(Qt.PenStyle.NoPen)
        self._view.resetTransform()
        self._view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._is_fitted = True

    def _update_brightness_overlay(self) -> None:
        if not self.has_pixmap or self._brightness == 0:
            self._brightness_overlay.setRect(QRectF())
            self._brightness_overlay.setBrush(Qt.BrushStyle.NoBrush)
            self._brightness_overlay.setPen(Qt.PenStyle.NoPen)
            return
        alpha = min(180, abs(self._brightness) * 18)
        color = (
            QColor(255, 255, 255, alpha)
            if self._brightness > 0
            else QColor(0, 0, 0, alpha)
        )
        self._brightness_overlay.setRect(self._scene.sceneRect())
        self._brightness_overlay.setBrush(color)
        self._brightness_overlay.setPen(Qt.PenStyle.NoPen)

    def _image_description(self, size: str | None = None) -> str:
        if not self.has_pixmap:
            return "Loading…"
        prefix = size.title() if size else self._status.text().split(" · ", 1)[0]
        return f"{prefix} · {self._pixmap.width()}×{self._pixmap.height()}"


def _clamp_scale(value: float) -> float:
    return max(0.10, min(8.0, value))
