"""Pure Phase 2B evidence-graph policy.

This module deliberately has no database, network, or UI dependencies. Every
irreversible boundary calls the same validator over structured evidence rows.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional, Sequence

from .types import ConsolidationEvidenceEdge, RemoteSite

MemberKey = tuple[RemoteSite, int]
CanonicalAnchorSignature = tuple[str, str]

STRONG_EVIDENCE_TYPES = frozenset(
    {
        "authoritative_link_mo_to_inat",
        "authoritative_link_inat_to_mo",
        "exact_voucher",
        "exact_collection_number",
        "native_media_identity",
    }
)
CORROBORATING_EVIDENCE_TYPES = frozenset(
    {
        "exact_date_close_coordinates",
    }
)
EVIDENCE_STRENGTH_BY_TYPE = {
    **{evidence_type: "strong" for evidence_type in STRONG_EVIDENCE_TYPES},
    **{
        evidence_type: "corroborating" for evidence_type in CORROBORATING_EVIDENCE_TYPES
    },
}


@dataclass(frozen=True)
class ValidatedEvidenceHop:
    left: MemberKey
    right: MemberKey
    edge: ConsolidationEvidenceEdge


@dataclass(frozen=True)
class ValidatedDonorPath:
    donor: MemberKey
    canonical: MemberKey
    hops: tuple[ValidatedEvidenceHop, ...]
    strong_anchor_index: int


@dataclass(frozen=True)
class ConsolidationGraphValidation:
    valid: bool
    reasons: tuple[str, ...]
    donor_paths: tuple[ValidatedDonorPath, ...]


def canonical_strong_anchor_signatures(
    edges: Sequence[ConsolidationEvidenceEdge],
    *,
    canonical_mo_id: Optional[int],
    canonical_inat_id: Optional[int],
) -> frozenset[CanonicalAnchorSignature]:
    """Return the durable strong anchors directly identifying a canonical pair."""
    if canonical_mo_id is None or canonical_inat_id is None:
        return frozenset()
    canonical_pair = {
        (RemoteSite.MO, canonical_mo_id),
        (RemoteSite.INAT, canonical_inat_id),
    }
    return frozenset(
        (edge.evidence_type, edge.reviewed_evidence_fingerprint)
        for edge in edges
        if EVIDENCE_STRENGTH_BY_TYPE.get(edge.evidence_type) == "strong"
        and edge.evidence_strength == "strong"
        and {
            (edge.left_site, edge.left_observation_id),
            (edge.right_site, edge.right_observation_id),
        }
        == canonical_pair
    )


def validate_consolidation_graph(
    keys: Sequence[MemberKey],
    edges: Sequence[ConsolidationEvidenceEdge],
    *,
    canonical_mo_id: Optional[int] = None,
    canonical_inat_id: Optional[int] = None,
) -> ConsolidationGraphValidation:
    """Validate connectivity, canonical anchoring, and every donor path.

    A donor may traverse corroborating edges, but it must encounter a strong
    edge before first reaching either canonical record. The strong edge joining
    the canonical pair cannot retroactively prove a donor that arrived using
    corroboration alone.
    """
    nodes = set(keys)
    if len(nodes) != len(keys):
        return _invalid("The reviewed evidence graph contains duplicate members.")
    adjacency: dict[
        MemberKey,
        list[tuple[MemberKey, ConsolidationEvidenceEdge]],
    ] = {node: [] for node in nodes}
    strong_edges = 0
    for edge in edges:
        left = (edge.left_site, edge.left_observation_id)
        right = (edge.right_site, edge.right_observation_id)
        if left not in nodes or right not in nodes or left == right:
            return _invalid(
                "The reviewed evidence graph contains a malformed member edge."
            )
        expected_strength = EVIDENCE_STRENGTH_BY_TYPE.get(edge.evidence_type)
        if expected_strength is None:
            return _invalid(
                "The reviewed evidence graph contains an unsupported evidence type."
            )
        if edge.evidence_strength != expected_strength:
            return _invalid(
                f"Evidence type {edge.evidence_type!r} must have strength "
                f"{expected_strength!r}, not {edge.evidence_strength!r}."
            )
        if not edge.reviewed_evidence_fingerprint:
            return _invalid(
                "The reviewed evidence graph contains an incomplete evidence record."
            )
        if edge.evidence_strength == "strong":
            strong_edges += 1
        adjacency[left].append((right, edge))
        adjacency[right].append((left, edge))
    if nodes and strong_edges == 0:
        return _invalid(
            "Supporting evidence alone cannot prove specimen identity; the "
            "reviewed graph has no strong identity evidence."
        )
    if nodes:
        seen: set[MemberKey] = set()
        queue = deque((next(iter(nodes)),))
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(
                neighbor for neighbor, _edge in adjacency[node] if neighbor not in seen
            )
        if seen != nodes:
            missing = sorted(
                f"{site.value} #{observation_id}"
                for site, observation_id in nodes - seen
            )
            return _invalid(
                "The proposed records do not form one connected specimen-evidence "
                f"graph; unsupported component includes {', '.join(missing)}."
            )

    canonical = {
        key
        for key in (
            (RemoteSite.MO, canonical_mo_id),
            (RemoteSite.INAT, canonical_inat_id),
        )
        if key[1] is not None
    }
    if not canonical:
        return ConsolidationGraphValidation(True, (), ())
    if not canonical.issubset(nodes):
        return _invalid(
            "A selected canonical observation is not a reviewed graph member."
        )
    if canonical_mo_id is not None and canonical_inat_id is not None:
        mo_key = (RemoteSite.MO, canonical_mo_id)
        inat_key = (RemoteSite.INAT, canonical_inat_id)
        direct = [edge for neighbor, edge in adjacency[mo_key] if neighbor == inat_key]
        if not direct:
            return _invalid(
                "The selected canonical MO and iNaturalist records do not have a "
                "direct reviewed specimen-identity edge."
            )
        if not any(edge.evidence_strength == "strong" for edge in direct):
            return _invalid(
                "The selected canonical MO and iNaturalist records require a "
                "direct strong identity edge; supporting evidence is insufficient."
            )

    paths: list[ValidatedDonorPath] = []
    for donor in sorted(nodes - canonical, key=lambda item: (item[0].value, item[1])):
        path = _strong_anchored_path(donor, canonical, adjacency)
        if path is None:
            return ConsolidationGraphValidation(
                False,
                (
                    f"{donor[0].value} #{donor[1]} has no evidence path to the "
                    "canonical specimen containing a strong identity anchor.",
                ),
                tuple(paths),
            )
        paths.append(path)
    return ConsolidationGraphValidation(True, (), tuple(paths))


def _strong_anchored_path(
    donor: MemberKey,
    canonical: set[MemberKey],
    adjacency: dict[
        MemberKey,
        list[tuple[MemberKey, ConsolidationEvidenceEdge]],
    ],
) -> Optional[ValidatedDonorPath]:
    start = (donor, False)
    queue = deque((start,))
    seen = {start}
    previous: dict[
        tuple[MemberKey, bool],
        tuple[tuple[MemberKey, bool], ConsolidationEvidenceEdge],
    ] = {}
    target: Optional[tuple[MemberKey, bool]] = None
    while queue:
        state = queue.popleft()
        node, has_strong = state
        if node in canonical and has_strong:
            target = state
            break
        if node in canonical:
            continue
        for neighbor, edge in adjacency[node]:
            next_state = (
                neighbor,
                has_strong or edge.evidence_strength == "strong",
            )
            if next_state not in seen:
                seen.add(next_state)
                previous[next_state] = (state, edge)
                queue.append(next_state)
    if target is None:
        return None
    hops: list[ValidatedEvidenceHop] = []
    cursor = target
    while cursor != start:
        prior, edge = previous[cursor]
        hops.append(ValidatedEvidenceHop(prior[0], cursor[0], edge))
        cursor = prior
    hops.reverse()
    return ValidatedDonorPath(
        donor=donor,
        canonical=target[0],
        hops=tuple(hops),
        strong_anchor_index=next(
            index
            for index, hop in enumerate(hops)
            if hop.edge.evidence_strength == "strong"
        ),
    )


def _invalid(reason: str) -> ConsolidationGraphValidation:
    return ConsolidationGraphValidation(False, (reason,), ())
