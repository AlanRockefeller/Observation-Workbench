"""
Study loader service: fetches identifications + observations from the iNat API
and normalizes them into StudyObservation objects.

Generation token pattern:
  Each load() call bumps the generation. Workers check their generation before
  emitting results — stale workers are silently ignored.

Pagination:
  load_page() fetches one page. Call repeatedly with page=2,3,... and append
  results to your list. total_results is returned so the UI can show progress.
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional, Tuple

from observation_workbench.api.client import INatClient
from observation_workbench.api.observation_url import (
    ObservationURLParseError,
    ObservationURLQuery,
    parse_observations_url,
    with_taxon_filter,
)
from observation_workbench.api.parsers import parse_observation, parse_observation_from_ident
from observation_workbench.models import StudyObservation
from observation_workbench.storage.cache_db import CacheDB

log = logging.getLogger(__name__)

# iNat rank level numbers — lower means more specific.
RANK_LEVELS: dict = {
    "stateofmatter": 100,
    "kingdom": 70,
    "phylum": 60,
    "subphylum": 57,
    "superclass": 53,
    "class": 50,
    "subclass": 47,
    "infraclass": 45,
    "superorder": 43,
    "order": 40,
    "suborder": 37,
    "infraorder": 35,
    "superfamily": 33,
    "epifamily": 32,
    "family": 30,
    "subfamily": 27,
    "supertribe": 26,
    "tribe": 25,
    "subtribe": 24,
    "genus": 20,
    "subgenus": 15,
    "section": 13,
    "subsection": 12,
    "complex": 11,
    "species": 10,
    "hybrid": 10,
    "subspecies": 5,
    "variety": 5,
    "form": 5,
    "infrahybrid": 5,
}


class LoadFilters:
    """All filter parameters for a study session."""

    def __init__(
        self,
        username: str,
        place_id: Optional[int] = None,
        taxon_id: Optional[int] = None,
        leading_only: bool = False,
        d1: Optional[str] = None,
        d2: Optional[str] = None,
        rank_level: Optional[int] = None,
        rank_name: Optional[str] = None,
        exact_rank: bool = False,
        provisional_name_only: bool = False,
        observation_query: Optional[ObservationURLQuery] = None,
        apply_taxon_filter_to_observation_url: bool = True,
    ) -> None:
        self.source_input = username.strip()
        self.apply_taxon_filter_to_observation_url = apply_taxon_filter_to_observation_url
        parsed_observation_query = observation_query or parse_observations_url(self.source_input)
        if (
            parsed_observation_query is not None
            and self.apply_taxon_filter_to_observation_url
        ):
            parsed_observation_query = with_taxon_filter(parsed_observation_query, taxon_id)
        self.observation_query = parsed_observation_query
        self.username = "" if self.observation_query else self.source_input
        self.place_id = None if self.observation_query else place_id
        self.taxon_id = taxon_id
        self.leading_only = False if self.observation_query else leading_only
        self.d1 = None if self.observation_query else (d1 or None)
        self.d2 = None if self.observation_query else (d2 or None)
        self.rank_level = None if self.observation_query else rank_level
        normalized_rank_name = rank_name.strip().lower() if rank_name else None
        self.rank_name = None if self.observation_query else normalized_rank_name
        self.exact_rank = False if self.observation_query else exact_rank
        self.provisional_name_only = provisional_name_only

    def __repr__(self) -> str:
        if self.observation_query:
            return (
                f"LoadFilters(observations_url={self.observation_query.display_url!r}, "
                f"taxon={self.taxon_id})"
            )
        return (
            f"LoadFilters(user={self.username!r}, place={self.place_id}, "
            f"taxon={self.taxon_id}, leading={self.leading_only}, "
            f"rank_level={self.rank_level}, exact_rank={self.exact_rank}, "
            f"provisional={self.provisional_name_only})"
        )


class StudyLoader:
    """Orchestrates paginated fetching of study results."""

    def __init__(self, client: INatClient, db: CacheDB) -> None:
        self._client = client
        self._db = db
        self._cache_hits = 0
        self._cache_hits_lock = __import__("threading").Lock()

    @property
    def cache_hit_count(self) -> int:
        with self._cache_hits_lock:
            return self._cache_hits

    def reset_cache_hit_count(self) -> None:
        with self._cache_hits_lock:
            self._cache_hits = 0

    def load_page(
        self,
        filters: LoadFilters,
        page: int = 1,
        per_page: int = 200,
        generation: int = 0,
        is_cancelled: Optional[Callable[[], bool]] = None,
        use_cache: bool = True,
    ) -> Tuple[List[StudyObservation], int]:
        """
        Fetch one page of study results and return (observations, total_results).

        is_cancelled: optional callable that returns True if this load should abort.
        generation: used for logging/tracing; callers handle generation checks.
        """
        if (
            filters.observation_query is not None
            and filters.observation_query.source_kind == "identify"
        ):
            raise ObservationURLParseError(
                "iNaturalist /observations/identify URLs require the authenticated "
                "Identify workflow and cannot be loaded by the study browser."
            )
        if is_cancelled and is_cancelled():
            log.debug("Load cancelled before fetch (gen=%d page=%d)", generation, page)
            return [], 0

        if filters.observation_query:
            cache_key = self._db.make_observation_query_key(
                filters.observation_query.source_key,
                page,
                per_page,
            )
        else:
            cache_key = self._db.make_query_key(
                filters.username,
                filters.place_id,
                filters.taxon_id,
                filters.leading_only,
                filters.d1,
                filters.d2,
                page,
                per_page,
            )

        # Try metadata cache first
        if use_cache:
            cached = self._db.get_query_cache(cache_key)
            if cached is not None:
                raw_list, total = cached
                log.debug("Cache hit: page=%d total=%d", page, total)
                with self._cache_hits_lock:
                    self._cache_hits += 1
                observations = self._parse_raw_list(raw_list, filters)
                return observations, total

        # Fetch from API
        if is_cancelled and is_cancelled():
            return [], 0

        try:
            if filters.observation_query:
                raw = self._client.get_observations(
                    filters.observation_query.params,
                    page=page,
                    per_page=per_page,
                )
            else:
                raw = self._client.get_identifications(
                    user_login=filters.username,
                    taxon_id=filters.taxon_id,
                    place_id=filters.place_id,
                    leading=True if filters.leading_only else None,
                    current_only=True,
                    d1=filters.d1,
                    d2=filters.d2,
                    page=page,
                    per_page=per_page,
                )
        except Exception as exc:
            log.error("API error fetching study results: %s", exc)
            raise

        if is_cancelled and is_cancelled():
            return [], 0

        total = int(raw.get("total_results", 0))
        raw_results = raw.get("results") or []

        # Cache the raw results
        if use_cache:
            try:
                self._db.set_query_cache(cache_key, raw_results, total)
            except Exception as exc:
                log.warning("Failed to cache query results: %s", exc)

        observations = self._parse_raw_list(raw_results, filters)
        return observations, total

    def _parse_raw_list(
        self, raw_list: list, filters: LoadFilters
    ) -> List[StudyObservation]:
        observations: List[StudyObservation] = []
        if filters.observation_query:
            for raw_obs in raw_list:
                obs = parse_observation(raw_obs)
                if obs is not None and _passes_provisional_filter(obs, filters):
                    observations.append(obs)
            return observations

        for raw_ident in raw_list:
            obs = parse_observation_from_ident(raw_ident, filters.username)
            if obs is None:
                continue
            # Client-side leading filter (API 'leading' param may not be supported).
            # Only drop the observation if is_leading is explicitly False;
            # None (field absent from payload) is treated as unknown → keep.
            if filters.leading_only and obs.target_identification:
                if obs.target_identification.is_leading is False:
                    log.debug(
                        "leading filter: dropped ident %d (is_leading=False)",
                        obs.target_identification.ident_id,
                    )
                    continue
                if obs.target_identification.is_leading is None:
                    log.debug(
                        "leading filter: keeping ident %d (is_leading absent from payload)",
                        obs.target_identification.ident_id,
                    )
            if filters.rank_level is not None and obs.target_identification:
                taxon_rank = (obs.target_identification.taxon.rank or "").lower()
                if filters.exact_rank:
                    # Match by rank name so ranks that share a level (e.g. species
                    # and hybrid, both level 10) are not conflated. If no rank
                    # name was given, fail closed rather than accidentally
                    # matching every rank at this level.
                    if filters.rank_name is None or taxon_rank != filters.rank_name:
                        continue
                else:
                    obs_level = RANK_LEVELS.get(taxon_rank)
                    if obs_level is not None and obs_level > filters.rank_level:
                        continue
            if _passes_provisional_filter(obs, filters):
                observations.append(obs)
        return observations


def _has_provisional_name(taxon) -> bool:
    return bool(taxon and "'" in (taxon.name or ""))


def _passes_provisional_filter(obs: StudyObservation, filters: LoadFilters) -> bool:
    if not filters.provisional_name_only:
        return True
    if filters.observation_query:
        if any(ident.current and _has_provisional_name(ident.taxon) for ident in obs.all_identifications):
            return True
        return (
            _has_provisional_name(obs.community_taxon)
            or _has_provisional_name(obs.display_taxon)
            or _has_provisional_name(obs.taxon)
        )
    ident = obs.target_identification
    return bool(ident and _has_provisional_name(ident.taxon))
