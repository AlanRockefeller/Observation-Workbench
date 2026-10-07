"""Planning helpers for applying MycoMap autovalidator identifications.

MycoMap's sequence autovalidator posts a comment from a single account and
writes the identification it inferred into an observation field (a Provisional
Species Name, or a Species Name Override when the sequence matched a published
name). It never posts an identification itself, so the community consensus often
lags behind the autovalidated name -- sometimes because nobody has agreed yet,
sometimes because the provisional name does not exist on iNaturalist at all.

That comment takes two forms. Usually it is a sentence carrying a fixed marker
phrase; sometimes the account posts the inferred name on its own, with no
wording around it at all (observation 330558189, whose only comment reads
``Amanita sp. 'PK05'``). Both count as autovalidation here.

This module finds those lagging observations and turns them into
:class:`~observation_workbench.services.bulk_disagree.BulkDisagreeCandidate`
objects, so the existing supervised preview / photo-browser / posting pipeline
can review and post them. Each candidate carries its own target taxon, because
every observation has a different autovalidated name.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from observation_workbench.api.client import INatClient
from observation_workbench.api.observation_url import (
    ObservationURLQuery,
    QueryParams,
    canonical_source_key,
)
from observation_workbench.api.parsers import parse_taxon
from observation_workbench.models import StudyObservation, StudyTaxon
from observation_workbench.services.bulk_disagree import (
    BulkDisagreeCandidate,
    BulkDisagreePlanResult,
    BulkDisagreePlanStats,
    BulkDisagreeResult,
    collect_other_identifier_logins,
    current_user_has_taxon,
    is_auth_failure_error,
    observation_finished_at_target,
    post_bulk_disagreement,
    taxon_is_strict_ancestor,
)
from observation_workbench.services.identification_actions import (
    current_user_identification,
    refresh_observation,
    refresh_observations,
)
from observation_workbench.services.study_loader import LoadFilters, StudyLoader
from observation_workbench.storage.cache_db import CacheDB

log = logging.getLogger(__name__)

# The MycoMap autovalidator posts from this single account.
AUTOVALIDATOR_LOGIN = "stevilkinevil"

# Stable phrase in every autovalidation comment. Matched case-insensitively;
# the wording around it has varied between autovalidation runs.
AUTOVALIDATION_COMMENT_MARKER = "automated based on matching sequence reference data"

# The autovalidator's other comment form is the bare inferred name and nothing
# else. Recognizing it needs a shape test, since any comment from the account
# would otherwise qualify: a taxon name is one short line, starts capitalized,
# and carries none of the punctuation that marks a sentence, a list, or a link.
_BARE_NAME_MAX_CHARS = 80
_BARE_NAME_MAX_WORDS = 6
_BARE_NAME_DISALLOWED_RE = re.compile(r"[,;:!?/@|]|https?://", re.IGNORECASE)

# Observation field the autovalidator sets on every record it processes, and
# which the observer flips to "Yes" to contest the automated call. Filtering the
# search on this field is what makes autovalidated observations discoverable at
# all: the iNaturalist observations API cannot filter by commenter.
ID_UPDATE_NEEDED_FIELD_NAME = "ID Update Needed"
ID_UPDATE_NEEDED_OK_VALUE = "No"

DNA_BARCODE_ITS_FIELD_NAME = "DNA Barcode ITS"

_RESERVED_PARAM_KEYS = {
    f"field:{ID_UPDATE_NEEDED_FIELD_NAME}".casefold(),
    f"field:{DNA_BARCODE_ITS_FIELD_NAME}".casefold(),
}

BASE_QUERY_URL = (
    "https://www.inaturalist.org/observations"
    "?field:ID%20Update%20Needed=No&field:DNA%20Barcode%20ITS"
)

# Rank abbreviations that appear in observation field values but not in the
# matching iNaturalist taxon name (e.g. "Lactarius luculentus var. laetus" is
# "Lactarius luculentus laetus" on iNaturalist). Dropped from both sides before
# comparing, so the same normalization always applies symmetrically.
_RANK_TOKENS = {
    "sp.",
    "spp.",
    "var.",
    "v.",
    "f.",
    "form",
    "subsp.",
    "ssp.",
    "subg.",
    "sect.",
    "aff.",
    "cf.",
}

# How far the current observation taxon sits from the autovalidated name. A
# sequence that matched something in another family is the shape a mis-applied
# barcode takes, so the preview sorts and colours by these.
TAXON_CONFLICT_FAMILY = "family"
TAXON_CONFLICT_GENUS = "genus"

# Ancestor ranks are read in batches this size so a cancel during the lineage
# pass is noticed between requests rather than after all of them.
_TAXON_FETCH_CHUNK = 30


@dataclass
class AutovalidatedPlanStats(BulkDisagreePlanStats):
    """Bulk-disagree plan stats plus the skip reasons specific to this workflow."""

    scan_scope: str = ""
    pending_rechecked: int = 0
    scan_exhausted: bool = False
    skipped_not_autovalidated: int = 0
    skipped_no_suggested_name: int = 0
    skipped_consensus_already_matches: int = 0
    skipped_unresolved_name: int = 0
    skipped_identified_after_autovalidation: int = 0
    # (observation_id, suggested name) pairs for names with no iNaturalist taxon.
    # Populated only when the caller asked to report them instead of skipping
    # them quietly; these are the observations whose name still has to be created.
    unresolved_names: List[Tuple[int, str]] = field(default_factory=list)

    def one_line_summary(self) -> str:
        return (
            f"Scanned {self.total_url_results_scanned} autovalidated observation(s); "
            f"{self.candidate_count} candidate(s). "
            f"Skipped: {self.skipped_consensus_already_matches} consensus already matches, "
            f"{self.skipped_unresolved_name} name not on iNaturalist, "
            f"{self.skipped_already_target} already your ID, "
            f"{self.skipped_identified_after_autovalidation} your ID after autovalidation, "
            f"{self.skipped_not_autovalidated} not autovalidated, "
            f"{self.skipped_no_suggested_name} no autovalidated name, "
            f"{self.skipped_missing_dna_barcode_its} missing DNA Barcode ITS, "
            f"{self.skipped_permanent} permanent skip, "
            f"{self.skipped_refresh_failure} refresh failure."
        )


def normalize_taxon_name(value: str) -> str:
    """Normalize a taxon name for exact comparison across the two sources.

    Case, quote style, rank abbreviations, and whitespace all vary between an
    observation field value and the iNaturalist taxon name for the same taxon.
    Nothing else is touched: this is deliberately not a fuzzy match, because a
    wrong match here would post a wrong identification.
    """
    text = (value or "").strip()
    if not text:
        return ""
    # Every quote style collapses to a straight single quote. Provisional epithets
    # are quoted inconsistently across the two sources -- the same taxon appears as
    # Lactifluus "subvellereus-IN02" in an observation field and as
    # Lactifluus sp. 'subvellereus-IN02' on iNaturalist -- and no two distinct taxa
    # differ only by which quote character surrounds the epithet.
    for quote in ("’", "‘", '"', "“", "”"):
        text = text.replace(quote, "'")
    tokens = [
        token
        for token in re.split(r"\s+", text.casefold())
        if token and token not in _RANK_TOKENS
    ]
    return " ".join(tokens)


def _field_suggested_name(obs: StudyObservation) -> str:
    """Return the name the autovalidator wrote into an observation field.

    Provisional Species Name wins over Species Name Override: when both are set
    they disagree by design, the override holding the closest published name and
    the provisional name holding the sequence-based name the autovalidator
    actually inferred.
    """
    provisional = (obs.provisional_species_name or "").strip()
    if provisional:
        return provisional
    return (obs.species_name_override or "").strip()


def _bare_taxon_name(body: str) -> str:
    """Return ``body`` when it is nothing but a taxon name, else an empty string."""
    text = (body or "").strip()
    if not text or "\n" in text or len(text) > _BARE_NAME_MAX_CHARS:
        return ""
    if not text[:1].isupper():
        return ""
    if _BARE_NAME_DISALLOWED_RE.search(text):
        return ""
    if not 1 <= len(text.split()) <= _BARE_NAME_MAX_WORDS:
        return ""
    return text


def autovalidator_name_comments(obs: StudyObservation) -> List[str]:
    """Bare taxon names the autovalidator account posted as standalone comments."""
    login_key = AUTOVALIDATOR_LOGIN.casefold()
    names: List[str] = []
    for comment in obs.comments:
        if comment.user_login.casefold() != login_key:
            continue
        name = _bare_taxon_name(comment.body or "")
        if name:
            names.append(name)
    return names


def _has_marker_comment(obs: StudyObservation) -> bool:
    """True when the autovalidator left its worded automated-identification comment."""
    login_key = AUTOVALIDATOR_LOGIN.casefold()
    marker = AUTOVALIDATION_COMMENT_MARKER.casefold()
    return any(
        comment.user_login.casefold() == login_key
        and marker in (comment.body or "").casefold()
        for comment in obs.comments
    )


def _name_comment_autovalidation(obs: StudyObservation) -> str:
    """The bare-name comment that stands in for the marker comment, if there is one.

    When the observation also carries an autovalidated name field, the comment
    counts only if it names the same taxon: that corroboration is what keeps an
    unrelated short remark from the account out of this workflow. With no field
    set, the comment is the only record of the inferred name, so it is taken on
    its own -- the name still has to resolve to an exact iNaturalist taxon before
    anything is posted.
    """
    wanted = normalize_taxon_name(_field_suggested_name(obs))
    for name in autovalidator_name_comments(obs):
        if not wanted or normalize_taxon_name(name) == wanted:
            return name
    return ""


def suggested_name_for(obs: StudyObservation) -> str:
    """Return the autovalidated name, from an observation field or from the comment."""
    field_name = _field_suggested_name(obs)
    if field_name:
        return field_name
    return _name_comment_autovalidation(obs)


def is_autovalidated(obs: StudyObservation) -> bool:
    """True when the autovalidator account left either form of its comment."""
    if (obs.id_update_needed or "").strip().casefold() == "yes":
        return False
    return _has_marker_comment(obs) or bool(_name_comment_autovalidation(obs))


def consensus_taxon(obs: StudyObservation) -> Optional[StudyTaxon]:
    """The taxon an identification would have to move: community, else observation."""
    return obs.community_taxon or obs.taxon


def _timestamp(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _latest_autovalidation_time(obs: StudyObservation) -> Optional[datetime]:
    wanted = normalize_taxon_name(_field_suggested_name(obs))
    dates = []
    for comment in obs.comments:
        if comment.user_login.casefold() != AUTOVALIDATOR_LOGIN.casefold():
            continue
        body = comment.body or ""
        bare_name = _bare_taxon_name(body)
        if AUTOVALIDATION_COMMENT_MARKER not in body.casefold() and not (
            bare_name and (not wanted or normalize_taxon_name(bare_name) == wanted)
        ):
            continue
        date = _timestamp(comment.created_at)
        if date is not None:
            dates.append(date)
    return max(dates) if dates else None


def identified_after_autovalidation(obs: StudyObservation, login: str) -> bool:
    """Respect the operator's ID after the latest qualifying automated comment."""
    if not login.strip():
        return False
    latest = _latest_autovalidation_time(obs)
    if latest is None:
        return False
    for ident in obs.all_identifications:
        if ident.user_login.casefold() != login.strip().casefold():
            continue
        date = _timestamp(ident.created_at)
        if date is not None and date > latest:
            return True
    return False


