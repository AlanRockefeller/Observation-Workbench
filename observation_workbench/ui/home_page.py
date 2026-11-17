"""Start page shown in the main window until the first search is loaded.

Big, friendly entry points to every workflow so a new user is not left
staring at an empty photo viewer and a menu bar.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.ui.welcome_dialog import WelcomeFeature

CARD_STYLE = """
QWidget#homePageBody { background: palette(window); }
QPushButton[homeCard="true"] {
    text-align: left;
    border: 1px solid palette(mid);
    border-radius: 10px;
    background: palette(base);
    padding: 0px;
}
QPushButton[homeCard="true"]:hover,
QPushButton[homeCard="true"]:focus {
    border: 2px solid palette(highlight);
}
QPushButton[homeCard="true"]:pressed { background: palette(alternate-base); }
QPushButton[homeHero="true"] { border: 2px solid palette(highlight); }
"""


_CARD_MIN_WIDTH = 240


class CardButton(QPushButton):
    """A large clickable card (also used by the Identify setup dialog): a bold title, a sign-in badge, and a description.

    Child labels ignore the mouse so the whole card behaves as one button,
    including keyboard focus and Enter/Space activation.
    """

    def __init__(
        self,
        title: str,
        badge: str,
        description_html: str,
        *,
        hero: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("homeCard", True)
        if hero:
            self.setProperty("homeHero", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setToolTip(f"Open {title}")
        self.setAccessibleName(title)

        layout = QVBoxLayout(self)
        margin = 18 if hero else 14
        layout.setContentsMargins(margin, margin - 2, margin, margin)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(10)
        title_label = QLabel(html.escape(title))
        font = title_label.font()
        font.setPointSizeF(font.pointSizeF() * (1.6 if hero else 1.25))
        font.setBold(True)
        title_label.setFont(font)
        title_label.setWordWrap(True)
        header.addWidget(title_label, 1)
        badge_label = QLabel(f"<small>{html.escape(badge)}</small>")
        badge_label.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(badge_label, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        description = QLabel(description_html)
        description.setTextFormat(Qt.TextFormat.RichText)
        description.setWordWrap(True)
        layout.addWidget(description)
        layout.addStretch(1)

        for child in (title_label, badge_label, description):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._layout = layout
        self._title_label = title_label

    def set_title(self, title: str) -> None:
        self._title_label.setText(html.escape(title))
        self.setToolTip(f"Open {title}")
        self.setAccessibleName(title)
        self.updateGeometry()

    def sizeHint(self):  # noqa: N802 - Qt override
        # QPushButton ignores child layouts when sizing itself; ask the layout,
        # but never demand more width than a narrow window can give.
        width = max(self.width(), _CARD_MIN_WIDTH)
        return QSize(_CARD_MIN_WIDTH, self._layout.totalHeightForWidth(width))

    def minimumSizeHint(self):  # noqa: N802 - Qt override
        return self.sizeHint()

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return self._layout.totalHeightForWidth(width)


class HomePage(QWidget):
    """Start page with one card per workflow.

    ``study_requested`` asks the main window to focus its Identifier / URL box,
    since studying someone's identifications starts there rather than in a
    separate window.
    """

    study_requested = Signal()
    sign_in_requested = Signal()
    getting_started_requested = Signal()
    shortcuts_requested = Signal()
    back_to_results_requested = Signal()

    def __init__(
        self,
        *,
        identify: WelcomeFeature,
        study: WelcomeFeature,
        others: Sequence[WelcomeFeature],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        body = QWidget()
        body.setObjectName("homePageBody")
        body.setStyleSheet(CARD_STYLE)
        scroll.setWidget(body)

        # Center a column of readable width inside whatever space we get.
        centering = QHBoxLayout(body)
        centering.setContentsMargins(24, 20, 24, 24)
        centering.addStretch(1)
        column_widget = QWidget()
        column_widget.setMaximumWidth(1100)
        column_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        centering.addWidget(column_widget, 20)
        centering.addStretch(1)

        column = QVBoxLayout(column_widget)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)

        self._back_link = QLabel("<a href='back'>← Back to your results</a>")
        self._back_link.setTextFormat(Qt.TextFormat.RichText)
        self._back_link.linkActivated.connect(self._on_link)
        self._back_link.setVisible(False)
        column.addWidget(self._back_link)

        title = QLabel("What would you like to do?")
        title_font = title.font()
        title_font.setPointSizeF(title_font.pointSizeF() * 1.9)
        title_font.setBold(True)
        title.setFont(title_font)
        column.addWidget(title)

        self._auth_status = QLabel("")
        self._auth_status.setTextFormat(Qt.TextFormat.RichText)
        self._auth_status.setWordWrap(True)
        self._auth_status.linkActivated.connect(self._on_link)
        column.addWidget(self._auth_status)

        hero = CardButton(
            identify.title,
            identify.sign_in,
            "<span style='font-size:large'>A better iNaturalist Identify page.</span>"
            "<ul style='margin-top:4px; margin-bottom:0'>"
            "<li><b>Faster:</b> the next observations' full-size photos load in the "
            "background while you work, so pressing → is instant.</li>"
            "<li><b>Full-resolution photos</b> that fill the screen, with 1:1 zoom "
            "and brightness controls.</li>"
            "<li><b>One-key actions:</b> A agree, I add ID, C comment, R reviewed, "
            "F favorite.</li>"
            "<li>Start from any iNaturalist Identify or observations web address; "
            "the order never shuffles underneath you.</li>"
            "</ul>",
            hero=True,
        )
        if identify.open_feature is not None:
            hero.clicked.connect(lambda _checked=False: identify.open_feature())
        column.addWidget(hero)

        study_card = CardButton(
            study.title,
            study.sign_in,
            f"{html.escape(study.why)}<br><b>Start by typing a username or pasting "
            "an iNaturalist link in the box at the top of this window, then click "
            "Load.</b>",
        )
        study_card.clicked.connect(lambda _checked=False: self.study_requested.emit())
        column.addWidget(study_card)

        more = QLabel("More tools")
        more_font = more.font()
        more_font.setPointSizeF(more_font.pointSizeF() * 1.3)
        more_font.setBold(True)
        more.setFont(more_font)
        column.addSpacing(6)
        column.addWidget(more)
        column.addWidget(
            _wrapped(
                "The bulk tools always show you a preview and ask before changing "
                "anything on iNaturalist."
            )
        )

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        columns = 2
        for index, feature in enumerate(others):
            card = CardButton(feature.title, feature.sign_in, html.escape(feature.why))
            if feature.open_feature is not None:
                card.clicked.connect(
                    lambda _checked=False, cb=feature.open_feature: cb()
                )
            else:
                card.setToolTip(feature.where)
                card.clicked.connect(
                    lambda _checked=False, where=feature.where: self._show_where(where)
                )
            grid.addWidget(card, index // columns, index % columns)
        for c in range(columns):
            grid.setColumnStretch(c, 1)
        column.addLayout(grid)

        footer = _wrapped(
            "<a href='getting-started'>Getting started guide</a> &nbsp;·&nbsp; "
            "<a href='shortcuts'>Keyboard shortcuts</a> &nbsp;·&nbsp; "
            "Everything here is also in the <b>Action</b> menu."
        )
        footer.linkActivated.connect(self._on_link)
        column.addWidget(footer)
        self._where_hint = _wrapped("")
        column.addWidget(self._where_hint)
        column.addStretch(1)

    def set_auth_status(self, login: str, signed_in: bool, expired: bool) -> None:
        if signed_in and not expired:
            text = f"Signed in to iNaturalist as <b>{html.escape(login)}</b>."
        elif signed_in:
            text = (
                f"Your iNaturalist sign-in for <b>{html.escape(login)}</b> has "
                "expired. <a href='sign-in'>Sign in again</a> to make changes on "
                "iNaturalist."
            )
        else:
            text = (
                "You are not signed in. Looking at observations works without "
                "signing in; <a href='sign-in'>sign in to iNaturalist</a> to add "
                "identifications, agree, or comment."
            )
        self._auth_status.setText(text)

    def set_results_available(self, available: bool) -> None:
        """Offer a way back to loaded results when the page is reopened."""
        self._back_link.setVisible(available)

    def _show_where(self, where: str) -> None:
        self._where_hint.setText(f"<i>Find this in the menus: {html.escape(where)}</i>")

    def _on_link(self, link: str) -> None:
        if link == "sign-in":
            self.sign_in_requested.emit()
        elif link == "getting-started":
            self.getting_started_requested.emit()
        elif link == "shortcuts":
            self.shortcuts_requested.emit()
        elif link == "back":
            self.back_to_results_requested.emit()


def _wrapped(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setWordWrap(True)
    return label
