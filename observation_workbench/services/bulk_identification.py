"""Planning helpers for supervised bulk provisional-name agreements."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from observation_workbench.models import StudyIdentification, StudyObservation
from observation_workbench.services.identification_actions import (
    AgreeTarget,
    already_current_taxon,
    current_user_identification,
    make_target_from_ident,
    most_recent_non_self_current_identification,
    previously_withdrew_taxon,
    refresh_observations,
)
from observation_workbench.services.bulk_disagree import observation_finished_at_target
from observation_workbench.services.study_loader import LoadFilters, StudyLoader


@dataclass
class BulkAgreeCandidate:
    observation: StudyObservation
    target: AgreeTarget
    user_current_taxon: str = ""
    user_has_different_id: bool = False


@dataclass
class BulkAgreePlanStats:
    """Counters describing one provisional-agreement planning pass."""

    total_results_scanned: int = 0
    candidate_count: int = 0
    skipped_no_provisional_id: int = 0
    skipped_no_dna_barcode_its: int = 0
    skipped_already_agreed: int = 0
    skipped_already_research_grade: int = 0
    skipped_previously_withdrew: int = 0
    skipped_permanent: int = 0
    total_api_results: int = 0


@dataclass
class BulkAgreePlanResult:
    candidates: List[BulkAgreeCandidate] = field(default_factory=list)
    stats: BulkAgreePlanStats = field(default_factory=BulkAgreePlanStats)


def plan_provisional_candidates(
    loader: StudyLoader,
    filters: LoadFilters,
    login: str,
    *,
    api_token: str = "",
    per_page: int = 200,
    max_observations: Optional[int] = None,
    require_dna_barcode_its: bool = True,
    only_if_needed: bool = True,
    use_cache: bool = True,
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> BulkAgreePlanResult:
    """Fetch pages for a query and select provisional-ID agreement targets.

    ``max_observations`` caps how many results are scanned, mirroring the bulk
    disagree workflow; ``None`` scans every page. ``require_dna_barcode_its``
    keeps the long-standing default of only agreeing to observations that carry
    a DNA Barcode ITS observation field. ``api_token``, when supplied,
    authenticates the refresh-observations call so the identifications list
    reflects the caller's own visibility (matching the bulk disagree planners).
    """
    candidates: List[BulkAgreeCandidate] = []
    stats = BulkAgreePlanStats()
    page = 1
    max_scan = max(1, int(max_observations)) if max_observations else None
    while max_scan is None or stats.total_results_scanned < max_scan:
        if is_cancelled and is_cancelled():
            break
        observations, total = loader.load_page(
            filters,
            page=page,
            per_page=per_page,
            is_cancelled=is_cancelled,
            use_cache=use_cache,
        )
        stats.total_api_results = total
        if not observations:
            break
        if max_scan is not None:
            observations = observations[:max_scan - stats.total_results_scanned]
        stats.total_results_scanned += len(observations)
        if progress:
            progress(stats.total_results_scanned, total)
        provisional_targets: list[tuple[StudyObservation, StudyIdentification]] = []
        for obs in observations:
            if loader._db.is_bulk_agree_skipped(obs.obs_id):
                stats.skipped_permanent += 1
                continue
            ident = most_recent_non_self_current_identification(
                obs,
                login,
                provisional_only=True,
            )
            if ident is None:
                stats.skipped_no_provisional_id += 1
                continue
            if require_dna_barcode_its and not obs.dna_barcode_its:
                stats.skipped_no_dna_barcode_its += 1
                continue
            provisional_targets.append((obs, ident))

        # The index-endpoint payload may have an incomplete identifications
        # list, causing already_current_taxon to miss the user's existing
        # agreement. Fetch full observations in one batch so the check is
        # reliable without one API call per observation.
        fresh_by_id = {}
        if is_cancelled and is_cancelled():
            break
        if provisional_targets:
            refreshed = refresh_observations(
                loader._client,
                api_token,
                [obs.obs_id for obs, _ident in provisional_targets],
            )
            fresh_by_id = {obs.obs_id: obs for obs in refreshed}

        for obs, ident in provisional_targets:
            if is_cancelled and is_cancelled():
                break
            fresh = fresh_by_id.get(obs.obs_id)
            if fresh is not None:
                obs = fresh
                # Re-derive the provisional target from the fresh payload.
                ident = most_recent_non_self_current_identification(
                    obs, login, provisional_only=True,
                )
                if ident is None:
                    stats.skipped_no_provisional_id += 1
                    continue

            target = make_target_from_ident(obs, ident)
            if only_if_needed and observation_finished_at_target(obs, target.taxon_id):
                stats.skipped_already_research_grade += 1
                continue
            if already_current_taxon(obs, login, target.taxon_id):
                stats.skipped_already_agreed += 1
                continue
            if previously_withdrew_taxon(obs, login, target.taxon_id):
                stats.skipped_previously_withdrew += 1
                continue
            user_ident = current_user_identification(obs, login)
            candidates.append(
                BulkAgreeCandidate(
                    observation=obs,
                    target=target,
                    user_current_taxon=user_ident.taxon.name if user_ident else "",
                    user_has_different_id=bool(user_ident),
                )
            )
        if stats.total_api_results <= 0 or page * per_page >= stats.total_api_results:
            break
        page += 1
    stats.candidate_count = len(candidates)
    return BulkAgreePlanResult(candidates=candidates, stats=stats)
