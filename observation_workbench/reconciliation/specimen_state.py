"""Shared fresh specimen-identity validation for reviewed remote writes.

Reused by ITS synchronization (Gate 1C), coordinate copying (Gate 1D), and name
proposals (Gate 1D). Every reviewed write must prove the two records still
describe the same physical collection; each caller tolerates only the single
field it is intentionally reconciling and blocks on every other conflict.

The heavy hydration and fingerprint helpers live in ``its.py`` and
``coordinator.py``; they are imported lazily here to avoid a module import cycle.
"""
from __future__ import annotations

from typing import Any, Callable

from .db import ReconciliationDB
from .inat_reader import INatReconciliationReader
from .matching import scope_unknown_message, specimen_identity_conflicts
from .mo_client import MOClient
from .mo_parsing import parse_mo_observation
from .types import InventoryObservation, ReconciliationProfile, RemoteSite


def resolve_mo_fungi_status(
    mo_client: MOClient, inventory: InventoryObservation,
    cancelled: Callable[[], bool] = lambda: False,
) -> InventoryObservation:
    """Enrich an MO record's fungal scope from ``/names``.

    Mushroom Observer's observation payload carries no classification data even
    at ``detail=high``, so ``parse_mo_observation`` reports ``fungi_status
    ='unknown'`` for every real record (confirmed live; see
    ``observation_creation.py`` and ``coordinator._scan_mo``, which both perform
    this same enrichment). Without it every reviewed write would be blocked by
    "required evidence unavailable: mo fungal classification is unknown".
    """
    if inventory.fungi_status != "unknown" or inventory.taxon_id is None:
        return inventory
    # Imported lazily to avoid a module import cycle (the coordinator imports the
    # write services, which import this module).
    from .coordinator import _id_from, _name_fungi_status, _with_fungi_status
    from .mo_client import results_from_payload

    rows = results_from_payload(mo_client.names([inventory.taxon_id], cancelled))
    match = next(
        (row for row in rows if _id_from(row) == inventory.taxon_id),
        rows[0] if len(rows) == 1 else None,
    )
    if match is None:
        return inventory
    return _with_fungi_status(inventory, _name_fungi_status(match))


def evaluate_specimen_state(
    db: ReconciliationDB, profile: ReconciliationProfile, pair: dict[str, Any],
    inat_raw: dict[str, Any], mo_raw: dict[str, Any], reader: INatReconciliationReader,
    *, mo_client: MOClient, cancelled: Callable[[], bool] = lambda: False,
    include_coordinates: bool = True, tolerate_unknown_inat_scope: bool = False,
) -> tuple[str, str, tuple[str, ...]]:
    """Full fresh specimen-identity validation reusing the shared conflict logic.

    Returns ``(blocking_message, evidence_fingerprint, soft_warnings)``. A
    non-empty ``blocking_message`` means the reviewed write must not proceed. When
    ``include_coordinates`` is false the coordinate-distance conflict is skipped
    (coordinate copying reconciles the point on purpose); every other
    same-collection conflict still blocks.

    ``tolerate_unknown_inat_scope`` is set only by name proposals, whose whole
    purpose can be to give an unidentified iNaturalist observation its first
    name: such a record has no taxon at all, so its fungal scope is unknowable
    and blocking on it would disable the gate's primary use case. A *known*
    non-fungal iNaturalist record still conflicts, and the Mushroom Observer
    side must still prove its own scope.
    """
    # Imported lazily to avoid a module import cycle (coordinator imports the
    # write services, which import this module).
    from .coordinator import _hydrate_record
    from .its import _hydrate_mo_specimen, _normalized_locality, _specimen_evidence_fingerprint

    inat_inventory = reader.parse_inventory(inat_raw, profile.inat_user_id, None, None)
    mo_inventory = resolve_mo_fungi_status(
        mo_client, parse_mo_observation(mo_raw, profile.mo_user_id), cancelled,
    )
    inat_hydrated = _hydrate_record(inat_inventory, inat_raw, authorized=True, include_its=True)
    # iNaturalist and MO high-detail payloads have different shapes; MO specimen
    # fields need a dedicated site-aware hydrator.
    mo_hydrated = _hydrate_mo_specimen(mo_inventory, mo_raw)
    conflicts, unavailable = specimen_identity_conflicts(
        mo_hydrated, inat_hydrated, include_coordinates=include_coordinates,
    )
    if tolerate_unknown_inat_scope:
        tolerated = scope_unknown_message(RemoteSite.INAT)
        unavailable = tuple(item for item in unavailable if item != tolerated)
    reasons = list(conflicts)
    # Unavailable/unknown required specimen evidence is write-blocking: we cannot
    # prove these records are the same collection.
    reasons.extend(f"required evidence unavailable: {item}" for item in unavailable)
    if not inat_hydrated.required_values_available or not mo_hydrated.required_values_available:
        reasons.append("a required specimen-identity value is hidden or unavailable")
    # The pair's aggregate ``link_confirmed_with_metadata_conflicts`` state was
    # computed at pairing time and may exist SOLELY because the coordinates
    # differ. A coordinate action is intentionally reconciling that difference, so
    # for coordinate copies (include_coordinates=False) we rely only on the freshly
    # recomputed, coordinate-excluded conflicts above rather than this stale
    # aggregate. ITS/name operations keep coordinates included, so the aggregate
    # still blocks there.
    if include_coordinates and str(pair.get("link_state")) == "link_confirmed_with_metadata_conflicts":
        reasons.append("the confirmed pair has unresolved specimen-identity conflicts")
    if db.confirmed_pair_conflict(
        profile.profile_id, int(pair["mo_observation_id"]), int(pair["inat_observation_id"]),
    ):
        reasons.append("another confirmed one-to-one pairing conflicts with this pair")
    if db.pair_is_excluded(profile.profile_id, int(pair["pair_id"])):
        reasons.append("this pair is excluded")
    if str(pair.get("review_state")) != "confirmed":
        reasons.append("this pair is no longer confirmed")

    mo_loc = _normalized_locality(mo_inventory.public_locality)
    inat_loc = _normalized_locality(inat_inventory.public_locality)
    warnings: list[str] = []
    # Different phrasings are common between the sites, so a locality mismatch is a
    # soft review warning, not a hard block (a real coordinate conflict, when
    # coordinates are included, is already blocked above).
    if mo_loc and inat_loc and mo_loc != inat_loc:
        warnings.append(
            "Public localities differ between the sites; confirm they describe the same place."
        )

    deduped = list(dict.fromkeys(reasons))
    message = "; ".join(deduped)
    # Fingerprint the normalized evidence itself (not just the message text) so a
    # change to owners, dates, taxon, fungal scope, vouchers, collections,
    # locality, coordinates, or availability is detected even when it produces no
    # new conflict. Values are fed into a non-reversible hash, never stored raw.
    fingerprint = _specimen_evidence_fingerprint(
        mo_inventory, inat_inventory, mo_hydrated, inat_hydrated, mo_loc, inat_loc, deduped,
    )
    return message, fingerprint, tuple(warnings)
