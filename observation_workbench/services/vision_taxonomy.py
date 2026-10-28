"""Resolve MycoMap Vision names into iNaturalist lineages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from observation_workbench.api.client import INatClient
from observation_workbench.api.parsers import parse_taxon
from observation_workbench.models import StudyTaxon

FUNGI_TAXON_ID = 47170


@dataclass(frozen=True)
class VisionTaxonCandidate:
    rank: str
    name: str
    confidence: float | None = None


@dataclass(frozen=True)
class VisionTaxonResolution:
    candidate: VisionTaxonCandidate
    taxon: StudyTaxon | None = None
    family: StudyTaxon | None = None
    lineage: tuple[StudyTaxon, ...] = ()
    error: str = ""


def candidates_from_vision_result(result: dict) -> tuple[VisionTaxonCandidate, ...]:
    """Return unique species and genus candidates in Vision's display order."""
    ranks = result.get("ranks")
    if not isinstance(ranks, dict):
        return ()
    candidates: list[VisionTaxonCandidate] = []
    seen: set[tuple[str, str]] = set()
    for rank, limit in (("species", 5), ("genus", 3)):
        values = ranks.get(rank)
        if not isinstance(values, list):
            continue
        for value in values[:limit]:
            if not isinstance(value, dict):
                continue
            name = str(value.get("name") or "").strip()
            key = (rank, name.casefold())
            if not name or key in seen:
                continue
            seen.add(key)
            candidates.append(
                VisionTaxonCandidate(
                    rank=rank,
                    name=name,
                    confidence=_optional_float(value.get("confidence")),
                )
            )
    return tuple(candidates)


def resolve_vision_taxa(
    client: INatClient,
    candidates: Iterable[VisionTaxonCandidate],
) -> tuple[VisionTaxonResolution, ...]:
    """Resolve exact active fungal names and hydrate their full lineages."""
    ordered = tuple(candidates)
    matches: dict[tuple[str, str], StudyTaxon] = {}
    errors: dict[tuple[str, str], str] = {}
    known_taxa: dict[int, StudyTaxon] = {}

    for candidate in ordered:
        key = _candidate_key(candidate)
        raw = client.get_taxa_autocomplete(candidate.name, per_page=10)
        exact: list[tuple[StudyTaxon, dict]] = []
        for record in raw.get("results") or []:
            if not isinstance(record, dict) or record.get("is_active") is False:
                continue
            taxon = parse_taxon(record)
            if taxon is None or taxon.taxon_id <= 0:
                continue
            if taxon.name.strip().casefold() != candidate.name.casefold():
                continue
            if taxon.rank.casefold() != candidate.rank.casefold():
                continue
            if not _is_fungus(taxon):
                continue
            exact.append((taxon, record))
        if len(exact) != 1:
            errors[key] = (
                "No exact active iNaturalist fungal taxon"
                if not exact
                else "Several exact iNaturalist taxa matched"
            )
            continue
        taxon, record = exact[0]
        matches[key] = taxon
        known_taxa[taxon.taxon_id] = taxon
        for ancestor in record.get("ancestors") or []:
            if not isinstance(ancestor, dict):
                continue
            parsed = parse_taxon(ancestor)
            if parsed is not None and parsed.taxon_id > 0:
                known_taxa[parsed.taxon_id] = parsed

    wanted_ancestor_ids = {
        ancestor_id
        for taxon in matches.values()
        for ancestor_id in _ancestry_ids(taxon.ancestry)
        if ancestor_id not in known_taxa
    }
    if wanted_ancestor_ids:
        raw = client.get_taxa_by_ids(sorted(wanted_ancestor_ids))
        for record in raw.get("results") or []:
            if not isinstance(record, dict):
                continue
            taxon = parse_taxon(record)
            if taxon is not None and taxon.taxon_id > 0:
                known_taxa[taxon.taxon_id] = taxon

    resolutions: list[VisionTaxonResolution] = []
    for candidate in ordered:
        key = _candidate_key(candidate)
        taxon = matches.get(key)
        if taxon is None:
            resolutions.append(
                VisionTaxonResolution(candidate=candidate, error=errors.get(key, ""))
            )
            continue
        lineage_ids = (*_ancestry_ids(taxon.ancestry), taxon.taxon_id)
        lineage = tuple(
            known_taxa[taxon_id]
            for taxon_id in lineage_ids
            if taxon_id in known_taxa
        )
        missing_ids = [
            taxon_id for taxon_id in lineage_ids if taxon_id not in known_taxa
        ]
        if missing_ids:
            resolutions.append(
                VisionTaxonResolution(
                    candidate=candidate,
                    taxon=taxon,
                    lineage=lineage,
                    error="iNaturalist returned an incomplete lineage",
                )
            )
            continue
        family = next(
            (member for member in reversed(lineage) if member.rank == "family"),
            None,
        )
        resolutions.append(
            VisionTaxonResolution(
                candidate=candidate,
                taxon=taxon,
                family=family,
                lineage=lineage,
                error="" if family is not None else "No family found in the lineage",
            )
        )
    return tuple(resolutions)


def most_specific_shared_taxon(
    resolutions: Iterable[VisionTaxonResolution],
) -> StudyTaxon | None:
    """Return the deepest iNaturalist ancestor shared by all resolutions."""
    selected = tuple(resolutions)
    if not selected or any(not resolution.lineage for resolution in selected):
        return None
    shared_ids = {taxon.taxon_id for taxon in selected[0].lineage}
    for resolution in selected[1:]:
        shared_ids.intersection_update(taxon.taxon_id for taxon in resolution.lineage)
    for taxon in reversed(selected[0].lineage):
        if taxon.taxon_id in shared_ids:
            return taxon
    return None


def _candidate_key(candidate: VisionTaxonCandidate) -> tuple[str, str]:
    return candidate.rank.casefold(), candidate.name.casefold()


def _ancestry_ids(ancestry: str) -> tuple[int, ...]:
    return tuple(
        int(part)
        for part in str(ancestry or "").split("/")
        if part.strip().isdigit()
    )


def _is_fungus(taxon: StudyTaxon) -> bool:
    return (
        taxon.taxon_id == FUNGI_TAXON_ID
        or taxon.iconic_taxon_name.casefold() == "fungi"
        or FUNGI_TAXON_ID in _ancestry_ids(taxon.ancestry)
    )


def _optional_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
