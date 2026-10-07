"""Unauthenticated, positively proven DNA source discovery and pair scoring."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlencode

from observation_workbench.api.client import INatAPIError, INatClient

from .db import DNALinkingDB
from .types import (
    ALGORITHM_VERSION,
    DNA_FIELD_NAME,
    FUNGI_TAXON_ID,
    CandidatePair,
    DiscoveryProgress,
    ObservationSnapshot,
    ScanConfig,
)

DNA_RE = re.compile(r"[ACGTRYSWKMBDHVN]{120,}")
_PAGINATION_KEYS = frozenset({"page", "per_page", "order", "order_by", "id_above"})


class DiscoveryCancelled(RuntimeError):
    pass


class DiscoveryDiagnostic(RuntimeError):
    pass


def normalize_dna(value: object) -> str:
    lines = [
        line for line in str(value or "").splitlines()
        if not line.lstrip().startswith(">")
    ]
    return re.sub(r"\s+", "", "\n".join(lines)).upper()


def contains_qualifying_dna(value: object) -> bool:
    return DNA_RE.search(normalize_dna(value)) is not None


def extract_observation_field_rows(raw: Mapping[str, Any], field_id: int) -> list[Mapping[str, Any]]:
    rows = raw.get("ofvs") or raw.get("observation_field_values") or []
    result = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        nested = row.get("observation_field")
        raw_id = row.get("field_id") or row.get("observation_field_id")
        if raw_id is None and isinstance(nested, Mapping):
            raw_id = nested.get("id")
        try:
            if int(raw_id) == int(field_id):
                result.append(row)
        except (TypeError, ValueError):
            continue
    return result


def scan_fingerprint(
    user_id: int, config: ScanConfig, field_id: int
) -> tuple[str, str]:
    effective = canonical_source_params(config.source_params)
    document = {
        "user_id": int(user_id),
        "source": effective,
        "candidate_login": config.candidate_login.strip().casefold(),
        "radius_m": round(float(config.radius_m), 6),
        "window_seconds": round(float(config.window_minutes) * 60),
        "field_id": int(field_id),
        "algorithm": ALGORITHM_VERSION,
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest(), urlencode(effective)


def canonical_source_params(params: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    cleaned = [
        (str(key), str(value)) for key, value in params
        if str(key).casefold() not in _PAGINATION_KEYS
        and str(key).strip().casefold() != f"field:{DNA_FIELD_NAME}".casefold()
    ]
    # Fungi is a workflow invariant. A narrower taxon URL is preserved and
    # checked locally; an absent taxon is made explicitly global-Fungi.
    if not any(key.casefold() == "taxon_id" for key, _ in cleaned):
        cleaned.append(("taxon_id", str(FUNGI_TAXON_ID)))
    return tuple(sorted(cleaned, key=lambda item: (item[0].casefold(), item[1])))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def score_pair(
    distance_m: float, time_seconds: float, radius_m: float,
    window_seconds: float, same_family: bool,
) -> tuple[float, float, float, int]:
    distance_score = 40.0 * (1.0 - distance_m / radius_m)
    time_score = 40.0 * (1.0 - time_seconds / window_seconds)
    family_score = 20.0 if same_family else 0.0
    score = max(0, min(100, round(distance_score + time_score + family_score)))
    return distance_score, time_score, family_score, score


class DNADiscovery:
    def __init__(self, client: INatClient, db: DNALinkingDB) -> None:
        self.client = client
        self.db = db
        self._family_cache: dict[int, tuple[Optional[int], str]] = {}

    def resolve_field(self) -> tuple[int, str]:
        raw = self.client.get_observation_fields_autocomplete(DNA_FIELD_NAME)
        matches = []
        for item in raw.get("results") or []:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("name") or "").strip().casefold() != DNA_FIELD_NAME.casefold():
                continue
            datatype = str(item.get("datatype") or "").strip().casefold()
            if datatype in {"dna", "dna_sequence"}:
                matches.append((int(item["id"]), datatype))
        if len(matches) != 1:
            raise DiscoveryDiagnostic(
                "Could not resolve exactly one observation field named "
                f"{DNA_FIELD_NAME!r} with the DNA datatype."
            )
        return matches[0]

    def prove_source_endpoint(
        self, config: ScanConfig, field_id: int
    ) -> str:
        base: list[tuple[str, Any]] = list(canonical_source_params(config.source_params))
        # v2 does not currently document a positive field filter. Probe the
        # plausible field_id form, but accept it only if every returned row
        # itself proves the resolved field is present.
        try:
            probe = self.client.get_dna_linking_observations_v2(
                [*base, ("field_id", int(field_id)), ("order_by", "id"), ("order", "asc")],
                page=1,
            )
        except INatAPIError:
            probe = {}
        if _page_proves_field(probe, field_id):
            return "v2"
        v1_params: list[tuple[str, Any]] = list(base)
        v1_params.extend(
            [(f"field:{DNA_FIELD_NAME}", ""), ("order_by", "id"), ("order", "asc")]
        )
        fallback = self.client.get_observations(v1_params, page=1, per_page=200)
        if _page_proves_field(fallback, field_id):
            return "v1"
        if not _page_results(fallback):
            # An empty page is not evidence that filtering is broken: the narrowing
            # filter simply matched no DNA-barcoded observations.
            raise DiscoveryDiagnostic(
                "No observations carrying the DNA barcode field matched the supplied "
                "filter, so there is nothing to scan. Widen the search URL and try again."
            )
        raise DiscoveryDiagnostic(
            "iNaturalist did not prove that positive DNA-field filtering is honored "
            "by either observations endpoint. The scan was blocked; no global Fungi "
            "fallback was attempted."
        )

    def discover_chunk(
        self, *, session_id: int, config: ScanConfig, field_id: int,
        endpoint: str, is_cancelled: Callable[[], bool] = lambda: False,
        progress: Callable[[DiscoveryProgress], None] = lambda _p: None,
    ) -> int:
        session = self.db.session(session_id)
        cursor = int(session["source_cursor"])
        source_rows = self._load_source_chunk(
            config, field_id, endpoint, cursor, is_cancelled, progress
        )
        if not source_rows:
            return 0

        calls_at_start = self.client.call_count
        estimated = max(1, len(source_rows) * 2)
        window_seconds = float(config.window_minutes) * 60.0
        valid_count = 0
        pair_count = 0

        # Sources are completed and committed strictly in ID order. Review is
        # still delayed until the entire chunk finishes, but cancellation while
        # processing a later source preserves every earlier atomic source result.
        for source_index, raw_source in enumerate(source_rows, start=1):
            if is_cancelled():
                raise DiscoveryCancelled()
            source_id = int(raw_source.get("id") or 0)
            field_rows = extract_observation_field_rows(raw_source, field_id)
            values = [row.get("value") for row in field_rows]
            source = self._snapshot(raw_source)
            if source is None or not any(
                contains_qualifying_dna(value) for value in values
            ):
                if is_cancelled():
                    raise DiscoveryCancelled()
                if source_id:
                    self.db.commit_source(session_id, source_id, ())
                progress(DiscoveryProgress(
                    "Skipped an ineligible field-bearing source and saved its cursor",
                    self.client.call_count - calls_at_start, estimated,
                    source_index, valid_count, pair_count,
                ))
                continue

            candidate_login = config.candidate_login.strip().casefold()
            if not source.observer or (
                candidate_login and source.observer.casefold() == candidate_login
            ):
                if is_cancelled():
                    raise DiscoveryCancelled()
                self.db.commit_source(session_id, source_id, ())
                progress(DiscoveryProgress(
                    "Skipped a DNA source not proven to belong to another observer",
                    self.client.call_count - calls_at_start, estimated,
                    source_index, valid_count, pair_count,
                ))
                continue

            valid_count += 1
            candidate_ids: list[int] = []
            raw_candidates: dict[int, Mapping[str, Any]] = {}
            page = 1
            source_time = _parse_time(source.observed_at)
            start = source_time - timedelta(seconds=window_seconds)
            end = source_time + timedelta(seconds=window_seconds)
            while True:
                if is_cancelled():
                    raise DiscoveryCancelled()
                params = {
                    "taxon_id": FUNGI_TAXON_ID,
                    "photos": "true",
                    "geo": "true",
                    "geoprivacy": "open",
                    "lat": source.latitude,
                    "lng": source.longitude,
                    "radius": float(config.radius_m) / 1000.0,
                    # The observations API documents these as calendar-date
                    # bounds. Query the encompassing dates and enforce the exact
                    # inclusive timestamp window locally below.
                    # Pad by one date on either side because `time_observed_at`
                    # is normalized to UTC while the API's observed-date index
                    # can reflect the observation's local calendar date.
                    "d1": (start - timedelta(days=1)).date().isoformat(),
                    "d2": (end + timedelta(days=1)).date().isoformat(),
                    "verifiable": "any",
                    "order_by": "id",
                    "order": "asc",
                }
                if config.candidate_login:
                    params["user_login"] = config.candidate_login
                raw_page = self.client.get_observations(params, page=page, per_page=200)
                results = [r for r in raw_page.get("results") or [] if isinstance(r, Mapping)]
                for raw_candidate in results:
                    try:
                        candidate_id = int(raw_candidate.get("id") or 0)
                    except (TypeError, ValueError):
                        continue
                    raw_user = raw_candidate.get("user")
                    raw_login = (
                        str(raw_user.get("login") or "")
                        if isinstance(raw_user, Mapping)
                        else ""
                    )
                    # A DNA link is evidence transferred between different
                    # observers' records, not a same-observer duplicate finder.
                    # Require both identities to be present and unequal even
                    # though candidate results are hydrated again below.
                    if (
                        not raw_login
                        or raw_login.casefold() == source.observer.casefold()
                    ):
                        continue
                    if candidate_login and raw_login.casefold() != candidate_login:
                        continue
                    # Unconditional local self-exclusion. No server exclusion is
                    # sent because none is documented/proven for this endpoint.
                    if candidate_id and candidate_id != source.observation_id:
                        candidate_ids.append(candidate_id)
                        raw_candidates[candidate_id] = raw_candidate
                total = int(raw_page.get("total_results") or len(results))
                if page * 200 >= total or len(results) < 200:
                    break
                page += 1
                estimated += 1

            calls_so_far = self.client.call_count - calls_at_start
            estimated = max(
                estimated,
                calls_so_far + (len(source_rows) - source_index)
                + math.ceil(len(raw_candidates) / 30) + len(raw_candidates),
            )
            progress(DiscoveryProgress(
                "Searching nearby observations", self.client.call_count - calls_at_start,
                estimated, source_index, valid_count, pair_count,
            ))

            # Hydrate and score this source completely before advancing its
            # cursor. These are still public unauthenticated reads.
            ids = sorted(raw_candidates)
            hydrated: dict[int, Mapping[str, Any]] = {}
            for start_index in range(0, len(ids), 30):
                if is_cancelled():
                    raise DiscoveryCancelled()
                response = self.client.get_observations_by_ids(
                    ids[start_index : start_index + 30]
                )
                for item in response.get("results") or []:
                    if isinstance(item, Mapping):
                        hydrated[int(item.get("id") or 0)] = item
                progress(DiscoveryProgress(
                    "Hydrating candidates for the current source",
                    self.client.call_count - calls_at_start, estimated,
                    source_index, valid_count, pair_count,
                ))

            candidate_snapshots: dict[int, ObservationSnapshot] = {}
            for candidate_id in ids:
                if is_cancelled():
                    raise DiscoveryCancelled()
                snapshot = self._snapshot(
                    hydrated.get(candidate_id) or raw_candidates[candidate_id]
                )
                if snapshot is not None:
                    candidate_snapshots[candidate_id] = snapshot

            pairs: list[CandidatePair] = []
            for candidate_id in sorted(set(candidate_ids)):
                if candidate_id == source.observation_id:
                    continue
                candidate = candidate_snapshots.get(candidate_id)
                if candidate is None:
                    continue
                if (
                    not candidate.observer
                    or candidate.observer.casefold() == source.observer.casefold()
                ):
                    continue
                if (
                    candidate_login
                    and candidate.observer.casefold() != candidate_login
                ):
                    continue
                distance = haversine_m(
                    source.latitude, source.longitude,
                    candidate.latitude, candidate.longitude,
                )
                difference = abs((_parse_time(candidate.observed_at) - source_time).total_seconds())
                if distance > float(config.radius_m) or difference > window_seconds:
                    continue
                same_family = (
                    source.family_id is not None
                    and candidate.family_id is not None
                    and source.family_id == candidate.family_id
                )
                ds, ts, fs, score = score_pair(
                    distance, difference, float(config.radius_m), window_seconds, same_family
                )
                pairs.append(CandidatePair(
                    source, candidate, distance, difference, ds, ts, fs, score
                ))
            pairs.sort(key=lambda pair: (
                -pair.score, pair.distance_m, pair.time_difference_seconds,
                pair.source.observation_id, pair.candidate.observation_id,
            ))
            if is_cancelled():
                raise DiscoveryCancelled()
            self.db.commit_source(session_id, source.observation_id, pairs)
            pair_count += len(pairs)
            progress(DiscoveryProgress(
                "Saved one fully discovered source",
                self.client.call_count - calls_at_start,
                max(estimated, self.client.call_count - calls_at_start),
                source_index, valid_count, pair_count,
            ))

        progress(DiscoveryProgress(
            "Chunk ready for review", self.client.call_count - calls_at_start,
            max(estimated, self.client.call_count - calls_at_start),
            len(source_rows), valid_count, pair_count,
        ))
        return pair_count

    def _load_source_chunk(
        self, config: ScanConfig, field_id: int, endpoint: str, cursor: int,
        is_cancelled: Callable[[], bool], progress: Callable[[DiscoveryProgress], None],
    ) -> list[Mapping[str, Any]]:
        base: list[tuple[str, Any]] = list(canonical_source_params(config.source_params))
        base.extend((("id_above", int(cursor)), ("order_by", "id"), ("order", "asc")))
        if endpoint == "v1":
            base.append((f"field:{DNA_FIELD_NAME}", ""))
        elif endpoint == "v2":
            base.append(("field_id", int(field_id)))
        else:
            raise DiscoveryDiagnostic(f"Unknown proven source endpoint: {endpoint}")
        rows: list[Mapping[str, Any]] = []
        page = 1
        while len(rows) < int(config.chunk_size):
            if is_cancelled():
                raise DiscoveryCancelled()
            raw = (
                self.client.get_dna_linking_observations_v2(base, page=page)
                if endpoint == "v2"
                else self.client.get_observations(base, page=page, per_page=200)
            )
            results = [item for item in raw.get("results") or [] if isinstance(item, Mapping)]
            if results and not all(extract_observation_field_rows(item, field_id) for item in results):
                raise DiscoveryDiagnostic(
                    "The proven source filter stopped being honored: a returned source "
                    "page lacked the resolved DNA field. Scanning stopped safely."
                )
            rows.extend(results[: max(0, int(config.chunk_size) - len(rows))])
            progress(DiscoveryProgress(
                "Reading DNA-bearing source rows (lowest observation ID first)",
                page, max(1, math.ceil(int(config.chunk_size) / 200)),
                len(rows), 0, 0,
            ))
            total = int(raw.get("total_results") or len(results))
            if not results or page * 200 >= total or len(results) < 200:
                break
            page += 1
        return rows

    def _snapshot(self, raw: Mapping[str, Any]) -> Optional[ObservationSnapshot]:
        try:
            if not _has_unobscured_public_coordinates(raw):
                return None
            observation_id = int(raw.get("id") or 0)
            uuid = str(raw.get("uuid") or "")
            observed_at = str(raw.get("time_observed_at") or "")
            point = raw.get("geojson")
            coordinates = point.get("coordinates") if isinstance(point, Mapping) else None
            if (
                not observation_id or not observed_at or
                not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2
            ):
                return None
            longitude, latitude = float(coordinates[0]), float(coordinates[1])
            if (
                not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90.0 <= latitude <= 90.0
                or not -180.0 <= longitude <= 180.0
            ):
                return None
            _parse_time(observed_at)
            taxon = raw.get("taxon")
            photos = raw.get("photos") or []
            if not photos:
                photos = [
                    row.get("photo") for row in raw.get("observation_photos") or []
                    if isinstance(row, Mapping) and isinstance(row.get("photo"), Mapping)
                ]
            if not isinstance(taxon, Mapping) or not photos or not _taxon_in_fungi(taxon):
                return None
            family_id, family_name = self._resolve_family(taxon)
            user = raw.get("user")
            urls = tuple(
                str(photo.get("url") or "") for photo in photos
                if isinstance(photo, Mapping) and photo.get("url")
            )
            accuracy_raw = raw.get("positional_accuracy")
            accuracy = float(accuracy_raw) if accuracy_raw not in (None, "") else None
            return ObservationSnapshot(
                observation_id=observation_id, uuid=uuid,
                observer=str(user.get("login") or "") if isinstance(user, Mapping) else "",
                observed_at=observed_at, latitude=latitude, longitude=longitude,
                positional_accuracy=accuracy, taxon_id=int(taxon.get("id") or 0),
                taxon_name=str(taxon.get("name") or ""),
                taxon_rank=str(taxon.get("rank") or ""), family_id=family_id,
                family_name=family_name, photo_urls=urls,
            )
        except (TypeError, ValueError, OverflowError):
            return None

    def _resolve_family(self, taxon: Mapping[str, Any]) -> tuple[Optional[int], str]:
        taxon_id = int(taxon.get("id") or 0)
        if taxon_id in self._family_cache:
            return self._family_cache[taxon_id]
        if str(taxon.get("rank") or "").casefold() == "family":
            result = (taxon_id, str(taxon.get("name") or ""))
            self._family_cache[taxon_id] = result
            return result
        for ancestor in taxon.get("ancestors") or []:
            if isinstance(ancestor, Mapping) and str(ancestor.get("rank") or "").casefold() == "family":
                result = (int(ancestor.get("id") or 0), str(ancestor.get("name") or ""))
                self._family_cache[taxon_id] = result
                return result
        # Index responses often contain ancestry IDs without ranks. Resolve
        # only those ancestors, cache them, and keep above-family unresolved.
        ancestry = [part for part in str(taxon.get("ancestry") or "").split("/") if part.isdigit()]
        for ancestor_id_text in reversed(ancestry):
            ancestor_id = int(ancestor_id_text)
            cached = self._family_cache.get(ancestor_id)
            if cached and cached[0] is not None:
                self._family_cache[taxon_id] = cached
                return cached
            response = self.client.get_taxon_by_id(ancestor_id)
            results = response.get("results") or []
            ancestor = results[0] if results and isinstance(results[0], Mapping) else None
            if ancestor and str(ancestor.get("rank") or "").casefold() == "family":
                result = (int(ancestor.get("id") or 0), str(ancestor.get("name") or ""))
                self._family_cache[ancestor_id] = result
                self._family_cache[taxon_id] = result
                return result
        self._family_cache[taxon_id] = (None, "")
        return None, ""


def _page_results(raw: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for item in raw.get("results") or [] if isinstance(item, Mapping)]


def _page_proves_field(raw: Mapping[str, Any], field_id: int) -> bool:
    results = _page_results(raw)
    return bool(results) and all(extract_observation_field_rows(item, field_id) for item in results)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("exact observation timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _taxon_in_fungi(taxon: Mapping[str, Any]) -> bool:
    if int(taxon.get("id") or 0) == FUNGI_TAXON_ID:
        return True
    iconic = str(taxon.get("iconic_taxon_name") or "").casefold()
    ancestry = {part for part in str(taxon.get("ancestry") or "").split("/")}
    return iconic == "fungi" or str(FUNGI_TAXON_ID) in ancestry


def _has_unobscured_public_coordinates(raw: Mapping[str, Any]) -> bool:
    """Enforce the same local public-coordinate invariant for every side."""
    if bool(raw.get("obscured")):
        return False
    privacy = str(raw.get("geoprivacy") or "").casefold()
    taxon_privacy = str(raw.get("taxon_geoprivacy") or "").casefold()
    return privacy in {"", "open"} and taxon_privacy in {"", "open"}
