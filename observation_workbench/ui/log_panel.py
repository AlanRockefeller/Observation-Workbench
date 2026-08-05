"""
Debug log panel: a floating dock widget that shows live log messages.

Thread-safe: the handler emits a Qt signal so messages from background
threads are safely delivered to the main-thread QPlainTextEdit.

Usage:
    handler = QtLogHandler()
    logging.getLogger().addHandler(handler)
    panel = LogPanel(handler, parent=main_window)
    main_window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, panel)
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import (
    QDockWidget, QHBoxLayout, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)


class _LogSignals(QObject):
    message = Signal(str, int)  # formatted message, levelno


class QtLogHandler(logging.Handler):
    """
    A logging.Handler that safely delivers records to a Qt widget
    from any thread by routing through a queued signal.
    """

    def __init__(self) -> None:
        super().__init__()
        self.signals = _LogSignals()
        # Default format: time level name: message
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        self.setFormatter(fmt)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            # Signal emission is thread-safe; Qt delivers via queued connection
            self.signals.message.emit(msg, record.levelno)
        except Exception:
            self.handleError(record)


class LogPanel(QDockWidget):
    """Dockable log viewer. Pass a QtLogHandler to connect it."""

    MAX_LINES = 2000  # cap to avoid unbounded memory growth

    def __init__(
        self,
        handler: QtLogHandler,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__("Debug Log", parent)
        self.setObjectName("LogPanel")
        self.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(self.MAX_LINES)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._text.setStyleSheet(
            "QPlainTextEdit { font-family: monospace; font-size: 10px; "
            "background: #1a1a1a; color: #ddd; }"
        )
        layout.addWidget(self._text)

        btn_row = QHBoxLayout()
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(self._text.clear)
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.setWidget(container)

        # Connect handler's signal to our slot (queued by default for cross-thread)
        handler.signals.message.connect(self._append)

    def _append(self, msg: str, levelno: int) -> None:
        # Color-code by level
        if levelno >= logging.ERROR:
            color = "#ff6666"
        elif levelno >= logging.WARNING:
            color = "#ffcc66"
        elif levelno >= logging.DEBUG:
            color = "#888"
        else:
            color = "#ddd"
        # appendHtml is slightly heavier but gives us colors
        safe = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self._text.appendHtml(f'<span style="color:{color}">{safe}</span>')
        # Auto-scroll to bottom
        sb = self._text.verticalScrollBar()
        sb.setValue(sb.maximum())
