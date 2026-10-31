"""Identify keyboard-shortcut reference and its help dialog.

``identify_shortcut_sections()`` is the single list shown to the user.
Whenever an Identify key binding changes (``IdentifyWindow.
_handle_identify_shortcut``, the Identify dialogs' ``eventFilter`` methods,
``TaxonAutocompleteField``, ``MycomapVisionPanel`` or ``clipboard_shortcuts``),
update this table in the same change.
"""

from __future__ import annotations

import html
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)


def _meta_key_name() -> str:
    # Qt reports macOS Control and the Linux/Windows Super/Win key as Meta.
    if sys.platform == "darwin":
        return "Control"
    if sys.platform.startswith("win"):
        return "Win"
    return "Super"


ShortcutSection = tuple[str, tuple[tuple[str, str], ...]]


def identify_shortcut_sections() -> tuple[ShortcutSection, ...]:
    """(section title, ((keys, description), ...)) in display order."""
    alt = "Option" if sys.platform == "darwin" else "Alt"
    meta = _meta_key_name()
    alt_or_meta = f"{alt} or {meta}"
    # Qt's Control modifier is the Command key on macOS.
    ctrl = "Cmd" if sys.platform == "darwin" else "Ctrl"
    sections: list[ShortcutSection] = [
        (
            "Navigation",
            (
                ("Left / Right", "Previous / next observation"),
                ("Up / Down", "Previous / next photo"),
                (f"{alt_or_meta} + Left / Right", "Previous / next photo"),
                ("Shift + Left / Right", "Previous / next tab"),
            ),
        ),
        (
            "Actions",
            (
                ("I", "Add an identification"),
                ("A", "Agree with the observation taxon"),
                ("C", "Add a comment"),
                (
                    "R",
                    "Mark reviewed; moves to the next observation when reviewed "
                    "observations are hidden",
                ),
                ("F", "Add or remove favorite"),
                ("X", "Change your Captive/Cultivated Data Quality vote"),
                (
                    "V",
                    "Ask MycoMap Vision and advance immediately; the result "
                    "appears when you come back",
                ),
            ),
        ),
        (
            "Photo",
            (
                ("Z", "Toggle fit / 1:1 zoom"),
                (f"{alt_or_meta} + Up / Down", "Brighter / darker"),
            ),
        ),
        (
            "Add identification dialog",
            (
                (
                    "Type a name",
                    "Each word matches the start of a name word, e.g. "
                    "“C moly” → Chlorophyllum molybdites",
                ),
                ("Down / Up", "Move into the suggestion list"),
                ("Enter (suggestions shown)", "Pick the highlighted (or first) one"),
                ("Enter (taxon chosen)", "Submit"),
                ("Esc (suggestions shown)", "Hide the suggestions"),
                (f"{ctrl} + Enter (in Body)", "Submit"),
                ("Tab (in Body)", "Move to the next control"),
                ("Esc", "Close the dialog"),
            ),
        ),
        (
            "Comment, Favorite and Captive dialogs",
            (
                (f"{ctrl} + Enter (comment text)", "Submit the comment"),
                ("Enter (option selected)", "Submit Favorite / Captive choice"),
                ("Esc", "Close the dialog"),
            ),
        ),
        (
            "Vision results",
            (
                (
                    f"{ctrl} + C",
                    "Copy the selected text and return to arrow-key navigation",
                ),
            ),
        ),
        ("Help", (("?", "Show this list"),)),
    ]
    if sys.platform.startswith("linux"):
        sections.insert(
            -1,
            (
                "Text fields (Linux)",
                (
                    (
                        f"{meta} + A / C / V / X",
                        f"Select all / copy / paste / cut, like {ctrl}",
                    ),
                ),
            ),
        )
    return tuple(sections)


def identify_shortcuts_html() -> str:
    # One table, so the key column lines up across every section.
    parts = [
        "<p>Single-key shortcuts are ignored while you are typing in a text "
        "field or a dialog is open.</p>",
        '<table cellspacing="0" cellpadding="3">',
    ]
    for title, rows in identify_shortcut_sections():
        parts.append(
            f'<tr><td colspan="2" style="padding-top:12px">'
            f'<span style="font-size:large; font-weight:bold">'
            f"{html.escape(title)}</span></td></tr>"
        )
        for keys, description in rows:
            parts.append(
                f'<tr><td style="padding-right:18px; white-space:nowrap">'
                f"<b>{html.escape(keys)}</b></td>"
                f"<td>{html.escape(description)}</td></tr>"
            )
    parts.append("</table>")
    return "".join(parts)


class IdentifyShortcutsDialog(QDialog):
    """Modeless reference; Esc or Close dismisses it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("Identify keyboard shortcuts")
        layout = QVBoxLayout(self)
        browser = QTextBrowser(self)
        browser.setOpenLinks(False)
        browser.setHtml(identify_shortcuts_html())
        layout.addWidget(browser, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(680, 760)
