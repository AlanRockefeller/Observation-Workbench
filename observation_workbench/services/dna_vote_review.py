"""Find DNA-barcoded observations whose identification votes deserve a second look.

The premise is deliberately simple, because iNaturalist does not expose when an
observation field was added: if an observation carries a qualifying ``DNA
Barcode ITS`` sequence *and* its current identifications disagree, the
disagreement was probably driven by that sequence. Nothing here writes; the
whole module is unauthenticated read plus local classification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Optional, Sequence

from observation_workbench.api.client import INatClient
from observation_workbench.dna_linking.discovery import (
    contains_qualifying_dna,
    extract_observation_field_rows,
)
from observation_workbench.dna_linking.types import DNA_FIELD_NAME, FUNGI_TAXON_ID

log = logging.getLogger(__name__)

PAGE_SIZE = 200
# The observations index refuses to page past 10k results.
MAX_INDEX_RESULTS = 10_000
HYDRATION_BATCH = 30

# Finding kinds, ordered by how strongly they suggest a vote is worth revisiting.
KIND_CONTESTED = "contested"
KIND_OUTVOTED = "outvoted"
KIND_REFINE = "refine"
KIND_UNSETTLED = "unsettled"
KIND_UNDERSUPPORTED = "undersupported"

KIND_LABELS = {
    KIND_OUTVOTED: "Outvoted",
    KIND_CONTESTED: "Contested",
    KIND_REFINE: "Others are more specific",
    KIND_UNSETTLED: "Identifiers disagree",
    KIND_UNDERSUPPORTED: "Consensus needs one more vote",
}


class VoteReviewCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class VoteReviewConfig:
    """One scan request. ``login`` empty means "anyone"."""

    login: str = ""
    # Numeric account id for ``login``, resolved before the scan starts. The
    # index filter is keyed by id; the login string alone is not a safe filter
    # value there.
    login_user_id: Optional[int] = None
    source_url: str = ""
    source_params: tuple[tuple[str, str], ...] = ()
    max_observations: int = 600
    include_refinements: bool = True
    verify_sequence: bool = True


@dataclass(frozen=True)
class IdentificationView:
    identification_id: int
    login: str
    taxon_id: int
    taxon_name: str
    taxon_rank: str
    rank_level: Optional[float]
    ancestor_ids: tuple[int, ...]
    category: str
    created_at: str
    disagreement: bool

    @property
    def lineage(self) -> frozenset[int]:
        return frozenset({self.taxon_id, *self.ancestor_ids})

    @property
    def display(self) -> str:
        return self.taxon_name or f"taxon {self.taxon_id}"


@dataclass(frozen=True)
class VoteReviewFinding:
    observation_id: int
    observation_uuid: str
    observer: str
    quality_grade: str
    observed_on: str
    place_guess: str
    observation_taxon_id: int
    observation_taxon_name: str
    community_taxon_id: Optional[int]
    community_taxon_name: str
    subject: Optional[IdentificationView]
    opposing: tuple[IdentificationView, ...]
    finer: tuple[IdentificationView, ...]
    agreeing: tuple[IdentificationView, ...]
    kind: str
    reason: str
    score: int
    sequence_verified: bool = False
    sequence_length: int = 0

    @property
    def url(self) -> str:
        return f"https://www.inaturalist.org/observations/{self.observation_id}"

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


@dataclass(frozen=True)
class VoteReviewProgress:
    message: str
    observations_scanned: int
    observations_total: int
    findings: int
    calls_made: int


@dataclass(frozen=True)
class VoteReviewResult:
    findings: tuple[VoteReviewFinding, ...]
    observations_scanned: int
    observations_available: int
    truncated: bool
    dropped_unverified: int
    login: str
    counts: dict[str, int] = field(default_factory=dict)


# ----------------------------------------------------------------------
# Taxon relationships
# ----------------------------------------------------------------------


def relation(a: IdentificationView, b: IdentificationView) -> str:
    """Describe ``a``'s taxon relative to ``b``'s.

    ``ancestor`` means a is coarser and contains b; ``descendant`` means a is
    more specific than b; ``disjoint`` means neither contains the other, which
    is what an actual identification conflict looks like.
    """
    if a.taxon_id == b.taxon_id:
        return "same"
    if a.taxon_id in b.lineage:
        return "ancestor"
    if b.taxon_id in a.lineage:
        return "descendant"
    return "disjoint"


def _rank_key(view: IdentificationView) -> float:
    # rank_level counts down towards species (10); missing ranks sort coarsest.
    return view.rank_level if view.rank_level is not None else 100.0


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------


def parse_identification(raw: Mapping[str, Any]) -> Optional[IdentificationView]:
    """Build a view of one identification, or None when it cannot be trusted."""
    if not isinstance(raw, Mapping):
        return None
    if not raw.get("current") or raw.get("hidden") or raw.get("spam"):
        return None
    taxon = raw.get("taxon")
    if not isinstance(taxon, Mapping):
        return None
    try:
        taxon_id = int(taxon.get("id") or 0)
    except (TypeError, ValueError):
        return None
    if not taxon_id:
        return None
    user = raw.get("user")
    login = str(user.get("login") or "") if isinstance(user, Mapping) else ""
    if not login:
        return None
    ancestors = []
    for value in taxon.get("ancestor_ids") or []:
        try:
            ancestors.append(int(value))
        except (TypeError, ValueError):
            continue
    rank_level_raw = taxon.get("rank_level")
    try:
        rank_level = float(rank_level_raw) if rank_level_raw is not None else None
    except (TypeError, ValueError):
        rank_level = None
    try:
        identification_id = int(raw.get("id") or 0)
    except (TypeError, ValueError):
        identification_id = 0
    return IdentificationView(
        identification_id=identification_id,
        login=login,
        taxon_id=taxon_id,
        taxon_name=str(taxon.get("name") or ""),
        taxon_rank=str(taxon.get("rank") or ""),
        rank_level=rank_level,
        ancestor_ids=tuple(ancestors),
        category=str(raw.get("category") or ""),
        created_at=str(raw.get("created_at") or ""),
        disagreement=bool(raw.get("disagreement")),
    )


def current_identifications(raw: Mapping[str, Any]) -> list[IdentificationView]:
    rows = raw.get("identifications") or []
    views = []
    for row in rows if isinstance(rows, list) else []:
        view = parse_identification(row)
        if view is not None:
            views.append(view)
    return views


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------


def classify(
    raw: Mapping[str, Any], login: str, include_refinements: bool
) -> Optional[VoteReviewFinding]:
    """Return a finding when this observation's votes look worth revisiting."""
    views = current_identifications(raw)
    if not views:
        return None
    community_taxon_id = _optional_int(raw.get("community_taxon_id"))
    raw_taxon = raw.get("taxon")
    observation_taxon = raw_taxon if isinstance(raw_taxon, Mapping) else {}
    observation_taxon_id = _optional_int(observation_taxon.get("id")) or 0
    community_name = _taxon_name(community_taxon_id, views, raw)

    if login:
        subject, kind, opposing, finer, agreeing, reason, score = _classify_for_user(
            views, login, community_taxon_id, include_refinements
        )
    else:
        subject, kind, opposing, finer, agreeing, reason, score = _classify_for_anyone(
            views, community_taxon_id, str(raw.get("quality_grade") or "")
        )
    if kind is None:
        return None

    user_obj = raw.get("user")
    return VoteReviewFinding(
        observation_id=_optional_int(raw.get("id")) or 0,
        observation_uuid=str(raw.get("uuid") or ""),
        observer=(
            str(user_obj.get("login") or "") if isinstance(user_obj, Mapping) else ""
        ),
        quality_grade=str(raw.get("quality_grade") or ""),
        observed_on=str(raw.get("observed_on") or ""),
        place_guess=str(raw.get("place_guess") or ""),
        observation_taxon_id=observation_taxon_id,
        observation_taxon_name=str(observation_taxon.get("name") or ""),
        community_taxon_id=community_taxon_id,
        community_taxon_name=community_name,
        subject=subject,
        opposing=tuple(opposing),
        finer=tuple(finer),
        agreeing=tuple(agreeing),
        kind=kind,
        reason=reason,
        score=score,
    )