def commented_after_autovalidation(obs: StudyObservation) -> bool:
    """Later human discussion warrants review, without judging its contents."""
    latest = _latest_autovalidation_time(obs)
    if latest is None:
        return False
    for comment in obs.comments:
        if (
            comment.hidden
            or comment.user_login.casefold() == AUTOVALIDATOR_LOGIN.casefold()
        ):
            continue
        date = _timestamp(comment.created_at)
        if date is not None and date > latest:
            return True
    return False


def autovalidated_query(
    observation_query: Optional[ObservationURLQuery] = None,
) -> ObservationURLQuery:
    """Return the discovery query, narrowed by an optional user-supplied URL.

    The autovalidation filters are always appended and always replace a
    conflicting value from the pasted URL, so a narrowing URL can restrict the
    search by place, taxon, or observer but can never widen it past DNA-barcoded
    records the autovalidator has processed and nobody has contested.
    """
    params: QueryParams = []
    sources: List[str] = []
    if observation_query is not None:
        for param, source in zip(
            observation_query.params, observation_query.parameter_sources
        ):
            if param[0].casefold() in _RESERVED_PARAM_KEYS:
                continue
            params.append(param)
            sources.append(source)
    params.append((f"field:{ID_UPDATE_NEEDED_FIELD_NAME}", ID_UPDATE_NEEDED_OK_VALUE))
    sources.append("Autovalidation filter")
    params.append((f"field:{DNA_BARCODE_ITS_FIELD_NAME}", ""))
    sources.append("Autovalidation filter")
    return ObservationURLQuery(
        display_url=(
            observation_query.display_url
            if observation_query is not None
            else BASE_QUERY_URL
        ),
        source_key=canonical_source_key(params),
        params=params,
        source_kind="observations",
        parameter_sources=tuple(sources),
    )


