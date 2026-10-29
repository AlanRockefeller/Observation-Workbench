"""Explicit Super-key clipboard shortcuts for Qt text widgets on Linux."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent


def handle_super_clipboard_shortcut(widget: object, event: QKeyEvent) -> bool:
    """Run a clipboard action when Super/Meta is used instead of Ctrl.

    Qt's Linux standard key bindings normally recognize Ctrl+C/V but not the
    Super-key bindings used by this desktop. Keep ordinary Qt handling intact
    and translate only an otherwise-unmodified Meta shortcut.
    """
    if not sys.platform.startswith("linux"):
        return False
    modifiers = event.modifiers()
    if not modifiers & Qt.KeyboardModifier.MetaModifier:
        return False
    if modifiers & (
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
    ):
        return False
    action_name = {
        Qt.Key.Key_A: "selectAll",
        Qt.Key.Key_C: "copy",
        Qt.Key.Key_V: "paste",
        Qt.Key.Key_X: "cut",
    }.get(event.key())
    if action_name is None:
        return False
    action = getattr(widget, action_name, None)
    if not callable(action):
        return False
    action()
    event.accept()
    return True