def _classify_for_user(
    views: Sequence[IdentificationView],
    login: str,
    community_taxon_id: Optional[int],
    include_refinements: bool,
) -> tuple[
    Optional[IdentificationView], Optional[str],
    list[IdentificationView], list[IdentificationView], list[IdentificationView],
    str, int,
]:
    target = login.casefold()
    mine = [view for view in views if view.login.casefold() == target]
    if not mine:
        # The user withdrew or never held a current identification here.
        return None, None, [], [], [], "", 0
    # An account can only hold one current identification; take the newest if
    # the API ever returns more than one.
    subject = max(mine, key=lambda view: (view.created_at, view.identification_id))
    others = [view for view in views if view.login.casefold() != target]
    if not others:
        return None, None, [], [], [], "", 0

    opposing = [view for view in others if relation(subject, view) == "disjoint"]
    finer = [view for view in others if relation(subject, view) == "ancestor"]
    agreeing = [view for view in others if relation(subject, view) == "same"]

    if opposing:
        # "Outvoted" means the community taxon itself left this branch, not
        # merely that it retreated to a shared ancestor of both proposals.
        community_disjoint = _community_is_disjoint(
            subject, views, community_taxon_id
        )
        names = _name_list(opposing)
        if community_disjoint:
            kind = KIND_OUTVOTED
            reason = (
                f"The community taxon has moved away from your {subject.display}; "
                f"{names} identified it as something else."
            )
            score = 80
        else:
            kind = KIND_CONTESTED
            verb = "disagrees" if len(opposing) == 1 else "disagree"
            reason = f"{names} {verb} with your {subject.display}."
            score = 65
        score += min(15, 5 * len(opposing))
        score -= min(10, 3 * len(agreeing))
        return subject, kind, opposing, finer, agreeing, reason, max(1, min(100, score))

    if finer and include_refinements:
        # A broad subject can contain competing descendant branches; those
        # votes do not establish a shared refinement.
        if any(
            relation(left, right) == "disjoint"
            for index, left in enumerate(finer)
            for right in finer[index + 1 :]
        ):
            reason = (
                f"Others proposed conflicting refinements of your {subject.display}; "
                "there is no agreed narrower identification."
            )
            return subject, KIND_CONTESTED, [], finer, agreeing, reason, 65
        deepest = min(finer, key=_rank_key)
        supporters = [view for view in finer if view.taxon_id == deepest.taxon_id]
        reason = (
            f"You stopped at {subject.display}; "
            f"{_name_list(supporters)} narrowed it to {deepest.display}."
        )
        score = 40 + min(20, 6 * len(supporters))
        return subject, KIND_REFINE, [], finer, agreeing, reason, min(100, score)

    return None, None, [], [], [], "", 0


