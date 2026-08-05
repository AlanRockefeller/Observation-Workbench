"""Planning and posting helpers for supervised bulk disagree-to-taxon runs."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from observation_workbench.api.client import INatAPIError, INatClient
from observation_workbench.api.observation_url import ObservationURLQuery
from observation_workbench.api.parsers import parse_taxon
from observation_workbench.models import StudyObservation, StudyTaxon
from observation_workbench.services.identification_actions import (
    current_user_identification,
    load_users_not_to_tag,
    refresh_observation,
    refresh_observations,
)
from observation_workbench.services.study_loader import LoadFilters, StudyLoader

log = logging.getLogger(__name__)

# The "ID is as good as it can be" vote posts to the www.inaturalist.org votes
# endpoint. This endpoint/auth has been verified to work with the JWT API token
# this app uses, so DQA voting is enabled.
DQA_POSTING_ENABLED = True
DQA_DISABLED_MESSAGE = (
    "DQA voting is disabled because the endpoint and payload for this vote "
    "have not been verified in this app."
)


@dataclass
class BulkDisagreeCandidate:
    observation: StudyObservation
    source_taxon_id: int
    source_taxon_name: str
    target_taxon_id: int
    target_taxon_name: str
    target_taxon_rank: str = ""
    # Set when the URL identified its source by a Provisional Species Name field
    # value rather than a numeric taxon_id. The safety re-check then verifies the
    # observation still carries this provisional name instead of a taxon match.
    source_provisional_name: str = ""
    current_observation_taxon_name: str = ""
    community_taxon_name: str = ""
    has_dna_barcode_its: bool = False
    dna_barcode_its_value: str = ""
    user_current_taxon: str = ""
    dqa_vote_planned: bool = False
    explicit_disagreement: bool = True
    other_identifier_logins: List[str] = field(default_factory=list)


@dataclass
class BulkDisagreePlanStats:
    total_url_results_scanned: int = 0
    candidate_count: int = 0
    skipped_dna_barcode_its: int = 0
    skipped_missing_dna_barcode_its: int = 0
    skipped_already_target: int = 0
    skipped_source_no_match: int = 0
    skipped_permanent: int = 0
    skipped_missing_invalid_data: int = 0
    skipped_refresh_failure: int = 0
    total_api_results: int = 0


@dataclass
class BulkDisagreePlanResult:
    candidates: List[BulkDisagreeCandidate] = field(default_factory=list)
    stats: BulkDisagreePlanStats = field(default_factory=BulkDisagreePlanStats)


@dataclass
class BulkDisagreeResult:
    status: str
    message: str
    candidate: Optional[BulkDisagreeCandidate] = None
    refreshed_observation: Optional[StudyObservation] = None
    raw_response: Optional[dict] = None


def resolve_taxon(client: INatClient, taxon_id: int) -> StudyTaxon:
    """Fetch and parse a taxon by ID, raising if the response is unusable."""
    raw = client.get_taxon_by_id(int(taxon_id))
    raw_taxon = None
    if isinstance(raw.get("results"), list):
        raw_taxon = raw["results"][0] if raw["results"] else None
    elif isinstance(raw, dict):
        raw_taxon = raw
    taxon = parse_taxon(raw_taxon)
    if taxon is None or not taxon.taxon_id or taxon.name == "Unknown":
        raise ValueError(f"Could not resolve taxon_id {taxon_id}.")
    return taxon


def resolve_taxon_name(client: INatClient, taxon_id: int) -> str:
    return resolve_taxon(client, taxon_id).name


def taxon_is_ancestor_or_same(source_taxon: StudyTaxon, target_taxon_id: int) -> bool:
    target = int(target_taxon_id)
    if source_taxon.taxon_id == target:
        return True
    return taxon_is_strict_ancestor(source_taxon, target)


def taxon_is_strict_ancestor(source_taxon: StudyTaxon, target_taxon_id: int) -> bool:
    target = int(target_taxon_id)
    ancestry_ids = {
        int(part)
        for part in (source_taxon.ancestry or "").split("/")
        if part.strip().isdigit()
    }
    return target in ancestry_ids


def taxon_matches_or_descends_from(
    taxon: Optional[StudyTaxon],
    source_taxon_id: int,
) -> bool:
    """Return true when taxon is the source taxon or one of its descendants."""
    if taxon is None or not taxon.taxon_id:
        return False
    source = int(source_taxon_id)
    if taxon.taxon_id == source:
        return True
    ancestry_ids = {
        int(part)
        for part in (taxon.ancestry or "").split("/")
        if part.strip().isdigit()
    }
    return source in ancestry_ids


def observation_matches_source_taxon(
    obs: StudyObservation,
    source_taxon_id: int,
) -> bool:
    return any(
        taxon_matches_or_descends_from(taxon, source_taxon_id)
        for taxon in (obs.taxon, obs.community_taxon)
    )


def observation_matches_source_provisional_name(
    obs: StudyObservation,
    source_provisional_name: str,
) -> bool:
    """Return true when the observation still carries the source provisional name."""
    wanted = (source_provisional_name or "").strip().casefold()
    if not wanted:
        return False
    return (obs.provisional_species_name or "").strip().casefold() == wanted


def _observation_matches_source(
    obs: StudyObservation,
    *,
    source_taxon_id: int,
    source_provisional_name: str,
) -> bool:
    """Check the source-identity safeguard, by provisional name or taxon subtree.

    A provisional-name source (URL filtered by ``field:Provisional Species
    Name``) is matched against the observation's provisional name; otherwise the
    numeric source taxon subtree is used.
    """
    if (source_provisional_name or "").strip():
        return observation_matches_source_provisional_name(obs, source_provisional_name)
    return observation_matches_source_taxon(obs, source_taxon_id)


def candidate_source_still_matches(
    obs: StudyObservation,
    candidate: "BulkDisagreeCandidate",
) -> bool:
    """Re-check a candidate's source-identity safeguard against a fresh observation."""
    return _observation_matches_source(
        obs,
        source_taxon_id=candidate.source_taxon_id,
        source_provisional_name=candidate.source_provisional_name,
    )


