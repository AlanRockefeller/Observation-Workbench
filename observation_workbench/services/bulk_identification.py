"""Planning helpers for supervised agreement with DNA-backed identifications.

An observation qualifies when it carries a DNA Barcode ITS field and somebody
else has proposed a species-level name, provisional or published. The name the
sequence supports is read from the observation itself: Species Name Override
first, then Provisional Species Name, then a bare-name autovalidation comment,
then the sequencing pipeline's Barcode Inferred Species or Name.

Wrong agreements come from two places: a sequence attached to the wrong
observation, or a better name proposed after the one being agreed with. So an
agreement auto-posts only when the proposed name matches the DNA-backed name
and nothing since (a different ID, a comment, a family-level clash with the
consensus) suggests it is wrong; everything else waits for a human.

One case posts a name nobody has proposed yet: an undisputed provisional name
(see :func:`provisional_name_settled`) whose taxon already exists on
iNaturalist. Those observations cannot reach species until somebody uses the
taxon, so the workflow proposes it instead of agreeing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from observation_workbench.api.observation_url import (
    ObservationURLQuery,
    canonical_source_key,
)
from observation_workbench.models import (
    StudyIdentification,
    StudyObservation,
    StudyTaxon,
)
from observation_workbench.services.ai_observation_review import AIReviewFinding
from observation_workbench.services.autovalidated_ids import (
    AUTOVALIDATOR_LOGIN,
    DNA_BARCODE_ITS_FIELD_NAME,
    TAXON_CONFLICT_FAMILY,
    TaxonLineageIndex,
    TaxonLookupFailed,
    TaxonNameResolver,
    has_autovalidation_comment,
    latest_autovalidation_time,
    normalize_taxon_name,
    suggested_name_for,
    taxon_conflict_for,
)
from observation_workbench.services.identification_actions import (
    AgreeTarget,
    already_current_taxon,
    current_user_identification,
    make_target_from_ident,
    previously_withdrew_taxon,
    refresh_observations,
)
from observation_workbench.services.bulk_disagree import observation_finished_at_target
from observation_workbench.services.study_loader import LoadFilters, StudyLoader

# Ranks a DNA-backed name is given at. Anything coarser goes to a human, since
# it usually means a species-level (or temporary) name still has to be made.
SPECIES_LEVEL_RANKS = frozenset(
    {"species", "hybrid", "subspecies", "variety", "form", "infrahybrid"}
)

# An observation the operator identified or commented on this recently was
# just handled by hand, so planning skips it.
RECENT_ACTIVITY_SKIP_SECONDS = 60 * 60

REVIEW_NO_SPECIES_ID = "no_species_id"
REVIEW_NAME_MISMATCH = "name_mismatch"
REVIEW_UNCONFIRMED = "unconfirmed"
REVIEW_LATER_ID = "later_id"
REVIEW_COMMENTS = "comments"
REVIEW_YOUR_ID = "your_id"
REVIEW_FAMILY_CONFLICT = "family_conflict"


@dataclass(frozen=True)
class ReviewReason:
    kind: str
    text: str


@dataclass
class BulkAgreeCandidate:
    observation: StudyObservation
    target: AgreeTarget
    user_current_taxon: str = ""
    user_has_different_id: bool = False
    ai_review: Optional[AIReviewFinding] = None
    expected_name: str = ""
    expected_source: str = ""
    autovalidated: bool = False
    review_reasons: List[ReviewReason] = field(default_factory=list)
    # Nothing here can be agreed with (no species-level ID); review only.
    review_only: bool = False
    # ``target`` is the DNA-backed provisional taxon itself, which no current
    # identification uses yet, rather than somebody's identification.
    proposes_name: bool = False
    # The proposed taxon as looked up, ancestry included, for the post-time check.
    proposal_taxon: Optional[StudyTaxon] = None
    # time.time() when ``observation`` was fetched; 0 means unknown.
    checked_at: float = 0.0
    # The operator's current identification in that snapshot, if any.
    user_ident_id: Optional[int] = None


@dataclass
class BulkAgreePlanStats:
    """Counters describing one DNA-backed agreement planning pass."""

    total_results_scanned: int = 0
    candidate_count: int = 0
    review_count: int = 0
    skipped_no_identification: int = 0
    skipped_no_dna_barcode_its: int = 0
    skipped_already_agreed: int = 0
    skipped_already_research_grade: int = 0
    skipped_previously_withdrew: int = 0
    skipped_permanent: int = 0
    skipped_provisional_not_on_inat: int = 0
    provisional_proposals: int = 0
    skipped_you_hold_dna_name: int = 0
    skipped_recent_activity: int = 0
    total_api_results: int = 0


@dataclass
class BulkAgreePlanResult:
    candidates: List[BulkAgreeCandidate] = field(default_factory=list)
    stats: BulkAgreePlanStats = field(default_factory=BulkAgreePlanStats)


@dataclass
class DnaBackedAssessment:
    target: StudyIdentification
    expected_name: str
    expected_source: str
    autovalidated: bool
    review_reasons: List[ReviewReason]
    review_only: bool = False


def is_species_level(taxon: Optional[StudyTaxon]) -> bool:
    if taxon is None:
        return False
    rank = (taxon.rank or "").strip().casefold()
    if rank:
        return rank in SPECIES_LEVEL_RANKS
    # Rank missing from the payload: a provisional epithet is species-level.
    return "'" in (taxon.name or "")


def expected_dna_name(obs: StudyObservation) -> Tuple[str, str]:
    """Return (name, where it came from) for the name the sequence supports."""
    override = (obs.species_name_override or "").strip()
    if override:
        return override, "Species Name Override"
    provisional = (obs.provisional_species_name or "").strip()
    if provisional:
        return provisional, "Provisional Species Name"
    comment_name = suggested_name_for(obs)
    if comment_name:
        return comment_name, "autovalidation comment"
    # Last: set automatically from the BLAST result, so a person-entered or
    # autovalidated name above wins whenever there is one.
    inferred = (obs.barcode_inferred_name or "").strip()
    if inferred:
        return inferred, "Barcode Inferred Species or Name"
    return "", ""


def _timestamp(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_ancestor(ancestor: StudyTaxon, taxon: StudyTaxon) -> bool:
    """True when ``ancestor`` contains ``taxon``, from the payload alone."""
    if not ancestor.taxon_id or ancestor.taxon_id == taxon.taxon_id:
        return False
    if taxon.ancestry:
        return str(ancestor.taxon_id) in taxon.ancestry.split("/")
    # No lineage in the payload: a genus contains names that start with it.
    return (ancestor.rank or "").casefold() == "genus" and (
        taxon.name or ""
    ).startswith(f"{ancestor.name} ")


def _shares_lineage(first: StudyTaxon, second: StudyTaxon) -> bool:
    return (
        first.taxon_id == second.taxon_id
        or _is_ancestor(first, second)
        or _is_ancestor(second, first)
    )


def assess_dna_backed_target(
    obs: StudyObservation, login: str
) -> Optional[DnaBackedAssessment]:
    """Pick the identification to agree with and say whether a human must look.

    Returns None when nobody else has a current identification. Works purely
    on the payload: no API calls.
    """
    login_key = login.casefold()
    others = [
        ident
        for ident in obs.all_identifications
        if ident.current
        and ident.taxon
        and ident.taxon.taxon_id
        and ident.user_login.casefold() != login_key
    ]
    if not others:
        return None
    others.sort(key=lambda i: (i.created_at or "", i.ident_id), reverse=True)

    expected, source = expected_dna_name(obs)
    autovalidated = has_autovalidation_comment(obs)
    reasons: List[ReviewReason] = []

    species_level = [i for i in others if is_species_level(i.taxon)]
    if not species_level:
        target = others[0]
        text = (
            f"No species-level ID to agree with (newest is {target.taxon.name}); "
            "a species or temporary name may need to be created"
        )
        if expected:
            text += f" for {expected}"
        reasons.append(ReviewReason(REVIEW_NO_SPECIES_ID, text))
        return DnaBackedAssessment(
            target, expected, source, autovalidated, reasons, review_only=True
        )

    wanted = normalize_taxon_name(expected)
    matches = [
        i for i in species_level if wanted and normalize_taxon_name(i.taxon.name) == wanted
    ]
    if matches:
        target = matches[0]
    else:
        target = species_level[0]
        if expected:
            reasons.append(
                ReviewReason(
                    REVIEW_NAME_MISMATCH,
                    f"{target.taxon.name} differs from the {source} ({expected})",
                )
            )
        else:
            reasons.append(
                ReviewReason(
                    REVIEW_UNCONFIRMED,
                    "No Species Name Override, Provisional Species Name, "
                    "autovalidation comment, or Barcode Inferred Species or Name "
                    "confirms this name",
                )
            )

    # Anything after the autovalidation comment (or, without one, after the
    # target ID) may be somebody who found a better name.
    anchor = latest_autovalidation_time(obs) if autovalidated else None
    anchor_label = "the autovalidation comment"
    if anchor is None:
        anchor = _timestamp(target.created_at)
        anchor_label = f"{target.user_login}'s ID"
    if anchor is not None:
        later = []
        for ident in others:
            if ident is target or ident.taxon.taxon_id == target.taxon.taxon_id:
                continue
            created = _timestamp(ident.created_at)
            if created is None or created <= anchor:
                continue
            if not ident.is_disagreement and _is_ancestor(ident.taxon, target.taxon):
                continue
            later.append(ident)
        if later:
            names = ", ".join(f"{i.taxon.name} (@{i.user_login})" for i in later)
            reasons.append(
                ReviewReason(
                    REVIEW_LATER_ID, f"Different ID after {anchor_label}: {names}"
                )
            )
        autovalidator_key = AUTOVALIDATOR_LOGIN.casefold()
        comment_count = sum(
            1
            for comment in obs.comments
            if not comment.hidden
            and comment.user_login.casefold() != autovalidator_key
            and (created := _timestamp(comment.created_at)) is not None
            and created > anchor
        )
        if comment_count:
            reasons.append(
                ReviewReason(
                    REVIEW_COMMENTS,
                    f"{comment_count} comment(s) after {anchor_label}",
                )
            )

    mine = current_user_identification(obs, login)
    if (
        mine is not None
        and mine.taxon
        and mine.taxon.taxon_id != target.taxon.taxon_id
        and not _is_ancestor(mine.taxon, target.taxon)
    ):
        reasons.append(
            ReviewReason(REVIEW_YOUR_ID, f"Your current ID is {mine.taxon.name}")
        )

    return DnaBackedAssessment(target, expected, source, autovalidated, reasons)


def provisional_name_settled(
    obs: StudyObservation,
    expected_name: str,
    taxon: Optional[StudyTaxon] = None,
) -> bool:
    """True when nothing disputes proposing the provisional DNA-backed name.

    The DNA-backed name is a provisional epithet (``Mycena sp. 'pura-PNW15'``)
    and nobody but the autovalidator has commented anything other than that
    name. Every current ID then has to be either

    * the name's iNaturalist ``taxon`` or one of its ancestors (genus,
      subsection, ...) -- or, when ``taxon`` is None because the name is not on
      iNaturalist, on one lineage with the others, at least one of them being
      the name's genus, or
    * a different species in the same genus that a later explicit disagreement
      at a rank containing it has already overruled -- typically the
      observer's field guess, set aside once the sequence came back.

    An explicit disagreement that does not overrule such an ID, or any
    unoverruled conflicting species, means somebody disputes the name and a
    human decides. Makes no API calls; ``taxon`` comes from the caller's lookup.
    """
    wanted = normalize_taxon_name(expected_name)
    if "'" not in wanted:
        return False
    genus = wanted.split(" ", 1)[0]
    current = [
        ident
        for ident in obs.all_identifications
        if ident.current and ident.taxon and ident.taxon.taxon_id
    ]
    if not current:
        return False

    def overrules(dissent: StudyIdentification, ident: StudyIdentification) -> bool:
        created = _timestamp(dissent.created_at)
        earlier = _timestamp(ident.created_at)
        return (
            dissent.is_disagreement
            and created is not None
            and earlier is not None
            and created > earlier
            and _is_ancestor(dissent.taxon, ident.taxon)
        )

    conflicting = [
        ident
        for ident in current
        if is_species_level(ident.taxon)
        and normalize_taxon_name(ident.taxon.name) != wanted
    ]
    for ident in conflicting:
        if (ident.taxon.name or "").casefold().split(" ", 1)[0] != genus:
            return False
        if not any(overrules(dissent, ident) for dissent in current):
            return False
    consistent = [ident for ident in current if ident not in conflicting]
    for ident in consistent:
        if ident.is_disagreement and not any(
            overrules(ident, other) for other in conflicting
        ):
            return False
    if taxon is not None:
        if not all(
            ident.taxon.taxon_id == taxon.taxon_id or _is_ancestor(ident.taxon, taxon)
            for ident in consistent
        ):
            return False
    else:
        if not any(
            (ident.taxon.rank or "").casefold() == "genus"
            and (ident.taxon.name or "").casefold() == genus
            for ident in consistent
        ):
            return False
        for i, first in enumerate(consistent):
            for second in consistent[i + 1 :]:
                if not _shares_lineage(first.taxon, second.taxon):
                    return False

    autovalidator_key = AUTOVALIDATOR_LOGIN.casefold()
    for comment in obs.comments:
        if comment.hidden or comment.user_login.casefold() == autovalidator_key:
            continue
        if normalize_taxon_name(comment.body) != wanted:
            return False
    return True


def proposal_applies(assessment: DnaBackedAssessment) -> bool:
    """Nobody else holds the DNA-backed name, so proposing it is the only way on."""
    return assessment.review_only or any(
        reason.kind == REVIEW_NAME_MISMATCH for reason in assessment.review_reasons
    )


def holds_dna_name(obs: StudyObservation, login: str, expected_name: str) -> bool:
    """True when the operator's current ID already is the DNA-backed name."""
    wanted = normalize_taxon_name(expected_name)
    mine = current_user_identification(obs, login)
    return bool(
        wanted
        and mine is not None
        and mine.taxon
        and normalize_taxon_name(mine.taxon.name) == wanted
    )


