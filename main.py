"""
Observation Workbench — entry point.

Usage:
    python main.py              # normal mode
    python main.py --debug      # verbose logging + log panel shown on start
    python main.py -v           # same as --debug
    python main.py --skipagree  # skip bulk provisional-ID preview confirmation
"""
import argparse
import logging
import os
import signal
import sys
from pathlib import Path

# Ensure the package directory is on the path when running directly
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtCore import QLockFile, QTimer, QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QMessageBox

from observation_workbench.storage.settings import ORG_NAME, AppSettings
from observation_workbench.ui.hidpi import ensure_scale_factor
from observation_workbench.ui.log_panel import QtLogHandler
from observation_workbench.ui.main_window import MainWindow

_INSTANCE_LOCK: QLockFile | None = None


def main() -> None:
    global _INSTANCE_LOCK
    parser = argparse.ArgumentParser(description="Observation Workbench")
    parser.add_argument(
        "--debug", "-v", action="store_true",
        help="Enable verbose (DEBUG) logging and show the log panel on startup",
    )
    parser.add_argument(
        "--skipagree",
        action="store_true",
        help="Skip the bulk provisional-ID preview dialog and AGREE confirmation",
    )
    args, qt_args = parser.parse_known_args()

    log_level = logging.DEBUG if args.debug else logging.INFO

    # Configure root logger — messages also go to stderr for terminal users
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    log = logging.getLogger(__name__)
    log.debug(
        "Starting Observation Workbench (debug=%s, skipagree=%s)",
        args.debug,
        args.skipagree,
    )

    if not args.debug:
        # Qt6 uses an embedded Chromium layer for platform services (credential
        # storage etc.).  On non-KDE Linux systems this produces a flood of
        # KWallet / D-Bus ERROR lines written directly to C-level stderr —
        # they bypass Python logging and cannot be filtered there.
        #
        # --password-store=basic  → don't attempt KWallet/keychain lookups at all
        # --log-level=3           → only FATAL Chromium messages reach stderr
        #
        # Must be set before QApplication so Qt picks them up at Chromium init.
        _extra = "--password-store=basic --log-level=3"
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") + " " + _extra
        ).strip()

        # Suppress Qt's ICC profile parse warnings (qt.gui.icc: fromIccProfile:
        # Failed to parse description).  These are emitted by Qt's C-level
        # categorised logger and bypass Python logging entirely.
        _rules = "qt.gui.icc.warning=false"
        existing_rules = os.environ.get("QT_LOGGING_RULES", "")
        os.environ["QT_LOGGING_RULES"] = (
            existing_rules + ";" + _rules if existing_rules else _rules
        )

        # Filter non-categorized qWarning() messages that can't be silenced
        # via QT_LOGGING_RULES.  Unmatched messages are forwarded to stderr
        # to preserve normal Qt error reporting.
        _SUPPRESSED = frozenset([
            "This plugin supports grabbing the mouse only for popup windows",
            "Opening in existing browser session.",
        ])

        def _qt_message_filter(msg_type, _context, message):
            if message in _SUPPRESSED:
                return
            print(message, file=sys.stderr)

        qInstallMessageHandler(_qt_message_filter)

    # HiDPI sizing.  Both of these are read while the QApplication is being
    # constructed, so they must be settled first.  start.sh sets them too; the
    # helpers below leave any existing value alone, so running `python main.py`
    # directly gets the same sizing as launching through the script.
    ensure_scale_factor()
    # Pin the logical font DPI: WSLg re-negotiates the virtual display on
    # suspend/resume and can report a different DPI afterwards, which makes all
    # text change size mid-session.
    os.environ.setdefault("QT_FONT_DPI", "96")

    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName("Observation Workbench")
    app.setOrganizationName(ORG_NAME)
    app.setStyle("Fusion")
    _apply_dark_palette(app)

    # The Identify journal has intentionally conservative crash recovery but
    # no cross-process row lease.  Hold a lock beside the configured journal
    # for the full application lifetime so another live process can never
    # reclassify a write that this process still owns.
    try:
        journal_dir = AppSettings().cache_dir
        journal_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        QMessageBox.critical(
            None,
            "Cannot Start Observation Workbench",
            f"The application cache directory could not be prepared:\n{exc}",
        )
        return
    instance_lock = QLockFile(str(journal_dir / "observation-workbench-instance.lock"))
    if not instance_lock.tryLock(0):
        QMessageBox.warning(
            None,
            "Observation Workbench Is Already Running",
            "Close the other Observation Workbench instance before starting another one.",
        )
        return
    # Keep the lock module-global so it is not released while Python and Qt
    # are still tearing down worker threads after the event loop returns.
    _INSTANCE_LOCK = instance_lock

    # Make Ctrl+C in the terminal quit the app gracefully (triggers closeEvent).
    # Qt replaces Python's SIGINT handler; we restore it here so the signal
    # reaches Python. A periodic no-op timer is required because Qt's event
    # loop only checks Python signals when it yields back to the interpreter.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    _sigint_timer = QTimer()
    _sigint_timer.start(200)
    _sigint_timer.timeout.connect(lambda: None)

    # Qt log handler (for the in-app log panel)
    qt_handler = QtLogHandler()
    qt_handler.setLevel(log_level)
    logging.getLogger().addHandler(qt_handler)

    window = MainWindow(skip_agree_confirmation=args.skipagree)
    window.setup_log_panel(qt_handler)

    if args.debug:
        # Show the log panel immediately in debug mode
        window._log_panel.show()
        window._act_show_log.setChecked(True)

    # Fill the available screen by default; the window stays resizable and
    # unmaximizes to a size computed from the actual screen geometry.
    window.showMaximized()
    log.debug("Window ready.")
    sys.exit(app.exec())