def source_changed_message(candidate: "BulkDisagreeCandidate") -> str:
    """Message for when an observation no longer matches its candidate's source."""
    if candidate.source_provisional_name.strip():
        return "Observation no longer has the URL's Provisional Species Name after refresh."
    return "Observation no longer matches the URL source taxon after refresh."


def observation_matches_target_taxon(
    obs: StudyObservation,
    target_taxon_id: int,
) -> bool:
    target = int(target_taxon_id)
    if obs.community_taxon is not None:
        return obs.community_taxon.taxon_id == target
    return obs.taxon is not None and obs.taxon.taxon_id == target


def observation_finished_at_target(
    obs: StudyObservation,
    target_taxon_id: int,
) -> bool:
    """True when the observation is already research grade at the target taxon.

    Such an observation is "finished": adding another identification would not
    improve it. Observations that are not yet research grade are not finished
    even when they already carry the target name, because an added ID can help
    push them to research grade.
    """
    return (
        (obs.quality_grade or "").casefold() == "research"
        and observation_matches_target_taxon(obs, target_taxon_id)
    )


def current_user_has_taxon(
    obs: StudyObservation,
    login: str,
    target_taxon_id: int,
) -> bool:
    if not login:
        return False
    login_key = login.casefold()
    target = int(target_taxon_id)
    return any(
        ident.current
        and ident.user_login.casefold() == login_key
        and ident.taxon is not None
        and ident.taxon.taxon_id == target
        for ident in obs.all_identifications
    )


def collect_other_identifier_logins(
    obs: StudyObservation,
    login: str,
    target_taxon_id: int,
) -> List[str]:
    """Return logins of users whose current ID differs from the proposed taxon.

    Used to @-mention identifiers who proposed a different name. Excludes the
    posting user and anyone whose current identification already matches the
    proposed taxon. Order follows first appearance; each login appears once.
    """
    target = int(target_taxon_id)
    my_login = (login or "").casefold()
    users_not_to_tag = load_users_not_to_tag()
    seen: set[str] = set()
    logins: List[str] = []
    for ident in obs.all_identifications:
        if not ident.current:
            continue
        if ident.taxon is None or not ident.taxon.taxon_id:
            continue
        if ident.taxon.taxon_id == target:
            continue
        user_login = (ident.user_login or "").strip()
        if not user_login:
            continue
        key = user_login.casefold()
        if key == my_login or key in seen or key in users_not_to_tag:
            continue
        seen.add(key)
        logins.append(user_login)
    return logins