class TaxonLookupFailed(Exception):
    """Autocomplete failed, so the name's existence is unknown."""


class TaxonNameResolver:
    """Resolve an autovalidated name to an iNaturalist taxon, exact matches only.

    Autocomplete happily returns a near miss -- searching for
    ``Clavaria sp. 'fragilis-PNW03'`` returns ``Clavaria lumbriciformis`` -- so a
    result is accepted only when its normalized name equals the normalized query.
    Anything else is treated as "this name does not exist on iNaturalist yet",
    which is the case the caller is allowed to skip. Lookups are memoized per
    scan, because the same provisional name recurs across many observations.
    """

    # Provisional names ("Amanita sp. 'PK05'") return autocomplete pages headed
    # by ordinary taxa of the same genus, so a short page can push the exact
    # match off the end and report an existing name as missing.
    def __init__(
        self,
        client: INatClient,
        per_page: int = 30,
        *,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._client = client
        self._is_cancelled = is_cancelled
        self._per_page = per_page
        self._cache: Dict[str, Optional[StudyTaxon]] = {}

    def resolve(self, name: str) -> Optional[StudyTaxon]:
        if self._is_cancelled and self._is_cancelled():
            return None
        wanted = normalize_taxon_name(name)
        if not wanted:
            return None
        if wanted in self._cache:
            return self._cache[wanted]
        resolved = self._lookup(name, wanted)
        if self._is_cancelled and self._is_cancelled():
            return None
        self._cache[wanted] = resolved
        return resolved

    def resolved_taxa(self) -> List[StudyTaxon]:
        """Every taxon this resolver has already fetched, ancestry included.

        Lets the lineage index reuse the autocomplete reads instead of asking the
        API again for taxa the scan has just looked up.
        """
        return [taxon for taxon in self._cache.values() if taxon is not None]

    def _lookup(self, name: str, wanted: str) -> Optional[StudyTaxon]:
        try:
            raw = self._client.get_taxa_autocomplete(
                name.strip(), per_page=self._per_page
            )
        except Exception as exc:
            if self._is_cancelled and self._is_cancelled():
                return None
            if is_auth_failure_error(exc):
                raise
            log.warning("Taxon autocomplete failed for an autovalidated name: %s", exc)
            raise TaxonLookupFailed("Taxon autocomplete could not be read") from exc
        for raw_taxon in raw.get("results") or []:
            if not isinstance(raw_taxon, dict):
                continue
            if raw_taxon.get("is_active") is False:
                continue
            taxon = parse_taxon(raw_taxon)
            if taxon is None or not taxon.taxon_id:
                continue
            if normalize_taxon_name(taxon.name) == wanted:
                return taxon
        return None


def _ancestry_ids(ancestry: str) -> List[int]:
    """Split an iNaturalist ancestry string ("48460/47170/...") into taxon ids."""
    ids: List[int] = []
    for part in (ancestry or "").split("/"):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


class TaxonLineageIndex:
    """Family and genus ids for a set of taxa, resolved in as few requests as possible.

    An observation payload and a taxon autocomplete result both carry the taxon's
    ancestry as bare ids with no ranks attached, so picking the family out of a
    lineage needs the rank of each ancestor. Those ancestors repeat heavily across
    a scan -- most candidates are fungi in a handful of families -- so every
    unknown id is collected first and fetched in batches, rather than one taxon at
    a time down a rate-limited connection.
    """

    def __init__(self, client: INatClient, db: Optional[CacheDB] = None) -> None:
        self._client = client
        self._db = db
        self._wanted: set = set()
        self._rank_by_id: Dict[int, str] = {}
        self._ancestry_by_id: Dict[int, List[int]] = {}
        # Ids the API was asked for but never returned, whether the request
        # failed outright or simply omitted them. Their rank is unknown, so any
        # taxon that depends on them cannot be classified.
        self._unread: set = set()
        self._complete = False

    def add(self, taxon: Optional[StudyTaxon]) -> None:
        """Register a taxon to be classified, seeding whatever it already knows."""
        if taxon is None or not taxon.taxon_id:
            return
        self._wanted.add(taxon.taxon_id)
        self._record(taxon.taxon_id, taxon.rank, taxon.ancestry)

    def _record(self, taxon_id: int, rank: str, ancestry: str) -> None:
        """Remember one taxon's rank and lineage, whatever the source."""
        if rank:
            self._rank_by_id.setdefault(taxon_id, rank)
        ancestor_ids = _ancestry_ids(ancestry)
        if ancestor_ids:
            self._ancestry_by_id.setdefault(taxon_id, ancestor_ids)

    def resolve(self, is_cancelled: Optional[Callable[[], bool]] = None) -> None:
        """Fill in every lineage and ancestor rank still missing.

        Two passes: the taxa themselves, then the ancestors their lineages name.
        Each pass reads the local cache first, so a repeat scan over familiar
        ground usually needs no network at all.
        """
        self._load(
            [tid for tid in self._wanted if tid not in self._ancestry_by_id],
            is_cancelled,
        )
        ancestors = {
            ancestor_id
            for lineage in self._ancestry_by_id.values()
            for ancestor_id in lineage
        }
        self._load(
            [tid for tid in ancestors if tid not in self._rank_by_id], is_cancelled
        )
        self._complete = not (is_cancelled and is_cancelled())

    def _load(
        self,
        taxon_ids: List[int],
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        pending = sorted(set(taxon_ids))
        if not pending:
            return
        self._fetch(self._take_from_cache(pending), is_cancelled)

    def _take_from_cache(self, taxon_ids: List[int]) -> List[int]:
        """Seed from the local cache, returning the ids that still need the API."""
        if self._db is None:
            return taxon_ids
        try:
            cached = self._db.get_taxon_lineages(taxon_ids)
        except Exception as exc:
            log.debug("Taxon lineage cache read failed: %s", exc)
            return taxon_ids
        for taxon_id, (rank, ancestry) in cached.items():
            self._record(taxon_id, rank, ancestry)
            self._unread.discard(taxon_id)
        return [taxon_id for taxon_id in taxon_ids if taxon_id not in cached]

    def _store_in_cache(self, entries: List[Tuple[int, str, str]]) -> None:
        if self._db is None or not entries:
            return
        try:
            self._db.set_taxon_lineages(entries)
        except Exception as exc:
            log.debug("Taxon lineage cache write failed: %s", exc)

    def _fetch(
        self,
        taxon_ids: List[int],
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        pending = sorted(taxon_ids)
        for start in range(0, len(pending), _TAXON_FETCH_CHUNK):
            if is_cancelled and is_cancelled():
                return
            chunk = pending[start : start + _TAXON_FETCH_CHUNK]
            # Assume nothing came back until it does: an id still marked unread
            # after the pass leaves its candidates unclassified rather than
            # being compared against ranks that were never read.
            self._unread.update(chunk)
            try:
                raw = self._client.get_taxa_by_ids(chunk)
            except Exception as exc:
                if is_auth_failure_error(exc):
                    raise
                # A lineage that cannot be read leaves its candidates
                # unclassified, which the preview renders as an ordinary row.
                # Nothing is posted or skipped on the strength of this, so a
                # failure here is not fatal.
                log.warning("Taxon lineage lookup failed: %s", exc)
                continue
            fetched: List[Tuple[int, str, str]] = []
            for raw_taxon in raw.get("results") or []:
                if not isinstance(raw_taxon, dict):
                    continue
                taxon = parse_taxon(raw_taxon)
                if taxon is None or not taxon.taxon_id:
                    continue
                self._record(taxon.taxon_id, taxon.rank, taxon.ancestry)
                self._unread.discard(taxon.taxon_id)
                fetched.append((taxon.taxon_id, taxon.rank, taxon.ancestry))
            self._store_in_cache(fetched)

    def knows(self, taxon_id: int) -> bool:
        """True when this taxon's placement was actually read, so it can be compared.

        A cancelled pass, or a lookup that failed or came back short, leaves
        ancestor ranks unread, which would make the taxon look unplaced rather
        than unknown; nothing is classified until its own record and every
        ancestor on its lineage have actually been read.
        """
        if not self._complete or taxon_id in self._unread:
            return False
        lineage = self._ancestry_by_id.get(taxon_id)
        if lineage is None:
            return False
        return not any(ancestor_id in self._unread for ancestor_id in lineage)

    def _rank_ancestor(self, taxon_id: int, rank: str) -> Optional[int]:
        if self._rank_by_id.get(taxon_id) == rank:
            return taxon_id
        for ancestor_id in self._ancestry_by_id.get(taxon_id) or []:
            if self._rank_by_id.get(ancestor_id) == rank:
                return ancestor_id
        return None

    def shares_lineage(self, first_id: int, second_id: int) -> bool:
        """True when one taxon sits on the other's ancestry, in either direction.

        An observation left at a coarser rank that already contains the
        autovalidated name (Hygrophoraceae against "Hygrophoraceae sp. 'IN01'")
        does not contradict it, so it is not a conflict to flag.
        """
        if first_id == second_id:
            return True
        return first_id in (self._ancestry_by_id.get(second_id) or []) or second_id in (
            self._ancestry_by_id.get(first_id) or []
        )

    def family_id(self, taxon_id: int) -> Optional[int]:
        return self._rank_ancestor(taxon_id, "family")

    def genus_id(self, taxon_id: int) -> Optional[int]:
        return self._rank_ancestor(taxon_id, "genus")


def taxon_conflict_for(
    index: TaxonLineageIndex,
    current: Optional[StudyTaxon],
    target_taxon_id: int,
) -> str:
    """Classify how far the current observation taxon is from the autovalidated name.

    An observation clears when the two taxa share a lineage — the same taxon, or
    one an ancestor of the other, which is the ordinary case of an ID left at a
    coarser rank that still contains the name — or when both resolve to the same
    genus. A pair in different families is the strongest sign the sequence
    belongs to something else entirely; anything else that cannot corroborate
    the genus is worth a look.
    """
    if current is None or not current.taxon_id or not target_taxon_id:
        return ""
    if current.taxon_id == target_taxon_id:
        return ""
    if not (index.knows(current.taxon_id) and index.knows(target_taxon_id)):
        return ""
    # A coarser identification on the same lineage corroborates the name as far
    # as it goes; only a genuine divergence is worth flagging.
    if index.shares_lineage(current.taxon_id, target_taxon_id):
        return ""

    current_family = index.family_id(current.taxon_id)
    target_family = index.family_id(target_taxon_id)
    if current_family and target_family and current_family != target_family:
        return TAXON_CONFLICT_FAMILY

    current_genus = index.genus_id(current.taxon_id)
    target_genus = index.genus_id(target_taxon_id)
    if current_genus and target_genus and current_genus == target_genus:
        return ""
    return TAXON_CONFLICT_GENUS


def _annotate_taxon_conflicts(
    candidates: List[BulkDisagreeCandidate],
    client: INatClient,
    resolver: TaxonNameResolver,
    is_cancelled: Optional[Callable[[], bool]] = None,
    db: Optional[CacheDB] = None,
) -> None:
    """Tag every candidate with how far its observation taxon is from the target."""
    if not candidates:
        return
    index = TaxonLineageIndex(client, db)
    for taxon in resolver.resolved_taxa():
        index.add(taxon)
    for candidate in candidates:
        index.add(candidate.observation.taxon)
    index.resolve(is_cancelled)
    for candidate in candidates:
        candidate.taxon_conflict = taxon_conflict_for(
            index, candidate.observation.taxon, candidate.target_taxon_id
        )


def autovalidated_scan_scope(login: str, query: ObservationURLQuery) -> str:
    """Account and effective filters identify independent scan progress."""
    params = sorted(
        (key.casefold(), value)
        for key, value in query.params
        if key.casefold() not in {"order", "order_by", "page", "per_page"}
    )
    return login.strip().casefold() + "|" + canonical_source_key(params)


def plan_autovalidated_id_candidates(
    loader: StudyLoader,
    login: str,
    api_token: str,
    *,
    observation_query: Optional[ObservationURLQuery] = None,
    report_unresolved_names: bool = False,
    max_observations: int = 200,
    per_page: int = 200,
    scan_mode: str = "continue",
    is_cancelled: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> BulkDisagreePlanResult:
    """Continue stable ID pagination, keeping unfinished work durably queued.

    Only IDs and scan metadata are persisted. Candidates are always reconstructed
    from fresh reads and the posting workflow retains its own pre-post checks.
    """
    if scan_mode not in {"continue", "retry", "rescan"}:
        raise ValueError("Unknown autovalidated scan mode")
    query = autovalidated_query(observation_query)
    scope = autovalidated_scan_scope(login, query)
    db = loader._db
    resolver = TaxonNameResolver(loader._client, is_cancelled=is_cancelled)
    candidates: List[BulkDisagreeCandidate] = []
    stats = AutovalidatedPlanStats(scan_scope=scope)
    max_scan = max(1, int(max_observations))
    page_size = max(1, min(200, int(per_page)))

    def cancelled() -> bool:
        return bool(is_cancelled and is_cancelled())

    def process(observations: list[StudyObservation], *, fresh: bool) -> None:
        shortlist = _shortlist_page(observations, loader, stats)
        shortlisted_ids = {obs.obs_id for obs, _ in shortlist}
        for obs in observations:
            if obs.obs_id not in shortlisted_ids:
                db.finish_autovalidated_pending(scope, obs.obs_id)
        resolved = _resolve_shortlist(
            shortlist,
            resolver,
            stats,
            report_unresolved_names=report_unresolved_names,
            is_cancelled=is_cancelled,
        )
        if not fresh:
            candidates.extend(
                _candidates_from_refresh(
                    resolved,
                    loader=loader,
                    login=login,
                    api_token=api_token,
                    report_unresolved_names=report_unresolved_names,
                    stats=stats,
                    page=1,
                    is_cancelled=is_cancelled,
                    scan_scope=scope,
                )
            )
            return
        for obs, _ in shortlist:
            if cancelled():
                break
            if obs.obs_id not in resolved:
                continue
            before = stats.skipped_unresolved_name
            candidate = _candidate_from_refreshed(
                obs,
                resolved[obs.obs_id],
                login=login,
                report_unresolved_names=report_unresolved_names,
                stats=stats,
            )
            if candidate is not None:
                candidates.append(candidate)
            elif before == stats.skipped_unresolved_name:
                db.finish_autovalidated_pending(scope, obs.obs_id)

    # Retry a bounded, rotating selection so deferred/unresolved items cannot
    # prevent later pending observations from being reached.
    pending = db.autovalidated_pending_ids(scope, max_scan)
    for offset in range(0, len(pending), page_size):
        if cancelled():
            break
        ids = pending[offset : offset + page_size]
        db.touch_autovalidated_pending(scope, ids)
        try:
            refreshed = refresh_observations(loader._client, api_token, ids)
        except Exception as exc:
            if is_auth_failure_error(exc):
                raise
            # Keep IDs queued; failures must never prevent the next batch.
            stats.skipped_refresh_failure += len(ids)
            continue
        stats.pending_rechecked += len(ids)
        stats.skipped_refresh_failure += len(
            set(ids) - {obs.obs_id for obs in refreshed}
        )
        process(refreshed, fresh=True)
        if progress:
            progress(stats.pending_rechecked, len(pending))

    if scan_mode == "rescan" and not cancelled():
        db.reset_autovalidated_cursor(scope)
    cursor = db.autovalidated_cursor(scope)
    base_params = [
        (key, value)
        for key, value in query.params
        if key.casefold() not in {"order", "order_by", "id_above"}
    ]
    original_above = max(
        (int(value) for key, value in query.params if key.casefold() == "id_above"),
        default=0,
    )
    while scan_mode != "retry" and stats.total_url_results_scanned < max_scan:
        if cancelled():
            break
        params = base_params + [
            ("order_by", "id"),
            ("order", "asc"),
            ("id_above", str(max(cursor, original_above))),
        ]
        batch_query = ObservationURLQuery(
            display_url=query.display_url,
            source_key=canonical_source_key(params),
            params=params,
        )
        filters = LoadFilters(
            query.display_url,
            observation_query=batch_query,
            apply_taxon_filter_to_observation_url=False,
        )
        size = min(page_size, max_scan - stats.total_url_results_scanned)
        observations, total = loader.load_page(
            filters,
            page=1,
            per_page=size,
            is_cancelled=is_cancelled,
            use_cache=False,
        )
        stats.total_api_results = total
        if cancelled():
            break
        if not observations:
            stats.scan_exhausted = True
            break
        ids = [obs.obs_id for obs in observations]
        if min(ids) <= max(cursor, original_above):
            raise ValueError(
                "Observation search did not respect the saved scan position"
            )
        db.queue_autovalidated_page(scope, ids)
        cursor = max(ids)
        stats.total_url_results_scanned += len(observations)
        process(observations, fresh=False)
        if progress:
            progress(stats.total_url_results_scanned, total)
        if len(observations) < size or total <= len(observations):
            stats.scan_exhausted = True
            break

    # Rescans can encounter an observation already reconstructed from the queue.
    candidates = list({item.observation.obs_id: item for item in candidates}.values())
    if not cancelled():
        try:
            _annotate_taxon_conflicts(
                candidates, loader._client, resolver, is_cancelled, db
            )
        except Exception as exc:
            if is_auth_failure_error(exc):
                raise
            log.warning("Could not classify autovalidated taxon conflicts: %s", exc)
    stats.candidate_count = len(candidates)
    return BulkDisagreePlanResult(candidates=candidates, stats=stats)


def _shortlist_page(
    page_observations: List[StudyObservation],
    loader: StudyLoader,
    stats: AutovalidatedPlanStats,
) -> List[Tuple[StudyObservation, str]]:
    """Cheaply reject the search results that plainly need no identification."""
    shortlist: List[Tuple[StudyObservation, str]] = []
    for obs in page_observations:
        if loader._db.is_bulk_disagree_skipped(obs.obs_id):
            stats.skipped_permanent += 1
            continue
        if not (obs.dna_barcode_its or "").strip():
            stats.skipped_missing_dna_barcode_its += 1
            continue
        if not is_autovalidated(obs):
            stats.skipped_not_autovalidated += 1
            continue
        suggested = suggested_name_for(obs)
        if not suggested:
            stats.skipped_no_suggested_name += 1
            continue
        # Search payloads carry no community taxon object, only the observation
        # taxon. A name match there is enough to reject the observation outright;
        # everything else is decided on the authenticated re-read below.
        current = consensus_taxon(obs)
        if current is not None and normalize_taxon_name(
            current.name
        ) == normalize_taxon_name(suggested):
            stats.skipped_consensus_already_matches += 1
            continue
        shortlist.append((obs, suggested))
    return shortlist


def _resolve_shortlist(
    shortlist: List[Tuple[StudyObservation, str]],
    resolver: TaxonNameResolver,
    stats: AutovalidatedPlanStats,
    *,
    report_unresolved_names: bool,
    is_cancelled: Optional[Callable[[], bool]],
) -> Dict[int, StudyTaxon]:
    """Map each shortlisted observation to the taxon its autovalidated name names."""
    resolved: Dict[int, StudyTaxon] = {}
    for obs, suggested in shortlist:
        if is_cancelled and is_cancelled():
            break
        try:
            taxon = resolver.resolve(suggested)
        except TaxonLookupFailed:
            stats.skipped_refresh_failure += 1
            continue
        if is_cancelled and is_cancelled():
            break
        if taxon is None:
            _record_unresolved(
                stats,
                obs.obs_id,
                suggested,
                report_unresolved_names=report_unresolved_names,
            )
            continue
        resolved[obs.obs_id] = taxon
    return resolved


def _candidates_from_refresh(
    resolved: Dict[int, StudyTaxon],
    *,
    loader: StudyLoader,
    login: str,
    api_token: str,
    report_unresolved_names: bool,
    stats: AutovalidatedPlanStats,
    page: int,
    is_cancelled: Optional[Callable[[], bool]],
    scan_scope: str = "",
) -> List[BulkDisagreeCandidate]:
    """Re-read the resolved observations and build the candidates worth posting."""
    pending_ids = list(resolved)
    if not pending_ids:
        return []

    refresh_failed_all = False
    try:
        refreshed = refresh_observations(loader._client, api_token, pending_ids)
    except Exception as exc:
        if is_auth_failure_error(exc):
            raise
        stats.skipped_refresh_failure += len(pending_ids)
        log.error(
            "Autovalidated-ID preview refresh failed for page=%s ids=%s: %s",
            page,
            pending_ids,
            exc,
        )
        refreshed = []
        refresh_failed_all = True
    fresh_by_id = {obs.obs_id: obs for obs in refreshed}

    candidates: List[BulkDisagreeCandidate] = []
    for obs_id in pending_ids:
        if is_cancelled and is_cancelled():
            break
        fresh = fresh_by_id.get(obs_id)
        if fresh is None:
            if not refresh_failed_all:
                stats.skipped_refresh_failure += 1
            continue
        before_unresolved = stats.skipped_unresolved_name
        candidate = _candidate_from_refreshed(
            fresh,
            resolved[obs_id],
            login=login,
            report_unresolved_names=report_unresolved_names,
            stats=stats,
        )
        if candidate is not None:
            candidates.append(candidate)
        elif scan_scope and before_unresolved == stats.skipped_unresolved_name:
            loader._db.finish_autovalidated_pending(scan_scope, obs_id)
    return candidates


def _candidate_from_refreshed(
    fresh: StudyObservation,
    suggested_taxon: StudyTaxon,
    *,
    login: str,
    report_unresolved_names: bool,
    stats: AutovalidatedPlanStats,
) -> Optional[BulkDisagreeCandidate]:
    """Re-check every precondition against the fresh read and build a candidate."""
    if not (fresh.dna_barcode_its or "").strip():
        stats.skipped_missing_dna_barcode_its += 1
        return None
    if not is_autovalidated(fresh):
        stats.skipped_not_autovalidated += 1
        return None
    if identified_after_autovalidation(fresh, login):
        stats.skipped_identified_after_autovalidation += 1
        return None
    suggested = suggested_name_for(fresh)
    if not suggested:
        stats.skipped_no_suggested_name += 1
        return None
    # The field value must still name the taxon this candidate was planned for;
    # an edit between the search and the re-read invalidates the plan.
    if normalize_taxon_name(suggested) != normalize_taxon_name(suggested_taxon.name):
        _record_unresolved(
            stats,
            fresh.obs_id,
            suggested,
            report_unresolved_names=report_unresolved_names,
        )
        return None

    current = consensus_taxon(fresh)
    if current is not None and current.taxon_id == suggested_taxon.taxon_id:
        stats.skipped_consensus_already_matches += 1
        return None
    if observation_finished_at_target(fresh, suggested_taxon.taxon_id):
        stats.skipped_already_target += 1
        return None
    if current_user_has_taxon(fresh, login, suggested_taxon.taxon_id):
        stats.skipped_already_target += 1
        return None

    # Disagree only when the autovalidated name is coarser than the current
    # consensus; refining to a descendant, or moving sideways, is a plain ID.
    explicit_disagreement = bool(
        current is not None
        and taxon_is_strict_ancestor(current, suggested_taxon.taxon_id)
    )
    user_ident = current_user_identification(fresh, login)
    candidate = BulkDisagreeCandidate(
        observation=fresh,
        source_taxon_id=0,
        source_taxon_name="",
        # Re-checked before posting: the observation must still carry this exact
        # autovalidated name in its Provisional Species Name field.
        source_provisional_name=(
            suggested if (fresh.provisional_species_name or "").strip() else ""
        ),
        source_display_name=suggested,
        target_taxon_id=suggested_taxon.taxon_id,
        target_taxon_name=suggested_taxon.name,
        target_taxon_rank=suggested_taxon.rank,
        current_observation_taxon_name=fresh.taxon.name if fresh.taxon else "",
        community_taxon_name=(
            fresh.community_taxon.name if fresh.community_taxon else ""
        ),
        has_dna_barcode_its=True,
        dna_barcode_its_value=(fresh.dna_barcode_its or "").strip(),
        user_current_taxon=(
            user_ident.taxon.name if user_ident and user_ident.taxon else ""
        ),
        dqa_vote_planned=False,
        explicit_disagreement=explicit_disagreement,
    )
    candidate.comments_after_autovalidation = commented_after_autovalidation(fresh)
    # Filled in unconditionally; the setup dialog's tag option decides at post
    # time whether these logins are @-mentioned in the comment body.
    candidate.other_identifier_logins = collect_other_identifier_logins(
        fresh, login, suggested_taxon.taxon_id
    )
    return candidate


def _record_unresolved(
    stats: AutovalidatedPlanStats,
    obs_id: int,
    suggested: str,
    *,
    report_unresolved_names: bool,
) -> None:
    stats.skipped_unresolved_name += 1
    if report_unresolved_names:
        stats.unresolved_names.append((obs_id, suggested))


def post_autovalidated_identification(
    client: INatClient,
    api_token: str,
    login: str,
    candidate: BulkDisagreeCandidate,
    *,
    body: str = "",
    skip_with_dna_barcode_its: bool = False,
    only_with_dna_barcode_its: bool = True,
    require_source_taxon_match: bool = False,
    dry_run: bool = False,
    dqa_posting_enabled: bool = False,
    explicit_disagreement: Optional[bool] = None,
) -> BulkDisagreeResult:
    """Re-verify the autovalidation, then post through the shared identification path.

    The shared poster's source-identity safeguard is built around a URL's source
    taxon, which this workflow has none of, so the equivalent safeguard is
    applied here: the observation must still be autovalidated and must still
    carry an autovalidated name that resolves to the taxon this candidate plans
    to post. Only then is the write delegated, with its own refresh, duplicate
    checks, and post-write verification.

    The signature matches :func:`post_bulk_disagreement` so the two are
    interchangeable to the posting worker.
    """
    verified = refresh_observation(client, api_token, candidate.observation.obs_id)
    if verified is None:
        return BulkDisagreeResult(
            "failed",
            "Could not refresh observation before posting; no identification was posted.",
            candidate=candidate,
        )

    if not is_autovalidated(verified):
        return BulkDisagreeResult(
            "changed",
            "Observation no longer qualifies for autovalidation or its observer requested an ID update.",
            candidate=candidate,
            refreshed_observation=verified,
        )

    if identified_after_autovalidation(verified, login):
        return BulkDisagreeResult(
            "skipped",
            "You identified this observation after autovalidation; no identification was added.",
            candidate=candidate,
            refreshed_observation=verified,
        )

    suggested = suggested_name_for(verified)
    if normalize_taxon_name(suggested) != normalize_taxon_name(
        candidate.target_taxon_name
    ):
        return BulkDisagreeResult(
            "changed",
            (
                "The autovalidated name changed after refresh: the observation now "
                f"reads {suggested or '(no name)'} rather than "
                f"{candidate.target_taxon_name}."
            ),
            candidate=candidate,
            refreshed_observation=verified,
        )

    current = consensus_taxon(verified)
    if current is not None and current.taxon_id == candidate.target_taxon_id:
        return BulkDisagreeResult(
            "skipped",
            (
                f"The consensus is already {candidate.target_taxon_name}; "
                "no identification was added."
            ),
            candidate=candidate,
            refreshed_observation=verified,
        )

    return post_bulk_disagreement(
        client,
        api_token,
        login,
        candidate,
        body=body,
        skip_with_dna_barcode_its=False,
        only_with_dna_barcode_its=True,
        # This workflow has no URL source taxon; the autovalidation re-checks
        # above are its equivalent safeguard.
        require_source_taxon_match=False,
        dry_run=dry_run,
        dqa_posting_enabled=False,
        explicit_disagreement=explicit_disagreement,
        refreshed_disagreement=lambda obs: bool(
            (current := consensus_taxon(obs)) is not None
            and taxon_is_strict_ancestor(current, candidate.target_taxon_id)
        ),
        refreshed_skip_reason=lambda obs: (
            "You identified this observation after autovalidation; no identification was added."
            if identified_after_autovalidation(obs, login)
            else ""
        ),
    )