def weighed_in_recently(obs: StudyObservation, login: str, now: float) -> bool:
    """True when the operator identified or commented within the last hour.

    A fresh ID or comment means the operator has just looked at this
    observation by hand, so the run leaves it alone rather than second-guess it.
    """
    login_key = login.casefold()
    cutoff = now - RECENT_ACTIVITY_SKIP_SECONDS
    stamps = [
        ident.created_at
        for ident in obs.all_identifications
        if ident.user_login.casefold() == login_key
    ] + [
        comment.created_at
        for comment in obs.comments
        if comment.user_login.casefold() == login_key
    ]
    for stamp in stamps:
        when = _timestamp(stamp)
        if when is not None and when.timestamp() >= cutoff:
            return True
    return False


def candidate_from_assessment(
    obs: StudyObservation,
    assessment: DnaBackedAssessment,
    login: str,
    checked_at: float,
) -> BulkAgreeCandidate:
    user_ident = current_user_identification(obs, login)
    return BulkAgreeCandidate(
        observation=obs,
        target=make_target_from_ident(obs, assessment.target),
        user_current_taxon=user_ident.taxon.name if user_ident else "",
        user_has_different_id=bool(user_ident),
        expected_name=assessment.expected_name,
        expected_source=assessment.expected_source,
        autovalidated=assessment.autovalidated,
        review_reasons=list(assessment.review_reasons),
        review_only=assessment.review_only,
        checked_at=checked_at,
        user_ident_id=user_ident.ident_id if user_ident else None,
    )


