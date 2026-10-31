"""Compact lower-right presentation for MycoMap Vision answers."""

from __future__ import annotations

import html
import re
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QGroupBox,
    QGridLayout,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from observation_workbench.services.vision_taxonomy import (
    VisionTaxonCandidate,
    candidates_from_vision_result,
)
from observation_workbench.ui.clipboard_shortcuts import (
    handle_super_clipboard_shortcut,
)


class _CopyableTextBrowser(QTextBrowser):
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            self.clearFocus()
            return
        if handle_super_clipboard_shortcut(self, event):
            if event.key() == Qt.Key.Key_C:
                # Restore Identify's global navigation after copying, so Right
                # Arrow does not remain trapped in this text pane.
                self.clearFocus()
            return
        super().keyPressEvent(event)


class MycomapVisionPanel(QGroupBox):
    family_comparison_requested = Signal()
    id_comment_requested = Signal(str)
    top_name_requested = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__("MycoMap Vision", parent)
        self.setMinimumHeight(190)
        self.setMaximumHeight(340)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 7)
        self._browser = _CopyableTextBrowser(self)
        self._browser.setOpenExternalLinks(True)
        layout.addWidget(self._browser)
        button_row = QGridLayout()
        self._top_name_button = QPushButton("Apply top name", self)
        self._top_name_button.clicked.connect(self._request_top_name)
        self._top_name_button.setEnabled(False)
        button_row.addWidget(self._top_name_button, 0, 0)
        self._family_button = QPushButton("Compare families…", self)
        self._family_button.clicked.connect(self.family_comparison_requested.emit)
        self._family_button.hide()
        button_row.addWidget(self._family_button, 0, 1)
        self._id_comment_button = QPushButton("Copy results to ID comment", self)
        self._id_comment_button.clicked.connect(self._request_id_comment)
        button_row.addWidget(self._id_comment_button, 1, 0, 1, 2)
        layout.addLayout(button_row)
        self._candidates: tuple[VisionTaxonCandidate, ...] = ()
        self._comment_text = ""
        self._top_name = ""
        self._top_name_comment = ""
        self.hide()

    def show_result(self, result: dict[str, Any]) -> None:
        self._candidates = candidates_from_vision_result(result)
        self._comment_text = _result_comment_text(result)
        self._top_name, self._top_name_comment = _top_name_action(result)
        self._browser.setHtml(_result_html(result))
        self._family_button.setVisible(bool(self._candidates))
        self._id_comment_button.setEnabled(bool(self._comment_text))
        self._top_name_button.setText(
            f"Apply {self._top_name}" if self._top_name else "Apply top name"
        )
        self._top_name_button.setEnabled(bool(self._top_name))
        self.show()

    def show_error(self, message: str) -> None:
        self._candidates = ()
        self._comment_text = ""
        self._top_name = ""
        self._top_name_comment = ""
        self._family_button.hide()
        self._id_comment_button.setEnabled(False)
        self._top_name_button.setEnabled(False)
        self._browser.setHtml(
            "<p><b>Vision could not answer.</b></p>"
            f"<p>{html.escape(str(message))}</p>"
            "<p><small>Press V on this observation to try again.</small></p>"
        )
        self.show()

    def clear_result(self) -> None:
        self._candidates = ()
        self._comment_text = ""
        self._top_name = ""
        self._top_name_comment = ""
        self._browser.clear()
        self._family_button.hide()
        self._top_name_button.setEnabled(False)
        self.hide()

    @property
    def taxon_candidates(self) -> tuple[VisionTaxonCandidate, ...]:
        return self._candidates

    def set_family_lookup_state(self, *, loading: bool, ready: bool = False) -> None:
        self._family_button.setEnabled(not loading)
        if loading:
            self._family_button.setText("Looking up families…")
        elif ready:
            self._family_button.setText("Compare families…")
        else:
            self._family_button.setText("Look up and compare families…")

    def _request_id_comment(self) -> None:
        if self._comment_text:
            self.id_comment_requested.emit(self._comment_text)

    def _request_top_name(self) -> None:
        if self._top_name:
            self.top_name_requested.emit(self._top_name, self._top_name_comment)