def _community_is_disjoint(
    subject: IdentificationView,
    views: Sequence[IdentificationView],
    community_taxon_id: Optional[int],
) -> bool:
    """True when the community taxon lies outside the subject's branch.

    A community taxon that is an ancestor of the subject (the usual "the
    consensus fell back to the genus" case) is not disjoint; it means the
    dispute is unresolved rather than decided against the subject.
    """
    if community_taxon_id is None or community_taxon_id in subject.lineage:
        return False
    for view in views:
        if view.taxon_id == community_taxon_id and subject.taxon_id in view.lineage:
            return False
    return True


def _classify_for_anyone(
    views: Sequence[IdentificationView],
    community_taxon_id: Optional[int],
    quality_grade: str,
) -> tuple[
    Optional[IdentificationView], Optional[str],
    list[IdentificationView], list[IdentificationView], list[IdentificationView],
    str, int,
]:
    disjoint_pairs = [
        (left, right)
        for index, left in enumerate(views)
        for right in views[index + 1 :]
        if relation(left, right) == "disjoint"
    ]
    if disjoint_pairs:
        involved = {
            view.identification_id: view
            for pair in disjoint_pairs
            for view in pair
        }
        opposing = sorted(involved.values(), key=lambda view: view.created_at)
        reason = (
            "Identifiers disagree: "
            + " vs ".join(sorted({view.display for view in opposing}))
            + ". A vote could settle the consensus."
        )
        score = min(100, 60 + 5 * len(opposing))
        return None, KIND_UNSETTLED, opposing, [], [], reason, score

    if quality_grade.casefold() == "research":
        return None, None, [], [], [], "", 0
    deepest = min(views, key=_rank_key)
    supporters = [view for view in views if view.taxon_id == deepest.taxon_id]
    if (
        community_taxon_id is None
        or community_taxon_id == deepest.taxon_id
        or community_taxon_id not in deepest.lineage
    ):
        return None, None, [], [], [], "", 0
    reason = (
        f"Only {_login_list(supporters)} proposed {deepest.display}; the community "
        "taxon is still coarser, so one agreeing vote would sharpen it."
    )
    score = 55 - min(20, 5 * (len(views) - len(supporters)))
    return None, KIND_UNDERSUPPORTED, [], list(views), supporters, reason, max(1, score)


