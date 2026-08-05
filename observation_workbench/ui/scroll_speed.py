"""Application-wide mouse-wheel scroll speed adjustment."""
from __future__ import annotations

from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSlider,
    QAbstractSpinBox,
    QComboBox,
    QWidget,
)


class ScrollSpeedFilter(QObject):
    """Scale wheel scrolling for Qt scroll areas.

    Windows wheel-line preferences are not reliably reflected in WSL/X11 Qt
    sessions, so this applies an app-local multiplier to scrollable widgets.
    """

    def __init__(self, multiplier: float = 1.0, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._multiplier = 1.0
        self.set_multiplier(multiplier)

    def set_multiplier(self, multiplier: float) -> None:
        try:
            value = float(multiplier)
        except (TypeError, ValueError):
            value = 1.0
        self._multiplier = max(0.25, min(10.0, value))

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.Wheel or self._multiplier == 1.0:
            return False
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False

        area = self._scroll_area_for(obj)
        if area is None:
            return False

        bar = area.verticalScrollBar()
        if bar.maximum() <= bar.minimum():
            return False

        pixel_delta = event.pixelDelta().y()
        if pixel_delta:
            amount = pixel_delta * self._multiplier
        else:
            angle_delta = event.angleDelta().y()
            if not angle_delta:
                return False
            notches = angle_delta / 120.0
            base_pixels = max(1, bar.singleStep() * 3)
            amount = notches * base_pixels * self._multiplier

        bar.setValue(bar.value() - int(round(amount)))
        event.accept()
        return True

    # Controls that use the mouse wheel for their own purpose (changing a
    # value or selection). The wheel must reach them rather than being
    # redirected to an enclosing scroll area.
    _WHEEL_CONSUMING = (QAbstractSpinBox, QComboBox, QAbstractSlider)

    @classmethod
    def _scroll_area_for(cls, obj: QObject) -> QAbstractScrollArea | None:
        widget = obj if isinstance(obj, QWidget) else None
        while widget is not None:
            if isinstance(widget, cls._WHEEL_CONSUMING):
                return None
            if isinstance(widget, QAbstractScrollArea):
                return widget
            widget = widget.parentWidget()
        return None
