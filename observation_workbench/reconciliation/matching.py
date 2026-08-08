"""Deterministic candidate blocking, explainable scoring, and link validation."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Iterable, Optional

from .types import (
    CandidateScore,
    EvidenceFamily,
    EvidenceTier,
    FAMILY_CAPS,
    HydratedObservation,
    InventoryObservation,
    MatchEvidence,
    ObservationPair,
    RemoteSite,
)

# Identifier kinds that describe a physical specimen and may create a candidate
# pair. Barcode identifiers (accession) are deliberately excluded — they support
# but never originate a candidate.
CANDIDATE_IDENTIFIER_KINDS = frozenset(
    {"voucher", "collection", "collection_number", "field_number"}
)
BARCODE_IDENTIFIER_KINDS = frozenset({"accession"})


def _is_candidate_identifier(kind: str) -> bool:
    return kind in CANDIDATE_IDENTIFIER_KINDS


def score_evidence(items: Iterable[MatchEvidence]) -> CandidateScore:
    evidence = tuple(items)
    grouped: dict[EvidenceFamily, int] = defaultdict(int)
    for item in evidence:
        grouped[item.family] += max(0, item.score)
    capped = {
        family: min(score, FAMILY_CAPS[family]) for family, score in grouped.items()
    }
    total = sum(capped.values())
    # Barcode is useful corroboration but cannot be the independent basis of strength.
    non_barcode_families = {
        family
        for family, score in capped.items()
        if score and family != EvidenceFamily.BARCODE
    }
    strong = (
        total >= 70
        and len({family for family, score in capped.items() if score}) >= 2
        and bool(non_barcode_families)
    )
    classification = "strong" if strong else "possible" if total >= 35 else "hidden"
    return CandidateScore(total, capped, evidence, classification)


def build_candidates(
    mo_records: Iterable[InventoryObservation],
    inat_records: Iterable[InventoryObservation],
) -> list[ObservationPair]:
    """Join inverted blocking indexes; never scan every opposite record."""
    mo = [
        item
        for item in mo_records
        if item.fungi_status in {"fungi", "unknown"}
        and item.scope_state == "in_scope"
        and not item.deleted
    ]
    inat = [
        item
        for item in inat_records
        if item.fungi_status == "fungi"
        and item.scope_state == "in_scope"
        and not item.deleted
    ]
    inat_by_id = {item.key.observation_id: item for item in inat}
    mo_by_id = {item.key.observation_id: item for item in mo}
    blocks: set[tuple[int, int]] = set()
    direct: set[tuple[int, int]] = set()
    for item in mo:
        for target in item.authoritative_targets:
            if target in inat_by_id:
                blocks.add((item.key.observation_id, target))
                direct.add((item.key.observation_id, target))
    for item in inat:
        for target in item.authoritative_targets:
            if target in mo_by_id:
                blocks.add((target, item.key.observation_id))
                direct.add((target, item.key.observation_id))

    # Non-barcode specimen identifiers (voucher and collection numbers) are
    # allowed to create candidate pairs. Barcode evidence — accessions in any
    # namespace and sequence fingerprints — must never create a candidate on its
    # own; it may only score or support one created by another blocking family.
    specimen_index: dict[tuple[str, str], set[int]] = defaultdict(set)
    media_index: dict[tuple[RemoteSite, str], set[int]] = defaultdict(set)
    date_taxon_index: dict[tuple[object, str], set[int]] = defaultdict(set)
    date_genus_index: dict[tuple[object, str], set[int]] = defaultdict(set)
    date_locality_index: dict[tuple[object, str], set[int]] = defaultdict(set)
    for item in inat:
        for kind, value in item.identifiers:
            if value and _is_candidate_identifier(kind):
                specimen_index[(kind, value)].add(item.key.observation_id)
        for media in item.media:
            if media.provenance_key:
                media_index[media.provenance_key].add(item.key.observation_id)
        if item.observed_on:
            taxon = _normalized_taxon(item.taxon_name)
            genus = _normalized_genus(item.taxon_name)
            locality = _normalized_locality(item.public_locality)
            if taxon:
                date_taxon_index[(item.observed_on, taxon)].add(item.key.observation_id)
            if genus:
                date_genus_index[(item.observed_on, genus)].add(item.key.observation_id)
            if locality:
                date_locality_index[(item.observed_on, locality)].add(
                    item.key.observation_id
                )

    for left in mo:
        if left.observed_on:
            taxon = _normalized_taxon(left.taxon_name)
            genus = _normalized_genus(left.taxon_name)
            locality = _normalized_locality(left.public_locality)
            for delta in (-1, 0, 1):
                observed = left.observed_on + timedelta(days=delta)
                candidate_ids: set[int] = set()
                if taxon:
                    candidate_ids.update(date_taxon_index.get((observed, taxon), ()))
                if genus:
                    candidate_ids.update(date_genus_index.get((observed, genus), ()))
                if locality:
                    candidate_ids.update(
                        date_locality_index.get((observed, locality), ())
                    )
                blocks.update(
                    (left.key.observation_id, value) for value in candidate_ids
                )
        # Voucher and collection numbers create candidates; barcode evidence
        # (accessions, sequence fingerprints) intentionally does not.
        for kind, value in left.identifiers:
            if value and _is_candidate_identifier(kind):
                blocks.update(
                    (left.key.observation_id, target)
                    for target in specimen_index.get((kind, value), ())
                )
        for media in left.media:
            if media.provenance_key:
                blocks.update(
                    (left.key.observation_id, target)
                    for target in media_index.get(media.provenance_key, ())
                )

    results: list[ObservationPair] = []
    for mo_id, inat_id in sorted(blocks):
        left, right = mo_by_id[mo_id], inat_by_id[inat_id]
        evidence = _inventory_evidence(left, right)
        score = score_evidence(evidence)
        if score.classification == "hidden" and (mo_id, inat_id) not in direct:
            continue
        link_state = authoritative_link_state(left, right)
        results.append(
            ObservationPair(
                mo_id, inat_id, link_state, score.total, evidence=score.evidence
            )
        )
    return results


def score_candidate(
    left: InventoryObservation,
    right: InventoryObservation,
    extra: Iterable[MatchEvidence] = (),
    *,
    explicit_review: bool = False,
) -> Optional[ObservationPair]:
    evidence = (*_inventory_evidence(left, right), *tuple(extra))
    score = score_evidence(evidence)
    if score.classification == "hidden" and not explicit_review:
        return None
    return ObservationPair(
        left.key.observation_id,
        right.key.observation_id,
        authoritative_link_state(left, right),
        score.total,
        evidence=score.evidence,
    )


def _inventory_evidence(
    left: InventoryObservation, right: InventoryObservation
) -> tuple[MatchEvidence, ...]:
    evidence: list[MatchEvidence] = []
    linked = (
        right.key.observation_id in left.authoritative_targets
        or left.key.observation_id in right.authoritative_targets
    )
    if linked:
        evidence.append(
            MatchEvidence(
                "authoritative_link",
                EvidenceFamily.LINK,
                65,
                "An authoritative cross-site link targets this observation.",
            )
        )
    if left.observed_on and right.observed_on:
        difference = abs((left.observed_on - right.observed_on).days)
        if difference == 0:
            evidence.append(
                MatchEvidence(
                    "exact_date",
                    EvidenceFamily.TEMPORAL,
                    15,
                    "Observed dates are equal.",
                )
            )
        elif difference == 1:
            evidence.append(
                MatchEvidence(
                    "adjacent_date",
                    EvidenceFamily.TEMPORAL,
                    5,
                    "Observed dates are adjacent.",
                )
            )
    if _same_locality(left, right):
        evidence.append(
            MatchEvidence(
                "same_public_locality",
                EvidenceFamily.SPATIAL,
                3,
                "Public locality labels match after normalization.",
            )
        )
    if left.taxon_name and right.taxon_name:
        if left.taxon_name.casefold() == right.taxon_name.casefold():
            evidence.append(
                MatchEvidence(
                    "exact_taxon", EvidenceFamily.TAXON, 10, "Taxon names match."
                )
            )
        elif _same_genus(left.taxon_name, right.taxon_name):
            evidence.append(
                MatchEvidence(
                    "same_genus", EvidenceFamily.TAXON, 5, "Taxa share a genus."
                )
            )
    left_identifiers = {(kind, value) for kind, value in left.identifiers if value}
    for kind, value in sorted(left_identifiers.intersection(right.identifiers)):
        family = (
            EvidenceFamily.BARCODE if kind == "accession" else EvidenceFamily.SPECIMEN
        )
        points = 35 if family == EvidenceFamily.BARCODE else 50
        evidence.append(
            MatchEvidence(
                f"exact_{kind}",
                family,
                points,
                f"Exact normalized {kind} matches.",
                EvidenceTier.METADATA,
            )
        )
    if set(left.sequence_hashes).intersection(right.sequence_hashes):
        evidence.append(
            MatchEvidence(
                "sequence_equivalence",
                EvidenceFamily.BARCODE,
                35,
                "Normalized sequences are equal, including reverse-complement equivalence.",
                EvidenceTier.METADATA,
            )
        )
    left_sources = {item.provenance_key for item in left.media if item.provenance_key}
    right_sources = {item.provenance_key for item in right.media if item.provenance_key}
    if left_sources.intersection(right_sources):
        evidence.append(
            MatchEvidence(
                "native_media_identity",
                EvidenceFamily.MEDIA,
                60,
                "Both records identify the same explicit source-qualified media.",
                EvidenceTier.METADATA,
            )
        )
    return tuple(evidence)


def authoritative_link_state(
    left: InventoryObservation, right: InventoryObservation
) -> str:
    if left.link_malformed or right.link_malformed:
        return "ambiguous_link"
    left_targets = set(left.authoritative_targets)
    right_targets = set(right.authoritative_targets)
    forward = right.key.observation_id in left_targets
    reverse = left.key.observation_id in right_targets
    if len(left_targets) > 1 or len(right_targets) > 1:
        return "ambiguous_link"
    if forward and reverse:
        return "reciprocal_unvalidated"
    if forward or reverse:
        return "one_way_link"
    return "candidate"


def scope_unknown_message(site: RemoteSite) -> str:
    """The exact ``unavailable`` entry emitted for an unknown fungal scope.

    Exposed so a caller that deliberately tolerates one site's unknown scope can
    match the entry exactly instead of pattern-matching prose.
    """
    return f"{site.value} fungal classification is unknown and requires review."


def specimen_identity_conflicts(
    left: HydratedObservation,
    right: HydratedObservation,
    *,
    include_coordinates: bool = True,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Material same-collection checks shared by pair validation and remote writes.

    Returns ``(conflicts, unavailable)`` where a non-empty ``conflicts`` tuple
    proves the two records cannot currently be treated as the same physical
    collection, and ``unavailable`` lists checks that could not be completed.

    ``include_coordinates`` is set false by coordinate copying, which is
    intentionally reconciling the point: the coordinate-distance conflict is
    then skipped while every other same-collection conflict (owner, fungal
    scope, deletion/availability, date, voucher/collection identifiers) still
    blocks.
    """
    conflicts: list[str] = []
    unavailable: list[str] = []
    for record in (left.inventory, right.inventory):
        if record.owner_id != record.account_id:
            conflicts.append(
                f"{record.key.site.value} owner differs from the selected profile account."
            )
        if record.fungi_status == "nonfungal":
            conflicts.append(
                f"{record.key.site.value} record is outside kingdom Fungi."
            )
        elif record.fungi_status == "unknown":
            unavailable.append(scope_unknown_message(record.key.site))
        if record.deleted or record.availability_state == "deleted":
            conflicts.append(
                f"{record.key.site.value} record is deleted or unavailable."
            )
    if left.inventory.observed_on and right.inventory.observed_on:
        if abs((left.inventory.observed_on - right.inventory.observed_on).days) > 1:
            conflicts.append("Observed dates differ by more than one day.")
    left_specimen = set(left.voucher_identifiers) | set(left.collection_identifiers)
    right_specimen = set(right.voucher_identifiers) | set(right.collection_identifiers)
    if left_specimen and right_specimen and left_specimen.isdisjoint(right_specimen):
        conflicts.append("Voucher or collection identifiers are disjoint.")
    if (
        include_coordinates
        and left.coordinates_available
        and right.coordinates_available
    ):
        if None not in (left.latitude, left.longitude, right.latitude, right.longitude):
            distance = _distance_m(left.latitude, left.longitude, right.latitude, right.longitude)  # type: ignore[arg-type]
            tolerance = max(
                10_000.0, (left.accuracy_m or 0.0) + (right.accuracy_m or 0.0)
            )
            if distance > tolerance:
                conflicts.append(
                    "Available points exceed 10 km and their combined accuracy tolerance."
                )
    return tuple(conflicts), tuple(unavailable)


