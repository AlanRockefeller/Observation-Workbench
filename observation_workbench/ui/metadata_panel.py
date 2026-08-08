"""
Metadata panel: displays observation and identification details.

Shows:
- Target identifier's taxon (prominent)
- Community taxon
- Observer, date, place
- ID body/comment
- Leading/disagreement indicators
- Observation URL (clickable)
"""

from __future__ import annotations

import html
import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from observation_workbench.models import StudyObservation
from observation_workbench.ui.external_links import open_external_url_silently

log = logging.getLogger(__name__)


class _Section(QWidget):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(2)
        lbl = QLabel(f"<b>{title}</b>")
        lbl.setStyleSheet("color: #999;")
        layout.addWidget(lbl)
        self._content_layout = layout

    def add_row(self, label: str, value: str, big: bool = False) -> QLabel:
        if label:
            lbl = QLabel(f"<span style='color:#888'>{label}:</span> {value}")
        else:
            lbl = QLabel(value)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        lbl.linkActivated.connect(open_external_url_silently)
        if big:
            f = lbl.font()
            f.setPointSize(f.pointSize() + 3)
            f.setBold(True)
            lbl.setFont(f)
        self._content_layout.addWidget(lbl)
        return lbl


class MetadataPanel(QWidget):
    """Right-side metadata panel. Call update_observation() to populate."""

    open_obs_requested = Signal()
    copy_obs_url_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._show_photo_info = False  # set True to render per-photo metadata
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(8)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Placeholder
        self._placeholder = QLabel("Load results and select one to see details.")
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet("color: #999;")
        self._layout.addWidget(self._placeholder)

        scroll.setWidget(container)
        outer.addWidget(scroll)

        # Observation action buttons
        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 4, 0, 0)
        button_row.setSpacing(4)

        self._open_btn = QPushButton("(O)pen Observation")
        self._open_btn.setToolTip("Open the current observation on iNaturalist.org")
        self._open_btn.clicked.connect(self.open_obs_requested)
        self._open_btn.setEnabled(False)
        button_row.addWidget(self._open_btn, 1)

        self._copy_url_btn = QPushButton("Copy Obs URL")
        self._copy_url_btn.setToolTip(
            "Copy the current observation URL to the clipboard"
        )
        self._copy_url_btn.clicked.connect(self.copy_obs_url_requested)
        self._copy_url_btn.setEnabled(False)
        button_row.addWidget(self._copy_url_btn, 1)

        outer.addLayout(button_row)

    def update_observation(
        self,
        obs: StudyObservation,
        photo_idx: int = 0,
        authenticated_login: str = "",
        pending_target_taxon: str = "",
    ) -> None:
        """Populate metadata panel with observation data."""
        self._clear()
        self._open_btn.setEnabled(True)
        self._copy_url_btn.setEnabled(True)

        # Identification taxon (most prominent)
        ident = obs.target_identification
        if ident:
            taxon = ident.taxon
            s = _Section("Identifier's Taxon")
            s.add_row("", _escape(taxon.display_name), big=True)
            if taxon.rank:
                s.add_row("Rank", _escape(taxon.rank))
            indicators = []
            if ident.is_leading:
                indicators.append("★ Leading ID")
            if ident.is_disagreement:
                indicators.append("⚡ Disagrees with observation taxon")
            if ident.is_provisional:
                indicators.append("Provisional name")
            if indicators:
                s.add_row("", " · ".join(indicators))
            if ident.body:
                s.add_row("Comment", _escape(ident.body))
            if ident.created_at:
                s.add_row("ID date", ident.created_at[:10])
            self._layout.addWidget(s)
            self._layout.addWidget(_divider())

        # Community taxon
        community = obs.community_taxon
        obs_taxon = obs.taxon
        s2 = _Section("Observation")
        if community:
            s2.add_row(
                "Community ID", _taxon_html(community.display_name, community.name)
            )
        elif obs_taxon:
            s2.add_row(
                "Obs. taxon", _taxon_html(obs_taxon.display_name, obs_taxon.name)
            )
        s2.add_row("Observer", _escape(obs.observer_login))
        if obs.observed_on:
            s2.add_row("Observed", _escape(obs.display_date))
        if obs.place_guess:
            s2.add_row("Place", _escape(obs.place_guess))
        if obs.quality_grade:
            grade = obs.quality_grade.replace("_", " ").title()
            if obs.quality_grade == "research":
                grade = f"<span style='color:#66aa66'>{grade}</span>"
            s2.add_row("Quality", grade)
        if obs.provisional_species_name:
            s2.add_row(
                "Provisional Species Name", _escape(obs.provisional_species_name)
            )
        if obs.species_name_override:
            s2.add_row("Species Name Override", _escape(obs.species_name_override))
        if obs.num_identification_agreements or obs.num_identification_disagreements:
            a = obs.num_identification_agreements
            d = obs.num_identification_disagreements
            s2.add_row("Agreements", f"{a} agree / {d} disagree")
        if obs.obscured:
            s2.add_row("", "🔒 Location obscured")
        self._layout.addWidget(s2)

        if pending_target_taxon:
            self._layout.addWidget(_divider())
            pending = _Section("Pending Agreement")
            pending.add_row("", f"<b>{_escape(pending_target_taxon)}</b>")
            self._layout.addWidget(pending)

        self._layout.addWidget(_divider())
        ids = _Section("Identification History")
        idents = list(obs.all_identifications)
        idents.sort(key=lambda i: (i.created_at or "", i.ident_id), reverse=True)
        if not idents:
            ids.add_row(
                "",
                "<span style='color:#888'>No identification history in payload.</span>",
            )
        for idx, item in enumerate(idents):
            ids.add_row("", _ident_html(item, idx == 0, authenticated_login))
        self._layout.addWidget(ids)

        self._layout.addWidget(_divider())
        comments = _Section("Observation Comments")
        if not obs.comments:
            comments.add_row(
                "", "<span style='color:#888'>No comments in payload.</span>"
            )
        for comment in obs.comments:
            comments.add_row("", _comment_html(comment))
        self._layout.addWidget(comments)

        # Observation URL
        self._layout.addWidget(_divider())
        s4 = _Section("Links")
        s4.add_row("", f'<a href="{obs.url}">{obs.url}</a>')
        self._layout.addWidget(s4)

        self._layout.addStretch()

    def clear(self) -> None:
        self._clear()
        self._open_btn.setEnabled(False)
        self._copy_url_btn.setEnabled(False)

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()


