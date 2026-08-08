"""
iNaturalist API v1 client.

Rate limiting: 1 request/second (iNat recommends ~100/min).
Retry: exponential backoff on 429/503.
All methods are synchronous — designed to run inside QRunnable workers.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.inaturalist.org/v1"
V2_BASE_URL = "https://api.inaturalist.org/v2"
WWW_BASE_URL = "https://www.inaturalist.org"
DEFAULT_TIMEOUT = 20.0  # seconds
MAX_RETRIES = 4
MAX_OBSERVATION_DETAIL_IDS = 30

QueryParams = Union[Dict[str, Any], Sequence[Tuple[str, Any]]]

# v2 responses are intentionally minimal unless fields are requested.  This
# RISON expression mirrors the fields the live v2 serializers expose.  The
# OpenAPI User schema advertises ``uuid``, but the user serializers currently
# omit it even for ``fields=all``.  Numeric user IDs and logins are therefore
# the stable account identity fields available to this client.
V2_CURRENT_USER_FIELDS = "(id:!t,login:!t)"
V2_OBSERVATION_IDENTITY_FIELDS = "(id:!t,uuid:!t)"
V2_RECONCILIATION_INVENTORY_FIELDS = (
    "(id:!t,uuid:!t,observed_on:!t,updated_at:!t,place_guess:!t,"
    "user:(id:!t,login:!t),taxon:(id:!t,name:!t,rank:!t,ancestry:!t,iconic_taxon_name:!t),"
    "ofvs:(id:!t,value:!t,field_id:!t,"
    "observation_field:(id:!t,name:!t,datatype:!t)),"
    "observation_photos:(photo:(id:!t)))"
)
V2_RECONCILIATION_VALIDATION_FIELDS = (
    "(id:!t,uuid:!t,observed_on:!t,updated_at:!t,place_guess:!t,positional_accuracy:!t,"
    "geojson:!t,private_geojson:!t,geoprivacy:!t,taxon_geoprivacy:!t,user:(id:!t,login:!t),"
    "taxon:(id:!t,name:!t,rank:!t,ancestry:!t,iconic_taxon_name:!t),"
    "ofvs:(id:!t,uuid:!t,value:!t,field_id:!t,user:(id:!t,login:!t),"
    "observation_field:(id:!t,name:!t,datatype:!t)),"
    "observation_photos:(photo:(id:!t)))"
)
V2_RECONCILIATION_CONTEXT_FIELDS = (
    "(id:!t,uuid:!t,observed_on:!t,updated_at:!t,place_guess:!t,"
    "user:(id:!t,login:!t),"
    "taxon:(id:!t,name:!t,rank:!t,ancestry:!t,iconic_taxon_name:!t),"
    "ofvs:(id:!t,value:!t,field_id:!t,"
    "observation_field:(id:!t,name:!t,datatype:!t)),"
    "observation_photos:(photo:(id:!t)))"
)
V2_RECONCILIATION_DEEP_FIELDS = (
    "(id:!t,uuid:!t,observed_on:!t,updated_at:!t,place_guess:!t,positional_accuracy:!t,"
    "geojson:!t,private_geojson:!t,geoprivacy:!t,taxon_geoprivacy:!t,description:!t,user:(id:!t,login:!t),"
    "taxon:(id:!t,name:!t,rank:!t,ancestry:!t,iconic_taxon_name:!t),"
    "ofvs:(id:!t,uuid:!t,value:!t,field_id:!t,user:(id:!t,login:!t),"
    "observation_field:(id:!t,name:!t,datatype:!t)),"
    "comments:(id:!t,body:!t,user:(id:!t,login:!t),created_at:!t),"
    "observation_photos:(photo:(id:!t,url:!t)))"
)
# Gate 1E photo transfer. ``license_code`` is requested for display only -- the
# uploaded photo takes the account default and is never re-licensed by this app.
V2_OBSERVATION_PHOTO_FIELDS = (
    "(id:!t,uuid:!t,position:!t," "photo:(id:!t,license_code:!t,attribution:!t,url:!t))"
)
V2_OBSERVATION_PHOTOS_FIELDS = (
    f"(id:!t,uuid:!t,observation_photos:{V2_OBSERVATION_PHOTO_FIELDS})"
)
V2_OBSERVATION_VERIFICATION_FIELDS = (
    "(id:!t,uuid:!t,reviewed_by:!t,"
    "faves:(user:(id:!t,login:!t)),"
    "identifications:(id:!t,uuid:!t,"
    "user:(id:!t,login:!t),taxon_id:!t,taxon:(id:!t),"
    "body:!t,current:!t,created_at:!t),"
    "comments:(id:!t,uuid:!t,"
    "user:(id:!t,login:!t),body:!t,created_at:!t,hidden:!t),"
    "quality_metrics:(id:!t,metric:!t,agree:!t,"
    "user:(id:!t,login:!t)))"
)

# The Captive/Cultivated gate supports only the "wild" Data Quality
# Assessment metric, voted on via the documented tri-state operation below.
_SUPPORTED_QUALITY_METRICS = frozenset({"wild"})
_SUPPORTED_QUALITY_METRIC_VOTES = frozenset({"agree", "disagree", "remove"})


class INatAPIError(RuntimeError):
    """Machine-readable error from an iNaturalist API request.

    ``outcome_unknown`` is meaningful only for unsafe requests.  Callers must
    use this field instead of inferring write safety from an exception string.
    """

    def __init__(
        self,
        message: str,
        *,
        endpoint: str = "",
        status_code: Optional[int] = None,
        response_body: str = "",
        request_phase: str = "safe_read",
        response_received: bool = False,
        outcome_unknown: bool = False,
        method: str = "",
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code
        self.response_body = response_body
        self.request_phase = request_phase
        self.response_received = response_received
        self.outcome_unknown = outcome_unknown
        self.method = method


class UnsafeWriteOutcomeUnknown(INatAPIError):
    """An unsafe HTTP request began but its result cannot be known safely."""

    def __init__(self, *, endpoint: str, method: str, cause: Exception) -> None:
        super().__init__(
            f"{method} {endpoint} failed after it may have reached iNaturalist: "
            f"{type(cause).__name__}. The result is unknown.",
            endpoint=endpoint,
            request_phase="unsafe_write",
            response_received=False,
            outcome_unknown=True,
            method=method,
        )
        self.__cause__ = cause


@dataclass(frozen=True)
class V2ResponseMetadata:
    """Non-sensitive metadata about a successful v2 response."""

    endpoint: str
    method: str
    status_code: int


class V2Response(dict):
    """Dictionary response with non-payload metadata for durable journaling."""

    def __init__(self, data: Dict[str, Any], metadata: V2ResponseMetadata) -> None:
        super().__init__(data)
        self.metadata = metadata


class RateLimiter:
    """Simple token-bucket-ish rate limiter (thread-safe)."""

    def __init__(self, calls_per_second: float = 1.0) -> None:
        self._min_interval = 1.0 / max(calls_per_second, 0.01)
        self._last_call = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait_s = self._min_interval - (now - self._last_call)
            if wait_s > 0:
                time.sleep(wait_s)
            self._last_call = time.monotonic()


class INatClient:
    """Synchronous iNaturalist API v1 client."""

    def __init__(
        self,
        calls_per_second: float = 1.0,
        on_rate_limited: Optional[Callable[[int, float], None]] = None,
    ) -> None:
        self._rate = RateLimiter(calls_per_second)
        self.on_rate_limited = on_rate_limited
        self._call_count = 0
        self._call_count_lock = threading.Lock()
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": "Observation-Workbench/1.0"},
            follow_redirects=True,
        )

    @property
    def call_count(self) -> int:
        with self._call_count_lock:
            return self._call_count

    def _increment_call_count(self) -> None:
        with self._call_count_lock:
            self._call_count += 1

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[QueryParams] = None) -> Dict:
        """GET with rate limiting and exponential-backoff retry."""
        params = _clean_params(params)
        for attempt in range(MAX_RETRIES):
            self._rate.wait()
            try:
                # Query parameters can contain observation-field values and
                # other sensitive filters. Endpoint-only logging is sufficient.
                log.debug("GET %s", path)
                self._increment_call_count()
                resp = self._client.get(path, params=params)
                if resp.status_code in (429, 503):
                    wait_s = min(2**attempt * 2, 60)
                    log.warning(
                        "Rate limited (%s) on %s, waiting %ss",
                        resp.status_code,
                        path,
                        wait_s,
                    )
                    if self.on_rate_limited:
                        self.on_rate_limited(resp.status_code, wait_s)
                    time.sleep(wait_s)
                    continue
                _raise_for_status(resp, path)
                if resp.status_code == 204:
                    return {}
                return resp.json()
            except httpx.TimeoutException as exc:
                if attempt == MAX_RETRIES - 1:
                    # Normalise to INatAPIError like every other transport in
                    # this class. Callers guard on INatAPIError (and only that);
                    # a raw httpx exception escaping here sailed past their
                    # handlers and left journalled actions stuck in 'running'.
                    raise INatAPIError(
                        f"Timed out contacting iNaturalist after {MAX_RETRIES} attempts.",
                        endpoint=path,
                    ) from exc
                wait_s = 2**attempt
                log.warning(
                    "Timeout on %s (attempt %d), retrying in %ss", path, attempt, wait_s
                )
                time.sleep(wait_s)
            except httpx.TransportError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise INatAPIError(
                        f"Network error contacting iNaturalist after {MAX_RETRIES} attempts.",
                        endpoint=path,
                    ) from exc
                wait_s = 2**attempt
                log.warning(
                    "Network error on %s (attempt %d): %s; retrying in %ss",
                    path,
                    attempt,
                    type(exc).__name__,
                    wait_s,
                )
                time.sleep(wait_s)
            except httpx.HTTPStatusError as exc:
                err_msg = f"iNaturalist returned HTTP {exc.response.status_code}"
                log.error("HTTP error on %s: status=%s", path, exc.response.status_code)
                raise INatAPIError(
                    err_msg,
                    endpoint=path,
                    status_code=exc.response.status_code,
                ) from exc
        # Reached only when every attempt was consumed by a 429/503 `continue`.
        # Must be an INatAPIError for the same reason as the timeout above.
        raise INatAPIError(
            f"iNaturalist rate-limited this request for all {MAX_RETRIES} attempts.",
            endpoint=path,
            status_code=429,
        )

    def _request_auth(
        self,
        method: str,
        path: str,
        api_token: str,
        *,
        params: Optional[QueryParams] = None,
        data: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        """Authenticated request; retry only methods that are safe to repeat."""
        if data is not None and json is not None:
            raise ValueError("Cannot specify both data and json parameters")
        method_upper = method.upper()
        request_phase = (
            "safe_read" if method_upper in {"GET", "HEAD"} else "unsafe_write"
        )
        if not api_token:
            raise INatAPIError(
                "Missing iNaturalist API token",
                endpoint=path,
                request_phase=request_phase,
                method=method_upper,
            )
        retry_safe = method_upper in {"GET", "HEAD"}
        max_attempts = MAX_RETRIES if retry_safe else 1
        headers = {"Authorization": api_token.strip()}
        params = _clean_params(params)
        for attempt in range(max_attempts):
            self._rate.wait()
            try:
                # Do not log request bodies: they can contain comments and
                # identification text, while auth headers contain credentials.
                log.debug("%s %s", method_upper, path)
                self._increment_call_count()
                resp = self._client.request(
                    method_upper,
                    path,
                    params=params,
                    data=data,
                    json=json,
                    headers=headers,
                )
                if retry_safe and resp.status_code in (429, 503):
                    wait_s = min(2**attempt * 2, 60)
                    log.warning(
                        "Rate limited (%s) on %s, waiting %ss",
                        resp.status_code,
                        path,
                        wait_s,
                    )
                    if self.on_rate_limited:
                        self.on_rate_limited(resp.status_code, wait_s)
                    time.sleep(wait_s)
                    continue
                _raise_for_status(resp, path)
                if not resp.content:
                    return {}
                return resp.json()
            except httpx.TimeoutException as exc:
                if not retry_safe:
                    raise UnsafeWriteOutcomeUnknown(
                        endpoint=path, method=method_upper, cause=exc
                    ) from exc
                if attempt == max_attempts - 1:
                    raise INatAPIError(
                        f"Timed out contacting iNaturalist after {max_attempts} attempts.",
                        endpoint=path,
                        request_phase=request_phase,
                        method=method_upper,
                    ) from exc
                wait_s = 2**attempt
                log.warning(
                    "Timeout on authenticated %s %s (attempt %d), retrying in %ss",
                    method_upper,
                    path,
                    attempt,
                    wait_s,
                )
                time.sleep(wait_s)
            except httpx.TransportError as exc:
                if not retry_safe:
                    raise UnsafeWriteOutcomeUnknown(
                        endpoint=path, method=method_upper, cause=exc
                    ) from exc
                if attempt == max_attempts - 1:
                    raise INatAPIError(
                        f"Network error contacting iNaturalist after {MAX_RETRIES} attempts: {exc}",
                        endpoint=path,
                    ) from exc
                wait_s = 2**attempt
                log.warning(
                    "Network error on authenticated %s %s (attempt %d): %s; retrying in %ss",
                    method_upper,
                    path,
                    attempt,
                    exc,
                    wait_s,
                )
                time.sleep(wait_s)
            except httpx.HTTPStatusError as exc:
                msg = _extract_error_message(exc.response)
                raise INatAPIError(
                    msg,
                    endpoint=path,
                    status_code=exc.response.status_code,
                    response_body=exc.response.text,
                    request_phase=request_phase,
                    response_received=True,
                    method=method_upper,
                ) from exc
        raise INatAPIError(f"Max retries exceeded for {path}", endpoint=path)

    def _request_v2_auth(
        self,
        method: str,
        path: str,
        jwt: str,
        *,
        params: Optional[QueryParams] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
    ) -> V2Response:
        """Authenticated v2 request using iNaturalist's JWT header convention.

        This deliberately does not reuse the v1 helper: established v1 calls
        retain their existing authentication convention, while every v2 JWT
        request sends the bare token as the ``Authorization`` header value.

        iNaturalist documents this as an ``apiKey`` header (rather than an
        HTTP bearer-auth scheme), so prefixing the token with ``Bearer`` makes
        otherwise valid v2 requests unauthenticated.
        """
        token = _normalise_jwt(jwt)
        method_upper = method.upper()
        request_phase = (
            "safe_read" if method_upper in {"GET", "HEAD"} else "unsafe_write"
        )
        endpoint = _normalise_v2_path(path)
        if not token:
            raise INatAPIError(
                "Missing iNaturalist JWT",
                endpoint=endpoint,
                request_phase=request_phase,
                method=method_upper,
            )
        return self._request_v2(
            method_upper,
            endpoint,
            params=params,
            json=json,
            data=data,
            files=files,
            headers={"Authorization": token},
        )

    def _request_v2(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[QueryParams] = None,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
    ) -> V2Response:
        """Narrow v2 request path with safe-read-only retries.

        A timeout or transport interruption for POST/PUT/DELETE is represented
        by :class:`UnsafeWriteOutcomeUnknown`; it is never retried here.

        ``data``/``files`` carry a multipart body (photo upload). They are only
        ever used by unsafe writes, which take the single-attempt path, so the
        body is never re-sent after a partial transmission.
        """
        method_upper = method.upper()
        endpoint = _normalise_v2_path(endpoint)
        retry_safe = method_upper in {"GET", "HEAD"}
        request_phase = "safe_read" if retry_safe else "unsafe_write"
        max_attempts = MAX_RETRIES if retry_safe else 1
        clean_params = _clean_params(params)
        log_endpoint = _fixed_endpoint_name(endpoint)

        for attempt in range(max_attempts):
            self._rate.wait()
            try:
                # Never include headers, JWTs, or write payloads in logs.
                log.debug("v2 %s %s", method_upper, log_endpoint)
                self._increment_call_count()
                response = self._client.request(
                    method_upper,
                    endpoint,
                    params=clean_params,
                    json=json,
                    data=data,
                    files=files,
                    headers=headers,
                )
                if retry_safe and response.status_code in (429, 503):
                    wait_s = min(2**attempt * 2, 60)
                    log.warning(
                        "Rate limited (%s) on v2 %s; waiting %ss",
                        response.status_code,
                        log_endpoint,
                        wait_s,
                    )
                    if self.on_rate_limited:
                        self.on_rate_limited(response.status_code, wait_s)
                    time.sleep(wait_s)
                    continue

                if response.is_error:
                    outcome_unknown = not retry_safe and response.status_code not in {
                        400,
                        401,
                        403,
                        404,
                        409,
                        422,
                    }
                    raise INatAPIError(
                        f"iNaturalist returned HTTP {response.status_code}",
                        endpoint=endpoint,
                        status_code=response.status_code,
                        request_phase=request_phase,
                        response_received=True,
                        outcome_unknown=outcome_unknown,
                        method=method_upper,
                    )

                metadata = V2ResponseMetadata(
                    endpoint=endpoint,
                    method=method_upper,
                    status_code=response.status_code,
                )
                if response.status_code == 204 or not response.content:
                    return V2Response({}, metadata)
                # Deliberately NOT named `data`: that is this function's own
                # multipart-body parameter, and rebinding it inside the retry
                # loop would re-send the parsed response as the request body the
                # moment writes ever become retryable.
                try:
                    payload = response.json()
                except ValueError:
                    # A successful write response may be empty or malformed;
                    # keep its received status and let safe verification decide.
                    log.debug(
                        "v2 %s %s returned a non-JSON success response",
                        method_upper,
                        log_endpoint,
                    )
                    payload = {}
                if not isinstance(payload, dict):
                    log.debug(
                        "v2 %s %s returned JSON type %s instead of an object",
                        method_upper,
                        log_endpoint,
                        type(payload).__name__,
                    )
                    payload = {}
                results = payload.get("results")
                result_count = len(results) if isinstance(results, list) else None
                log.debug(
                    "v2 %s %s -> HTTP %s, top-level keys=%s, results=%s",
                    method_upper,
                    log_endpoint,
                    response.status_code,
                    sorted(str(key) for key in payload)[:12],
                    result_count if result_count is not None else "not-a-list",
                )
                return V2Response(payload, metadata)
            except httpx.TimeoutException as exc:
                if not retry_safe:
                    raise UnsafeWriteOutcomeUnknown(
                        endpoint=endpoint, method=method_upper, cause=exc
                    ) from exc
                if attempt == max_attempts - 1:
                    raise INatAPIError(
                        "Timed out reading from iNaturalist.",
                        endpoint=endpoint,
                        request_phase=request_phase,
                        method=method_upper,
                    ) from exc
                time.sleep(2**attempt)
            except httpx.TransportError as exc:
                if not retry_safe:
                    raise UnsafeWriteOutcomeUnknown(
                        endpoint=endpoint, method=method_upper, cause=exc
                    ) from exc
                if attempt == max_attempts - 1:
                    raise INatAPIError(
                        "Network error reading from iNaturalist.",
                        endpoint=endpoint,
                        request_phase=request_phase,
                        method=method_upper,
                    ) from exc
                time.sleep(2**attempt)

        raise INatAPIError(
            "Safe iNaturalist read exhausted its retry budget.",
            endpoint=endpoint,
            request_phase=request_phase,
            method=method_upper,
        )

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def get_observations(
        self,
        query_params: QueryParams,
        page: int = 1,
        per_page: int = 200,
        api_token: str = "",
    ) -> Dict:
        """Fetch observations, optionally authenticated (never cached by callers)."""
        params = _with_pagination(query_params, page=page, per_page=per_page)
        if api_token:
            return self._request_auth("GET", "/observations", api_token, params=params)
        return self._get("/observations", params)

    def get_observation_by_id(
        self,
        observation_id: int,
        api_token: str = "",
    ) -> Dict:
        """Fetch full details for one observation, optionally authenticated."""
        if api_token:
            return self._request_auth(
                "GET", f"/observations/{int(observation_id)}", api_token
            )
        return self._get(f"/observations/{int(observation_id)}")

    def get_observations_by_ids(
        self,
        observation_ids: Sequence[int],
        api_token: str = "",
    ) -> Dict:
        """Fetch full details for multiple observations in one request."""
        ids = [int(obs_id) for obs_id in observation_ids]
        if not ids:
            return {"results": []}

        results = []
        for start in range(0, len(ids), MAX_OBSERVATION_DETAIL_IDS):
            batch_ids = ",".join(
                str(obs_id)
                for obs_id in ids[start : start + MAX_OBSERVATION_DETAIL_IDS]
            )
            path = f"/observations/{batch_ids}"
            raw = (
                self._request_auth("GET", path, api_token)
                if api_token
                else self._get(path)
            )
            results.extend(raw.get("results") or [])
        return {"total_results": len(results), "results": results}

    def get_current_user(self, api_token: str) -> Dict:
        """Fetch the currently authenticated iNaturalist user."""
        return self._request_auth("GET", "/users/me", api_token)

    def create_identification(
        self,
        api_token: str,
        observation_id: int,
        taxon_id: int,
        body: str = "",
        disagreement: Optional[bool] = None,
    ) -> Dict:
        """Create an identification for an observation."""
        payload: Dict[str, Any] = {
            "identification": {
                "observation_id": int(observation_id),
                "taxon_id": int(taxon_id),
            }
        }
        if body:
            payload["identification"]["body"] = body
        if disagreement is not None:
            payload["identification"]["disagreement"] = bool(disagreement)
        return self._request_auth("POST", "/identifications", api_token, json=payload)

    # Gate 2 writes intentionally use the documented v2 API without changing
    # the established v1 browsing path above.
    def get_current_user_v2(self, api_token: str) -> V2Response:
        """Fetch the authenticated v2 account with only identity fields."""
        return self._request_v2_auth(
            "GET",
            "/users/me",
            api_token,
            params={"fields": V2_CURRENT_USER_FIELDS},
        )

    def get_observation_v2(self, observation_uuid: str, api_token: str) -> V2Response:
        """Fetch exactly the fields needed for durable action verification."""
        return self._request_v2_auth(
            "GET",
            f"/observations/{observation_uuid}",
            api_token,
            params={"fields": V2_OBSERVATION_VERIFICATION_FIELDS},
        )

    def get_observation_identity_v2(
        self,
        observation_id: int,
        api_token: str = "",
    ) -> V2Response:
        """Safely resolve a numeric observation ID to its required UUID."""
        params = {
            "id": int(observation_id),
            "per_page": 1,
            "fields": V2_OBSERVATION_IDENTITY_FIELDS,
        }
        if api_token:
            return self._request_v2_auth(
                "GET", "/observations", api_token, params=params
            )
        return self._request_v2("GET", "/observations", params=params)

    def get_reconciliation_observations(
        self,
        query_params: QueryParams,
        *,
        page: int = 1,
    ) -> V2Response:
        """Read one explicit-field reconciliation inventory page.

        Gate 1A inventory is public and deliberately does not attach an auth
        header, even when the application has a token.
        """
        params = _with_pagination(query_params, page=page, per_page=200)
        if isinstance(params, dict):
            params["fields"] = V2_RECONCILIATION_INVENTORY_FIELDS
        else:
            params = [*params, ("fields", V2_RECONCILIATION_INVENTORY_FIELDS)]
        return self._request_v2("GET", "/observations", params=params)

    def get_creation_duplicate_search(
        self,
        api_token: str,
        *,
        user_id: int,
        page: int = 1,
        d1: str = "",
        d2: str = "",
        id_above: Optional[int] = None,
    ) -> V2Response:
        """Round-5 finding 1: an AUTHENTICATED, UNFILTERED search of one
        account's observations, purpose-built for Gate 2A's pre-creation
        duplicate check — deliberately distinct from
        ``get_reconciliation_observations`` (Gate 1A's public,
        unauthenticated inventory scan, whose caller filters to
        ``taxon_id=47170`` on the full-baseline branch only — see
        ``INatReconciliationReader.inventory_page``; do not rely on that
        filter being present on every call through
        it). Authenticated so the account's own hidden/private-coordinate
        observations are visible to a search of its own account; carries no
        taxon filter at all, since an existing counterpart may be taxon-less
        or misidentified outside Fungi — taxon must stay a SCORING signal
        for the caller, never an inclusion requirement baked into the query.

        Pass ``d1``/``d2`` (observed-date bounds, ``YYYY-MM-DD``) for a
        bounded date-window search; pass ``id_above`` instead for a
        cursor-paginated full-account scan (mutually exclusive with
        ``d1``/``d2`` — the caller picks one strategy per call).
        """
        params: dict[str, Any] = {
            "user_id": int(user_id),
            "fields": V2_RECONCILIATION_DEEP_FIELDS,
        }
        if id_above is not None:
            params.update({"id_above": int(id_above), "order_by": "id", "order": "asc"})
        else:
            params["order_by"] = "observed_on"
            params["order"] = "desc"
            if d1:
                params["d1"] = d1
            if d2:
                params["d2"] = d2
        paginated = _with_pagination(params, page=page, per_page=200)
        return self._request_v2_auth(
            "GET", "/observations", api_token, params=paginated
        )

    def get_reconciliation_deleted(
        self,
        api_token: str,
        *,
        deleted_since: str,
    ) -> V2Response:
        """Read the authenticated deleted-observation feed."""
        return self._request_v2_auth(
            "GET",
            "/observations/deleted",
            api_token,
            params={
                "since": deleted_since,
                "fields": V2_OBSERVATION_IDENTITY_FIELDS,
            },
        )

    def get_reconciliation_detail(
        self,
        observation_id: int,
        api_token: str = "",
        *,
        deep: bool = True,
    ) -> V2Response:
        """Read one observation with an explicit reconciliation-only field set."""
        params = {
            "id": int(observation_id),
            "per_page": 1,
            "fields": (
                V2_RECONCILIATION_DEEP_FIELDS
                if deep
                else V2_RECONCILIATION_VALIDATION_FIELDS
            ),
        }
        if api_token:
            return self._request_v2_auth(
                "GET", "/observations", api_token, params=params
            )
        return self._request_v2("GET", "/observations", params=params)

    def get_reconciliation_validation(
        self,
        observation_ids: Sequence[int],
        api_token: str = "",
    ) -> V2Response:
        """Read up to 200 observations with the validation field set in ONE call.

        The batched sibling of ``get_reconciliation_detail(deep=False)``. Pair
        validation asks the same question about every reciprocally linked
        record, and one-request-per-observation put that phase behind the 1
        req/sec limiter for as many seconds as the account has pairs (hours,
        for an account with five figures of links). ``id`` accepts a
        comma-separated list on this endpoint, so the whole phase collapses to
        ``ceil(n / 200)`` requests.

        The caller MUST key results off each returned ``id`` rather than
        assuming the response echoes the requested order or length: deleted or
        newly hidden observations are silently omitted from the batch, exactly
        as a single-id read returns no result for them.
        """
        ids = [int(value) for value in observation_ids]
        if not ids:
            # Same trap as get_reconciliation_context: an empty `id` is not a
            # no-op filter, it serves the global observation index.
            return V2Response(
                {"total_results": 0, "results": []},
                V2ResponseMetadata(
                    endpoint="/observations", method="GET", status_code=200
                ),
            )
        params = {
            "id": ",".join(str(value) for value in ids),
            "per_page": min(200, len(ids)),
            "fields": V2_RECONCILIATION_VALIDATION_FIELDS,
        }
        if api_token:
            return self._request_v2_auth(
                "GET", "/observations", api_token, params=params
            )
        return self._request_v2("GET", "/observations", params=params)

    def get_reconciliation_context(
        self,
        observation_ids: Sequence[int],
        api_token: str = "",
    ) -> V2Response:
        """Fetch linked out-of-scope records without broadening normal inventory."""
        ids = [int(value) for value in observation_ids]
        if not ids:
            # An empty `id` string is NOT a no-op filter: iNaturalist ignores it
            # and serves the global observation index, so a caller that forgot
            # to pre-guard would parse a stranger's observation as linked
            # context. Answer locally instead of asking a question that means
            # something entirely different from what was intended.
            return V2Response(
                {"total_results": 0, "results": []},
                V2ResponseMetadata(
                    endpoint="/observations", method="GET", status_code=200
                ),
            )
        params = {
            "id": ",".join(str(value) for value in ids),
            "per_page": min(200, len(ids)),
            "fields": V2_RECONCILIATION_CONTEXT_FIELDS,
        }
        if api_token:
            return self._request_v2_auth(
                "GET", "/observations", api_token, params=params
            )
        return self._request_v2("GET", "/observations", params=params)

    def create_reconciliation_field_value_v2(
        self,
        api_token: str,
        observation_uuid: str,
        observation_field_id: int,
        value: str,
    ) -> V2Response:
        """Create one explicitly confirmed reconciliation field value."""
        return self._request_v2_auth(
            "POST",
            "/observation_field_values",
            api_token,
            json={
                "observation_field_value": {
                    "observation_id": observation_uuid,
                    "observation_field_id": int(observation_field_id),
                    "value": value,
                }
            },
        )

    def update_reconciliation_field_value_v2(
        self,
        api_token: str,
        field_value_uuid: str,
        observation_uuid: str,
        observation_field_id: int,
        value: str,
    ) -> V2Response:
        """Repair one exact iNaturalist observation-field-value UUID."""
        return self._request_v2_auth(
            "PUT",
            f"/observation_field_values/{field_value_uuid}",
            api_token,
            json={
                "observation_field_value": {
                    "observation_id": observation_uuid,
                    "observation_field_id": int(observation_field_id),
                    "value": value,
                }
            },
        )

    def delete_reconciliation_field_value_v2(
        self,
        api_token: str,
        field_value_uuid: str,
    ) -> V2Response:
        """Remove one exact, explicitly reviewed iNaturalist field-value row."""
        return self._request_v2_auth(
            "DELETE",
            f"/observation_field_values/{field_value_uuid}",
            api_token,
        )

    def update_observation_coordinates_v2(
        self,
        api_token: str,
        observation_uuid: str,
        *,
        latitude: float,
        longitude: float,
        positional_accuracy: Optional[float] = None,
        geoprivacy: Optional[str] = None,
    ) -> V2Response:
        """Set one observation's coordinate for a Gate 1D coordinate copy.

        Only the geo attributes are sent so a partial update never disturbs
        photos, taxon, or any other observation data. ``geoprivacy`` is included
        so the destination deliberately preserves an equivalent privacy state.
        """
        observation: Dict[str, Any] = {
            "latitude": float(latitude),
            "longitude": float(longitude),
        }
        if positional_accuracy is not None:
            observation["positional_accuracy"] = int(round(float(positional_accuracy)))
        if geoprivacy is not None:
            observation["geoprivacy"] = geoprivacy
        # Per the bundled ObservationsUpdate schema, ``ignore_photos``, ``fields``,
        # and ``observation`` are JSON body properties, not query parameters.
        # ``ignore_photos`` is a destructive safety control (a false/omitted value
        # can drop photo associations), so it is sent explicitly rather than
        # relying on the server default.
        return self._request_v2_auth(
            "PUT",
            f"/observations/{observation_uuid}",
            api_token,
            json={
                "ignore_photos": True,
                "fields": V2_RECONCILIATION_VALIDATION_FIELDS,
                "observation": observation,
            },
        )

    def create_observation_v2(
        self,
        api_token: str,
        *,
        client_uuid: str,
        species_guess: Optional[str] = None,
        taxon_id: Optional[int] = None,
        observed_on_string: Optional[str] = None,
        place_guess: Optional[str] = None,
        description: Optional[str] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        positional_accuracy: Optional[float] = None,
        geoprivacy: Optional[str] = None,
    ) -> V2Response:
        """Gate 2A: create a brand-new observation (``POST /observations``).

        ``client_uuid`` is MANDATORY, never optional: live-proven 2026-07-23
        (``docs/gate_2a_capability_note.md``) that the ``uuid`` sent here
        becomes the created observation's own uuid exactly, and a same-uuid
        re-POST is de-duplicated rather than creating a second observation —
        this is the idempotency anchor a lost create response is recovered by
        (``GET /observations/{uuid}``), never a blind retry.

        No photos field exists on ``ObservationsCreate`` (confirmed against
        ``api-docs.json``) — attach photos afterward via the existing
        ``create_observation_photo_v2`` once this call's returned uuid is
        known. Never pass ``taxon_geoprivacy``: it is derived from the taxon's
        conservation status and is not a writable field on either create or
        update.
        """
        observation: Dict[str, Any] = {"uuid": client_uuid}
        if species_guess is not None:
            observation["species_guess"] = species_guess
        if taxon_id is not None:
            observation["taxon_id"] = taxon_id
        if observed_on_string is not None:
            observation["observed_on_string"] = observed_on_string
        if place_guess is not None:
            observation["place_guess"] = place_guess
        if description is not None:
            observation["description"] = description
        if latitude is not None:
            observation["latitude"] = float(latitude)
        if longitude is not None:
            observation["longitude"] = float(longitude)
        if positional_accuracy is not None:
            observation["positional_accuracy"] = float(positional_accuracy)
        if geoprivacy is not None:
            observation["geoprivacy"] = geoprivacy
        return self._request_v2_auth(
            "POST",
            "/observations",
            api_token,
            json={
                "fields": V2_RECONCILIATION_VALIDATION_FIELDS,
                "observation": observation,
            },
        )

    def delete_observation_v2(
        self, api_token: str, observation_uuid: str
    ) -> V2Response:
        """Gate 2A: ``DELETE /observations/{uuid}``.

        Live-proven 2026-07-23 that v2 exposes this for the owning user,
        unlike photos (no ``DELETE /photos/{id}`` exists at all, Gate 1E).
        The Phase 2A saga design never calls this automatically on a partial
        failure — a created observation is preserved and repaired, not
        deleted — this method exists for completeness and for tooling
        (e.g. the proof harness's own cleanup), not for saga recovery logic.
        """
        return self._request_v2_auth(
            "DELETE", f"/observations/{observation_uuid}", api_token
        )

    def get_observation_photos_v2(
        self, observation_uuid: str, api_token: str
    ) -> V2Response:
        """Read exactly the observation_photo rows needed to verify a transfer.

        This is the mandatory post-write re-read: a photo transfer is only ever
        marked succeeded when this call proves the attachment exists.
        """
        return self._request_v2_auth(
            "GET",
            f"/observations/{observation_uuid}",
            api_token,
            params={"fields": V2_OBSERVATION_PHOTOS_FIELDS},
        )

    def create_observation_photo_v2(
        self,
        api_token: str,
        observation_uuid: str,
        *,
        image_bytes: bytes,
        filename: str,
        content_type: str,
        client_uuid: str,
    ) -> V2Response:
        """Upload one photo and attach it to an observation in a SINGLE call.

        Gate 1E deliberately uses the multipart ``POST /observation_photos``
        shape rather than ``POST /photos`` followed by a separate attach. The
        two-call path has a failure window in which the photo exists but is
        attached to nothing, and iNaturalist exposes **no ``DELETE /photos/{id}``**
        (proven live, report §2.2/§12), so such a photo can never be cleaned up.
        One call has no separable orphan window.

        ``client_uuid`` becomes the *observation_photo* uuid. That matters: a
        bare photo's uuid is not readable back (the ``Photo`` schema has no uuid
        field and the create response does not echo it), but the
        observation_photo uuid IS returned by a destination re-read, which is
        what makes a lost response recoverable (report §12.1a).

        No license is sent: ``POST /photos`` accepts none, so the photo lands
        under the account's default photo license by design.
        """
        if not image_bytes:
            raise INatAPIError(
                "Refusing to upload an empty photo body.",
                endpoint="/observation_photos",
                request_phase="unsafe_write",
                method="POST",
            )
        return self._request_v2_auth(
            "POST",
            "/observation_photos",
            api_token,
            params={"fields": V2_OBSERVATION_PHOTO_FIELDS},
            data={
                "observation_photo[observation_id]": observation_uuid,
                "observation_photo[uuid]": client_uuid,
            },
            files={"file": (filename, image_bytes, content_type)},
        )

    def create_identification_v2(
        self,
        api_token: str,
        observation_uuid: str,
        taxon_id: int,
        body: str = "",
        disagreement: Optional[bool] = None,
    ) -> V2Response:
        # When ``disagreement`` is True the identification is posted as an
        # explicit ancestor disagreement, mirroring the website's "I disagree"
        # control (which typically knocks the community taxon back to the
        # coarser rank being proposed).  Leaving it None posts a normal ID.
        payload: Dict[str, Any] = {
            "identification": {
                "observation_id": observation_uuid,
                "taxon_id": int(taxon_id),
            },
            "fields": {"id": True, "uuid": True},
        }
        if body:
            payload["identification"]["body"] = body
        if disagreement is not None:
            payload["identification"]["disagreement"] = bool(disagreement)
        return self._request_v2_auth(
            "POST", "/identifications", api_token, json=payload
        )

    def create_comment_v2(
        self,
        api_token: str,
        observation_uuid: str,
        body: str,
    ) -> V2Response:
        payload = {
            "comment": {
                "parent_type": "Observation",
                "parent_id": observation_uuid,
                "body": body,
            },
            "fields": {"id": True, "uuid": True},
        }
        return self._request_v2_auth("POST", "/comments", api_token, json=payload)

    def set_reviewed_v2(
        self,
        api_token: str,
        observation_uuid: str,
        reviewed: bool,
    ) -> V2Response:
        method = "POST" if reviewed else "DELETE"
        return self._request_v2_auth(
            method,
            f"/observations/{observation_uuid}/review",
            api_token,
        )

    def set_favorite_v2(
        self,
        api_token: str,
        observation_uuid: str,
        favorite: bool,
    ) -> V2Response:
        method = "POST" if favorite else "DELETE"
        return self._request_v2_auth(
            method,
            f"/observations/{observation_uuid}/fave",
            api_token,
        )

    def set_quality_metric_vote_v2(
        self,
        api_token: str,
        observation_uuid: str,
        metric: str,
        vote: str,
    ) -> V2Response:
        """Vote (or remove a vote) on one Data Quality Assessment metric.

        This is the authenticated user's own DQA vote -- never an
        observation-field update. Captive/Cultivated is the "wild" metric
        with ``agree=false``; Vote Wild is the same metric with
        ``agree=true``; removing the vote is a DELETE with no ``agree``
        parameter at all. ``agree`` is sent as a query parameter, matching
        the documented endpoint, never as a JSON body.
        """
        if metric not in _SUPPORTED_QUALITY_METRICS:
            raise ValueError(f"Unsupported quality metric: {metric}")
        if vote not in _SUPPORTED_QUALITY_METRIC_VOTES:
            raise ValueError(f"Unsupported quality metric vote: {vote}")
        path = f"/observations/{observation_uuid}/quality/{metric}"
        if vote == "remove":
            return self._request_v2_auth("DELETE", path, api_token)
        return self._request_v2_auth(
            "POST",
            path,
            api_token,
            params={"agree": "true" if vote == "agree" else "false"},
        )

    def vote_id_is_as_good_as_can_be(
        self,
        api_token: str,
        observation_id: int,
    ) -> Dict:
        """Vote that the observation does not need more ID: ID is as good as it can be."""
        return self._request_auth(
            "POST",
            f"{WWW_BASE_URL}/votes/vote/observation/{int(observation_id)}.json",
            api_token,
            json={"scope": "needs_id", "vote": "no"},
        )

    def update_observation_field_value(
        self,
        api_token: str,
        observation_field_value_id: int | str,
        value: str,
        *,
        observation_id: Optional[int] = None,
        observation_field_id: Optional[int] = None,
    ) -> Dict:
        """Update an existing observation field value."""
        payload: Dict[str, Any] = {
            "observation_field_value[value]": value,
        }
        if observation_id is not None:
            payload["observation_field_value[observation_id]"] = int(observation_id)
        if observation_field_id is not None:
            payload["observation_field_value[observation_field_id]"] = int(
                observation_field_id
            )
        return self._request_auth(
            "PUT",
            f"{WWW_BASE_URL}/observation_field_values/{observation_field_value_id}.json",
            api_token,
            data=payload,
        )

    def create_observation_field_value(
        self,
        api_token: str,
        observation_id: int,
        observation_field_id: int,
        value: str,
    ) -> Dict:
        """Create an observation field value on an observation."""
        payload: Dict[str, Any] = {
            "observation_field_value": {
                "observation_id": int(observation_id),
                "observation_field_id": int(observation_field_id),
                "value": value,
            }
        }
        return self._request_auth(
            "POST",
            "/observation_field_values",
            api_token,
            json=payload,
        )

    def get_observation_fields_autocomplete(self, query: str) -> Dict:
        """Search observation fields by name."""
        return self._get("/observation_fields/autocomplete", {"q": query.strip()})

    def find_observation_field_id(self, field_name: str) -> Optional[int]:
        """Return the exact matching observation field id, if iNaturalist finds one."""
        wanted = field_name.strip().casefold()
        if not wanted:
            return None
        raw = self.get_observation_fields_autocomplete(field_name)
        for item in raw.get("results") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name.casefold() != wanted:
                continue
            field_id = item.get("id")
            try:
                return int(field_id)
            except (TypeError, ValueError):
                return None
        return None

    def get_identifications(
        self,
        user_login: str,
        taxon_id: Optional[int] = None,
        place_id: Optional[int] = None,
        leading: Optional[bool] = None,
        current_only: bool = True,
        d1: Optional[str] = None,
        d2: Optional[str] = None,
        page: int = 1,
        per_page: int = 200,
    ) -> Dict:
        """
        Fetch identifications by a user.

        NOTE: The 'taxon_id' parameter automatically includes descendant taxa
        on the iNaturalist API — no separate descendant flag needed here.
        The API field 'is_leading' appears in results but 'leading' as a filter
        parameter may not be supported in all API versions; filter client-side if needed.

        Returns raw API JSON dict with keys: 'total_results', 'page', 'per_page', 'results'.
        """
        params: Dict[str, Any] = {
            "user_login": user_login,
            "taxon_id": taxon_id,
            "place_id": place_id,
            "current": "true" if current_only else None,
            "d1": d1,
            "d2": d2,
            "page": page,
            "per_page": per_page,
            "order_by": "created_at",
            "order": "desc",
        }
        if leading is True:
            # NOTE: 'leading' may not be a valid API parameter; kept for forward-compat.
            params["is_leading"] = "true"
        return self._get("/identifications", params)

    def get_taxa_autocomplete(self, q: str, per_page: int = 10) -> Dict:
        """Autocomplete taxa by name. Returns raw API JSON."""
        return self._get("/taxa/autocomplete", {"q": q, "per_page": per_page})

    def get_places_autocomplete(self, q: str, per_page: int = 10) -> Dict:
        """Autocomplete places by name. Returns raw API JSON."""
        return self._get("/places/autocomplete", {"q": q, "per_page": per_page})

    def get_observations_species_counts(
        self,
        ident_user_login: str,
        taxon_id: Optional[int] = None,
        place_id: Optional[int] = None,
        leading: Optional[bool] = None,
        d1: Optional[str] = None,
        d2: Optional[str] = None,
        per_page: int = 500,
        page: int = 1,
    ) -> Dict:
        """
        Fetch species counts for observations identified by a user.

        Uses /observations/species_counts — this gives a count of distinct
        taxa in observations where ident_user_login has made an identification.
        Used to populate the Taxon Summary learning panel.

        NOTE: 'ident_user_login' may not appear in all API docs versions; it filters
        observations that have an identification by this user.
        """
        params: Dict[str, Any] = {
            "ident_user_login": ident_user_login,
            "taxon_id": taxon_id,
            "place_id": place_id,
            "d1": d1,
            "d2": d2,
            "per_page": per_page,
            "page": page,
        }
        return self._get("/observations/species_counts", params)

    def get_observation_species_counts(
        self,
        query_params: QueryParams,
        per_page: int = 500,
        page: int = 1,
    ) -> Dict:
        """Fetch species counts for an arbitrary observations-index query."""
        params = _with_pagination(query_params, page=page, per_page=per_page)
        return self._get("/observations/species_counts", params)

    def get_user(self, login: str) -> Optional[Dict]:
        """Look up a user by login via autocomplete. Returns user dict or None if not found."""
        raw = self._get("/users/autocomplete", {"q": login, "per_page": 5})
        for result in raw.get("results", []):
            if result.get("login", "").lower() == login.lower():
                return result
        return None

    def get_place_by_id(self, place_id: int) -> Dict:
        """Fetch a single place by ID."""
        return self._get(f"/places/{place_id}")

    def get_taxon_by_id(self, taxon_id: int) -> Dict:
        """Fetch a single taxon by ID."""
        return self._get(f"/taxa/{taxon_id}")

    def download_image(self, url: str) -> bytes:
        """Download raw image bytes from iNat CDN (S3 / static.inaturalist.org).

        NOT rate-limited: images are served from CDN, not api.inaturalist.org.
        Rate-limiting image downloads would stall the UI for no benefit.
        """
        for attempt in range(MAX_RETRIES):
            try:
                resp = httpx.get(
                    url,
                    timeout=DEFAULT_TIMEOUT,
                    headers={"User-Agent": "Observation-Workbench/1.0"},
                    follow_redirects=True,
                )
                if resp.status_code == 404:
                    raise FileNotFoundError(f"Image not found: {url}")
                if resp.status_code in (429, 503):
                    wait_s = 2**attempt
                    time.sleep(wait_s)
                    continue
                resp.raise_for_status()
                return resp.content
            except httpx.TransportError:
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError(f"Max retries exceeded for image {url}")


def _clean_params(params: Optional[QueryParams]) -> QueryParams:
    if params is None:
        return {}
    if isinstance(params, dict):
        return {k: v for k, v in params.items() if v is not None}
    return [(k, v) for k, v in params if v is not None]


def _normalise_jwt(token: str) -> str:
    """Return a bare JWT without ever echoing it into diagnostics or logs."""
    value = (token or "").strip()
    if value.casefold().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.casefold().startswith("bearer "):
        value = value.split(None, 1)[1].strip()
    return value


def _fixed_endpoint_name(endpoint: str) -> str:
    """Return a log-only fixed path without hosts or remote row identities."""
    path = urlsplit(endpoint).path
    path = re.sub(
        r"(?<=/observation_field_values/)[^/]+",
        "{field_value_id}",
        path,
    )
    path = re.sub(r"(?<=/observations/)[^/]+", "{observation_id}", path)
    return path or "/v2"


def _normalise_v2_path(path: str) -> str:
    """Accept a relative v2 path or an absolute api.inaturalist.org v2 URL."""
    value = (path or "").strip()
    if not value:
        raise ValueError("A v2 API path is required")
    parsed = urlsplit(value)
    if parsed.query or parsed.fragment:
        raise ValueError("v2 request paths must not embed query parameters")
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc != "api.inaturalist.org":
            raise ValueError("v2 requests must target api.inaturalist.org over HTTPS")
        normalised_path = parsed.path.rstrip("/") or "/v2"
    else:
        normalised_path = value if value.startswith("/") else f"/{value}"
        normalised_path = normalised_path.rstrip("/") or "/v2"
    if normalised_path == "/v2":
        return V2_BASE_URL
    if normalised_path.startswith("/v2/"):
        return f"https://api.inaturalist.org{normalised_path}"
    return f"{V2_BASE_URL}{normalised_path}"


def _raise_for_status(resp: httpx.Response, endpoint: str) -> None:
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Response bodies can echo field values, notes, or other private data.
        log.error("HTTP error on %s: status=%s", endpoint, resp.status_code)
        raise exc


def _extract_error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            for key in ("error", "errors", "message"):
                value = data.get(key)
                if value:
                    return f"{value} (HTTP {resp.status_code})"
    except Exception:
        pass
    text = resp.text.strip()
    if text:
        return f"{text[:500]} (HTTP {resp.status_code})"
    return f"HTTP {resp.status_code}"


def _with_pagination(
    query_params: QueryParams,
    page: int,
    per_page: int,
) -> QueryParams:
    if isinstance(query_params, dict):
        params = {
            k: v
            for k, v in query_params.items()
            if k not in ("page", "per_page") and v is not None
        }
        params["page"] = page
        params["per_page"] = per_page
        return params

    params = [
        (k, v)
        for k, v in query_params
        if k not in ("page", "per_page") and v is not None
    ]
    params.extend([("page", page), ("per_page", per_page)])
    return params