def _apply_dark_palette(app: QApplication) -> None:
    from PySide6.QtGui import QColor, QPalette
    palette = QPalette()
    dark = QColor(45, 45, 45)
    darker = QColor(30, 30, 30)
    text = QColor(220, 220, 220)
    highlight = QColor(42, 130, 218)
    disabled = QColor(120, 120, 120)

    palette.setColor(QPalette.ColorRole.Window, dark)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, darker)
    palette.setColor(QPalette.ColorRole.AlternateBase, dark)
    palette.setColor(QPalette.ColorRole.ToolTipBase, dark)
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, dark)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, QColor("red"))
    palette.setColor(QPalette.ColorRole.Link, QColor(42, 160, 218))
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    app.setPalette(palette)

    # Fusion style on Linux falls back to the platform theme for input widgets
    # (QLineEdit, QTextEdit, QTableWidget, etc.), rendering black text on a dark
    # background.  An explicit stylesheet overrides the platform default and
    # ensures all input-type widgets use the same dark-theme colours as the rest
    # of the app.
    app.setStyleSheet("""
        QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox,
        QDateEdit, QDateTimeEdit {
            color: #dcdcdc;
            background-color: #1e1e1e;
            border: 1px solid #555;
            border-radius: 3px;
        }
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
        QSpinBox:focus, QDoubleSpinBox:focus,
        QDateEdit:focus, QDateTimeEdit:focus {
            border: 1px solid #2a82da;
        }
        QLineEdit:disabled, QTextEdit:disabled {
            color: #787878;
            background-color: #2d2d2d;
        }
        QTableWidget, QTableView {
            color: #dcdcdc;
            background-color: #1e1e1e;
            gridline-color: #444;
        }
        QHeaderView::section {
            color: #dcdcdc;
            background-color: #2d2d2d;
            border: 1px solid #444;
            padding: 2px 4px;
        }
        QTableCornerButton::section {
            background-color: #2d2d2d;
            border: 1px solid #444;
        }
        QComboBox {
            color: #dcdcdc;
            background-color: #1e1e1e;
            border: 1px solid #555;
            border-radius: 3px;
        }
        QComboBox QAbstractItemView {
            color: #dcdcdc;
            background-color: #1e1e1e;
            selection-background-color: #2a82da;
            selection-color: white;
        }
    """)


if __name__ == "__main__":
    main()