def _escape(text: str) -> str:
    return html.escape(text or "", quote=True)


def _taxon_html(display_name: str, scientific_name: str) -> str:
    value = _escape(display_name)
    if "'" in (scientific_name or display_name or ""):
        return f"<span style='color:#f0c674; font-weight:bold'>{value}</span>"
    return value


def _ident_html(ident, most_recent: bool, authenticated_login: str) -> str:
    bits = []
    if most_recent:
        bits.append("<b>Most recent</b>")
    bits.append("current" if ident.current else "withdrawn/previous")
    if ident.category:
        bits.append(_escape(ident.category))
    if ident.is_leading:
        bits.append("leading")
    if ident.is_disagreement:
        bits.append("disagreement")
    if ident.is_provisional:
        bits.append("<span style='color:#f0c674'>provisional</span>")
    own = (
        authenticated_login
        and ident.user_login.casefold() == authenticated_login.casefold()
    )

    user = _escape(ident.user_login or "?")
    if own:
        user = f"<span style='color:#80cbc4; font-weight:bold'>{user} (you)</span>"
    taxon = _taxon_html(ident.taxon.display_name, ident.taxon.name)
    rank = (
        f" <span style='color:#999'>[{_escape(ident.taxon.rank)}]</span>"
        if ident.taxon.rank
        else ""
    )
    date = (
        f" <span style='color:#999'>{_escape(ident.created_at[:19])}</span>"
        if ident.created_at
        else ""
    )
    body = (
        f"<br><span style='color:#bbb'>{_escape(ident.body)}</span>"
        if ident.body
        else ""
    )
    status = " · ".join(bits)
    if status:
        status = f"<br><span style='color:#999'>{status}</span>"
    return f"<b>{user}</b>: {taxon}{rank}{date}{status}{body}"


def _comment_html(comment) -> str:
    user = _escape(comment.user_login or "?")
    date = (
        f" <span style='color:#999'>{_escape(comment.created_at[:19])}</span>"
        if comment.created_at
        else ""
    )
    hidden = " <span style='color:#999'>(hidden)</span>" if comment.hidden else ""
    body = _escape(comment.body)
    return f"<b>{user}</b>{date}{hidden}<br><span style='color:#ddd'>{body}</span>"


def _divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    line.setStyleSheet("color: #666;")
    return line
