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


class AuthDialog(QDialog):
    def __init__(self, current_login: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Authenticate to iNaturalist")
        self.resize(560, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        intro = QLabel(
            "Open the iNaturalist API token page in your browser, sign in, "
            "copy the token shown there, and paste it below. Tokens expire "
            "after about 24 hours."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        if current_login:
            current = QLabel(f"Currently authenticated as: <b>{current_login}</b>")
            layout.addWidget(current)

        open_btn = QPushButton("Open iNaturalist API Token Page")
        open_btn.clicked.connect(lambda: open_external_url_silently(API_TOKEN_URL))
        layout.addWidget(open_btn)

        self._token_edit = QTextEdit()
        self._token_edit.setPlaceholderText("Paste API token here")
        self._token_edit.setAcceptRichText(False)
        layout.addWidget(self._token_edit, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Validate and Save")
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
