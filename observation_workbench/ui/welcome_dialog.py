"""Startup "Getting started" window: sign-in status, first steps, feature tour."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True)
class WelcomeFeature:
    title: str
    why: str
    where: str
    sign_in: str  # short badge, e.g. "Needs sign-in"
    open_feature: Optional[Callable[[], None]] = None


def _wrapped(text: str, *, rich: bool = True) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    if rich:
        label.setTextFormat(Qt.TextFormat.RichText)
    return label


def _section_title(text: str) -> QLabel:
    label = QLabel(f"<h3 style='margin:0'>{text}</h3>")
    label.setTextFormat(Qt.TextFormat.RichText)
    return label


class WelcomeDialog(QDialog):
    """Explains what the program is for and whether/how to sign in.

    Buttons that launch a workflow close this window first and then run the
    callback from the event loop, so the launched window is never parented
    under a dialog that is about to disappear.
    """

    def __init__(
        self,
        *,
        login: str,
        signed_in: bool,
        token_expired: bool,
        features: Sequence[WelcomeFeature],
        on_sign_in: Callable[[], None],
        on_show_readme: Callable[[], None],
        show_at_startup: bool,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Getting started — Observation Workbench")
        self.resize(820, 760)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(10)

        heading = _wrapped(
            "<h2 style='margin:0'>Welcome to Observation Workbench</h2>"
            "A desktop companion to iNaturalist for studying expert identifications, "
            "identifying quickly with full-size photos, and carefully supervised "
            "bulk fixes to identifications — especially for DNA-barcoded fungi."
        )
        outer.addWidget(heading)

        outer.addWidget(
            self._build_sign_in_box(login, signed_in, token_expired, on_sign_in)
        )

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 8, 0)
        body_layout.setSpacing(10)

        body_layout.addWidget(
            _section_title("Quick start: look through someone's identifications")
        )
        body_layout.addWidget(
            _wrapped(
                "<ol style='margin-top:0'>"
                "<li>In the <b>Identifier / URL</b> box at the top of the main window, "
                "type an iNaturalist username (for example an expert in a group you "
                "are learning), <i>or</i> paste the web address of any iNaturalist "
                "observation search or project.</li>"
                "<li>Optionally narrow it with <b>Place</b> and <b>Taxon</b>.</li>"
                "<li>Click <b>Load</b>.</li>"
                "<li>Use the <b>→</b> and <b>←</b> arrow keys to move between "
                "observations and <b>↑</b> / <b>↓</b> to flip through photos. Press "
                "<b>O</b> to open the current observation on the iNaturalist website.</li>"
                "</ol>"
                "No sign-in is needed for this."
            )
        )

        body_layout.addWidget(_section_title("Everything else the program can do"))
        body_layout.addWidget(
            _wrapped(
                "All of these are also in the <b>Action</b> menu. The bulk tools "
                "always show you a preview and ask before changing anything on "
                "iNaturalist."
            )
        )
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        for row, feature in enumerate(features):
            text = _wrapped(
                f"<b>{feature.title}</b> &nbsp;<small>({feature.sign_in})</small><br>"
                f"{feature.why}<br><small><i>{feature.where}</i></small>"
            )
            grid.addWidget(text, row, 0)
            if feature.open_feature is not None:
                button = QPushButton("Open")
                button.setToolTip(f"Close this window and open {feature.title}")
                button.clicked.connect(
                    lambda _checked=False, cb=feature.open_feature: self._close_then(cb)
                )
                grid.addWidget(button, row, 1, Qt.AlignmentFlag.AlignTop)
        grid.setColumnStretch(0, 1)
        body_layout.addLayout(grid)
        body_layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        footer = QHBoxLayout()
        self._show_at_startup = QCheckBox("Show this window when the program starts")
        self._show_at_startup.setChecked(show_at_startup)
        footer.addWidget(self._show_at_startup)
        footer.addStretch(1)
        readme = QPushButton("Full instructions (Readme)")
        readme.clicked.connect(lambda _checked=False: self._close_then(on_show_readme))
        footer.addWidget(readme)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        outer.addLayout(footer)

        footnote = _wrapped(
            "<small>You can reopen this window any time from <b>Help → Getting "
            "started</b>.</small>"
        )
        outer.addWidget(footnote)

    @property
    def show_at_startup(self) -> bool:
        return self._show_at_startup.isChecked()

    def _build_sign_in_box(
        self,
        login: str,
        signed_in: bool,
        token_expired: bool,
        on_sign_in: Callable[[], None],
    ) -> QFrame:
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(12, 10, 12, 10)
        login = html.escape(login)

        if signed_in and not token_expired:
            status = (
                f"<b>✔ Signed in to iNaturalist as {login}.</b><br>"
                "You can use every feature. Sign-ins last about 24 hours; the "
                "program will tell you when you need to sign in again."
            )
            button_text = "Sign in as someone else…"
        else:
            if signed_in and token_expired:
                first = f"<b>Your iNaturalist sign-in for {login} has expired.</b>"
            else:
                first = "<b>You are not signed in to iNaturalist.</b>"
            status = (
                f"{first}<br>"
                "<b>Do I need to sign in?</b> Only if you want this program to make "
                "changes on iNaturalist for you — add or agree with identifications, "
                "comment, mark observations reviewed, or edit observation fields. "
                "Just looking at observations and photos works without signing in."
                "<br><br>"
                "Signing in takes about a minute: the button opens iNaturalist in "
                "your web browser, you copy a code from that page, and paste it back "
                "here. The sign-in window walks you through each step."
            )
            button_text = "Sign in to iNaturalist…"

        layout.addWidget(_wrapped(status), 1)
        button = QPushButton(button_text)
        if not signed_in or token_expired:
            button.setDefault(True)
        button.clicked.connect(lambda _checked=False: self._close_then(on_sign_in))
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        return box

    def _close_then(self, callback: Callable[[], None]) -> None:
        self.accept()
        QTimer.singleShot(0, callback)