def _result_html(result: dict[str, Any]) -> str:
    parts: list[str] = []
    answer = result.get("answer")
    if isinstance(answer, dict) and answer.get("name"):
        name = html.escape(str(answer.get("name")))
        rank = html.escape(str(answer.get("rank") or "answer"))
        confidence = _percent(answer.get("confidence"))
        suffix = f" — {confidence}" if confidence else ""
        parts.append(f"<p><b>{name}</b> <small>({rank}){suffix}</small></p>")
    else:
        parts.append("<p><b>No confident answer</b></p>")

    ranks = result.get("ranks")
    if isinstance(ranks, dict):
        for rank, limit in (("species", 5), ("genus", 3), ("family", 3)):
            candidates = ranks.get(rank)
            if not isinstance(candidates, list) or not candidates:
                continue
            rows: list[str] = []
            for candidate in candidates[:limit]:
                if not isinstance(candidate, dict) or not candidate.get("name"):
                    continue
                candidate_name = html.escape(str(candidate.get("name")))
                confidence = _percent(candidate.get("confidence"))
                references = _positive_int(candidate.get("reference_records"))
                details = [value for value in (confidence, references) if value]
                detail_text = (
                    f" <small>— {', '.join(details)}</small>" if details else ""
                )
                rows.append(f"<li><i>{candidate_name}</i>{detail_text}</li>")
            if rows:
                parts.append(
                    f"<p><b>{html.escape(rank.title())}</b></p><ol>{''.join(rows)}</ol>"
                )

    hints = result.get("hints")
    if isinstance(hints, list):
        clean_hints = [html.escape(str(hint)) for hint in hints if str(hint).strip()]
        if clean_hints:
            parts.append(f"<p><small>{'<br>'.join(clean_hints)}</small></p>")

    metadata: list[str] = []
    model = result.get("model")
    if isinstance(model, dict) and model.get("version"):
        metadata.append(f"model {html.escape(str(model.get('version')))}")
    if result.get("cached") is True:
        metadata.append("kept answer")
    usage = result.get("usage")
    if isinstance(usage, dict):
        used = _plain_int(usage.get("used"))
        limit = _plain_int(usage.get("limit"))
        if used is not None and limit is not None:
            metadata.append(f"monthly use {used}/{limit}")
    if metadata:
        parts.append(f"<p><small>{' · '.join(metadata)}</small></p>")
    parts.append(
        '<p><small>A photo suggestion is not a DNA-confirmed identification. '
        "Never use it to decide whether a mushroom is safe to eat.</small></p>"
    )
    return "".join(parts)


def _result_comment_text(result: dict[str, Any]) -> str:
    lines = ["MycoMap Vision suggestions:"]
    answer = result.get("answer")
    if isinstance(answer, dict) and answer.get("name"):
        confidence = _percent(answer.get("confidence"))
        detail = f", {confidence}" if confidence else ""
        lines.append(
            f"Answer: {answer.get('name')} ({answer.get('rank') or 'taxon'}{detail})"
        )

    ranks = result.get("ranks")
    if isinstance(ranks, dict):
        for rank, limit in (("species", 5), ("genus", 3), ("family", 3)):
            values = ranks.get(rank)
            if not isinstance(values, list):
                continue
            candidates: list[str] = []
            for value in values[:limit]:
                if not isinstance(value, dict) or not value.get("name"):
                    continue
                confidence = _percent(value.get("confidence"))
                references = _plain_int(value.get("reference_records"))
                details = []
                if confidence:
                    details.append(confidence)
                if references is not None:
                    details.append(f"{references:,} DNA refs")
                suffix = f" ({', '.join(details)})" if details else ""
                candidates.append(f"{value.get('name')}{suffix}")
            if candidates:
                lines.append(f"{rank.title()}: " + "; ".join(candidates))

    hints = result.get("hints")
    if isinstance(hints, list):
        clean_hints = [str(hint).strip() for hint in hints if str(hint).strip()]
        if clean_hints:
            lines.append("Hints: " + "; ".join(clean_hints))
    model = result.get("model")
    if isinstance(model, dict) and model.get("version"):
        lines.append(f"Model: {model.get('version')}")
    return "\n".join(lines).strip()


def _top_name_action(result: dict[str, Any]) -> tuple[str, str]:
    """Pick the name to apply: the top species when the answer is coarser."""
    answer = result.get("answer")
    answer = answer if isinstance(answer, dict) else {}
    chosen = answer
    ranks = result.get("ranks")
    species = ranks.get("species") if isinstance(ranks, dict) else None
    answer_rank = str(answer.get("rank") or "").strip().lower()
    if (
        answer_rank != "species"
        and isinstance(species, list)
        and species
        and isinstance(species[0], dict)
        and species[0].get("name")
    ):
        chosen = species[0]
    name = _strip_internal_suffix(str(chosen.get("name") or "").strip())
    if not name:
        return "", ""
    confidence = _percent(chosen.get("confidence"))
    comment = "According to MycoMap Vision"
    if confidence:
        comment += f" ({confidence} sure)"
    return name, comment


def _strip_internal_suffix(name: str) -> str:
    """Drop MycoMap-internal group labels (``-ca02``) and infraspecific ranks."""
    name = re.sub(r"-[a-z]{2}\d{2}$", "", name, flags=re.IGNORECASE)
    # Lookup is by species name; omit infraspecific ranks (var., subsp., f.).
    return re.sub(
        r"\s+(?:var|subvar|subsp|ssp|f|fo|forma)\.?\s+\S.*$",
        "",
        name,
        flags=re.IGNORECASE,
    )


def _percent(value: object) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return f"{number:.0%}"


def _positive_int(value: object) -> str:
    number = _plain_int(value)
    if number is None:
        return ""
    label = "DNA reference" if number == 1 else "DNA references"
    return f"{number:,} {label}"


def _plain_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
