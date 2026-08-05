"""Persistent iNaturalist API-token auth state."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

from observation_workbench.storage.settings import AppSettings

API_TOKEN_URL = "https://www.inaturalist.org/users/api_token"

# iNaturalist API tokens are JWTs.  Keeping the boundaries explicit prevents
# punctuation, labels, and quotes copied from a browser page from becoming
# part of the credential.
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
    r"(?![A-Za-z0-9_-])"
)


@dataclass
class AuthState:
    api_token: str = ""
    login: str = ""

    @property
    def is_authenticated(self) -> bool:
        return bool(self.api_token and self.login)


class AuthService:
    """Small wrapper around QSettings-backed iNaturalist auth state."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    def load(self) -> AuthState:
        return AuthState(
            api_token=self._settings.inat_api_token,
            login=self._settings.inat_login,
        )

    def save(self, token: str, login: str) -> AuthState:
        state = AuthState(api_token=normalise_token(token), login=login.strip())
        self._settings.inat_api_token = state.api_token
        self._settings.inat_login = state.login
        self._settings.sync()
        return state

    def clear(self) -> None:
        self._settings.clear_auth()
        self._settings.sync()


def normalise_token(token: Optional[str]) -> str:
    """Return the token from plain or browser-copied token text.

    The API-token page can copy a table row such as ``api_token "<JWT>"``
    rather than just the JWT.  Extracting the JWT also handles surrounding
    quotes, labels, tabs, and newlines without requiring users to edit the
    pasted value first.
    """
    value = (token or "").strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith("bearer "):
        value = value.split(None, 1)[1].strip()

    match = _JWT_RE.search(value)
    if match:
        return match.group(1)

    # Preserve support for non-JWT values while removing harmless wrapping
    # quotes from manually entered credentials.
    return value.strip().strip('"\'').strip()
