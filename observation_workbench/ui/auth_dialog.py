"""Dialog for manual browser-based iNaturalist API-token authentication."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from observation_workbench.api.auth import API_TOKEN_URL
from observation_workbench.ui.external_links import open_external_url_silently


def _step_label(number: int, html: str) -> QLabel:
    label = QLabel(f"<b>Step {number}.</b> {html}")
    label.setWordWrap(True)
    return label


class AuthDialog(QDialog):
    def __init__(self, current_login: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sign in to iNaturalist")
        self.resize(620, 600)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        intro = QLabel(
            "Signing in lets this program add identifications, agree, comment, and "
            "mark observations reviewed <i>as you</i>. Browsing and studying "
            "observations works without signing in."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        how = QLabel(
            "This program never sees your iNaturalist password. Instead, iNaturalist "
            "gives you a temporary <b>API token</b> (a long string of letters and "
            "numbers) that you copy into this program."
        )
        how.setWordWrap(True)
        layout.addWidget(how)

        if current_login:
            current = QLabel(f"Currently signed in as: <b>{current_login}</b>")
            layout.addWidget(current)

        layout.addWidget(
            _step_label(
                1,
                "Click the button below. Your web browser opens iNaturalist's API "
                "token page. If iNaturalist asks you to log in, do so.",
            )
        )
        open_btn = QPushButton("Open iNaturalist API Token Page")
        open_btn.clicked.connect(lambda: open_external_url_silently(API_TOKEN_URL))
        layout.addWidget(open_btn)

        layout.addWidget(
            _step_label(
                2,
                "The page shows <code>api_token</code> followed by a long code. "
                "Select it and copy it (Ctrl+C, or ⌘C on a Mac). It is fine to copy "
                "the whole line, quotes included.",
            )
        )
        layout.addWidget(_step_label(3, "Paste it into the box below:"))

        self._token_edit = QTextEdit()
        self._token_edit.setPlaceholderText("Paste API token here")
        self._token_edit.setAcceptRichText(False)
        layout.addWidget(self._token_edit, 1)

        layout.addWidget(
            _step_label(
                4,
                "Click <b>Sign in</b>. The program checks the token with iNaturalist "
                "and shows your username in the bottom-right corner of the main window.",
            )
        )

        note = QLabel(
            "<i>Tokens expire after about 24 hours. When yours runs out, the program "
            "will tell you, and you repeat these steps.</i>"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Sign in")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def token(self) -> str:
        raw = self._token_edit.toPlainText().strip()
        if raw.startswith("{"):
            try:
                import json

                parsed = json.loads(raw)
                if isinstance(parsed.get("api_token"), str):
                    return parsed["api_token"].strip()
            except Exception:
                pass
        return raw
