"""Stable identity and mutable-snapshot fingerprints for Phase 2B."""
from __future__ import annotations

from .normalization import public_fingerprint
from .types import RemoteSite


def canonical_identity_fingerprint(
    site: RemoteSite | str,
    observation_id: int,
    remote_uuid: str,
    account_identity: str,
) -> str:
    """Legacy v14 identity fingerprint retained for migration compatibility."""
    site_value = site.value if isinstance(site, RemoteSite) else str(site)
    return public_fingerprint(
        "phase_2b_canonical_identity_v1",
        site_value,
        int(observation_id),
        str(remote_uuid or ""),
        str(account_identity or ""),
    )


def canonical_stable_identity_fingerprint(
    site: RemoteSite | str,
    observation_id: int,
    remote_uuid: str,
    owner_account_id: int,
) -> str:
    site_value = site.value if isinstance(site, RemoteSite) else str(site)
    return public_fingerprint(
        "phase_2b_canonical_identity_v2",
        site_value,
        int(observation_id),
        str(remote_uuid or ""),
        int(owner_account_id),
    )


def parse_legacy_account_identity(value: object) -> tuple[int, str]:
    """Parse the historical ``numeric-id:display-login`` representation."""
    text = str(value or "")
    numeric, separator, login = text.partition(":")
    if not separator or not login or not numeric.isdecimal():
        raise ValueError("legacy account identity is not numeric-id:login")
    try:
        account_id = int(numeric)
    except (TypeError, ValueError) as exc:
        raise ValueError("legacy account identity has no numeric account id") from exc
    if account_id <= 0:
        raise ValueError("legacy account identity has an invalid numeric account id")
    return account_id, login
