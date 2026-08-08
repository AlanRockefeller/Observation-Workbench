"""Safe, read-only observation information panel."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from observation_workbench.models import StudyObservation

if TYPE_CHECKING:
    from observation_workbench.services.identify_action_presentation import (
        IdentifyActionPresentation,
    )


class IdentifyInfoTab(QScrollArea):
    def __init__(self, login: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._login = _normalize_login(login)
        self._content = QWidget(self)
        self._layout = QVBoxLayout(self._content)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self._content)
        self.setWidgetResizable(True)
        self._observation: StudyObservation | None = None
        self._pending_actions: tuple["IdentifyActionPresentation", ...] = ()

    def set_observation(self, observation: StudyObservation) -> None:
        reset_scroll = (
            self._observation is None or self._observation.obs_id != observation.obs_id
        )
        self._observation = observation
        self._render()
        if reset_scroll:
            self.verticalScrollBar().setValue(0)

    def set_login(self, login: str) -> None:
        """Refresh the active-account marker without retaining stale login."""
        normalized = _normalize_login(login)
        if normalized == self._login:
            return
        self._login = normalized
        if self._observation is not None:
            self._render()

    def set_pending_actions(self, actions: list["IdentifyActionPresentation"]) -> None:
        """Display local journal intent separately from server-confirmed history."""
        self._pending_actions = tuple(actions)
        if self._observation is not None:
            self._render()

    def _render(self) -> None:
        observation = self._observation
        if observation is None:
            return
        scroll_position = self.verticalScrollBar().value()
        self._clear()

        self._add_heading("Observation details")
        self._add_value("Observer", observation.observer_login)
        self._add_value("Observed date", observation.observed_on)
        self._add_value("Created date", observation.created_at)
        self._add_value("Place", observation.place_guess)
        self._add_value("Coordinates", _coordinates(observation))
        self._add_value(
            "Positional accuracy", _positional_accuracy(observation.positional_accuracy)
        )
        self._add_value("Captive/Cultivated", _captive_status(observation.captive))
        self._add_value("Quality grade", observation.quality_grade)
        self._add_value("Observation taxon", _taxon_name(observation.taxon))
        self._add_value("Community taxon", _taxon_name(observation.community_taxon))
        self._add_value("Description", observation.description)
        self._add_value("Provisional species", observation.provisional_species_name)
        self._add_value(
            "DNA Barcode ITS", _dna_barcode_summary(observation.dna_barcode_its)
        )
        self._add_observation_link(observation.obs_id)

        self._add_heading("Identifications")
        if not observation.all_identifications:
            self._add_note("None")
        else:
            for identification in observation.all_identifications:
                login = getattr(identification, "user_login", "") or "Unknown user"
                is_withdrawn = not bool(getattr(identification, "current", True))
                is_current_user = (
                    bool(self._login)
                    and login.casefold() == self._login
                    and bool(getattr(identification, "current", False))
                )
                suffix = " (withdrawn)" if is_withdrawn else ""
                if is_current_user:
                    suffix = " (your current ID)"
                taxon = (
                    _taxon_name(getattr(identification, "taxon", None))
                    or "Unknown taxon"
                )
                created_at = _display_timestamp(
                    getattr(identification, "created_at", "")
                )
                body = getattr(identification, "body", "") or ""
                detail_parts = [taxon]
                identification_context = _identification_context(identification)
                if identification_context:
                    detail_parts.append(identification_context)
                if created_at:
                    detail_parts.append(created_at)
                if body:
                    detail_parts.append(body)
                self._add_value(f"{login}{suffix}", "\n".join(detail_parts))

        self._add_heading("Comments")
        visible_comments = [
            comment
            for comment in observation.comments
            if not bool(getattr(comment, "hidden", False))
        ]
        if not visible_comments:
            self._add_note("None")
        else:
            for comment in visible_comments:
                login = getattr(comment, "user_login", "") or "Unknown user"
                body = getattr(comment, "body", "") or ""
                created_at = _display_timestamp(getattr(comment, "created_at", ""))
                detail = body if not created_at else f"{created_at}\n{body}"
                self._add_value(login, detail)

        pending_actions = [
            action
            for action in self._pending_actions
            if _action_observation_id(action) == observation.obs_id
        ]
        if pending_actions:
            self._add_heading("Pending local actions")
            for action in pending_actions:
                self._add_value(
                    f"#{action.local_action_id} · {action.action_type}",
                    f"{action.state_text}\n{action.intended_summary}",
                )
        self.verticalScrollBar().setValue(scroll_position)

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _add_heading(self, text: str) -> None:
        label = QLabel(f"<h3>{escape(text)}</h3>")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._layout.addWidget(label)

    def _add_value(self, title: str, value: object) -> None:
        if value is None or value == "":
            return
        escaped_title = escape(str(title))
        escaped_value = _escaped_multiline(value)
        label = QLabel(f"<b>{escaped_title}</b><br>{escaped_value}")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._layout.addWidget(label)

    def _add_note(self, text: str) -> None:
        label = QLabel(f"<i>{escape(text)}</i>")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._layout.addWidget(label)

    def _add_observation_link(self, observation_id: int) -> None:
        safe_id = int(observation_id)
        url = f"https://www.inaturalist.org/observations/{safe_id}"
        label = QLabel(f'<a href="{url}">Open observation {safe_id} in browser</a>')
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setOpenExternalLinks(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._layout.addWidget(label)


def _escaped_multiline(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return escape(text).replace("\n", "<br>")


def _normalize_login(value: object) -> str:
    return str(value or "").strip().casefold()


def _coordinates(observation: StudyObservation) -> str:
    if observation.latitude is None or observation.longitude is None:
        return "Unavailable (obscured)" if observation.obscured else ""
    suffix = " (obscured)" if observation.obscured else ""
    return f"{observation.latitude}, {observation.longitude}{suffix}"


def _positional_accuracy(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return f"{value:g} m"


def _captive_status(value: object) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return ""


def _display_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text or ("T" not in text and " " not in text):
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.strftime("%Y-%m-%d %H:%M")


def _dna_barcode_summary(value: object) -> str:
    sequence = "".join(str(value or "").split())
    if not sequence:
        return ""
    preview_length = 60
    preview = sequence[:preview_length]
    suffix = "…" if len(sequence) > preview_length else ""
    return f"{len(sequence)} bases · {preview}{suffix}"


def _identification_context(identification: object) -> str:
    labels: list[str] = []
    category = str(getattr(identification, "category", "") or "").strip()
    if category:
        labels.append(category)
    if bool(getattr(identification, "disagreement", False)):
        labels.append("disagrees")
    return " · ".join(labels)


def _action_observation_id(action: object) -> int | None:
    try:
        observation_id = int(getattr(action, "observation_id", 0))
    except (TypeError, ValueError):
        return None
    return observation_id if observation_id > 0 else None


def _taxon_name(taxon: object) -> str:
    return getattr(taxon, "display_name", "") or ""
