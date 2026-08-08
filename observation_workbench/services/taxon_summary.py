"""
Taxon summary service: fetches species_counts for the learning panel.

How it works:
  1. Calls /observations/species_counts with ident_user_login + filters
  2. Returns a TaxonSummary sorted by count descending
  3. Results are cached in SQLite (1-hour TTL)

How to extend for compare mode:
  - Add a compare_user_login parameter
  - Fetch species_counts for both users
  - Compute agreement/disagreement rates by taxon
  - The TaxonSummary model can be extended with per-taxon statistics
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from observation_workbench.api.client import INatClient
from observation_workbench.api.observation_url import QueryParams
from observation_workbench.api.parsers import parse_taxon_summary
from observation_workbench.models import TaxonSummary
from observation_workbench.storage.cache_db import CacheDB

log = logging.getLogger(__name__)


class TaxonSummaryService:
    def __init__(self, client: INatClient, db: CacheDB) -> None:
        self._client = client
        self._db = db

    def fetch(
        self,
        username: str,
        taxon_id: Optional[int] = None,
        place_id: Optional[int] = None,
        d1: Optional[str] = None,
        d2: Optional[str] = None,
    ) -> TaxonSummary:
        """
        Fetch taxon summary. Returns cached result if fresh.

        NOTE: /observations/species_counts returns observations where this
        user has made any identification, not just the leading one.
        It counts by the observation's community taxon, not the identifier's taxon.
        This is the most reliable endpoint for aggregate learning stats.
        """
        cache_key = self._db.make_summary_key(username, place_id, taxon_id, d1, d2)
        cached = self._db.get_summary_cache(cache_key)
        if cached is not None:
            log.debug("Taxon summary: cache hit for %s", username)
            return parse_taxon_summary(cached)

        raw = self._client.get_observations_species_counts(
            ident_user_login=username,
            taxon_id=taxon_id,
            place_id=place_id,
            d1=d1,
            d2=d2,
        )
        self._db.set_summary_cache(cache_key, raw)
        summary = parse_taxon_summary(raw)
        summary.counts.sort(key=lambda c: c.count, reverse=True)
        return summary

    def fetch_observation_query(
        self,
        source_key: str,
        query_params: QueryParams,
    ) -> TaxonSummary:
        """Fetch taxon summary for an arbitrary observations URL query."""
        cache_key = self._db.make_observation_summary_key(source_key)
        cached = self._db.get_summary_cache(cache_key)
        if cached is not None:
            log.debug("Taxon summary: cache hit for observations URL")
            return parse_taxon_summary(cached)

        raw = self._client.get_observation_species_counts(query_params)
        self._db.set_summary_cache(cache_key, raw)
        summary = parse_taxon_summary(raw)
        summary.counts.sort(key=lambda c: c.count, reverse=True)
        return summary
