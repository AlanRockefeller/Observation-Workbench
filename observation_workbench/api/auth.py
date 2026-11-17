"""Persistent iNaturalist API-token auth state."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
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
    user_id: int = 0

    @property
    def is_authenticated(self) -> bool:
        return bool(self.api_token and self.login)

    @property
    def expires_at(self) -> Optional[datetime]:
        return token_expiry(self.api_token)

    @property
    def is_expired(self) -> bool:
        """True only when the token carries an expiry that has already passed."""
        expiry = self.expires_at
        return expiry is not None and expiry <= datetime.now(timezone.utc)


class AuthService:
    """Small wrapper around QSettings-backed iNaturalist auth state."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    def load(self) -> AuthState:
        return AuthState(
            api_token=self._settings.inat_api_token,
            login=self._settings.inat_login,
            user_id=self._settings.inat_user_id,
        )

    def save(self, token: str, login: str, user_id: int = 0) -> AuthState:
        state = AuthState(
            api_token=normalise_token(token), login=login.strip(), user_id=int(user_id or 0)
        )
        self._settings.inat_api_token = state.api_token
        self._settings.inat_login = state.login
        self._settings.inat_user_id = state.user_id
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
    return value.strip().strip("\"'").strip()


def token_expiry(token: Optional[str]) -> Optional[datetime]:
    """Return the ``exp`` claim of a JWT as an aware UTC datetime, if present.

    The signature is not verified: this is only a local hint used to tell the
    user their saved sign-in has lapsed. iNaturalist remains the authority on
    whether a token is accepted.
    """
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