def validate_reciprocal_pair(
    left: HydratedObservation, right: HydratedObservation
) -> tuple[str, tuple[str, ...]]:
    """Complete every material check before a reciprocal pair can auto-confirm."""
    if (
        authoritative_link_state(left.inventory, right.inventory)
        != "reciprocal_unvalidated"
    ):
        return "ambiguous_link", (
            "The authoritative links are not uniquely reciprocal.",
        )
    if not left.required_values_available or not right.required_values_available:
        return "ambiguous_link", (
            "A required validation value is hidden or unavailable.",
        )
    conflicts, unavailable = specimen_identity_conflicts(left, right)
    if conflicts:
        return "link_confirmed_with_metadata_conflicts", conflicts
    if unavailable:
        return "ambiguous_link", unavailable
    return "link_confirmed", ()


def is_same_site_duplicate(
    first: InventoryObservation,
    second: InventoryObservation,
    *,
    shared_strong_opposite: bool = False,
) -> bool:
    if first.key.site != second.key.site or first.key == second.key:
        return False
    if shared_strong_opposite:
        return True
    first_media = {(item.site, item.photo_id) for item in first.media}
    same_native_photo = bool(
        first_media.intersection((item.site, item.photo_id) for item in second.media)
    )
    return bool(
        same_native_photo
        and first.observed_on == second.observed_on
        and _taxon_compatible(first, second)
    )


def _same_locality(left: InventoryObservation, right: InventoryObservation) -> bool:
    return bool(
        _normalized_locality(left.public_locality)
        and _normalized_locality(left.public_locality)
        == _normalized_locality(right.public_locality)
    )


def _same_genus(left: str, right: str) -> bool:
    return bool(
        _normalized_genus(left) and _normalized_genus(left) == _normalized_genus(right)
    )


def _normalized_taxon(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalized_genus(value: str) -> str:
    normalized = _normalized_taxon(value)
    return normalized.split(maxsplit=1)[0] if normalized else ""


def _normalized_locality(value: str) -> str:
    return " ".join(value.casefold().split())


def _taxon_compatible(left: InventoryObservation, right: InventoryObservation) -> bool:
    return bool(
        left.taxon_name
        and right.taxon_name
        and (
            left.taxon_name.casefold() == right.taxon_name.casefold()
            or _same_genus(left.taxon_name, right.taxon_name)
        )
    )


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi, d_lambda = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 6371000.0 * 2 * asin(sqrt(a))