def _name_list(views: Sequence[IdentificationView]) -> str:
    entries = [f"{view.login} ({view.display})" for view in views[:3]]
    if len(views) > 3:
        entries.append(f"and {len(views) - 3} more")
    return ", ".join(entries) if entries else "another identifier"


def _login_list(views: Sequence[IdentificationView]) -> str:
    logins = [view.login for view in views[:3]]
    if len(views) > 3:
        logins.append(f"and {len(views) - 3} more")
    return ", ".join(logins) if logins else "one identifier"


def _taxon_name(
    taxon_id: Optional[int],
    views: Sequence[IdentificationView],
    raw: Mapping[str, Any],
) -> str:
    if taxon_id is None:
        return ""
    for view in views:
        if view.taxon_id == taxon_id:
            return view.taxon_name
    taxon = raw.get("taxon")
    if isinstance(taxon, Mapping) and _optional_int(taxon.get("id")) == taxon_id:
        return str(taxon.get("name") or "")
    return f"taxon {taxon_id}"


def qualifying_sequence_length(raw: Mapping[str, Any], field_id: int) -> int:
    """Length of the longest qualifying ITS sequence on this observation row.

    Returns 0 when the field is absent or holds nothing that looks like a
    sequence. The observations index carries complete ``ofvs`` values, so this
    normally answers the question without spending a detail read. Only the
    length is ever returned: the sequence itself stays out of state, logs and
    exception text by design.
    """
    best = 0
    for field_row in extract_observation_field_rows(raw, field_id):
        value = field_row.get("value")
        if contains_qualifying_dna(value):
            best = max(best, len(str(value or "").strip()))
    return best


def _optional_int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------