def with_dna_barcode_filter(filters: LoadFilters) -> LoadFilters:
    """Ask the API for DNA-barcoded observations only, when the query allows it.

    Server-side filtering means pages of observations without a sequence are
    never downloaded. Username-mode queries cannot carry the field filter and
    are filtered locally instead.
    """
    query = filters.observation_query
    if query is None:
        return filters
    field_key = f"field:{DNA_BARCODE_ITS_FIELD_NAME}"
    if any(key.casefold() == field_key.casefold() for key, _value in query.params):
        return filters
    params = list(query.params) + [(field_key, "")]
    narrowed = ObservationURLQuery(
        display_url=query.display_url,
        source_key=canonical_source_key(params),
        params=params,
        source_kind=query.source_kind,
        parameter_sources=tuple(query.parameter_sources) + ("DNA-backed filter",),
    )
    return LoadFilters(
        filters.source_input,
        taxon_id=filters.taxon_id,
        provisional_name_only=filters.provisional_name_only,
        observation_query=narrowed,
        apply_taxon_filter_to_observation_url=False,
    )


def _annotate_family_conflicts(
    loader: StudyLoader,
    pending: List[Tuple[BulkAgreeCandidate, StudyTaxon]],
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> None:
    """Flag targets in a different family from the consensus: a misplaced sequence.

    Only pairs the payload's own lineage cannot reconcile are resolved, and
    ancestor ranks come from the local cache first, so this rarely costs a request.
    """
    unresolved = []
    for candidate, target_taxon in pending:
        consensus = candidate.observation.community_taxon or candidate.observation.taxon
        if consensus is None or not consensus.taxon_id:
            continue
        if _shares_lineage(consensus, target_taxon):
            continue
        unresolved.append((candidate, target_taxon, consensus))
    if not unresolved:
        return
    index = TaxonLineageIndex(loader._client, loader._db)
    for _candidate, target_taxon, consensus in unresolved:
        index.add(target_taxon)
        index.add(consensus)
    index.resolve(is_cancelled)
    for candidate, target_taxon, consensus in unresolved:
        if taxon_conflict_for(index, consensus, target_taxon.taxon_id) == (
            TAXON_CONFLICT_FAMILY
        ):
            candidate.review_reasons.append(
                ReviewReason(
                    REVIEW_FAMILY_CONFLICT,
                    f"{target_taxon.name} is in a different family from the "
                    f"consensus {consensus.name}; the sequence may belong to "
                    "another specimen",
                )
            )


def plan_dna_backed_candidates(
    loader: StudyLoader,
    filters: LoadFilters,
    login: str,
    *,
    api_token: str = "",
    per_page: int = 200,
    max_observations: Optional[int] = None,
    only_if_needed: bool = True,
    use_cache: bool = True,
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> BulkAgreePlanResult:
    """Fetch pages for a query and select DNA-backed agreement targets.

    ``max_observations`` caps how many results are scanned; ``None`` scans
    every page. Each page's survivors are refreshed in one batched request,
    because the index payload's identification list can be incomplete.
    """
    filters = with_dna_barcode_filter(filters)
    resolver = TaxonNameResolver(loader._client, is_cancelled=is_cancelled)
    candidates: List[BulkAgreeCandidate] = []
    family_pending: List[Tuple[BulkAgreeCandidate, StudyTaxon]] = []
    stats = BulkAgreePlanStats()
    page = 1
    login_key = login.casefold()
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
            observations = observations[: max_scan - stats.total_results_scanned]
        stats.total_results_scanned += len(observations)
        if progress:
            progress(stats.total_results_scanned, total)
        survivors: List[StudyObservation] = []
        for obs in observations:
            if loader._db.is_bulk_agree_skipped(obs.obs_id):
                stats.skipped_permanent += 1
                continue
            if not (obs.dna_barcode_its or "").strip():
                stats.skipped_no_dna_barcode_its += 1
                continue
            if not any(
                ident.current and ident.user_login.casefold() != login_key
                for ident in obs.all_identifications
            ):
                stats.skipped_no_identification += 1
                continue
            survivors.append(obs)

        if is_cancelled and is_cancelled():
            break
        fresh_by_id = {}
        if survivors:
            refreshed = refresh_observations(
                loader._client,
                api_token,
                [obs.obs_id for obs in survivors],
            )
            fresh_by_id = {obs.obs_id: obs for obs in refreshed}
        checked_at = time.time()

        for obs in survivors:
            obs = fresh_by_id.get(obs.obs_id, obs)
            assessment = assess_dna_backed_target(obs, login)
            if assessment is None:
                stats.skipped_no_identification += 1
                continue
            if weighed_in_recently(obs, login, checked_at):
                stats.skipped_recent_activity += 1
                continue
            if holds_dna_name(obs, login, assessment.expected_name):
                stats.skipped_you_hold_dna_name += 1
                continue
            proposal: Optional[StudyTaxon] = None
            expected = assessment.expected_name
            if proposal_applies(assessment) and "'" in normalize_taxon_name(expected):
                try:
                    resolved = resolver.resolve(expected)
                except TaxonLookupFailed:
                    resolved = None  # existence unknown: leave it for a human
                else:
                    if resolved is None and provisional_name_settled(obs, expected):
                        stats.skipped_provisional_not_on_inat += 1
                        continue
                if resolved is not None and provisional_name_settled(
                    obs, expected, resolved
                ):
                    proposal = resolved
            target_taxon = proposal or assessment.target.taxon
            target_id = target_taxon.taxon_id
            if proposal is not None or not assessment.review_only:
                if only_if_needed and observation_finished_at_target(obs, target_id):
                    stats.skipped_already_research_grade += 1
                    continue
                if already_current_taxon(obs, login, target_id):
                    stats.skipped_already_agreed += 1
                    continue
                if previously_withdrew_taxon(obs, login, target_id):
                    stats.skipped_previously_withdrew += 1
                    continue
            candidate = candidate_from_assessment(obs, assessment, login, checked_at)
            if proposal is not None:
                candidate.target = AgreeTarget(
                    observation_id=obs.obs_id,
                    taxon_id=proposal.taxon_id,
                    taxon_name=proposal.name,
                )
                candidate.review_reasons = []
                candidate.review_only = False
                candidate.proposes_name = True
                candidate.proposal_taxon = proposal
                stats.provisional_proposals += 1
            candidates.append(candidate)
            if not candidate.review_only:
                family_pending.append((candidate, target_taxon))
        if stats.total_api_results <= 0 or page * per_page >= stats.total_api_results:
            break
        page += 1
    if not (is_cancelled and is_cancelled()):
        _annotate_family_conflicts(loader, family_pending, is_cancelled)
    stats.candidate_count = len(candidates)
    stats.review_count = sum(1 for c in candidates if c.review_reasons)
    return BulkAgreePlanResult(candidates=candidates, stats=stats)
