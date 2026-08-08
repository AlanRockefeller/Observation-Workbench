"""Gate 2B non-destructive duplicate-observation consolidation.

The shipped capability scope is deliberately link-only: reciprocal links and
the local canonical/superseded decision are implemented; every optional data
transfer remains disabled and durably disclosed. Donors are never edited,
hidden, withdrawn, or deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
from typing import Any, Callable, Optional, Sequence

from observation_workbench.api.auth import AuthState
from observation_workbench.api.client import INatClient

from .actions import LinkRepairService
from .consolidation_graph import (
    ConsolidationGraphValidation,
    canonical_strong_anchor_signatures,
    validate_consolidation_graph,
)
from .consolidation_identity import canonical_stable_identity_fingerprint
from .db import ReconciliationDB, consolidation_evidence_graph_fingerprint
from .inat_reader import INatReconciliationReader
from .matching import specimen_identity_conflicts
from .mo_client import MOClient, ReconciliationCancelled, results_from_payload
from .mo_parsing import parse_mo_external_link, parse_mo_observation, positive_int
from .normalization import parse_mo_observation_url, public_fingerprint
from .photos import (
    _inat_photo_snapshots,
    _inat_photos_fingerprint,
    _mo_photo_snapshots,
    _mo_photos_fingerprint,
)
from .types import (
    AuthoritativeLinkSnapshot,
    LinkActionType,
    ConsolidationConflict,
    ConsolidationEligibility,
    ConsolidationItemDisclosure,
    ConsolidationEvidenceEdge,
    ConsolidationEvidencePath,
    ConsolidationMemberSnapshot,
    ConsolidationPreview,
    HydratedObservation,
    InventoryObservation,
    PriorConsolidationMember,
    ReconciliationProfile,
    RemoteSite,
)


class ConsolidationError(RuntimeError):
    """A safe, user-displayable Gate 2B precondition error."""

    def __init__(self, message: str, code: str = "consolidation_unavailable") -> None:
        super().__init__(message)
        self.code = code


# The full Phase 2B v1 disclosure list. Every entry stays enabled=False; there
# is no code path anywhere in this module that flips one to True. Enabling any
# of these requires a separate capability-proof spike outside Phase 2B v1 —
# see docs/gate_2b_capability_note.md's "What this means for M1-M12" section.
UNSUPPORTED_ITEM_DISCLOSURES: tuple[ConsolidationItemDisclosure, ...] = (
    ConsolidationItemDisclosure(
        item_type="photo",
        description="Photo transfer to an already-existing canonical observation",
        enabled=False,
        disabled_reason=(
            "Not capability-proven for Phase 2B: the existing photo transfer service "
            "only writes MO->iNat within one confirmed pair, has no duplicate detection "
            "proven for any other direction, and has never targeted a donor/canonical "
            "identity that isn't its own confirmed pair partner. "
            "See docs/gate_2b_capability_note.md."
        ),
    ),
    ConsolidationItemDisclosure(
        item_type="its_sequence",
        description="ITS/sequence transfer to the canonical observation",
        enabled=False,
        disabled_reason=(
            "Not capability-proven for Phase 2B: ITSSyncService only operates within one "
            "confirmed cross-site pair, and MOClient.sequences() pagination is still "
            "structurally unverified. Sequence evidence is not inspected by this workflow. "
            "See docs/gate_2b_capability_note.md."
        ),
    ),
    ConsolidationItemDisclosure(
        item_type="voucher_collection_identifier",
        description="Voucher/collection identifier transfer",
        enabled=False,
        disabled_reason=(
            "Unsupported in all directions: no code path writes an arbitrary identifier "
            "to iNaturalist, and Mushroom Observer has no update-observation endpoint to "
            "patch one onto an existing observation. See docs/gate_2b_capability_note.md."
        ),
    ),
    ConsolidationItemDisclosure(
        item_type="description_notes",
        description="Description/notes transfer",
        enabled=False,
        disabled_reason=(
            "Unsupported: neither site has a proven update-observation primitive in this "
            "codebase for notes/description. See docs/gate_2b_capability_note.md."
        ),
    ),
    ConsolidationItemDisclosure(
        item_type="coordinates_date_taxon",
        description="Coordinates, observation date, or taxon replacement",
        enabled=False,
        disabled_reason=(
            "Forbidden by policy (M6): these are same-observation field replacements, "
            "not additive writes, and must never be selected for automatic transfer."
        ),
    ),
)


def _hydrate_candidate(
    site: RemoteSite,
    observation_id: int,
    raw: dict[str, Any],
    profile: ReconciliationProfile,
    reader: INatReconciliationReader,
    *,
    inat_mo_field_id: Optional[int] = None,
) -> tuple[InventoryObservation, HydratedObservation]:
    # Imported lazily to avoid the module import cycle documented in
    # specimen_state.py (coordinator imports the write services, which import
    # this module).
    from .coordinator import _hydrate_record
    from .its import _hydrate_mo_specimen

    if site == RemoteSite.INAT:
        if not str(raw.get("uuid") or "").strip():
            raise ConsolidationError(
                f"iNaturalist #{observation_id} has no stable remote UUID.",
                "remote_uuid_unavailable",
            )
        its_binding = None
        inventory = reader.parse_inventory(
            raw,
            profile.inat_user_id,
            inat_mo_field_id,
            its_binding,
        )
        hydrated = _hydrate_record(inventory, raw, authorized=True, include_its=True)
    else:
        inventory = parse_mo_observation(raw, profile.mo_user_id)
        hydrated = _hydrate_mo_specimen(inventory, raw)
    if inventory.key.observation_id != observation_id:
        raise ConsolidationError(
            f"{site.value} record id mismatch: expected {observation_id}, "
            f"got {inventory.key.observation_id}",
            "record_id_mismatch",
        )
    return inventory, hydrated


def _member_key(site: RemoteSite, observation_id: int) -> tuple[str, int]:
    return site.value, int(observation_id)


def _link_rows_as_snapshots(
    inv: InventoryObservation,
) -> tuple[AuthoritativeLinkSnapshot, ...]:
    """Adapt inventory-parsed link rows to the snapshot shape used everywhere else."""
    return tuple(
        AuthoritativeLinkSnapshot(
            site=inv.key.site,
            observation_id=inv.key.observation_id,
            row_id=row.row_id,
            binding_id=row.external_site_id,
            target_observation_id=row.target_observation_id,
            parse_state=row.parse_state,
            row_fingerprint=row.fingerprint,
        )
        for row in inv.authoritative_links
    )


def _ordered_edge_ends(
    left_site: RemoteSite,
    left_id: int,
    right_site: RemoteSite,
    right_id: int,
) -> tuple[RemoteSite, int, RemoteSite, int]:
    left = _member_key(left_site, left_id)
    right = _member_key(right_site, right_id)
    if left == right:
        raise ConsolidationError(
            "An evidence edge cannot point to itself.", "malformed_evidence"
        )
    return (
        (left_site, left_id, right_site, right_id)
        if left < right
        else (right_site, right_id, left_site, left_id)
    )


def _evidence_edge(
    left: InventoryObservation,
    right: InventoryObservation,
    evidence_type: str,
    strength: str,
    summary: str,
    *proof: object,
) -> ConsolidationEvidenceEdge:
    left_site, left_id, right_site, right_id = _ordered_edge_ends(
        left.key.site,
        left.key.observation_id,
        right.key.site,
        right.key.observation_id,
    )
    return ConsolidationEvidenceEdge(
        left_site=left_site,
        left_observation_id=left_id,
        right_site=right_site,
        right_observation_id=right_id,
        evidence_type=evidence_type,
        evidence_strength=strength,
        reviewed_evidence_fingerprint=public_fingerprint(
            "phase_2b_specimen_edge_v1",
            left_site.value,
            left_id,
            right_site.value,
            right_id,
            evidence_type,
            strength,
            *proof,
        ),
        display_summary=summary,
    )


def _supporting_evidence(
    hydrated: Sequence[tuple[InventoryObservation, HydratedObservation]],
    link_snapshots: Optional[
        dict[tuple[RemoteSite, int], Sequence[AuthoritativeLinkSnapshot]]
    ] = None,
) -> tuple[ConsolidationEvidenceEdge, ...]:
    """Build the positive specimen-identity graph.

    Taxon, date, locality, and absence of conflict never create an edge.
    Phase 2B deliberately excludes accession/sequence evidence because the MO
    sequence reader has not been capability-proven for this workflow.
    """
    from .matching import _distance_m

    links = link_snapshots or {}
    result: list[ConsolidationEvidenceEdge] = []
    for index, (left_inv, left_hyd) in enumerate(hydrated):
        for right_inv, right_hyd in hydrated[index + 1 :]:
            # Each direction is independent, so an expected reciprocal-link
            # addition does not mutate the fingerprint of a reviewed one-way
            # link edge.
            for source_inv, target_inv in (
                (left_inv, right_inv),
                (right_inv, left_inv),
            ):
                if source_inv.key.site is target_inv.key.site:
                    continue
                source_key = (
                    source_inv.key.site,
                    source_inv.key.observation_id,
                )
                snapshot_rows: Sequence[AuthoritativeLinkSnapshot]
                if source_key in links:
                    snapshot_rows = links[source_key]
                else:
                    # ``InventoryObservation.authoritative_links`` holds
                    # ``AuthoritativeLinkRow`` (``fingerprint``/
                    # ``external_site_id``), not ``AuthoritativeLinkSnapshot``
                    # (``row_fingerprint``/``binding_id``). Normalize exactly as
                    # ``_member_snapshot`` does so both branches expose one shape.
                    snapshot_rows = _link_rows_as_snapshots(source_inv)
                matching = tuple(
                    sorted(
                        (
                            str(row.row_id),
                            str(row.row_uuid),
                            str(row.row_fingerprint),
                        )
                        for row in snapshot_rows
                        if row.parse_state not in {"malformed", "conflicting"}
                        and row.target_observation_id == target_inv.key.observation_id
                    )
                )
                if matching:
                    result.append(
                        _evidence_edge(
                            source_inv,
                            target_inv,
                            f"authoritative_link_{source_inv.key.site.value}_to_"
                            f"{target_inv.key.site.value}",
                            "strong",
                            f"Authoritative {source_inv.key.site.value} reciprocal-link field "
                            f"targets {target_inv.key.site.value} "
                            f"#{target_inv.key.observation_id}.",
                            *("|".join(item) for item in matching),
                        )
                    )

            shared_vouchers = tuple(
                sorted(
                    set(left_hyd.voucher_identifiers)
                    & set(right_hyd.voucher_identifiers)
                )
            )
            if shared_vouchers:
                result.append(
                    _evidence_edge(
                        left_inv,
                        right_inv,
                        "exact_voucher",
                        "strong",
                        "Exact normalized voucher-to-voucher identity.",
                        *shared_vouchers,
                    )
                )
            shared_collection_numbers = tuple(
                sorted(
                    set(left_hyd.collection_identifiers)
                    & set(right_hyd.collection_identifiers)
                )
            )
            if shared_collection_numbers:
                result.append(
                    _evidence_edge(
                        left_inv,
                        right_inv,
                        "exact_collection_number",
                        "strong",
                        "Exact normalized collection-number-to-collection-number identity.",
                        *shared_collection_numbers,
                    )
                )

            # Explicit source-qualified media identity is accepted; visual
            # similarity and bare filenames are not.
            left_native = {(item.site.value, item.photo_id) for item in left_inv.media}
            right_native = {
                (item.site.value, item.photo_id) for item in right_inv.media
            }
            left_provenance = {
                (item.provenance_key[0].value, item.provenance_key[1])
                for item in left_inv.media
                if item.provenance_key
            }
            right_provenance = {
                (item.provenance_key[0].value, item.provenance_key[1])
                for item in right_inv.media
                if item.provenance_key
            }
            shared_media = tuple(
                sorted(
                    (left_native & right_native)
                    | (left_provenance & right_native)
                    | (right_provenance & left_native)
                    | (left_provenance & right_provenance)
                )
            )
            if shared_media:
                result.append(
                    _evidence_edge(
                        left_inv,
                        right_inv,
                        "native_media_identity",
                        "strong",
                        "Exact source-qualified photo identity.",
                        *(f"{site}:{photo_id}" for site, photo_id in shared_media),
                    )
                )

            # Date is acceptable only as a corroborating part of a precise
            # coordinate+date edge. Broad named locality never participates.
            coordinate_values = (
                left_hyd.latitude,
                left_hyd.longitude,
                right_hyd.latitude,
                right_hyd.longitude,
                left_hyd.accuracy_m,
                right_hyd.accuracy_m,
            )
            coordinate_semantics_permit = all(
                hyd.coordinate_source
                in {
                    "explicit_public",
                    "explicit_authorized_private",
                }
                and (
                    hyd.coordinate_privacy_state in {"", "open", "public"}
                    or hyd.coordinate_source == "explicit_authorized_private"
                )
                for hyd in (left_hyd, right_hyd)
            )
            if (
                left_inv.observed_on
                and left_inv.observed_on == right_inv.observed_on
                and left_hyd.coordinates_available
                and right_hyd.coordinates_available
                and None not in coordinate_values
                and all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    for value in coordinate_values
                )
                and -90.0 <= float(left_hyd.latitude) <= 90.0
                and -90.0 <= float(right_hyd.latitude) <= 90.0
                and -180.0 <= float(left_hyd.longitude) <= 180.0
                and -180.0 <= float(right_hyd.longitude) <= 180.0
                and 0.0 < float(left_hyd.accuracy_m) <= 100.0
                and 0.0 < float(right_hyd.accuracy_m) <= 100.0
                and coordinate_semantics_permit
            ):
                distance = _distance_m(
                    left_hyd.latitude,
                    left_hyd.longitude,  # type: ignore[arg-type]
                    right_hyd.latitude,
                    right_hyd.longitude,  # type: ignore[arg-type]
                )
                if distance <= 100.0:
                    result.append(
                        _evidence_edge(
                            left_inv,
                            right_inv,
                            "exact_date_close_coordinates",
                            "corroborating",
                            "Exact observation date plus coordinates within 100 metres.",
                            left_inv.observed_on.isoformat(),
                            right_inv.observed_on.isoformat(),
                            f"{left_hyd.latitude:.6f}",
                            f"{left_hyd.longitude:.6f}",
                            f"{right_hyd.latitude:.6f}",
                            f"{right_hyd.longitude:.6f}",
                            f"{left_hyd.accuracy_m:.3f}",
                            f"{right_hyd.accuracy_m:.3f}",
                            f"{distance:.3f}",
                            "threshold_m=100.000",
                            "coordinate_policy=phase_2b_v2",
                        )
                    )
    return tuple(
        sorted(
            result,
            key=lambda edge: (
                edge.left_site.value,
                edge.left_observation_id,
                edge.right_site.value,
                edge.right_observation_id,
                edge.evidence_type,
            ),
        )
    )


def _evidence_unavailable_notices(
    hydrated: Sequence[tuple[InventoryObservation, HydratedObservation]],
) -> tuple[str, ...]:
    """Safe explanations for potentially useful evidence that failed closed.

    Mirrors every rejection in ``_supporting_evidence``'s coordinate+date gate,
    so a pair that looks like an obvious date/locality match never fails closed
    with no explanation at all.
    """
    from .matching import _distance_m

    notices: list[str] = []
    for index, (left_inv, left_hyd) in enumerate(hydrated):
        for right_inv, right_hyd in hydrated[index + 1 :]:
            if (
                not left_inv.observed_on
                or left_inv.observed_on != right_inv.observed_on
                or not left_hyd.coordinates_available
                or not right_hyd.coordinates_available
            ):
                continue
            label = (
                f"{left_inv.key.site.value} #{left_inv.key.observation_id} ↔ "
                f"{right_inv.key.site.value} #{right_inv.key.observation_id}"
            )
            accuracies = (left_hyd.accuracy_m, right_hyd.accuracy_m)
            if any(
                value is None
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 < float(value) <= 100.0
                for value in accuracies
            ):
                notices.append(
                    f"{label}: coordinate/date support unavailable because both "
                    "positional accuracies must be explicit, finite, positive, "
                    "and no greater than 100 m."
                )
                continue
            if any(
                hyd.coordinate_source
                not in {
                    "explicit_public",
                    "explicit_authorized_private",
                }
                or (
                    hyd.coordinate_privacy_state not in {"", "open", "public"}
                    and hyd.coordinate_source != "explicit_authorized_private"
                )
                for hyd in (left_hyd, right_hyd)
            ):
                notices.append(
                    f"{label}: coordinate/date support unavailable because "
                    "coordinate privacy or source semantics do not permit comparison."
                )
                continue
            points = (
                left_hyd.latitude,
                left_hyd.longitude,
                right_hyd.latitude,
                right_hyd.longitude,
            )
            if any(
                value is None
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in points
            ) or not (
                -90.0 <= float(left_hyd.latitude or 0.0) <= 90.0
                and -90.0 <= float(right_hyd.latitude or 0.0) <= 90.0
                and -180.0 <= float(left_hyd.longitude or 0.0) <= 180.0
                and -180.0 <= float(right_hyd.longitude or 0.0) <= 180.0
            ):
                notices.append(
                    f"{label}: coordinate/date support unavailable because a "
                    "reported point is missing, non-finite, or outside valid "
                    "latitude/longitude bounds."
                )
                continue
            distance = _distance_m(
                left_hyd.latitude,
                left_hyd.longitude,  # type: ignore[arg-type]
                right_hyd.latitude,
                right_hyd.longitude,  # type: ignore[arg-type]
            )
            if distance > 100.0:
                notices.append(
                    f"{label}: coordinate/date support unavailable because the "
                    f"two points are {distance:.0f} m apart, beyond the 100 m "
                    "corroboration threshold."
                )
    return tuple(notices)


def _graph_reasons(
    keys: Sequence[tuple[RemoteSite, int]],
    edges: Sequence[ConsolidationEvidenceEdge],
    *,
    canonical_mo_id: Optional[int] = None,
    canonical_inat_id: Optional[int] = None,
) -> list[str]:
    return list(
        validate_consolidation_graph(
            keys,
            edges,
            canonical_mo_id=canonical_mo_id,
            canonical_inat_id=canonical_inat_id,
        ).reasons
    )


def _donor_paths(
    keys: Sequence[tuple[RemoteSite, int]],
    edges: Sequence[ConsolidationEvidenceEdge],
    canonical_mo_id: Optional[int],
    canonical_inat_id: Optional[int],
) -> tuple[ConsolidationEvidencePath, ...]:
    validation = validate_consolidation_graph(
        keys,
        edges,
        canonical_mo_id=canonical_mo_id,
        canonical_inat_id=canonical_inat_id,
    )
    return _render_donor_paths(validation)


def _render_donor_paths(
    validation: ConsolidationGraphValidation,
) -> tuple[ConsolidationEvidencePath, ...]:
    paths: list[ConsolidationEvidencePath] = []
    for path in validation.donor_paths:
        steps = tuple(
            f"{hop.left[0].value} #{hop.left[1]} → "
            f"{hop.edge.display_summary} "
            f"[{'IDENTITY — required strong anchor' if hop.edge.evidence_strength == 'strong' else 'SUPPORTING only'}] "
            f"→ {hop.right[0].value} #{hop.right[1]}"
            for hop in path.hops
        )
        paths.append(
            ConsolidationEvidencePath(
                donor_site=path.donor[0],
                donor_observation_id=path.donor[1],
                steps=steps,
                strong_anchor_step=steps[path.strong_anchor_index],
            )
        )
    return tuple(paths)


def check_duplicate_set_eligibility(
    db: ReconciliationDB,
    profile: ReconciliationProfile,
    hydrated: Sequence[tuple[InventoryObservation, HydratedObservation]],
    *,
    retry_consolidation_id: Optional[int] = None,
    allowed_consolidation_id: Optional[int] = None,
    evidence_edges: Sequence[ConsolidationEvidenceEdge] = (),
    evidence_unavailable: Sequence[str] = (),
) -> ConsolidationEligibility:
    """M2 eligibility rules over a caller-hydrated candidate set.

    ``hydrated`` must already be a FRESH read (the caller is responsible for
    the actual network fetch — this function only validates what was
    fetched). A non-empty ``blocking_reasons`` result means the candidate set
    may not proceed to a consolidation preview at all.
    """
    if len(hydrated) < 2:
        return ConsolidationEligibility(
            eligible=False,
            blocking_reasons=("A duplicate set requires at least two records.",),
        )
    profile_id = profile.profile_id
    reasons: list[str] = []
    keys = [inv.key for inv, _ in hydrated]
    if len(set(keys)) != len(keys):
        reasons.append("The candidate set contains the same record more than once.")
    site_counts = {
        site: sum(1 for inv, _ in hydrated if inv.key.site is site)
        for site in RemoteSite
    }
    if not any(count >= 2 for count in site_counts.values()):
        reasons.append(
            "The reviewed set has no duplicate observations on either site; "
            "at least one site must contribute a canonical and a donor record."
        )

    for inv, hyd in hydrated:
        label = f"{inv.key.site.value} #{inv.key.observation_id}"
        if inv.owner_id != inv.account_id:
            reasons.append(f"{label} is not owned by the profile's remote account.")
        if inv.fungi_status == "nonfungal":
            reasons.append(f"{label} is outside kingdom Fungi.")
        elif inv.fungi_status == "unknown":
            reasons.append(
                f"{label} fungal classification is unknown and requires review."
            )
        if inv.deleted or inv.availability_state == "deleted":
            reasons.append(f"{label} is deleted or unavailable.")
        if inv.scope_state not in {"in_scope", "linked_context"}:
            reasons.append(
                f"{label} is excluded or outside the selected profile scope."
            )
        if not hyd.required_values_available:
            reasons.append(
                f"{label} has a required specimen-identity value hidden or unavailable."
            )
        existing_membership = db.consolidation_membership_for_observation(
            profile_id,
            inv.key.site.value,
            inv.key.observation_id,
        )
        if existing_membership is not None:
            permitted_identity = (
                retry_consolidation_id
                if retry_consolidation_id is not None
                else allowed_consolidation_id
            )
            if int(existing_membership["consolidation_id"]) != permitted_identity:
                reasons.append(
                    f"{label} already belongs to consolidation "
                    f"{int(existing_membership['consolidation_id'])} "
                    f"({existing_membership['consolidation_state']})."
                )

    # Confirmed one-to-one pair conflicts: an MO candidate already confirmed-
    # paired to an iNat observation NOT also in this candidate set is a
    # conflict requiring explicit resolution, never silent override.
    #
    # This applies only to a CROSS-SITE set, where the attempt writes canonical
    # reciprocal links that would contradict the existing confirmed pair. A
    # same-site-only set writes no link at all, so an outside confirmed partner
    # is only a hazard when the paired record becomes a donor — which is not
    # known until canonical selection and is enforced there instead
    # (see select_canonical). Blocking it here made same-site consolidation of
    # any already-paired record impossible.
    candidate_mo_ids = {
        inv.key.observation_id for inv, _ in hydrated if inv.key.site == RemoteSite.MO
    }
    candidate_inat_ids = {
        inv.key.observation_id for inv, _ in hydrated if inv.key.site == RemoteSite.INAT
    }
    cross_site_set = bool(candidate_mo_ids) and bool(candidate_inat_ids)
    for inv, _ in hydrated:
        if inv.key.site != RemoteSite.MO or not cross_site_set:
            continue
        for mo_id, inat_id in db.confirmed_pair_keys(profile_id):
            if mo_id == inv.key.observation_id and inat_id not in candidate_inat_ids:
                reasons.append(
                    f"mo #{inv.key.observation_id} is already confirmed-paired to "
                    f"inat #{inat_id}, which is not in this candidate set."
                )

    for mo_id in candidate_mo_ids:
        for inat_id in candidate_inat_ids:
            pair = db.pair_by_records(profile_id, mo_id, inat_id)
            if pair and pair.get("excluded"):
                reasons.append(
                    f"mo #{mo_id} ↔ inat #{inat_id} is locally excluded; reopen it "
                    "before consolidation."
                )
    for inv, _ in hydrated:
        if inv.key.site != RemoteSite.INAT or not cross_site_set:
            continue
        for mo_id, inat_id in db.confirmed_pair_keys(profile_id):
            if inat_id == inv.key.observation_id and mo_id not in candidate_mo_ids:
                reasons.append(
                    f"inat #{inv.key.observation_id} is already confirmed-paired to "
                    f"mo #{mo_id}, which is not in this candidate set."
                )

    # Pairwise specimen-identity conflicts (owner/scope/date/voucher/
    # coordinates), reused from the shared conflict checker used everywhere
    # else in this codebase.
    for i in range(len(hydrated)):
        for j in range(i + 1, len(hydrated)):
            left_inv, left_hyd = hydrated[i]
            right_inv, right_hyd = hydrated[j]
            conflicts, unavailable = specimen_identity_conflicts(left_hyd, right_hyd)
            label = f"{left_inv.key.site.value} #{left_inv.key.observation_id} vs {right_inv.key.site.value} #{right_inv.key.observation_id}"
            for conflict in conflicts:
                reasons.append(f"{label}: {conflict}")
            for item in unavailable:
                reasons.append(f"{label}: required evidence unavailable: {item}")

    reasons.extend(
        _graph_reasons(
            [(inv.key.site, inv.key.observation_id) for inv, _ in hydrated],
            evidence_edges,
        )
    )

    deduped_reasons = list(dict.fromkeys(reasons))
    return ConsolidationEligibility(
        eligible=not deduped_reasons,
        blocking_reasons=tuple(deduped_reasons),
        supporting_evidence=tuple(
            f"{edge.left_site.value} #{edge.left_observation_id} ↔ "
            f"{edge.right_site.value} #{edge.right_observation_id}: "
            f"{edge.display_summary} [{edge.evidence_strength}]"
            for edge in evidence_edges
        ),
        evidence_unavailable=tuple(evidence_unavailable),
    )


def _member_snapshot(
    inv: InventoryObservation,
    hyd: HydratedObservation,
    raw: dict[str, Any],
    *,
    photo_payload: object = None,
    link_snapshots: Sequence[AuthoritativeLinkSnapshot] = (),
) -> ConsolidationMemberSnapshot:
    remote_uuid = str(raw.get("uuid") or "")
    geoprivacy = str(raw.get("geoprivacy") or raw.get("obscuration") or "")
    if inv.key.site is RemoteSite.INAT:
        photos = _inat_photo_snapshots(raw, inv.key.observation_id)
        photo_fingerprint = _inat_photos_fingerprint(photos)
    else:
        photos = _mo_photo_snapshots(
            (
                photo_payload
                if photo_payload is not None
                else {"results": raw.get("images") or []}
            ),
            inv.key.observation_id,
        )
        photo_fingerprint = _mo_photos_fingerprint(photos)
    sequence_summaries = ("Not inspected in Phase 2B.",)
    sequence_fingerprint = public_fingerprint("phase_2b_sequence_uninspected")
    if not link_snapshots:
        link_snapshots = _link_rows_as_snapshots(inv)
    link_fingerprint = public_fingerprint(
        "links",
        *(
            f"{row.row_id}|{row.row_uuid}|{row.target_observation_id}|"
            f"{row.parse_state}|{row.row_fingerprint}"
            for row in sorted(
                link_snapshots, key=lambda item: (item.row_id, item.row_uuid)
            )
        ),
    )
    stable_fingerprint = public_fingerprint(
        "consolidation_member",
        inv.key.site.value,
        inv.key.observation_id,
        remote_uuid,
        inv.owner_id,
        inv.account_id,
        inv.owner_login,
        inv.observed_on,
        inv.taxon_id,
        (inv.taxon_name or "").casefold(),
        inv.fungi_status,
        int(inv.deleted),
        inv.availability_state,
        "|",
        *sorted(hyd.voucher_identifiers),
        "|",
        *sorted(hyd.collection_identifiers),
        "|",
        public_fingerprint(hyd.description),
        photo_fingerprint,
        geoprivacy,
        inv.public_locality,
        "|",
        int(hyd.coordinates_available),
        hyd.latitude,
        hyd.longitude,
        hyd.accuracy_m,
    )
    fingerprint = public_fingerprint(
        stable_fingerprint,
        inv.updated_at,
        link_fingerprint,
    )
    mutable_components = tuple(
        sorted(
            {
                "date": public_fingerprint(inv.observed_on),
                "description": public_fingerprint(hyd.description),
                "geoprivacy": public_fingerprint(geoprivacy),
                "identifiers": public_fingerprint(
                    *sorted(hyd.voucher_identifiers),
                    "|",
                    *sorted(hyd.collection_identifiers),
                ),
                "locality_coordinates": public_fingerprint(
                    inv.public_locality,
                    int(hyd.coordinates_available),
                    hyd.latitude,
                    hyd.longitude,
                    hyd.accuracy_m,
                ),
                "photos": photo_fingerprint,
                "taxon": public_fingerprint(
                    inv.taxon_id,
                    (inv.taxon_name or "").casefold(),
                    inv.taxon_rank,
                ),
            }.items()
        )
    )
    return ConsolidationMemberSnapshot(
        site=inv.key.site,
        observation_id=inv.key.observation_id,
        remote_uuid=remote_uuid,
        owner_login=inv.owner_login,
        owner_id=inv.owner_id,
        account_id=inv.account_id,
        taxon_id=inv.taxon_id,
        taxon_name=inv.taxon_name,
        taxon_rank=inv.taxon_rank,
        observed_on_string=inv.observed_on.isoformat() if inv.observed_on else "",
        locality=inv.public_locality,
        latitude=hyd.latitude,
        longitude=hyd.longitude,
        accuracy_m=hyd.accuracy_m,
        geoprivacy=geoprivacy,
        description=hyd.description,
        voucher_identifiers=hyd.voucher_identifiers,
        collection_identifiers=hyd.collection_identifiers,
        accessions=hyd.accessions,
        sequence_summaries=sequence_summaries,
        photos=photos,
        reciprocal_links=tuple(link_snapshots),
        photo_metadata_fingerprint=photo_fingerprint,
        sequence_metadata_fingerprint=sequence_fingerprint,
        reciprocal_link_state=", ".join(
            f"{row.parse_state}→{row.target_observation_id or '?'}"
            for row in link_snapshots
        )
        or "none",
        remote_updated_at=inv.updated_at.isoformat() if inv.updated_at else "",
        record_fingerprint=fingerprint,
        preflight_fingerprint=stable_fingerprint,
        identity_fingerprint=canonical_stable_identity_fingerprint(
            inv.key.site,
            inv.key.observation_id,
            remote_uuid,
            inv.owner_id,
        ),
        mutable_component_fingerprints=mutable_components,
    )


def _compute_conflicts(
    members: Sequence[ConsolidationMemberSnapshot],
) -> tuple[ConsolidationConflict, ...]:
    conflicts: list[ConsolidationConflict] = []
    dates = {m.observed_on_string for m in members if m.observed_on_string}
    if len(dates) > 1:
        conflicts.append(
            ConsolidationConflict(
                conflict_type="observed_on",
                description=f"Observation dates differ: {sorted(dates)}.",
                blocking=False,
            )
        )
    taxa = {m.taxon_name for m in members if m.taxon_name}
    if len(taxa) > 1:
        conflicts.append(
            ConsolidationConflict(
                conflict_type="taxon",
                description=f"Taxa differ: {sorted(taxa)}.",
                blocking=False,
            )
        )
    descriptions = {m.description.strip() for m in members if m.description.strip()}
    if len(descriptions) > 1:
        conflicts.append(
            ConsolidationConflict(
                conflict_type="description",
                description="Descriptions/notes differ between members.",
                blocking=False,
            )
        )
    coords = [
        (m.latitude, m.longitude)
        for m in members
        if m.latitude is not None and m.longitude is not None
    ]
    if len(set(coords)) > 1:
        conflicts.append(
            ConsolidationConflict(
                conflict_type="coordinates",
                description="Coordinates differ between members.",
                blocking=False,
            )
        )
    voucher_sets = [
        set(m.voucher_identifiers) for m in members if m.voucher_identifiers
    ]
    if len(voucher_sets) > 1 and any(
        s1 != s2 for s1 in voucher_sets for s2 in voucher_sets
    ):
        conflicts.append(
            ConsolidationConflict(
                conflict_type="voucher_identifier",
                description="Voucher/collection identifiers are not identical across all members.",
                blocking=False,
            )
        )
    return tuple(conflicts)


def prepare_preview(
    db: ReconciliationDB,
    profile: ReconciliationProfile,
    candidates: Sequence[tuple[RemoteSite, int, dict[str, Any]]],
    reader: INatReconciliationReader,
    *,
    auth_generation: int = 0,
    mo_key_generation: int = 0,
    inat_mo_field_id: Optional[int] = None,
    retry_consolidation_id: Optional[int] = None,
    extension_consolidation_id: Optional[int] = None,
    fixed_canonical_mo_observation_id: Optional[int] = None,
    fixed_canonical_inat_observation_id: Optional[int] = None,
    previous_members: Sequence[PriorConsolidationMember] = (),
    photo_payloads: Optional[dict[tuple[RemoteSite, int], object]] = None,
    link_snapshots: Optional[
        dict[tuple[RemoteSite, int], Sequence[AuthoritativeLinkSnapshot]]
    ] = None,
) -> ConsolidationPreview:
    """M3 preview: hydrate every candidate, run M2 eligibility, list
    conflicts and the full v1 disclosure list. Canonical is always unset."""
    if not candidates:
        raise ConsolidationError(
            "A duplicate set requires at least one candidate.", "empty_candidates"
        )
    hydrated = [
        _hydrate_candidate(
            site,
            observation_id,
            raw,
            profile,
            reader,
            inat_mo_field_id=inat_mo_field_id,
        )
        for site, observation_id, raw in candidates
    ]
    link_snapshots = link_snapshots or {}
    evidence_edges = _supporting_evidence(hydrated, link_snapshots)
    evidence_unavailable = _evidence_unavailable_notices(hydrated)
    eligibility = check_duplicate_set_eligibility(
        db,
        profile,
        hydrated,
        retry_consolidation_id=retry_consolidation_id,
        allowed_consolidation_id=extension_consolidation_id,
        evidence_edges=evidence_edges,
        evidence_unavailable=evidence_unavailable,
    )
    photo_payloads = photo_payloads or {}
    members = tuple(
        _member_snapshot(
            inv,
            hyd,
            raw,
            photo_payload=photo_payloads.get((inv.key.site, inv.key.observation_id)),
            link_snapshots=link_snapshots.get(
                (inv.key.site, inv.key.observation_id), ()
            ),
        )
        for (inv, hyd), (_, _, raw) in zip(hydrated, candidates)
    )
    confirmed_pairs = db.confirmed_pair_keys(profile.profile_id)
    enriched_members: list[ConsolidationMemberSnapshot] = []
    for member in members:
        partner_site: Optional[RemoteSite] = None
        partner_id: Optional[int] = None
        for mo_id, inat_id in confirmed_pairs:
            if member.site is RemoteSite.MO and member.observation_id == mo_id:
                partner_site, partner_id = RemoteSite.INAT, inat_id
                break
            if member.site is RemoteSite.INAT and member.observation_id == inat_id:
                partner_site, partner_id = RemoteSite.MO, mo_id
                break
        enriched_members.append(
            replace(
                member,
                current_pair_partner_site=partner_site,
                current_pair_partner_id=partner_id,
                current_pair_review_state="confirmed" if partner_id is not None else "",
            )
        )
    conflicts = _compute_conflicts(members)
    preview = ConsolidationPreview(
        profile_id=profile.profile_id,
        consolidation_id=(
            extension_consolidation_id
            if extension_consolidation_id is not None
            else retry_consolidation_id
        ),
        members=tuple(enriched_members),
        eligibility=eligibility,
        auth_generation=auth_generation,
        mo_key_generation=mo_key_generation,
        canonical_mo_observation_id=None,
        canonical_inat_observation_id=None,
        unsupported_items=UNSUPPORTED_ITEM_DISCLOSURES,
        conflicts=conflicts,
        local_changes_preview=(),
        evidence_edges=evidence_edges,
        is_extension=extension_consolidation_id is not None,
        previous_members=tuple(previous_members),
    )
    if extension_consolidation_id is not None:
        return select_canonical(
            preview,
            fixed_canonical_mo_observation_id,
            fixed_canonical_inat_observation_id,
        )
    return preview


def select_canonical(
    preview: ConsolidationPreview,
    canonical_mo_observation_id: Optional[int],
    canonical_inat_observation_id: Optional[int],
) -> ConsolidationPreview:
    """Pure function: returns a new preview with the canonical selection
    applied. Never accepts an id outside the candidate set."""
    if (
        preview.is_extension
        and (
            canonical_mo_observation_id != preview.canonical_mo_observation_id
            or canonical_inat_observation_id != preview.canonical_inat_observation_id
        )
        and (
            preview.canonical_mo_observation_id is not None
            or preview.canonical_inat_observation_id is not None
        )
    ):
        raise ConsolidationError(
            "Canonical choices are fixed when adding donors to an existing consolidation.",
            "canonical_change_not_supported",
        )
    sites = {member.site for member in preview.members}
    if RemoteSite.MO in sites and canonical_mo_observation_id is None:
        raise ConsolidationError(
            "Select the canonical Mushroom Observer observation.",
            "no_canonical_selected",
        )
    if RemoteSite.INAT in sites and canonical_inat_observation_id is None:
        raise ConsolidationError(
            "Select the canonical iNaturalist observation.",
            "no_canonical_selected",
        )
    member_keys = {(m.site, m.observation_id) for m in preview.members}
    if (
        canonical_mo_observation_id is not None
        and (RemoteSite.MO, canonical_mo_observation_id) not in member_keys
    ):
        raise ConsolidationError(
            f"mo #{canonical_mo_observation_id} is not a member of this candidate set.",
            "canonical_not_in_set",
        )
    if (
        canonical_inat_observation_id is not None
        and (RemoteSite.INAT, canonical_inat_observation_id) not in member_keys
    ):
        raise ConsolidationError(
            f"inat #{canonical_inat_observation_id} is not a member of this candidate set.",
            "canonical_not_in_set",
        )
    graph_validation = validate_consolidation_graph(
        [(member.site, member.observation_id) for member in preview.members],
        preview.evidence_edges,
        canonical_mo_id=canonical_mo_observation_id,
        canonical_inat_id=canonical_inat_observation_id,
    )
    graph_reasons = list(graph_validation.reasons)
    # A canonical record with an authoritative link to another observation is
    # an identity conflict. Donor links remain untouched and do not interfere
    # unless they supplied a reviewed edge in this attempt.
    selected_targets = {
        RemoteSite.MO: canonical_inat_observation_id,
        RemoteSite.INAT: canonical_mo_observation_id,
    }
    for member in preview.members:
        selected_id = (
            canonical_mo_observation_id
            if member.site is RemoteSite.MO
            else canonical_inat_observation_id
        )
        if member.observation_id != selected_id:
            continue
        if selected_targets[member.site] is None:
            # Same-site-only consolidation: there is no opposite canonical, and
            # ``execute_group`` writes no link at all in that shape, so an
            # existing cross-site link cannot be overwritten and is not a
            # conflict. Comparing against None would reject every legitimately
            # linked canonical record.
            continue
        wrong_targets = {
            row.target_observation_id
            for row in member.reciprocal_links
            if row.parse_state not in {"malformed"}
            and row.target_observation_id is not None
            and row.target_observation_id != selected_targets[member.site]
        }
        if wrong_targets:
            graph_reasons.append(
                f"The selected canonical {member.site.value} observation has an "
                "authoritative link to a different observation; Phase 2B will not overwrite it."
            )
    # A record that is confirmed-paired to an observation outside this set may
    # stay canonical, but it must never become a donor: the surviving confirmed
    # pair would then point at a locally superseded record.
    for member in preview.members:
        is_canonical = (
            member.observation_id == canonical_mo_observation_id
            if member.site is RemoteSite.MO
            else member.observation_id == canonical_inat_observation_id
        )
        if is_canonical or member.current_pair_partner_id is None:
            continue
        partner = (
            member.current_pair_partner_site,
            member.current_pair_partner_id,
        )
        if partner not in member_keys:
            graph_reasons.append(
                f"{member.site.value} #{member.observation_id} would be marked "
                f"superseded, but it is still confirmed-paired to "
                f"{partner[0].value if partner[0] else '?'} #{partner[1]}, "
                "which is not in this candidate set. Resolve that pair first."
            )

    base_reasons = list(preview.eligibility.blocking_reasons)
    reasons = tuple(dict.fromkeys((*base_reasons, *graph_reasons)))
    eligibility = replace(
        preview.eligibility,
        eligible=not reasons,
        blocking_reasons=reasons,
    )
    paths = _render_donor_paths(graph_validation)
    expected_donors = (
        len(preview.members)
        - int(canonical_mo_observation_id is not None)
        - int(canonical_inat_observation_id is not None)
    )
    if len(paths) != expected_donors:
        eligibility = replace(
            eligibility,
            eligible=False,
            blocking_reasons=tuple(
                dict.fromkeys(
                    (
                        *eligibility.blocking_reasons,
                        "At least one donor has no auditable evidence path to the selected "
                        "canonical specimen.",
                    )
                )
            ),
        )

    local_changes: list[str] = []
    if (
        canonical_mo_observation_id is not None
        and canonical_inat_observation_id is not None
    ):
        local_changes.append(
            f"mo #{canonical_mo_observation_id} and inat #{canonical_inat_observation_id} "
            f"become the canonical pair."
        )
    for member in preview.members:
        is_canonical = (
            member.site == RemoteSite.MO
            and member.observation_id == canonical_mo_observation_id
        ) or (
            member.site == RemoteSite.INAT
            and member.observation_id == canonical_inat_observation_id
        )
        if not is_canonical:
            local_changes.append(
                f"{member.site.value} #{member.observation_id} will be marked locally superseded."
            )
    return replace(
        preview,
        eligibility=eligibility,
        canonical_mo_observation_id=canonical_mo_observation_id,
        canonical_inat_observation_id=canonical_inat_observation_id,
        local_changes_preview=tuple(local_changes),
        donor_evidence_paths=paths,
    )


@dataclass(frozen=True)
class ConsolidationActionResult:
    action_id: int
    state: str
    message: str


class ConsolidationService:
    """Synchronous Gate 2B saga service; run only in the action worker pool."""

    def __init__(
        self,
        db: ReconciliationDB,
        inat_client: INatClient,
        mo_client: MOClient,
        auth_provider: Callable[[], AuthState],
        mo_key_provider: Callable[[int], str],
        auth_generation_provider: Callable[[], int],
        mo_key_generation_provider: Callable[[], int],
        link_service: LinkRepairService,
    ) -> None:
        self.db = db
        self.inat_client = inat_client
        self.mo_client = mo_client
        self.auth_provider = auth_provider
        self.mo_key_provider = mo_key_provider
        self.auth_generation_provider = auth_generation_provider
        self.mo_key_generation_provider = mo_key_generation_provider
        self.link_service = link_service
        self.reader = INatReconciliationReader(inat_client)

    def prepare_preview(
        self,
        profile_id: int,
        candidates: Sequence[tuple[RemoteSite, int]],
        cancelled: Callable[[], bool],
    ) -> ConsolidationPreview:
        if len(set(candidates)) != len(candidates):
            raise ConsolidationError(
                "The duplicate set contains the same observation more than once.",
                "duplicate_member",
            )
        profile = self.db.profile(profile_id)
        requested = tuple(candidates)
        memberships = [
            self.db.consolidation_membership_for_observation(
                profile_id,
                site.value,
                observation_id,
            )
            for site, observation_id in requested
        ]
        existing_ids = {
            int(row["consolidation_id"]) for row in memberships if row is not None
        }
        if len(existing_ids) > 1:
            raise ConsolidationError(
                "The selected records belong to different stable consolidations. "
                "Automatically merging finalized consolidations is not supported.",
                "existing_consolidation_conflict",
            )
        extension_id: Optional[int] = None
        previous_members: tuple[PriorConsolidationMember, ...] = ()
        fixed_mo_id: Optional[int] = None
        fixed_inat_id: Optional[int] = None
        retry_consolidation_id: Optional[int] = None
        if existing_ids:
            existing_id = next(iter(existing_ids))
            identity = self.db.get_consolidation(profile_id, existing_id)
            if identity and str(identity["state"]) == "finalized":
                extension_id = existing_id
                fixed_mo_id = (
                    int(identity["canonical_mo_observation_id"])
                    if identity.get("canonical_mo_observation_id") is not None
                    else None
                )
                fixed_inat_id = (
                    int(identity["canonical_inat_observation_id"])
                    if identity.get("canonical_inat_observation_id") is not None
                    else None
                )
                old_rows = self.db.list_consolidation_members(profile_id, existing_id)
                previous_members = tuple(
                    PriorConsolidationMember(
                        site=RemoteSite(str(row["site"])),
                        observation_id=int(row["observation_id"]),
                        role=str(row["role"]),
                        local_state=str(row["local_state"]),
                        added_by_attempt_id=(
                            int(row["added_by_attempt_id"])
                            if row.get("added_by_attempt_id") is not None
                            else None
                        ),
                        superseded_by_attempt_id=(
                            int(row["superseded_by_attempt_id"])
                            if row.get("superseded_by_attempt_id") is not None
                            else None
                        ),
                        superseded_at=str(row.get("superseded_at") or ""),
                    )
                    for row in old_rows
                    if str(row["role"]) == "donor"
                    and str(row["local_state"]) == "superseded"
                )
                detail = self.db.consolidation_detail(profile_id, existing_id)
                attempts = list(detail.get("attempts", ())) if detail else []
                latest = attempts[-1] if attempts else None
                latest_state = str(latest.get("state") or "") if latest else ""
                if latest_state in {"pending", "outcome_unknown"}:
                    raise ConsolidationError(
                        "The existing consolidation has an unresolved attempt. "
                        "Resume or verify it before adding donors.",
                        "extension_attempt_unresolved",
                    )
                new_candidates = tuple(
                    item
                    for item, membership in zip(requested, memberships)
                    if membership is None
                )
                extension_retry = latest_state in {"failed", "cancelled"}
                if not new_candidates:
                    raise ConsolidationError(
                        "Add at least one observation that is not already an admitted "
                        "member of this finalized consolidation.",
                        "no_new_donors",
                    )
                reviewed_donors = new_candidates
                canonical_specs = tuple(
                    item
                    for item in (
                        (RemoteSite.MO, fixed_mo_id) if fixed_mo_id else None,
                        (RemoteSite.INAT, fixed_inat_id) if fixed_inat_id else None,
                    )
                    if item is not None
                )
                candidates = tuple(dict.fromkeys((*canonical_specs, *reviewed_donors)))
            else:
                detail = self.db.consolidation_detail(profile_id, existing_id)
                prior_attempts = list(detail.get("attempts") or ()) if detail else []
                latest = prior_attempts[-1] if prior_attempts else None
                canonical_requested = {
                    (RemoteSite(str(row["site"])), int(row["observation_id"]))
                    for row in self.db.list_consolidation_members(
                        profile_id,
                        existing_id,
                    )
                    if str(row["role"]) == "canonical"
                }
                retry_consolidation_id = (
                    existing_id
                    if identity
                    and str(identity["state"]) in {"draft", "confirmed"}
                    and latest
                    and str(latest.get("state")) in {"failed", "cancelled"}
                    and canonical_requested.issubset(set(requested))
                    else None
                )
                if retry_consolidation_id is None:
                    raise ConsolidationError(
                        "A selected observation belongs to an unfinished consolidation "
                        "that cannot be extended or superseded.",
                        "existing_consolidation_unavailable",
                    )
        # No `else` retry lookup: reaching here means every requested member
        # returned None from consolidation_membership_for_observation, which
        # checks both the admitted sync_consolidation_members rows and the
        # reserved proposals. A set with no membership anywhere cannot be a
        # retry of an earlier consolidation, so retry_consolidation_id stays
        # None. Retrying a failed draft/confirmed identity is handled above,
        # where the members DO still carry their membership rows.
        auth_generation = self.auth_generation_provider()
        mo_key_generation = self.mo_key_generation_provider()
        data = self._fetch_review_data(profile, candidates, cancelled)
        preview = prepare_preview(
            self.db,
            profile,
            data["candidates"],
            self.reader,
            auth_generation=auth_generation,
            mo_key_generation=mo_key_generation,
            inat_mo_field_id=data["inat_mo_field_id"],
            retry_consolidation_id=retry_consolidation_id,
            extension_consolidation_id=extension_id,
            fixed_canonical_mo_observation_id=fixed_mo_id,
            fixed_canonical_inat_observation_id=fixed_inat_id,
            previous_members=previous_members,
            photo_payloads=data["photo_payloads"],
            link_snapshots=data["link_snapshots"],
        )
        if extension_id is not None:
            identity = self.db.get_consolidation(profile_id, extension_id)
            baseline_id = (
                int(identity["current_finalized_attempt_id"])
                if identity and identity.get("current_finalized_attempt_id") is not None
                else None
            )
            baseline_by_key = {
                (RemoteSite(str(row["site"])), int(row["observation_id"])): row
                for row in (
                    self.db.consolidation_attempt_members(profile_id, baseline_id)
                    if baseline_id is not None
                    else ()
                )
                if str(row["participation_role"]) == "canonical_context"
            }
            baseline_anchor_edges = tuple(
                ConsolidationEvidenceEdge(
                    left_site=RemoteSite(str(row["left_site"])),
                    left_observation_id=int(row["left_observation_id"]),
                    right_site=RemoteSite(str(row["right_site"])),
                    right_observation_id=int(row["right_observation_id"]),
                    evidence_type=str(row["evidence_type"]),
                    evidence_strength=str(row["evidence_strength"]),
                    reviewed_evidence_fingerprint=str(
                        row["reviewed_evidence_fingerprint"]
                    ),
                    display_summary=str(row["display_summary"]),
                )
                for row in (
                    self.db.consolidation_evidence(profile_id, baseline_id)
                    if baseline_id is not None
                    else ()
                )
            )
            baseline_anchors = canonical_strong_anchor_signatures(
                baseline_anchor_edges,
                canonical_mo_id=fixed_mo_id,
                canonical_inat_id=fixed_inat_id,
            )
            current_anchors = canonical_strong_anchor_signatures(
                preview.evidence_edges,
                canonical_mo_id=fixed_mo_id,
                canonical_inat_id=fixed_inat_id,
            )
            identity_drifted = [
                member
                for member in preview.canonical_members
                if (
                    baseline_by_key.get((member.site, member.observation_id)) is None
                    or str(
                        baseline_by_key[(member.site, member.observation_id)].get(
                            "reviewed_identity_fingerprint"
                        )
                        or ""
                    )
                    != member.identity_fingerprint
                )
            ]
            missing_identity_anchors = baseline_anchors - current_anchors
            if identity_drifted or missing_identity_anchors:
                reason = (
                    "A canonical record's stable remote identity, reviewed "
                    "owner/account, or finalized strong specimen-identity anchor "
                    "changed after finalization."
                )
                preview = replace(
                    preview,
                    eligibility=replace(
                        preview.eligibility,
                        eligible=False,
                        blocking_reasons=tuple(
                            dict.fromkeys(
                                (
                                    *preview.eligibility.blocking_reasons,
                                    reason,
                                )
                            )
                        ),
                    ),
                    donor_evidence_paths=(),
                )
            mutable_change_notices: list[str] = []
            for member in preview.canonical_members:
                baseline = baseline_by_key.get((member.site, member.observation_id))
                if baseline is None:
                    continue
                try:
                    baseline_components = json.loads(
                        str(baseline.get("reviewed_mutable_components") or "{}")
                    )
                except (TypeError, ValueError):
                    baseline_components = {}
                current_components = dict(member.mutable_component_fingerprints)
                if not baseline_components:
                    mutable_change_notices.append(
                        f"{member.site.value} #{member.observation_id}: the "
                        "successful baseline predates component-level change "
                        "tracking; the complete current snapshot will be reviewed "
                        "and pinned by this attempt."
                    )
                    continue
                changed = sorted(
                    name
                    for name in set(baseline_components) | set(current_components)
                    if baseline_components.get(name) != current_components.get(name)
                )
                if changed:
                    mutable_change_notices.append(
                        f"{member.site.value} #{member.observation_id}: mutable "
                        f"canonical content changed ({', '.join(changed)}). "
                        "The current values shown here will be pinned for this attempt."
                    )
            preview = replace(
                preview,
                warnings=(
                    f"Extension of stable consolidation #{extension_id}. Canonical "
                    f"records are fixed at MO {fixed_mo_id or 'none'} / "
                    f"iNat {fixed_inat_id or 'none'}. Only the newly proposed donors "
                    "can change local state in this attempt."
                    + (
                        " This immutably supersedes the exact prior "
                        "failed/cancelled extension attempt."
                        if extension_retry
                        else ""
                    ),
                    *mutable_change_notices,
                ),
            )
        if retry_consolidation_id is not None:
            identity = self.db.get_consolidation(
                profile_id,
                retry_consolidation_id,
            )
            if identity:
                preview = replace(
                    preview,
                    warnings=(
                        "This is a fresh attempt that will supersede a "
                        f"definitively failed/cancelled attempt for consolidation "
                        f"#{retry_consolidation_id}. Select canonical records "
                        "explicitly again. Different canonical choices are allowed "
                        "only when the durable journal proves that no action in the "
                        "abandoned initial plan ever started or ambiguously completed "
                        "a write.",
                    ),
                )
        if (
            auth_generation != self.auth_generation_provider()
            or mo_key_generation != self.mo_key_generation_provider()
        ):
            raise ConsolidationError(
                "Authentication or Mushroom Observer credentials changed during preview.",
                "credential_context_changed",
            )
        return preview

    def execute_group(
        self,
        profile_id: int,
        group_id: int,
        cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> list[ConsolidationActionResult]:
        ledger = self.db.consolidation_ledger_for_group(profile_id, group_id)
        if not ledger:
            raise ConsolidationError(
                "The consolidation ledger is missing.", "missing_ledger"
            )
        if str(ledger["state"]) == "succeeded":
            return []
        if str(ledger["state"]) == "outcome_unknown":
            unknown = next(
                (
                    row
                    for row in self.db.action_group_rows(profile_id, group_id)
                    if str(row["state"]) == "outcome_unknown"
                ),
                None,
            )
            if unknown is None:
                raise ConsolidationError(
                    "The attempt is outcome-unknown but has no recoverable action row.",
                    "unknown_ledger_mismatch",
                )
            result = self.verify_unknown(
                profile_id,
                int(unknown["action_id"]),
                cancelled,
            )
            if result.state != "succeeded":
                return [result]
            ledger = self.db.consolidation_ledger_for_group(profile_id, group_id)
            if not ledger or str(ledger["state"]) != "pending":
                return [result]
            results: list[ConsolidationActionResult] = [result]
        elif str(ledger["state"]) != "pending":
            raise ConsolidationError(
                f"This consolidation attempt is {ledger['state']} and cannot resume.",
                "attempt_terminal",
            )
        else:
            results = []

        consolidation_id = int(ledger["consolidation_id"])
        pair_id = self.db.ensure_consolidation_pair(profile_id, consolidation_id)
        ledger = self.db.consolidation_ledger_for_group(profile_id, group_id) or ledger
        mo_id = ledger.get("canonical_mo_observation_id")
        inat_id = ledger.get("canonical_inat_observation_id")

        if mo_id is not None and inat_id is not None:
            for site, action_type in (
                (RemoteSite.MO, LinkActionType.MO_EXTERNAL_LINK_ADD.value),
                (RemoteSite.INAT, LinkActionType.INAT_OFV_ADD.value),
            ):
                row = next(
                    (
                        item
                        for item in self.db.action_group_rows(profile_id, group_id)
                        if str(item["action_type"]) == action_type
                    ),
                    None,
                )
                if row is not None and str(row["state"]) == "succeeded":
                    continue
                if row is not None and str(row["state"]) == "outcome_unknown":
                    result = self.verify_unknown(
                        profile_id,
                        int(row["action_id"]),
                        cancelled,
                    )
                    results.append(result)
                    if result.state != "succeeded":
                        return results
                    continue
                if row is not None and str(row["state"]) in {"failed", "cancelled"}:
                    self.db.set_consolidation_attempt_state(
                        profile_id,
                        int(ledger["attempt_id"]),
                        str(row["state"]),
                    )
                    results.append(
                        ConsolidationActionResult(
                            int(row["action_id"]),
                            str(row["state"]),
                            "A terminal canonical-link action blocks later consolidation steps.",
                        )
                    )
                    return results
                if cancelled():
                    self.db.set_consolidation_attempt_state(
                        profile_id,
                        int(ledger["attempt_id"]),
                        "cancelled",
                    )
                    return results
                progress(
                    f"Verifying every reviewed member before the {site.value} canonical link"
                )
                try:
                    fresh = self._require_reviewed_members_unchanged(
                        profile_id,
                        group_id,
                        cancelled,
                    )
                    self._require_same_specimen(fresh, ledger)
                    option, snapshot = self.link_service.prepare_consolidation_add(
                        profile_id,
                        group_id,
                        site,
                        cancelled,
                    )
                except Exception as exc:
                    self.db.set_consolidation_attempt_state(
                        profile_id,
                        int(ledger["attempt_id"]),
                        "failed",
                    )
                    results.append(
                        ConsolidationActionResult(
                            0,
                            "failed",
                            str(exc),
                        )
                    )
                    return results
                if not option.enabled:
                    self.db.set_consolidation_attempt_state(
                        profile_id,
                        int(ledger["attempt_id"]),
                        "failed",
                    )
                    raise ConsolidationError(
                        option.disabled_reason or "The canonical link is unavailable.",
                        "link_action_disabled",
                    )
                action_id = self.db.mint_consolidation_action(
                    profile_id,
                    group_id,
                    action_type,
                    site=site.value,
                    pair_id=pair_id,
                    mo_observation_id=int(mo_id),
                    inat_observation_id=int(inat_id),
                    inat_observation_uuid=str(snapshot["inat_observation_uuid"]),
                    binding_id=(
                        int(snapshot["mo_external_site_id"])
                        if site is RemoteSite.MO
                        else int(snapshot["inat_field_id"])
                    ),
                    desired_target_id=option.desired_target_id,
                    preview_inat_record_fingerprint=str(
                        snapshot["inat_record_fingerprint"]
                    ),
                    preview_mo_record_fingerprint=str(
                        snapshot["mo_record_fingerprint"]
                    ),
                    preview_inat_links_fingerprint=str(
                        snapshot["inat_links_fingerprint"]
                    ),
                    preview_mo_links_fingerprint=str(snapshot["mo_links_fingerprint"]),
                )
                row = self.db.action(profile_id, action_id)
                if row is None:
                    raise ConsolidationError(
                        "The just-minted canonical-link action is missing.",
                        "mint_lost",
                    )
                link_result = self.link_service.execute_journaled_action(
                    profile_id,
                    row,
                    cancelled,
                    progress,
                )
                result = ConsolidationActionResult(
                    link_result.action_id,
                    link_result.state,
                    link_result.message,
                )
                results.append(result)
                if result.state != "succeeded":
                    if result.state in {"pending", "running"}:
                        return results
                    terminal = (
                        "outcome_unknown"
                        if result.state == "outcome_unknown"
                        else "failed"
                    )
                    self.db.set_consolidation_attempt_state(
                        profile_id,
                        int(ledger["attempt_id"]),
                        terminal,
                    )
                    return results

        progress("Freshly verifying the canonical specimen and retained donors")
        try:
            fresh = self._require_reviewed_members_unchanged(
                profile_id,
                group_id,
                cancelled,
            )
            self._require_same_specimen(fresh, ledger)
            self._require_canonical_links(fresh, ledger)
        except Exception as exc:
            self.db.set_consolidation_attempt_state(
                profile_id,
                int(ledger["attempt_id"]),
                "failed",
            )
            results.append(ConsolidationActionResult(0, "failed", str(exc)))
            return results
        rows = self.db.action_group_rows(profile_id, group_id)
        finalize = next(
            (
                row
                for row in rows
                if str(row["action_type"]) == "consolidation_finalize"
            ),
            None,
        )
        if finalize is None:
            site = "mo" if mo_id is not None else "inat"
            inat_remote_uuid = next(
                (
                    str(member["remote_uuid"] or "")
                    for member in self.db.list_consolidation_members(
                        profile_id,
                        consolidation_id,
                    )
                    if str(member["site"]) == "inat"
                    and int(member["observation_id"]) == int(inat_id or 0)
                ),
                "",
            )
            action_id = self.db.mint_consolidation_action(
                profile_id,
                group_id,
                "consolidation_finalize",
                site=site,
                pair_id=pair_id,
                mo_observation_id=int(mo_id) if mo_id is not None else None,
                inat_observation_id=int(inat_id) if inat_id is not None else None,
                inat_observation_uuid=inat_remote_uuid,
            )
            finalize = self.db.action(profile_id, action_id)
        if finalize is None:
            raise ConsolidationError("The finalization row is missing.", "mint_lost")
        if str(finalize["state"]) == "succeeded":
            return results
        result = self._execute_finalize(finalize, cancelled, progress)
        results.append(result)
        return results

    def verify_unknown(
        self,
        profile_id: int,
        action_id: int,
        cancelled: Callable[[], bool],
    ) -> ConsolidationActionResult:
        row = self.db.action(profile_id, action_id)
        if not row or str(row["state"]) != "outcome_unknown":
            raise ConsolidationError(
                "Only an outcome-unknown consolidation action can be verified.",
                "invalid_unknown",
            )
        ledger = self.db.consolidation_ledger_for_group(
            profile_id, int(row["action_group_id"])
        )
        if not ledger:
            raise ConsolidationError(
                "The consolidation ledger is missing.", "missing_ledger"
            )
        if str(row["action_type"]) == "consolidation_finalize":
            self.db.finish_action(
                profile_id,
                action_id,
                "failed",
                phase="verification",
                error_code="verified_local_finalize_not_applied",
                verification_state="verified_not_applied",
            )
            self.db.resolve_consolidation_unknown(
                profile_id,
                int(ledger["attempt_id"]),
                applied=False,
            )
            return ConsolidationActionResult(
                action_id,
                "failed",
                "The interrupted local-only finalization did not commit; no remote write "
                "was involved. A new reviewed attempt is required.",
            )
        link = self.link_service.verify_unknown(profile_id, action_id, cancelled)
        attempt_state = str(ledger.get("state") or "")
        if link.state == "succeeded":
            if attempt_state == "outcome_unknown":
                self.db.resolve_consolidation_unknown(
                    profile_id,
                    int(ledger["attempt_id"]),
                    applied=True,
                )
        elif link.state == "failed":
            if attempt_state == "outcome_unknown":
                self.db.resolve_consolidation_unknown(
                    profile_id,
                    int(ledger["attempt_id"]),
                    applied=False,
                )
            else:
                self.db.set_consolidation_attempt_state(
                    profile_id,
                    int(ledger["attempt_id"]),
                    "failed",
                )
        elif link.state == "outcome_unknown" and attempt_state == "pending":
            self.db.set_consolidation_attempt_state(
                profile_id,
                int(ledger["attempt_id"]),
                "outcome_unknown",
            )
        return ConsolidationActionResult(link.action_id, link.state, link.message)

    def _execute_finalize(
        self,
        row: dict[str, Any],
        cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> ConsolidationActionResult:
        profile_id = int(row["profile_id"])
        action_id = int(row["action_id"])
        if not self.db.claim_action(profile_id, action_id, "verification"):
            current = self.db.action(profile_id, action_id) or row
            return ConsolidationActionResult(
                action_id,
                str(current["state"]),
                "Finalize is no longer pending.",
            )
        ledger = self.db.consolidation_ledger_for_group(
            profile_id, int(row["action_group_id"])
        )
        try:
            if cancelled():
                raise ReconciliationCancelled(
                    "Consolidation cancelled before local finalization"
                )
            if not ledger:
                raise ConsolidationError(
                    "The consolidation ledger is missing.", "missing_ledger"
                )
            progress("Finalization: repeating fresh member and specimen verification")
            fresh = self._require_reviewed_members_unchanged(
                profile_id,
                int(row["action_group_id"]),
                cancelled,
            )
            self._require_same_specimen(fresh, ledger)
            self._require_canonical_links(fresh, ledger)
            self.db.settle_consolidation_finalize_success(
                profile_id,
                action_id,
                int(row["action_group_id"]),
                int(ledger["attempt_id"]),
                int(ledger["consolidation_id"]),
            )
            return ConsolidationActionResult(
                action_id,
                "succeeded",
                "Canonical pair verified; donor observations were marked locally "
                "superseded and remain unchanged online.",
            )
        except ReconciliationCancelled:
            self.db.finish_action(
                profile_id,
                action_id,
                "cancelled",
                phase="verification",
                error_code="cancelled_before_local_finalize",
            )
            if ledger:
                self.db.set_consolidation_attempt_state(
                    profile_id,
                    int(ledger["attempt_id"]),
                    "cancelled",
                )
            return ConsolidationActionResult(
                action_id,
                "cancelled",
                "Cancelled before local finalization; donors remain active locally and online.",
            )
        except Exception as exc:
            self.db.finish_action(
                profile_id,
                action_id,
                "failed",
                phase="verification",
                error_code=str(getattr(exc, "code", "") or "finalize_failed"),
            )
            if ledger:
                self.db.set_consolidation_attempt_state(
                    profile_id,
                    int(ledger["attempt_id"]),
                    "failed",
                )
            return ConsolidationActionResult(action_id, "failed", str(exc))

    def _fetch_review_data(
        self,
        profile: ReconciliationProfile,
        candidates: Sequence[tuple[RemoteSite, int]],
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        if cancelled():
            raise ReconciliationCancelled("Consolidation preview cancelled")
        auth = self.auth_provider()
        needs_inat = any(site is RemoteSite.INAT for site, _ in candidates)
        if needs_inat and (
            not auth.is_authenticated
            or not auth.api_token
            or auth.login.strip().casefold() != profile.inat_login.strip().casefold()
        ):
            raise ConsolidationError(
                "Fresh private-safe iNaturalist reads require authentication for the "
                "profile's exact account.",
                "inat_auth_mismatch",
            )
        binding = self.db.field_binding(profile.profile_id, "mo_url")
        field_id = (
            int(binding["field_id"])
            if (
                binding is not None and str(binding["verification_state"]) == "verified"
            )
            else None
        )
        if field_id is None and {site for site, _ in candidates} == {
            RemoteSite.MO,
            RemoteSite.INAT,
        }:
            raise ConsolidationError(
                "The canonical reciprocal-link field binding must be verified first.",
                "link_binding_unavailable",
            )
        mo_site_id: Optional[int] = None
        if any(site is RemoteSite.MO for site, _ in candidates):
            site_rows = [
                row
                for row in results_from_payload(
                    self.mo_client.external_sites(cancelled)
                )
                if "inaturalist"
                in " ".join(
                    str(row.get(key) or "")
                    for key in ("name", "site", "url", "base_url")
                ).casefold()
            ]
            site_ids = {positive_int(row.get("id")) for row in site_rows} - {None}
            if len(site_ids) != 1:
                raise ConsolidationError(
                    "Mushroom Observer has no unique iNaturalist external-site definition.",
                    "mo_site_unavailable",
                )
            mo_site_id = int(next(iter(site_ids)))

        raw_candidates: list[tuple[RemoteSite, int, dict[str, Any]]] = []
        photo_payloads: dict[tuple[RemoteSite, int], object] = {}
        link_snapshots: dict[
            tuple[RemoteSite, int], Sequence[AuthoritativeLinkSnapshot]
        ] = {}
        for site, observation_id in candidates:
            if cancelled():
                raise ReconciliationCancelled("Consolidation preview cancelled")
            if site is RemoteSite.INAT:
                raw = _first_result(
                    self.inat_client.get_reconciliation_detail(
                        observation_id,
                        auth.api_token,
                        deep=True,
                    )
                )
                if raw is None:
                    raise ConsolidationError(
                        f"iNaturalist #{observation_id} could not be freshly read.",
                        "record_unavailable",
                    )
                link_snapshots[(site, observation_id)] = _inat_link_snapshots(
                    raw,
                    observation_id,
                    field_id,
                )
            else:
                raw = _first_result(
                    self.mo_client.observation(
                        observation_id,
                        cancelled,
                        detail="high",
                    )
                )
                if raw is None:
                    raise ConsolidationError(
                        f"Mushroom Observer #{observation_id} could not be freshly read.",
                        "record_unavailable",
                    )
                raw = self._enrich_mo_fungal_scope(raw, cancelled)
                photo_payloads[(site, observation_id)] = (
                    self.mo_client.images_for_observation(observation_id, cancelled)
                )
                mo_links = self.mo_client.external_links((observation_id,), cancelled)
                snapshots: list[AuthoritativeLinkSnapshot] = []
                for link_raw in results_from_payload(mo_links):
                    parsed = parse_mo_external_link(link_raw, int(mo_site_id))
                    if parsed is None or parsed[0] != observation_id:
                        continue
                    _, link = parsed
                    snapshots.append(
                        AuthoritativeLinkSnapshot(
                            site=RemoteSite.MO,
                            observation_id=observation_id,
                            row_id=link.row_id,
                            binding_id=link.external_site_id,
                            target_observation_id=link.target_observation_id,
                            parse_state=link.parse_state,
                            row_fingerprint=link.fingerprint,
                            display_value=str(
                                link_raw.get("url")
                                or link_raw.get("link_url")
                                or link_raw.get("derived_url")
                                or link_raw.get("external_url")
                                or ""
                            ),
                        )
                    )
                link_snapshots[(site, observation_id)] = tuple(snapshots)
            raw_candidates.append((site, observation_id, raw))
        return {
            "candidates": raw_candidates,
            "photo_payloads": photo_payloads,
            "link_snapshots": link_snapshots,
            "inat_mo_field_id": field_id,
        }

    def _enrich_mo_fungal_scope(
        self,
        raw: dict[str, Any],
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        inventory = parse_mo_observation(raw, account_id=0)
        if inventory.fungi_status != "unknown" or not inventory.taxon_id:
            return raw
        name_rows = results_from_payload(
            self.mo_client.names((inventory.taxon_id,), cancelled)
        )
        if not name_rows:
            return raw
        from .coordinator import _name_fungi_status

        status = _name_fungi_status(name_rows[0])
        if status == "unknown":
            return raw
        consensus = raw.get("consensus")
        consensus = consensus if isinstance(consensus, dict) else {}
        enriched = dict(raw)
        enriched["consensus"] = {
            **consensus,
            "classification": "Fungi" if status == "fungi" else "Plantae",
        }
        return enriched

    def _require_reviewed_members_unchanged(
        self,
        profile_id: int,
        group_id: int,
        cancelled: Callable[[], bool],
    ) -> dict[
        tuple[RemoteSite, int],
        tuple[InventoryObservation, HydratedObservation, ConsolidationMemberSnapshot],
    ]:
        ledger = self.db.consolidation_ledger_for_group(profile_id, group_id)
        if not ledger:
            raise ConsolidationError(
                "The consolidation ledger is missing.", "missing_ledger"
            )
        members = self.db.consolidation_attempt_members(
            profile_id, int(ledger["attempt_id"])
        )
        specs = [
            (RemoteSite(str(member["site"])), int(member["observation_id"]))
            for member in members
        ]
        profile = self.db.profile(profile_id)
        data = self._fetch_review_data(profile, specs, cancelled)
        by_key: dict[
            tuple[RemoteSite, int],
            tuple[
                InventoryObservation, HydratedObservation, ConsolidationMemberSnapshot
            ],
        ] = {}
        for site, observation_id, raw in data["candidates"]:
            inventory, hydrated = _hydrate_candidate(
                site,
                observation_id,
                raw,
                profile,
                self.reader,
                inat_mo_field_id=data["inat_mo_field_id"],
            )
            snapshot = _member_snapshot(
                inventory,
                hydrated,
                raw,
                photo_payload=data["photo_payloads"].get((site, observation_id)),
                link_snapshots=data["link_snapshots"].get((site, observation_id), ()),
            )
            by_key[(site, observation_id)] = (inventory, hydrated, snapshot)
        succeeded_link = any(
            str(row["state"]) == "succeeded"
            and str(row["action_type"])
            in {
                LinkActionType.MO_EXTERNAL_LINK_ADD.value,
                LinkActionType.INAT_OFV_ADD.value,
            }
            for row in self.db.action_group_rows(profile_id, group_id)
        )
        reviewed_rows = self.db.action_snapshot_rows(profile_id, group_id)
        for member in members:
            key = (RemoteSite(str(member["site"])), int(member["observation_id"]))
            current = by_key.get(key)
            if current is None:
                raise ConsolidationError(
                    f"{key[0].value} #{key[1]} is no longer readable.",
                    "record_unavailable",
                )
            snapshot = current[2]
            expected_role = (
                "canonical"
                if (
                    key
                    == (
                        RemoteSite.MO,
                        ledger.get("canonical_mo_observation_id"),
                    )
                    or key
                    == (
                        RemoteSite.INAT,
                        ledger.get("canonical_inat_observation_id"),
                    )
                )
                else "donor"
            )
            if str(member["role"]) != expected_role or (
                str(member["local_state"])
                not in (
                    {"active", "canonical"}
                    if expected_role == "canonical"
                    else {"proposed"}
                )
            ):
                raise ConsolidationError(
                    "The reviewed canonical/donor roles or local member state changed "
                    "after approval. No write was sent.",
                    "consolidation_changed",
                )
            is_canonical = expected_role == "canonical"
            if (
                int(member.get("reviewed_owner_account_id") or 0) != snapshot.owner_id
                or snapshot.owner_id != snapshot.account_id
            ):
                raise ConsolidationError(
                    f"The reviewed {key[0].value} numeric owner identity changed "
                    "after approval. No write was sent.",
                    "member_account_changed",
                )
            if (
                not str(member.get("reviewed_owner_login") or "")
                or str(member["reviewed_owner_login"]) != snapshot.owner_login
            ):
                raise ConsolidationError(
                    f"The reviewed {key[0].value} display login changed after "
                    "approval. No write was sent.",
                    "member_login_changed",
                )
            if (
                not str(member.get("reviewed_identity_fingerprint") or "")
                or str(member["reviewed_identity_fingerprint"])
                != snapshot.identity_fingerprint
            ):
                raise ConsolidationError(
                    f"The stable identity of {key[0].value} #{key[1]} changed "
                    "after approval. No write was sent.",
                    "member_identity_changed",
                )
            if is_canonical:
                suffix = "mo" if key[0] is RemoteSite.MO else "inat"
                reviewed_full = str(ledger.get(f"canonical_{suffix}_fingerprint") or "")
                reviewed_preflight = str(
                    ledger.get(f"canonical_{suffix}_preflight_fingerprint") or ""
                )
            else:
                reviewed_full = str(member.get("attempt_record_fingerprint") or "")
                reviewed_preflight = str(
                    member.get("attempt_preflight_fingerprint") or ""
                )
            if is_canonical:
                if reviewed_full != str(
                    member.get("attempt_record_fingerprint") or ""
                ) or reviewed_preflight != str(
                    member.get("attempt_preflight_fingerprint") or ""
                ):
                    raise ConsolidationError(
                        "The canonical attempt-member provenance is inconsistent.",
                        "attempt_fingerprint_mismatch",
                    )
            if not reviewed_full or not reviewed_preflight:
                raise ConsolidationError(
                    "The immutable attempt is missing a reviewed member fingerprint.",
                    "attempt_fingerprint_missing",
                )
            if not is_canonical or not succeeded_link:
                if snapshot.record_fingerprint != reviewed_full:
                    raise ConsolidationError(
                        f"{key[0].value} #{key[1]} changed after review. No write was sent.",
                        "source_changed" if not is_canonical else "canonical_changed",
                    )
            elif snapshot.preflight_fingerprint != reviewed_preflight:
                raise ConsolidationError(
                    f"{key[0].value} #{key[1]} changed outside the expected link "
                    "addition after review. No write was sent.",
                    "canonical_changed",
                )
            self._require_expected_link_delta(
                member,
                snapshot,
                reviewed_rows,
                ledger.get("canonical_mo_observation_id"),
                ledger.get("canonical_inat_observation_id"),
                allow_canonical_addition=is_canonical and succeeded_link,
            )
        return by_key

    @staticmethod
    def _require_expected_link_delta(
        member: dict[str, Any],
        snapshot: ConsolidationMemberSnapshot,
        reviewed_rows: Sequence[dict[str, Any]],
        canonical_mo_id: object,
        canonical_inat_id: object,
        *,
        allow_canonical_addition: bool,
    ) -> None:
        initial = {
            (
                str(row["remote_row_uuid"] or row["remote_row_id"]),
                row["normalized_target_id"],
            )
            for row in reviewed_rows
            if str(row["site"]) == str(member["site"])
            and int(row["observation_id"]) == int(member["observation_id"])
        }
        current = {
            (
                str(row.row_uuid or row.row_id),
                row.target_observation_id,
            )
            for row in snapshot.reciprocal_links
        }
        if not allow_canonical_addition:
            if current != initial:
                raise ConsolidationError(
                    f"{member['site']} #{member['observation_id']} reciprocal links "
                    "changed after review. No write was sent.",
                    "link_state_changed",
                )
            return
        if not initial.issubset(current):
            raise ConsolidationError(
                "A reviewed canonical reciprocal-link row was removed or retargeted.",
                "link_state_changed",
            )
        expected_target = (
            canonical_inat_id if str(member["site"]) == "mo" else canonical_mo_id
        )
        unexpected = {item for item in current - initial if item[1] != expected_target}
        if unexpected:
            raise ConsolidationError(
                "An unreviewed reciprocal-link change appeared on a canonical record.",
                "link_state_changed",
            )

    def _require_same_specimen(
        self,
        fresh: dict[
            tuple[RemoteSite, int],
            tuple[
                InventoryObservation, HydratedObservation, ConsolidationMemberSnapshot
            ],
        ],
        ledger: dict[str, Any],
    ) -> None:
        values = list(fresh.values())
        for index, (_left_inv, left, _left_snapshot) in enumerate(values):
            for _right_inv, right, _right_snapshot in values[index + 1 :]:
                conflicts, unavailable = specimen_identity_conflicts(left, right)
                if conflicts or unavailable:
                    detail = "; ".join(
                        (
                            *conflicts,
                            *(
                                f"required evidence unavailable: {item}"
                                for item in unavailable
                            ),
                        )
                    )
                    raise ConsolidationError(
                        f"Fresh specimen verification failed: {detail}",
                        "specimen_identity_changed",
                    )
        reviewed_rows = self.db.consolidation_evidence(
            int(ledger["profile_id"]), int(ledger["attempt_id"])
        )
        if not reviewed_rows:
            raise ConsolidationError(
                "The immutable attempt has no reviewed specimen-evidence graph.",
                "evidence_missing",
            )
        try:
            normalized_edges: list[ConsolidationEvidenceEdge] = []
            for row in reviewed_rows:
                left_site, left_id, right_site, right_id = _ordered_edge_ends(
                    RemoteSite(str(row["left_site"])),
                    int(row["left_observation_id"]),
                    RemoteSite(str(row["right_site"])),
                    int(row["right_observation_id"]),
                )
                normalized_edges.append(
                    ConsolidationEvidenceEdge(
                        left_site=left_site,
                        left_observation_id=left_id,
                        right_site=right_site,
                        right_observation_id=right_id,
                        evidence_type=str(row["evidence_type"]),
                        evidence_strength=str(row["evidence_strength"]),
                        reviewed_evidence_fingerprint=str(
                            row["reviewed_evidence_fingerprint"]
                        ),
                        display_summary=str(row["display_summary"]),
                    )
                )
            reviewed_edges = tuple(normalized_edges)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConsolidationError(
                "The immutable attempt contains a malformed evidence record.",
                "malformed_evidence",
            ) from exc
        reviewed_graph_fingerprint = consolidation_evidence_graph_fingerprint(
            (
                edge.left_site.value,
                edge.left_observation_id,
                edge.right_site.value,
                edge.right_observation_id,
                edge.evidence_type,
                edge.evidence_strength,
                edge.reviewed_evidence_fingerprint,
            )
            for edge in reviewed_edges
        )
        if not str(
            ledger.get("reviewed_evidence_graph_fingerprint") or ""
        ) or reviewed_graph_fingerprint != str(
            ledger["reviewed_evidence_graph_fingerprint"]
        ):
            raise ConsolidationError(
                "The immutable attempt evidence graph is incomplete or changed.",
                "malformed_evidence",
            )
        fresh_pairs = [(inventory, hydrated) for inventory, hydrated, _ in values]
        fresh_links = {key: value[2].reciprocal_links for key, value in fresh.items()}
        current_edges = _supporting_evidence(fresh_pairs, fresh_links)
        current_signatures = {
            (
                edge.left_site,
                edge.left_observation_id,
                edge.right_site,
                edge.right_observation_id,
                edge.evidence_type,
                edge.evidence_strength,
                edge.reviewed_evidence_fingerprint,
            )
            for edge in current_edges
        }
        for edge in reviewed_edges:
            signature = (
                edge.left_site,
                edge.left_observation_id,
                edge.right_site,
                edge.right_observation_id,
                edge.evidence_type,
                edge.evidence_strength,
                edge.reviewed_evidence_fingerprint,
            )
            if signature not in current_signatures:
                raise ConsolidationError(
                    "A reviewed specimen-evidence edge disappeared or changed. "
                    "No write was sent and local supersession is blocked.",
                    "evidence_changed",
                )
        graph_validation = validate_consolidation_graph(
            list(fresh),
            reviewed_edges,
            canonical_mo_id=(
                int(ledger["canonical_mo_observation_id"])
                if ledger.get("canonical_mo_observation_id") is not None
                else None
            ),
            canonical_inat_id=(
                int(ledger["canonical_inat_observation_id"])
                if ledger.get("canonical_inat_observation_id") is not None
                else None
            ),
        )
        expected_donors = sum(
            1
            for member in self.db.consolidation_attempt_members(
                int(ledger["profile_id"]), int(ledger["attempt_id"])
            )
            if str(member["participation_role"]) == "new_donor"
        )
        if (
            not graph_validation.valid
            or len(graph_validation.donor_paths) != expected_donors
        ):
            raise ConsolidationError(
                "; ".join(graph_validation.reasons)
                or "A donor no longer has a reviewed evidence path to the canonical specimen.",
                "evidence_graph_disconnected",
            )

    @staticmethod
    def _require_canonical_links(
        fresh: dict[
            tuple[RemoteSite, int],
            tuple[
                InventoryObservation, HydratedObservation, ConsolidationMemberSnapshot
            ],
        ],
        ledger: dict[str, Any],
    ) -> None:
        mo_id = ledger.get("canonical_mo_observation_id")
        inat_id = ledger.get("canonical_inat_observation_id")
        if mo_id is None or inat_id is None:
            return
        mo = fresh.get((RemoteSite.MO, int(mo_id)))
        inat = fresh.get((RemoteSite.INAT, int(inat_id)))
        if not mo or not inat:
            raise ConsolidationError(
                "The canonical pair is incomplete.", "canonical_missing"
            )
        mo_linked = any(
            row.parse_state != "malformed" and row.target_observation_id == int(inat_id)
            for row in mo[2].reciprocal_links
        )
        inat_linked = any(
            row.parse_state != "malformed" and row.target_observation_id == int(mo_id)
            for row in inat[2].reciprocal_links
        )
        if not (mo_linked and inat_linked):
            raise ConsolidationError(
                "The canonical reciprocal links are not both freshly verified.",
                "canonical_links_incomplete",
            )


def _first_result(payload: object) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    rows = payload.get("results")
    if isinstance(rows, list):
        return next((row for row in rows if isinstance(row, dict)), None)
    return payload if payload.get("id") else None


def _inat_link_snapshots(
    raw: dict[str, Any],
    observation_id: int,
    field_id: Optional[int],
) -> tuple[AuthoritativeLinkSnapshot, ...]:
    if field_id is None:
        return ()
    result: list[AuthoritativeLinkSnapshot] = []
    for index, row in enumerate(
        raw.get("ofvs") or raw.get("observation_field_values") or (), 1
    ):
        if not isinstance(row, dict):
            continue
        field = (
            row.get("observation_field")
            if isinstance(row.get("observation_field"), dict)
            else {}
        )
        try:
            row_field_id = int(
                row.get("field_id")
                or row.get("observation_field_id")
                or field.get("id")
                or 0
            )
        except (TypeError, ValueError):
            continue
        if row_field_id != field_id:
            continue
        value = str(row.get("value") or "")
        target = parse_mo_observation_url(value)
        row_id = str(row.get("id") or f"ofv:{observation_id}:{index}")
        row_uuid = str(row.get("uuid") or "")
        state = "valid" if target is not None else "malformed"
        result.append(
            AuthoritativeLinkSnapshot(
                site=RemoteSite.INAT,
                observation_id=observation_id,
                row_id=row_id,
                row_uuid=row_uuid,
                binding_id=field_id,
                target_observation_id=target,
                parse_state=state,
                row_fingerprint=public_fingerprint(
                    row_id,
                    row_uuid,
                    field_id,
                    target,
                    state,
                    value,
                ),
                display_value=value,
            )
        )
    if len(result) > 1:
        targets = {
            row.target_observation_id for row in result if row.target_observation_id
        }
        state = "conflicting" if len(targets) > 1 else "duplicate"
        result = [
            replace(
                row,
                parse_state=state if row.parse_state == "valid" else row.parse_state,
                row_fingerprint=public_fingerprint(row.row_fingerprint, state),
            )
            for row in result
        ]
    return tuple(result)