class DNAVoteReview:
    """Unauthenticated scan for DNA-barcoded observations with stale votes."""

    def __init__(self, client: INatClient) -> None:
        self.client = client

    def build_query(self, config: VoteReviewConfig) -> list[tuple[str, Any]]:
        params: list[tuple[str, Any]] = [
            (key, value)
            for key, value in config.source_params
            if key.casefold() not in {"page", "per_page", "order", "order_by"}
            and not key.casefold().startswith("field:")
            and key.casefold() != "ident_user_id"
        ]
        if not any(key.casefold() == "taxon_id" for key, _ in params):
            params.append(("taxon_id", str(FUNGI_TAXON_ID)))
        # The positive observation-field filter is the whole point of the scan;
        # v1 honours `field:<name>` with an empty value as "has any value".
        params.append((f"field:{DNA_FIELD_NAME}", ""))
        if config.login:
            if not config.login_user_id:
                raise ValueError(
                    f"Could not resolve an account id for {config.login!r}."
                )
            params.append(("ident_user_id", str(config.login_user_id)))
        params.extend([("order_by", "id"), ("order", "desc")])
        return params

    def scan(
        self,
        config: VoteReviewConfig,
        *,
        is_cancelled: Callable[[], bool] = lambda: False,
        progress: Callable[[VoteReviewProgress], None] = lambda _p: None,
    ) -> VoteReviewResult:
        params = self.build_query(config)
        calls_at_start = self.client.call_count
        limit = max(1, min(int(config.max_observations), MAX_INDEX_RESULTS))
        findings: list[VoteReviewFinding] = []
        scanned = 0
        available = 0
        page = 1

        # The server filter only proves the field exists with *some* value, so
        # the "is this really a sequence" test still has to run locally. It runs
        # against the index row, which already carries the complete value.
        field_id = 0
        if config.verify_sequence:
            field_id = int(self.client.find_observation_field_id(DNA_FIELD_NAME) or 0)
            if not field_id:
                log.warning(
                    "Could not resolve the %r field; sequence lengths are unavailable "
                    "and no finding will be dropped for lacking one.",
                    DNA_FIELD_NAME,
                )

        while scanned < limit:
            if is_cancelled():
                raise VoteReviewCancelled()
            raw = self.client.get_observations(params, page=page, per_page=PAGE_SIZE)
            results = [
                row for row in raw.get("results") or [] if isinstance(row, Mapping)
            ]
            available = max(
                available, _optional_int(raw.get("total_results")) or len(results)
            )
            if not results:
                break
            for row in results:
                if scanned >= limit:
                    break
                if is_cancelled():
                    raise VoteReviewCancelled()
                scanned += 1
                finding = classify(row, config.login, config.include_refinements)
                if finding is None or not finding.observation_id:
                    continue
                length = qualifying_sequence_length(row, field_id) if field_id else 0
                findings.append(replace(
                    finding, sequence_verified=length > 0, sequence_length=length
                ))
            progress(VoteReviewProgress(
                "Reading DNA-barcoded observations",
                scanned, min(limit, available or limit), len(findings),
                self.client.call_count - calls_at_start,
            ))
            if len(results) < PAGE_SIZE or page * PAGE_SIZE >= available:
                break
            page += 1

        dropped = 0
        if field_id and any(not item.sequence_verified for item in findings):
            findings, dropped = self._recheck_unverified(
                findings, field_id, is_cancelled=is_cancelled, progress=progress,
                scanned=scanned, limit=min(limit, available or limit),
                calls_at_start=calls_at_start,
            )

        findings.sort(key=lambda item: (-item.score, -item.observation_id))
        counts: dict[str, int] = {}
        for item in findings:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        progress(VoteReviewProgress(
            "Scan complete", scanned, min(limit, available or limit),
            len(findings), self.client.call_count - calls_at_start,
        ))
        return VoteReviewResult(
            findings=tuple(findings),
            observations_scanned=scanned,
            observations_available=available,
            truncated=scanned < available,
            dropped_unverified=dropped,
            login=config.login,
            counts=counts,
        )

    def _recheck_unverified(
        self,
        findings: Sequence[VoteReviewFinding],
        field_id: int,
        *,
        is_cancelled: Callable[[], bool],
        progress: Callable[[VoteReviewProgress], None],
        scanned: int,
        limit: int,
        calls_at_start: int,
    ) -> tuple[list[VoteReviewFinding], int]:
        """Re-read only the findings whose index row showed no usable sequence.

        The index normally carries complete field values, so this is expected to
        touch nothing. It exists because a value can legitimately be missing
        from a search row (a field hidden for privacy, say), and dropping such a
        finding on the strength of the search page alone would be wrong.
        """
        pending = [item for item in findings if not item.sequence_verified]
        ids = [item.observation_id for item in pending]
        lengths: dict[int, int] = {}
        for start in range(0, len(ids), HYDRATION_BATCH):
            if is_cancelled():
                raise VoteReviewCancelled()
            response = self.client.get_observations_by_ids(
                ids[start : start + HYDRATION_BATCH]
            )
            for row in response.get("results") or []:
                if not isinstance(row, Mapping):
                    continue
                observation_id = _optional_int(row.get("id"))
                if observation_id is not None:
                    lengths[observation_id] = qualifying_sequence_length(row, field_id)
            progress(VoteReviewProgress(
                "Re-reading observations whose sequence was not in the search page",
                scanned, limit, len(findings),
                self.client.call_count - calls_at_start,
            ))

        verified: list[VoteReviewFinding] = []
        dropped = 0
        for item in findings:
            if item.sequence_verified:
                verified.append(item)
                continue
            length = lengths.get(item.observation_id, 0)
            if length <= 0:
                dropped += 1
                continue
            verified.append(
                replace(item, sequence_verified=True, sequence_length=length)
            )
        return verified, dropped