def current_user_has_needs_id_no_vote(
    obs: StudyObservation,
    login: str,
) -> bool:
    if not login:
        return False
    login_key = login.casefold()
    return any(
        vote.user_login.casefold() == login_key
        and vote.vote_scope == "needs_id"
        and vote.vote_flag is False
        for vote in obs.votes
    )


def plan_bulk_disagree_candidates(
    loader: StudyLoader,
    observation_query: ObservationURLQuery,
    login: str,
    api_token: str,
    *,
    source_taxon_id: int,
    source_taxon_name: str,
    target_taxon_id: int,
    target_taxon_name: str,
    target_taxon_rank: str = "",
    source_provisional_name: str = "",
    skip_with_dna_barcode_its: bool = True,
    only_with_dna_barcode_its: bool = False,
    require_source_taxon_match: bool = True,
    max_observations: int = 100,
    dqa_vote_planned: bool = False,
    explicit_disagreement: bool = True,
    per_page: int = 200,
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> BulkDisagreePlanResult:
    """Fetch URL results and build safely refreshed bulk-disagree candidates."""
    filters = LoadFilters(
        observation_query.display_url,
        observation_query=observation_query,
        apply_taxon_filter_to_observation_url=False,
    )
    candidates: List[BulkDisagreeCandidate] = []
    stats = BulkDisagreePlanStats()
    page = 1
    max_scan = max(1, int(max_observations))
    target_valid = int(target_taxon_id or 0) > 0
    skip_present_dna_barcode_its = (
        bool(skip_with_dna_barcode_its) and not bool(only_with_dna_barcode_its)
    )

    while stats.total_url_results_scanned < max_scan:
        if is_cancelled and is_cancelled():
            break
        observations, total = loader.load_page(
            filters,
            page=page,
            per_page=per_page,
            is_cancelled=is_cancelled,
            use_cache=False,
        )
        stats.total_api_results = total
        if not observations:
            break

        remaining = max_scan - stats.total_url_results_scanned
        page_observations = observations[:remaining]
        stats.total_url_results_scanned += len(page_observations)
        if progress:
            progress(stats.total_url_results_scanned, total)

        refresh_queue: list[StudyObservation] = []
        for obs in page_observations:
            if loader._db.is_bulk_disagree_skipped(obs.obs_id):
                stats.skipped_permanent += 1
                continue
            if not target_valid:
                stats.skipped_missing_invalid_data += 1
                continue
            refresh_queue.append(obs)

        if refresh_queue:
            refresh_failed_all = False
            try:
                refreshed = refresh_observations(
                    loader._client,
                    api_token,
                    [obs.obs_id for obs in refresh_queue],
                )
            except Exception as exc:
                if _is_auth_failure_error(exc):
                    raise
                stats.skipped_refresh_failure += len(refresh_queue)
                log.error(
                    "Bulk disagree preview refresh failed for page=%s ids=%s: %s",
                    page,
                    [obs.obs_id for obs in refresh_queue],
                    exc,
                )
                refreshed = []
                refresh_failed_all = True
            fresh_by_id = {obs.obs_id: obs for obs in refreshed}

            for obs in refresh_queue:
                if is_cancelled and is_cancelled():
                    break
                fresh = fresh_by_id.get(obs.obs_id)
                if fresh is None:
                    if not refresh_failed_all:
                        stats.skipped_refresh_failure += 1
                    continue
                candidate = _make_candidate(
                    fresh,
                    login,
                    source_taxon_id=source_taxon_id,
                    source_taxon_name=source_taxon_name,
                    target_taxon_id=target_taxon_id,
                    target_taxon_name=target_taxon_name,
                    target_taxon_rank=target_taxon_rank,
                    source_provisional_name=source_provisional_name,
                    dqa_vote_planned=dqa_vote_planned,
                    explicit_disagreement=explicit_disagreement,
                )
                if candidate is None:
                    stats.skipped_missing_invalid_data += 1
                    continue
                has_dna_barcode_its = bool(candidate.dna_barcode_its_value.strip())
                if only_with_dna_barcode_its and not has_dna_barcode_its:
                    stats.skipped_missing_dna_barcode_its += 1
                    continue
                if skip_present_dna_barcode_its and has_dna_barcode_its:
                    stats.skipped_dna_barcode_its += 1
                    continue
                # Already finished: research grade with the target as the
                # consensus taxon. Adding another ID would not improve it.
                if observation_finished_at_target(fresh, target_taxon_id):
                    stats.skipped_already_target += 1
                    continue
                if current_user_has_taxon(fresh, login, target_taxon_id):
                    stats.skipped_already_target += 1
                    continue
                if require_source_taxon_match and not _observation_matches_source(
                    fresh,
                    source_taxon_id=source_taxon_id,
                    source_provisional_name=source_provisional_name,
                ):
                    stats.skipped_source_no_match += 1
                    continue
                candidates.append(candidate)

        if total <= 0 or page * per_page >= total:
            break
        if stats.total_url_results_scanned >= max_scan:
            break
        page += 1

    stats.candidate_count = len(candidates)
    return BulkDisagreePlanResult(candidates=candidates, stats=stats)


def plan_propose_name_candidates(
    loader: StudyLoader,
    login: str,
    api_token: str,
    *,
    observation_ids: List[int],
    target_taxon_id: int,
    target_taxon_name: str,
    target_taxon_rank: str = "",
    per_page: int = 30,
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> BulkDisagreePlanResult:
    """Refresh an explicit list of observation IDs and build propose-name candidates.

    Unlike :func:`plan_bulk_disagree_candidates`, the observations come from a
    user-typed list of numbers rather than an observations URL, so there is no
    source taxon and no source-taxon safety check. Candidates carry
    ``source_taxon_id == 0``. The explicit-disagreement flag is decided per
    observation: it is set only when the proposed name differs from the
    observation's current taxon, so an identification that simply agrees with the
    current taxon is posted as a plain ID.
    """
    candidates: List[BulkDisagreeCandidate] = []
    stats = BulkDisagreePlanStats()

    # De-duplicate while preserving the order the user entered them.
    unique_ids: List[int] = []
    seen: set[int] = set()
    for raw_id in observation_ids:
        obs_id = int(raw_id)
        if obs_id > 0 and obs_id not in seen:
            seen.add(obs_id)
            unique_ids.append(obs_id)

    total = len(unique_ids)
    stats.total_api_results = total

    for start in range(0, total, max(1, int(per_page))):
        if is_cancelled and is_cancelled():
            break
        batch = unique_ids[start : start + max(1, int(per_page))]

        refresh_queue: List[int] = []
        for obs_id in batch:
            if loader._db.is_bulk_disagree_skipped(obs_id):
                stats.skipped_permanent += 1
                continue
            refresh_queue.append(obs_id)

        if refresh_queue:
            refresh_failed_all = False
            try:
                refreshed = refresh_observations(loader._client, api_token, refresh_queue)
            except Exception as exc:
                if _is_auth_failure_error(exc):
                    raise
                stats.skipped_refresh_failure += len(refresh_queue)
                log.error(
                    "Propose-name preview refresh failed for ids=%s: %s",
                    refresh_queue,
                    exc,
                )
                refreshed = []
                refresh_failed_all = True
            fresh_by_id = {obs.obs_id: obs for obs in refreshed}

            for obs_id in refresh_queue:
                if is_cancelled and is_cancelled():
                    break
                fresh = fresh_by_id.get(obs_id)
                if fresh is None:
                    if not refresh_failed_all:
                        stats.skipped_refresh_failure += 1
                    continue
                # Disagree only when the proposed name differs from the
                # observation's current taxon; an agreeing ID is posted plain.
                explicit_disagreement = bool(
                    fresh.taxon
                    and fresh.taxon.taxon_id
                    and fresh.taxon.taxon_id != int(target_taxon_id)
                )
                candidate = _make_candidate(
                    fresh,
                    login,
                    source_taxon_id=0,
                    source_taxon_name="",
                    target_taxon_id=target_taxon_id,
                    target_taxon_name=target_taxon_name,
                    target_taxon_rank=target_taxon_rank,
                    dqa_vote_planned=False,
                    explicit_disagreement=explicit_disagreement,
                )
                if candidate is None:
                    stats.skipped_missing_invalid_data += 1
                    continue
                candidate.other_identifier_logins = collect_other_identifier_logins(
                    fresh, login, target_taxon_id
                )
                # Drop observations that are already finished: research grade
                # with the target name.
                if observation_finished_at_target(fresh, target_taxon_id):
                    stats.skipped_already_target += 1
                    continue
                # You already have this exact current identification, so there is
                # nothing to add (a duplicate ID would not move it toward research
                # grade).
                if current_user_has_taxon(fresh, login, target_taxon_id):
                    stats.skipped_already_target += 1
                    continue
                candidates.append(candidate)

        stats.total_url_results_scanned = min(start + len(batch), total)
        if progress:
            progress(stats.total_url_results_scanned, total)

    stats.candidate_count = len(candidates)
    return BulkDisagreePlanResult(candidates=candidates, stats=stats)


def post_bulk_disagreement(
    client: INatClient,
    api_token: str,
    login: str,
    candidate: BulkDisagreeCandidate,
    *,
    body: str = "",
    skip_with_dna_barcode_its: bool = True,
    only_with_dna_barcode_its: bool = False,
    require_source_taxon_match: bool = True,
    dry_run: bool = False,
    dqa_posting_enabled: bool = DQA_POSTING_ENABLED,
    explicit_disagreement: Optional[bool] = None,
) -> BulkDisagreeResult:
    """Refresh, re-check safeguards, and post one corrective identification."""
    refreshed = refresh_observation(
        client,
        api_token,
        candidate.observation.obs_id,
    )
    if refreshed is None:
        return BulkDisagreeResult(
            "failed",
            "Could not refresh observation before posting; no identification was posted.",
            candidate=candidate,
        )

    if require_source_taxon_match and not candidate_source_still_matches(
        refreshed, candidate
    ):
        return BulkDisagreeResult(
            "changed",
            source_changed_message(candidate),
            candidate=candidate,
            refreshed_observation=refreshed,
        )

    dna_value = (refreshed.dna_barcode_its or "").strip()
    if only_with_dna_barcode_its and not dna_value:
        return BulkDisagreeResult(
            "skipped",
            "Skipped because DNA Barcode ITS is not present after refresh.",
            candidate=candidate,
            refreshed_observation=refreshed,
        )
    if skip_with_dna_barcode_its and not only_with_dna_barcode_its and dna_value:
        return BulkDisagreeResult(
            "skipped",
            "Skipped because DNA Barcode ITS is present after refresh.",
            candidate=candidate,
            refreshed_observation=refreshed,
        )

    if observation_finished_at_target(refreshed, candidate.target_taxon_id):
        return BulkDisagreeResult(
            "skipped",
            (
                f"Already research grade as {candidate.target_taxon_name}; "
                "no identification was added."
            ),
            candidate=candidate,
            refreshed_observation=refreshed,
        )

    if current_user_has_taxon(refreshed, login, candidate.target_taxon_id):
        return BulkDisagreeResult(
            "skipped",
            (
                f"Already currently identified as {candidate.target_taxon_name}; "
                "no duplicate disagreement was posted."
            ),
            candidate=candidate,
            refreshed_observation=refreshed,
        )

    if dry_run:
        return BulkDisagreeResult(
            "skipped",
            (
                "Dry run: would add identification "
                f"{candidate.target_taxon_name} to observation {candidate.observation.obs_id}."
            ),
            candidate=candidate,
            refreshed_observation=refreshed,
        )

    try:
        disagreement_flag = (
            candidate.explicit_disagreement
            if explicit_disagreement is None
            else bool(explicit_disagreement)
        )
        response = client.create_identification(
            api_token=api_token,
            observation_id=candidate.observation.obs_id,
            taxon_id=candidate.target_taxon_id,
            body=body,
            disagreement=True if disagreement_flag else None,
        )
    except INatAPIError as exc:
        if _is_ambiguous_write_error(exc):
            return BulkDisagreeResult(
                "ambiguous_write",
                (
                    str(exc)
                    + "\n\nManual review is required before any retry."
                ),
                candidate=candidate,
                refreshed_observation=refreshed,
            )
        raise

    try:
        after_ident = refresh_observation(
            client,
            api_token,
            candidate.observation.obs_id,
        )
        if after_ident is None:
            raise RuntimeError("iNaturalist returned no observation details.")
    except Exception as exc:
        return BulkDisagreeResult(
            "ambiguous_write",
            (
                "The identification POST returned, but the follow-up refresh failed. "
                f"Observation {candidate.observation.obs_id} requires manual review "
                f"before any retry. Refresh error: {exc}"
            ),
            candidate=candidate,
            refreshed_observation=refreshed,
            raw_response=response,
        )

    if candidate.dqa_vote_planned:
        if not dqa_posting_enabled:
            return BulkDisagreeResult(
                "posted_id_dqa_not_attempted",
                (
                    f"Added identification: {candidate.target_taxon_name}. DQA vote was not attempted. "
                    + DQA_DISABLED_MESSAGE
                ),
                candidate=candidate,
                refreshed_observation=after_ident,
                raw_response=response,
            )
        if not observation_matches_target_taxon(after_ident, candidate.target_taxon_id):
            return BulkDisagreeResult(
                "posted_id_dqa_skipped",
                (
                    f"Added identification: {candidate.target_taxon_name}. "
                    "DQA vote was skipped because the refreshed observation's community "
                    "taxon, or current taxon when no community taxon exists, does not "
                    "match the target taxon."
                ),
                candidate=candidate,
                refreshed_observation=after_ident,
                raw_response=response,
            )
        if current_user_has_needs_id_no_vote(after_ident, login):
            return BulkDisagreeResult(
                "posted_id_dqa_skipped",
                (
                    f"Added identification: {candidate.target_taxon_name}. "
                    "DQA vote was skipped because your existing "
                    "'ID is already as good as it can be' vote is already present."
                ),
                candidate=candidate,
                refreshed_observation=after_ident,
                raw_response=response,
            )
        try:
            dqa_response = client.vote_id_is_as_good_as_can_be(
                api_token=api_token,
                observation_id=candidate.observation.obs_id,
            )
        except INatAPIError as exc:
            log.error(
                "Bulk disagree DQA vote failed obs=%s status=%s endpoint=%s body=%s message=%s",
                candidate.observation.obs_id,
                exc.status_code,
                exc.endpoint,
                exc.response_body,
                exc,
            )
            if _is_ambiguous_write_error(exc):
                return BulkDisagreeResult(
                    "ambiguous_write",
                    (
                        "The identification was posted, but the DQA vote may have reached "
                        "iNaturalist and the result is unknown. Manual review is required "
                        "before any retry."
                    ),
                    candidate=candidate,
                    refreshed_observation=after_ident,
                    raw_response=response,
                )
            return BulkDisagreeResult(
                "posted_id_dqa_failed",
                (
                    f"Added identification: {candidate.target_taxon_name}. "
                    f"DQA vote failed: {exc}"
                ),
                candidate=candidate,
                refreshed_observation=after_ident,
                raw_response=response,
            )
        try:
            after_dqa = refresh_observation(
                client,
                api_token,
                candidate.observation.obs_id,
            )
            if after_dqa is None:
                raise RuntimeError("iNaturalist returned no observation details.")
        except Exception as exc:
            return BulkDisagreeResult(
                "ambiguous_write",
                (
                    "The DQA vote POST returned, but the follow-up refresh failed. "
                    f"Observation {candidate.observation.obs_id} requires manual review "
                    f"before any retry. Refresh error: {exc}"
                ),
                candidate=candidate,
                refreshed_observation=after_ident,
                raw_response=dqa_response,
            )
        return BulkDisagreeResult(
            "posted_id_and_dqa",
            f"Added identification and DQA vote: {candidate.target_taxon_name}.",
            candidate=candidate,
            refreshed_observation=after_dqa,
            raw_response=dqa_response,
        )

    return BulkDisagreeResult(
        "posted_id",
        f"Added identification: {candidate.target_taxon_name}.",
        candidate=candidate,
        refreshed_observation=after_ident,
        raw_response=response,
    )


def post_alternate_identification(
    client: INatClient,
    api_token: str,
    login: str,
    candidate: BulkDisagreeCandidate,
    *,
    target_taxon_id: int,
    target_taxon_name: str,
    body: str = "",
    disagreement: bool = False,
    require_source_taxon_match: bool = True,
) -> BulkDisagreeResult:
    """Post an immediate alternate ID from the photo browser with core safeguards."""
    refreshed = refresh_observation(
        client,
        api_token,
        candidate.observation.obs_id,
    )
    if refreshed is None:
        return BulkDisagreeResult(
            "failed",
            "Could not refresh observation before posting alternate ID; no identification was posted.",
            candidate=candidate,
        )

    if require_source_taxon_match and not candidate_source_still_matches(
        refreshed, candidate
    ):
        return BulkDisagreeResult(
            "changed",
            source_changed_message(candidate),
            candidate=candidate,
            refreshed_observation=refreshed,
        )

    if current_user_has_taxon(refreshed, login, int(target_taxon_id)):
        return BulkDisagreeResult(
            "skipped",
            (
                f"Already currently identified as {target_taxon_name}; "
                "no duplicate alternate identification was posted."
            ),
            candidate=candidate,
            refreshed_observation=refreshed,
        )

    try:
        response = client.create_identification(
            api_token=api_token,
            observation_id=candidate.observation.obs_id,
            taxon_id=int(target_taxon_id),
            body=body,
            disagreement=bool(disagreement),
        )
    except INatAPIError as exc:
        if _is_ambiguous_write_error(exc):
            return BulkDisagreeResult(
                "ambiguous_write",
                str(exc) + "\n\nManual review is required before any retry.",
                candidate=candidate,
                refreshed_observation=refreshed,
            )
        raise

    try:
        after_ident = refresh_observation(
            client,
            api_token,
            candidate.observation.obs_id,
        )
        if after_ident is None:
            raise RuntimeError("iNaturalist returned no observation details.")
    except Exception as exc:
        return BulkDisagreeResult(
            "ambiguous_write",
            (
                "The alternate identification POST returned, but the follow-up refresh failed. "
                f"Observation {candidate.observation.obs_id} requires manual review "
                f"before any retry. Refresh error: {exc}"
            ),
            candidate=candidate,
            refreshed_observation=refreshed,
            raw_response=response,
        )

    return BulkDisagreeResult(
        "posted_id",
        f"Added alternate identification: {target_taxon_name}.",
        candidate=candidate,
        refreshed_observation=after_ident,
        raw_response=response,
    )


def _make_candidate(
    obs: StudyObservation,
    login: str,
    *,
    source_taxon_id: int,
    source_taxon_name: str,
    target_taxon_id: int,
    target_taxon_name: str,
    target_taxon_rank: str = "",
    source_provisional_name: str = "",
    dqa_vote_planned: bool,
    explicit_disagreement: bool = True,
) -> Optional[BulkDisagreeCandidate]:
    if not target_taxon_id or not target_taxon_name:
        return None
    user_ident = current_user_identification(obs, login)
    dna_value = (obs.dna_barcode_its or "").strip()
    return BulkDisagreeCandidate(
        observation=obs,
        source_taxon_id=int(source_taxon_id),
        source_taxon_name=source_taxon_name,
        source_provisional_name=source_provisional_name,
        target_taxon_id=int(target_taxon_id),
        target_taxon_name=target_taxon_name,
        target_taxon_rank=target_taxon_rank,
        current_observation_taxon_name=obs.taxon.name if obs.taxon else "",
        community_taxon_name=obs.community_taxon.name if obs.community_taxon else "",
        has_dna_barcode_its=bool(dna_value),
        dna_barcode_its_value=dna_value,
        user_current_taxon=user_ident.taxon.name if user_ident and user_ident.taxon else "",
        dqa_vote_planned=bool(dqa_vote_planned),
        explicit_disagreement=bool(explicit_disagreement),
    )


def _is_ambiguous_write_error(exc: INatAPIError) -> bool:
    text = str(exc).casefold()
    return "may have reached inaturalist" in text or "result is unknown" in text


def _is_auth_failure_error(exc: Exception) -> bool:
    if isinstance(exc, INatAPIError) and exc.status_code == 401:
        return True
    text = str(exc).casefold()
    return (
        "need to sign in" in text
        or "missing inaturalist api token" in text
        or "http status: 401" in text
        or "http 401" in text
    )
