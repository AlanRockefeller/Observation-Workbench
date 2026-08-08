"""Serialized Mushroom Observer API2 client for reconciliation.

Gate 1A reads are anonymous. Write gates supply a key only to narrowly scoped
calls. Logging is intentionally metadata-only: query values,
redirects, response bodies, notes, coordinates, and field values never reach
the log.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

import httpx

log = logging.getLogger(__name__)
MO_API_BASE = "https://mushroomobserver.org/api2"
# MO's own default page size for /observations. It is not a value we request —
# 'limit' is a fatal MO error (see observations_page) — so callers must treat a
# short page as "last page" rather than assuming a size they chose.
MO_OBSERVATIONS_PAGE_SIZE = 1000
# Retry budget for READS only (see _get). A full scan makes a couple of hundred
# sequential MO reads, so the chance of hitting one transient failure is high
# and the cost of not surviving it is the entire run.
MO_READ_ATTEMPTS = 3
MO_RETRY_BACKOFF_S = 4.0
# Statuses worth repeating a GET for. 5xx and 429 are the server saying "not
# now", never "your request is wrong" — a 4xx other than 429 would fail
# identically on every retry.
MO_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# Minimum spacing between MO IMAGE reads (see download_image). Images are
# static files on a separate host, not API calls, so the API's 5s spacing would
# make photo review unusable — but they are still MO's bandwidth, and the one
# caller is an automatic background prefetch, so they cannot be a free-for-all.
MO_IMAGE_MIN_INTERVAL_S = 0.5
# Retry budget for image reads. Lower than MO_READ_ATTEMPTS: a failure here
# costs one photo in a comparison, not a whole scan.
MO_IMAGE_ATTEMPTS = 2


class ReconciliationCancelled(RuntimeError):
    pass


class MOAPIError(RuntimeError):
    def __init__(
        self,
        endpoint: str,
        status_code: Optional[int],
        message: str,
        *,
        response_received: bool = False,
        outcome_unknown: bool = False,
        error_code: str = "",
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status_code = status_code
        self.response_received = response_received
        self.outcome_unknown = outcome_unknown
        self.error_code = error_code


def _is_transient_read_failure(exc: MOAPIError) -> bool:
    """True when repeating the same GET could plausibly succeed.

    Two shapes qualify. ``response_received=False`` means the request never
    produced a response at all (timeout, dropped connection) — nothing was
    served, so nothing is lost by asking again. A retryable STATUS means MO
    answered but told us it could not serve the request right now.

    Everything else is deterministic: MO's fatal-error-at-200 payloads and 4xx
    statuses reject the request itself and would reject it identically forever.
    """
    if not exc.response_received and exc.status_code is None:
        return True
    return exc.status_code in MO_RETRYABLE_STATUSES


@dataclass(frozen=True)
class MOResponseMetadata:
    """Non-sensitive metadata about a completed MO write."""

    endpoint: str
    method: str
    status_code: int


class MOResponse(dict):
    """Write payload plus its observed HTTP status, for durable journaling.

    Mirrors ``api.client.V2Response`` deliberately: every write executor reads
    the status with ``getattr(response, "metadata", None)``, and a plain ``dict``
    silently answered ``None`` there, so a successful Mushroom Observer write
    always journaled ``last_http_status`` NULL while the iNaturalist half of the
    same action group recorded a real status. Still a ``dict``, so every reader
    (``_results``, ``results_from_payload``, existing callers) is unaffected.
    """

    def __init__(self, data: dict[str, Any], metadata: MOResponseMetadata) -> None:
        super().__init__(data)
        self.metadata = metadata


class MOWriteOutcomeUnknown(MOAPIError):
    def __init__(self, endpoint: str, method: str, cause: Exception) -> None:
        super().__init__(
            endpoint,
            None,
            f"Mushroom Observer {method} result is unknown after a transport interruption.",
            outcome_unknown=True,
            error_code="unsafe_transport_interruption",
        )
        self.__cause__ = cause


class MOClient:
    """Serialized reader/writer with MO's conservative request spacing."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=MO_API_BASE,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "Observation-Workbench-Reconciliation/1.0"},
        )
        self._lock = threading.Lock()
        self._last_finished = 0.0
        self._last_runtime = 0.0
        # Separate transport for the image hosts: absolute URLs rather than
        # MO_API_BASE, and redirects MUST be followed because an archived
        # original is served by redirecting to the storage bucket. The API
        # client deliberately does not follow redirects, and that must not
        # change — a redirected API write is not a safe thing to replay.
        self._image_client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Observation-Workbench-Reconciliation/1.0"},
        )
        self._image_lock = threading.Lock()
        self._image_last_finished = 0.0

    def close(self) -> None:
        self._client.close()
        self._image_client.close()

    def _wait(self, cancelled: Callable[[], bool]) -> None:
        minimum = max(5.0, self._last_runtime)
        remaining = minimum - (time.monotonic() - self._last_finished)
        while remaining > 0:
            if cancelled():
                raise ReconciliationCancelled("Reconciliation scan cancelled")
            time.sleep(min(0.2, remaining))
            remaining = minimum - (time.monotonic() - self._last_finished)
        if cancelled():
            raise ReconciliationCancelled("Reconciliation scan cancelled")

    def download_image(
        self,
        url: str,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> bytes:
        """Read one MO image, SERIALIZED and spaced, and return its bytes.

        Exists because ``INatClient.download_image`` — which every MO photo
        used to go through — documents itself as unthrottled on the grounds
        that it talks to iNaturalist's CDN. That reasoning does not transfer to
        Mushroom Observer, and the caller is the candidate-review prefetch:
        an automatic path that reads BOTH complete photo sets for several pairs
        with no user action, on two pool threads, and re-fires on every scroll.
        Unthrottled that is a burst of dozens of requests at MO's image host.

        The lock is separate from the API lock on purpose. Sharing it would
        make each image wait out the API's 5s spacing, and would let a batch of
        image reads delay the scan's API calls — the opposite of the yielding
        the prefetch worker is built around.
        """
        stop = cancelled or (lambda: False)
        with self._image_lock:
            attempt = 0
            while True:
                attempt += 1
                # At the TOP of every attempt, so a retry is spaced out exactly
                # like a first request. The finally below stamps the clock on
                # the way out of each attempt, success or failure.
                self._wait_for_image(stop)
                try:
                    response = self._image_client.get(url)
                    if response.status_code in MO_RETRYABLE_STATUSES:
                        raise MOAPIError(
                            "image",
                            response.status_code,
                            f"Mushroom Observer image server returned {response.status_code}",
                            response_received=True,
                        )
                    response.raise_for_status()
                    return response.content
                # TransportError covers timeouts and dropped connections; a
                # 4xx arrives as HTTPStatusError and is deliberately not caught,
                # because it would fail identically on every retry.
                except (httpx.TransportError, MOAPIError):
                    if attempt >= MO_IMAGE_ATTEMPTS:
                        raise
                finally:
                    self._image_last_finished = time.monotonic()

    def _wait_for_image(self, cancelled: Callable[[], bool]) -> None:
        remaining = MO_IMAGE_MIN_INTERVAL_S - (
            time.monotonic() - self._image_last_finished
        )
        while remaining > 0:
            if cancelled():
                raise ReconciliationCancelled("Photo comparison cancelled")
            time.sleep(min(0.1, remaining))
            remaining = MO_IMAGE_MIN_INTERVAL_S - (
                time.monotonic() - self._image_last_finished
            )

    def _get(
        self,
        endpoint: str,
        params: dict[str, Any],
        cancelled: Callable[[], bool],
        *,
        accept_help_error: bool = False,
    ) -> dict[str, Any]:
        """Read one MO endpoint, RETRYING transient transport failures.

        Retries exist because a full scan issues a couple of hundred sequential
        MO reads over roughly fifteen minutes, and a single ``ReadTimeout``
        anywhere in that sequence used to discard the whole run and advance no
        cursors — observed live: a 14m38s scan died on request ~200 of the
        external_links phase.

        Only failures that prove NOTHING was applied are retried: transport and
        timeout errors, plus 5xx/429 statuses. That distinction is the entire
        safety argument, and it is why this must never be lifted into ``_write``
        — a read is idempotent, so repeating it cannot duplicate anything,
        whereas a write whose outcome is unknown must never be resent (see
        _write, which deliberately refuses).

        MO's own fatal-error-at-HTTP-200 convention is NOT retried: those are
        deterministic rejections of the request itself, so a retry would fail
        identically while wasting the spacing interval.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._get_once(
                    endpoint,
                    params,
                    cancelled,
                    accept_help_error=accept_help_error,
                )
            except MOAPIError as exc:
                if attempt > MO_READ_ATTEMPTS or not _is_transient_read_failure(exc):
                    raise
                # _wait already enforces >=5s between requests, so this is
                # additional headroom for a service that is visibly struggling
                # rather than the whole delay.
                delay = MO_RETRY_BACKOFF_S * attempt
                log.warning(
                    "MO GET /%s transient failure (%s); retry %d of %d in %.0fs",
                    endpoint,
                    exc.status_code or "no-response",
                    attempt,
                    MO_READ_ATTEMPTS,
                    delay,
                )
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if cancelled():
                        raise ReconciliationCancelled(
                            "Reconciliation scan cancelled"
                        ) from exc
                    time.sleep(0.2)

    def _get_once(
        self,
        endpoint: str,
        params: dict[str, Any],
        cancelled: Callable[[], bool],
        *,
        accept_help_error: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._wait(cancelled)
            if cancelled():
                raise ReconciliationCancelled("Reconciliation scan cancelled")
            started = time.monotonic()
            status: Optional[int] = None
            response_received = False
            try:
                response = self._client.get(
                    f"/{endpoint}", params={"format": "json", **params}
                )
                response_received = True
                status = response.status_code
                elapsed = time.monotonic() - started
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise MOAPIError(
                        endpoint, status, "Mushroom Observer returned invalid JSON"
                    ) from exc
                runtime = _runtime(payload)
                self._last_runtime = max(0.0, runtime)
                log.info("MO GET /%s status=%s time=%.3fs", endpoint, status, elapsed)
                if response.is_error and not (
                    accept_help_error and _is_help_payload(payload)
                ):
                    raise MOAPIError(
                        endpoint, status, _sanitized_error(payload, status)
                    )
                # MO returns HTTP 200 even for a fatal error (already established
                # for writes in _write; reads have the same convention but were
                # never checked for it here, so a rejected/misnamed filter param
                # silently returned an empty result set instead of raising —
                # discovered live via a hardcoded 'limit' param _batched sent to
                # several endpoints that do not accept it).
                if _has_fatal_error(payload) and not (
                    accept_help_error and _is_help_payload(payload)
                ):
                    # The MO error CODE is carried through (the human-readable
                    # details are not — they echo request values). Callers need
                    # it to tell a negative result apart from a real failure:
                    # 'this name does not exist' is FATAL to MO but an ordinary
                    # answer to us (see resolve_user).
                    raise MOAPIError(
                        endpoint,
                        status,
                        _sanitized_error(payload, status),
                        response_received=True,
                        error_code=_first_error_code(payload),
                    )
                if not isinstance(payload, dict):
                    raise MOAPIError(
                        endpoint,
                        status,
                        "Mushroom Observer returned an unexpected response",
                    )
                if cancelled():
                    raise ReconciliationCancelled("Reconciliation scan cancelled")
                return payload
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._last_finished = time.monotonic()
                log.warning(
                    "MO GET /%s status=%s failed=%s",
                    endpoint,
                    status,
                    type(exc).__name__,
                )
                raise MOAPIError(
                    endpoint,
                    status,
                    f"Mushroom Observer request failed: {type(exc).__name__}",
                ) from exc
            finally:
                if response_received:
                    # Even invalid JSON consumed a completed MO request and
                    # must start the conservative spacing interval.
                    self._last_finished = time.monotonic()

    def discover_observation_capabilities(
        self, cancelled: Callable[[], bool]
    ) -> dict[str, bool]:
        payload = self._get(
            "observations", {"help": 1}, cancelled, accept_help_error=True
        )
        flattened = str(payload).casefold()
        return {"updated_at": "updated_at" in flattened}

    def resolve_user(
        self, login: str, cancelled: Callable[[], bool]
    ) -> Optional[dict[str, Any]]:
        """Resolve one MO account by its login name.

        /api2/users accepts ONLY ``created_at``, ``id`` and ``updated_at``
        (verified against ``?help=1``). There is no ``login`` filter, so the
        ``login=<name>`` this used to send was an ``API2::UnusedParameters``
        FATAL error returned at HTTP 200 — every account resolve failed with a
        bare 'status=200' once _get learned to check for fatal errors.

        ``id`` is an MO "user list" parameter, which accepts a login string as
        well as a numeric id, and is the supported way to look a name up. An
        unknown name comes back as FATAL ``API2::ObjectNotFoundByString``; that
        is this method's normal negative answer, not a failure, so it maps to
        None and every other MO error still raises.

        ``detail=high`` is REQUIRED and is not an optimisation to undo: at
        ``detail=low`` MO serializes users as bare integer ids carrying no login
        at all, so the response could never be matched back to the requested
        name. The login lives in ``login_name`` (``mo_login_of``); MO has no
        ``login`` field, which is the second reason the old matching loop could
        not have succeeded even with a working filter.
        """
        wanted = login.strip()
        if not wanted:
            return None
        try:
            payload = self._get("users", {"id": wanted, "detail": "high"}, cancelled)
        except MOAPIError as exc:
            # _first_error_code strips MO's "API2::" prefix and casefolds, so
            # "API2::ObjectNotFoundByString" arrives in this normalized form.
            if exc.error_code == "objectnotfoundbystring":
                return None
            raise
        for item in _results(payload):
            if mo_login_of(item).casefold() == wanted.casefold():
                return item
        return None

    def observations_page(
        self,
        user_id: int,
        page: int,
        cancelled: Callable[[], bool],
        *,
        updated_at: str = "",
    ) -> dict[str, Any]:
        """Read one page of an MO account's observations.

        NOTE: 'limit' is deliberately NOT sent — /observations rejects it as an
        unexpected parameter, which is a FATAL MO error returned at HTTP 200
        (see _has_fatal_error). This is the same defect already documented in
        _batched; it was fixed there but survived here, silently returning an
        empty inventory before the fatal-error check existed and failing every
        scan outright afterwards. MO applies its own page size of 1000 for this
        endpoint, which is what MO_OBSERVATIONS_PAGE_SIZE records.

        ``updated_at`` must be an MO time RANGE (see
        ``coordinator._mo_time_range``), never an ISO 8601 timestamp.
        """
        params: dict[str, Any] = {"user": int(user_id), "detail": "low", "page": page}
        if updated_at:
            params["updated_at"] = updated_at
        return self._get("observations", params, cancelled)

    def observation(
        self,
        observation_id: int,
        cancelled: Callable[[], bool],
        *,
        detail: str = "high",
    ) -> dict[str, Any]:
        return self._get(
            "observations", {"id": int(observation_id), "detail": detail}, cancelled
        )

    def observations(
        self,
        observation_ids: Iterable[int],
        cancelled: Callable[[], bool],
        *,
        detail: str = "high",
    ) -> dict[str, Any]:
        """Read many observations by id, batched — the plural of ``observation``.

        /api2/observations accepts a comma-separated ``id`` list like every
        other MO endpoint this client batches, so pair validation does not need
        one round trip per record. Batch size is 100 because MO caps a
        ``detail=high`` page at 100 rows regardless of how many ids are asked
        for; a larger batch is not an error, it just makes ``_paged`` fetch the
        remainder as page 2, which costs the same requests for more latency.

        Ids MO does not return (deleted, or never existed) are simply absent
        from ``results``, so callers must key off each returned row's id.
        """
        return self._batched(
            "observations",
            "id",
            observation_ids,
            cancelled,
            batch_size=100,
            detail=detail,
        )

    def names(
        self,
        ids: Iterable[int],
        cancelled: Callable[[], bool],
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> dict[str, Any]:
        return self._batched("names", "id", ids, cancelled, progress=progress)

    def sequences(
        self,
        observer_id: int,
        observation_ids: Iterable[int],
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        """Read the sequences attached to one account's observations.

        /api2/sequences has NO ``observation`` parameter — verified against
        ``?help=1``, which offers ``id, observer, user, name, herbarium, locus,
        obs_date, accession_has, …``. The previous ``observation=<ids>`` filter
        was therefore an ``API2::UnusedParameters`` FATAL error returned at HTTP
        200, so every sequence read failed.

        ``observer`` (the OWNER of the observation) is the correct filter, not
        ``user`` (the author of the sequence row): a third party may attach a
        sequence to another user's observation, and Phase 2C must see exactly
        those. Rows carry ``observation_id``, so the requested observations are
        selected client-side.

        ``detail=high`` is REQUIRED and is not an optimisation to undo. Every
        consumer of these rows parses the composite sequence record —
        ``its._mo_composites`` needs ``locus`` (rows without it are dropped
        outright), ``bases``, ``accession``, ``archive``, ``notes`` and the
        creator ``user``; ``deletion`` needs the same to prove donor-content
        parity. MO's low serializer strips fields (proven for ``/images``, see
        ``images_for_observation``), and a stripped row here is silently
        indistinguishable from "this observation has no ITS sequence" — which
        makes the ITS gate propose MO_SEQUENCE_ADD and write a DUPLICATE
        sequence onto an observation that already has one.
        """
        wanted = {int(value) for value in observation_ids}
        rows = self._paged(
            "sequences",
            {"observer": int(observer_id)},
            cancelled,
            detail="high",
        )
        matched = [
            row for row in rows if _positive_int(row.get("observation_id")) in wanted
        ]
        return {"results": matched, "total_results": len(matched)}

    def external_sites(self, cancelled: Callable[[], bool]) -> dict[str, Any]:
        return self._get("external_sites", {"detail": "low"}, cancelled)

    def external_links(
        self,
        observation_ids: Iterable[int],
        cancelled: Callable[[], bool],
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> dict[str, Any]:
        return self._batched(
            "external_links",
            "observation",
            observation_ids,
            cancelled,
            progress=progress,
        )

    def images(
        self, image_ids: Iterable[int], cancelled: Callable[[], bool]
    ) -> dict[str, Any]:
        return self._batched("images", "id", image_ids, cancelled)

    def images_for_observation(
        self,
        observation_id: int,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        """Enumerate one observation's images with license and copyright holder.

        ``detail=high`` is required: the low-detail serializer omits ``license``,
        ``copyright_holder`` and the file URLs that Gate 1E needs. This is also
        the destination-enumeration signal used for duplicate detection, and the
        only way to learn the id of a just-created image (MO's create response
        does not return one).
        """
        return self._get(
            "images", {"observation": int(observation_id), "detail": "high"}, cancelled
        )

    def authenticated_user_id(
        self,
        api_key: str,
        expected_user_id: int,
        cancelled: Callable[[], bool],
    ) -> Optional[int]:
        """Resolve an API key without exposing it outside this client call."""
        payload = self._get(
            "users",
            {"api_key": api_key, "id": int(expected_user_id), "detail": "low"},
            cancelled,
        )
        try:
            value = int(payload.get("user") or 0)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def create_external_link(
        self,
        api_key: str,
        observation_id: int,
        external_site_id: int,
        url: str,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        return self._write(
            "POST",
            "external_links",
            {
                "api_key": api_key,
                "observation": int(observation_id),
                "external_site": int(external_site_id),
                "url": url,
            },
            cancelled,
        )

    def update_external_link(
        self,
        api_key: str,
        link_id: int,
        url: str,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        return self._write(
            "PATCH",
            "external_links",
            {"api_key": api_key, "id": int(link_id), "set_url": url},
            cancelled,
        )

    def delete_external_link(
        self,
        api_key: str,
        link_id: int,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        return self._write(
            "DELETE",
            "external_links",
            {"api_key": api_key, "id": int(link_id)},
            cancelled,
        )

    def create_sequence(
        self,
        api_key: str,
        observation_id: int,
        locus: str,
        cancelled: Callable[[], bool],
        *,
        bases: str = "",
        archive: str = "",
        accession: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        """Create one sequence using API2's documented SequenceAPI fields."""
        data: dict[str, Any] = {
            "api_key": api_key,
            "observation": int(observation_id),
            "locus": locus,
        }
        for key, value in (
            ("bases", bases),
            ("archive", archive),
            ("accession", accession),
            ("notes", notes),
        ):
            if value:
                data[key] = value
        return self._write("POST", "sequences", data, cancelled)

    def update_sequence(
        self,
        api_key: str,
        sequence_id: int,
        cancelled: Callable[[], bool],
        *,
        locus: Optional[str] = None,
        bases: Optional[str] = None,
        archive: Optional[str] = None,
        accession: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict[str, Any]:
        """Patch one exact sequence using only supported ``set_*`` fields."""
        data: dict[str, Any] = {"api_key": api_key, "id": int(sequence_id)}
        for key, value in (
            ("set_locus", locus),
            ("set_bases", bases),
            ("set_archive", archive),
            ("set_accession", accession),
            ("set_notes", notes),
        ):
            if value is not None:
                data[key] = value
        return self._write("PATCH", "sequences", data, cancelled)

    def create_observation(
        self,
        api_key: str,
        cancelled: Callable[[], bool],
        *,
        date: str,
        name: str,
        location: str = "",
        notes: str = "",
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        gps_hidden: Optional[bool] = None,
        has_specimen: Optional[bool] = None,
        collection_number: str = "",
        accession_number: str = "",
        herbarium: str = "",
    ) -> dict[str, Any]:
        """Gate 2A: ``POST /api2/observations`` — create a brand-new MO observation.

        Live-proven 2026-07-23 (``docs/gate_2a_capability_note.md``): the
        create parameter names above are exact (probed via ``?help=1`` on
        POST, which — like ``/images`` — is method-specific).

        CRITICAL, load-bearing on every call site, not just recovery: **MO's
        create response never carries a usable observation id, even on an
        ordinary, fully successful, synchronous create.** The caller MUST
        follow every create (success path included) with
        ``find_observation_by_marker`` against whatever correlation marker it
        embedded in ``notes`` to discover the new id — this is not an
        ``outcome_unknown`` fallback, it is the normal happy path for MO.
        ``notes`` is genuinely public on the created observation; the caller
        is responsible for disclosing that to the user before writing it.
        """
        data: dict[str, Any] = {
            "api_key": api_key,
            "date": date,
            "name": name,
        }
        if location:
            data["location"] = location
        if notes:
            data["notes"] = notes
        if latitude is not None:
            data["latitude"] = float(latitude)
        if longitude is not None:
            data["longitude"] = float(longitude)
        if gps_hidden is not None:
            data["gps_hidden"] = bool(gps_hidden)
        if has_specimen is not None:
            data["has_specimen"] = bool(has_specimen)
        if collection_number:
            data["collection_number"] = collection_number
        if accession_number:
            data["accession_number"] = accession_number
        if herbarium:
            data["herbarium"] = herbarium
        return self._write("POST", "observations", data, cancelled)

    def find_observation_by_marker(
        self,
        api_key: str,
        marker: str,
        cancelled: Callable[[], bool],
        *,
        user_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Gate 2A: locate a just-created (or lost-response) observation by its
        planted ``notes`` correlation marker.

        A READ, not a write: this is a ``GET /observations?notes_has=...``
        search, live-proven 2026-07-23 (docs/gate_2a_capability_note.md) as
        an exact, reliable search with the marker/user in the query string —
        it must go through ``_get`` exactly like every other search, not
        ``_write``. ``_write`` sends its payload as a request body (correct
        for the POST/PUT/DELETE writes it exists for) and — more importantly
        — treats a transport failure or a fatal-error response as an
        AMBIGUOUS WRITE (``MOWriteOutcomeUnknown``/``outcome_unknown=True``),
        which is wrong for a search: a failed *search* proves nothing about
        whether the create it is trying to verify succeeded, and must never
        be conflated with "the search itself might have partially applied."
        ``_get`` already gives this exactly the read semantics needed: it
        raises a plain ``MOAPIError`` (never ``outcome_unknown``) on a fatal
        error or transport failure, and distinguishes those from MO's
        harmless HTTP-200 notices the same way every other read does. If this
        raises, the ORIGINAL create action is left exactly where it was
        (typically ``outcome_unknown``) and is never retried automatically —
        callers must not treat a search failure as proof of anything.
        """
        params: dict[str, Any] = {
            "api_key": api_key,
            "notes_has": marker,
            "detail": "low",
        }
        if user_id is not None:
            params["user"] = int(user_id)
        return self._get("observations", params, cancelled)

    def create_image(
        self,
        api_key: str,
        observation_id: int,
        image_bytes: bytes,
        *,
        filename: str,
        content_type: str,
        license_id: int,
        copyright_holder: str,
        cancelled: Callable[[], bool],
        md5sum: str = "",
    ) -> dict[str, Any]:
        """Gate 2A: ``POST /api2/images`` with attach-at-create via ``observations``.

        Live-proven for an EXISTING MO observation (Gate 1E,
        ``docs/gate_1e_capability_report.md``): one atomic op, no separable
        orphan window. ``license`` MUST be MO's numeric License id (see
        ``observation_workbench/reconciliation/photo_license.py``'s ``MO_LICENSES``
        table) — MO rejects the human-readable name outright. Whether this
        also works against an observation created moments earlier in the same
        saga is a Gate 2A open risk (per the plan's unresolved-risks list),
        not yet separately proven.
        """
        if not image_bytes:
            raise MOAPIError(
                "images",
                None,
                "Refusing to upload an empty photo body.",
                error_code="empty_photo_body",
            )
        data: dict[str, Any] = {
            "api_key": api_key,
            "observations": int(observation_id),
            "license": int(license_id),
            "copyright_holder": copyright_holder,
        }
        if md5sum:
            data["md5sum"] = md5sum
        return self._write(
            "POST",
            "images",
            data,
            cancelled,
            files={"upload": (filename, image_bytes, content_type)},
        )

    def _write(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any],
        cancelled: Callable[[], bool],
        *,
        files: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Submit one unsafe API2 request exactly once, then return for verification."""
        with self._lock:
            self._wait(cancelled)
            if cancelled():
                raise ReconciliationCancelled("Reconciliation action cancelled")
            started = time.monotonic()
            status: Optional[int] = None
            try:
                response = self._client.request(
                    method,
                    f"/{endpoint}",
                    data={"format": "json", **data},
                    files=files,
                )
                status = response.status_code
                elapsed = time.monotonic() - started
                try:
                    payload = response.json()
                except ValueError as exc:
                    self._last_finished = time.monotonic()
                    log.info(
                        "MO %s /%s status=%s time=%.3fs",
                        method,
                        endpoint,
                        status,
                        elapsed,
                    )
                    raise MOAPIError(
                        endpoint,
                        status,
                        "Mushroom Observer returned invalid JSON after a write.",
                        response_received=True,
                        outcome_unknown=True,
                        error_code="invalid_write_response",
                    ) from exc
                self._last_runtime = max(0.0, _runtime(payload))
                self._last_finished = time.monotonic()
                log.info(
                    "MO %s /%s status=%s time=%.3fs", method, endpoint, status, elapsed
                )
                if not isinstance(payload, dict):
                    raise MOAPIError(
                        endpoint,
                        status,
                        "Mushroom Observer returned an unexpected write response.",
                        response_received=True,
                        outcome_unknown=True,
                        error_code="unexpected_write_response",
                    )
                # Only a FATAL entry is a rejection. MO returns advisory,
                # non-fatal notices in the same 'errors' array at HTTP 200 (the
                # help response is one), and _get has always distinguished the
                # two via _has_fatal_error. Treating any non-empty 'errors' as a
                # rejection here meant a fully successful create that carried a
                # notice was journaled 'failed' (outcome_unknown=False, because
                # the status was 200) — and for MO creates the exception escapes
                # before find_observation_by_marker runs, so the new observation
                # was orphaned with its id never learned and a retry made a
                # second one.
                if response.is_error or _has_fatal_error(payload):
                    code = _first_error_code(payload)
                    uncertain_status = bool(
                        response.is_error
                        and status not in {400, 401, 403, 404, 409, 422}
                    )
                    raise MOAPIError(
                        endpoint,
                        status,
                        "Mushroom Observer rejected the reconciliation action.",
                        response_received=True,
                        outcome_unknown=uncertain_status,
                        error_code=code or f"http_{status}",
                    )
                return MOResponse(
                    payload,
                    MOResponseMetadata(
                        endpoint=endpoint,
                        method=method,
                        status_code=int(status),
                    ),
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._last_finished = time.monotonic()
                log.warning(
                    "MO %s /%s status=%s failed=%s",
                    method,
                    endpoint,
                    status,
                    type(exc).__name__,
                )
                raise MOWriteOutcomeUnknown(endpoint, method, exc) from exc

    def _batched(
        self,
        endpoint: str,
        key: str,
        values: Iterable[int],
        cancelled: Callable[[], bool],
        batch_size: int = 100,
        *,
        detail: str = "low",
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> dict[str, Any]:
        """Read many ids in batches.

        ``progress`` reports ids CONSUMED out of ids requested, not rows
        returned: for a big account this loop is minutes of sequential requests
        (each spaced >=5s), and the caller needs a denominator it can show that
        does not move as results arrive.
        """
        ids = [int(value) for value in values]
        combined: list[dict[str, Any]] = []
        for start in range(0, len(ids), batch_size):
            batch = ",".join(str(value) for value in ids[start : start + batch_size])
            combined.extend(
                self._paged(endpoint, {key: batch}, cancelled, detail=detail)
            )
            if progress is not None:
                progress(min(start + batch_size, len(ids)), len(ids))
        return {"results": combined, "total_results": len(combined)}

    def _paged(
        self,
        endpoint: str,
        params: dict[str, Any],
        cancelled: Callable[[], bool],
        *,
        detail: str = "low",
    ) -> list[dict[str, Any]]:
        """Read every page of one filtered query, newest MO paging conventions.

        NOTE: 'limit' is deliberately NOT sent — confirmed live that
        observations/images/names/external_links all reject it as an unexpected
        parameter (a FATAL MO error, HTTP 200; see _has_fatal_error). 'page'
        alone is accepted and MO applies its own per-endpoint page size, so this
        loop keeps paging until the reported total is reached, or a page repeats
        or comes back empty.

        ``detail`` is a PARAMETER, not a constant: this used to hardcode
        ``detail=low`` *after* ``**params``, so no caller could page a
        richer read even by asking. MO's low serializer strips real fields
        (see images_for_observation), so any caller whose parser needs more
        than identity must say so explicitly.
        """
        combined: list[dict[str, Any]] = []
        page = 1
        seen_page: set[tuple[str, ...]] = set()
        while True:
            if cancelled():
                raise ReconciliationCancelled("Reconciliation scan cancelled")
            payload = self._get(
                endpoint,
                {**params, "detail": detail, "page": page},
                cancelled,
            )
            rows = _results(payload)
            signature = tuple(str(item.get("id") or item) for item in rows)
            if signature in seen_page or not rows:
                break
            seen_page.add(signature)
            combined.extend(rows)
            total = _total_results(payload)
            if total and len(combined) >= total:
                break
            page += 1
        return combined


def _runtime(payload: object) -> float:
    if not isinstance(payload, dict):
        return 0.0
    for container in (payload, payload.get("meta"), payload.get("response")):
        if isinstance(container, dict):
            try:
                return float(
                    container.get("run_time") or container.get("runtime") or 0.0
                )
            except (TypeError, ValueError):
                pass
    return 0.0


def _total_results(payload: object) -> int:
    # MO reports the total as 'number_of_records'; the other spellings are kept
    # for defensiveness. Each key must be SKIPPED when absent rather than
    # returned as 0 — `int(payload.get(key) or 0)` never raises, so an
    # unconditional `return` in the first iteration made every later key dead
    # code and reported 0 for every MO payload, disabling the early exit below.
    if not isinstance(payload, dict):
        return 0
    for key in ("number_of_records", "total_results", "total", "number_of_results"):
        if payload.get(key) is None:
            continue
        try:
            return int(payload[key])
        except (TypeError, ValueError):
            continue
    return 0


def _positive_int(value: object) -> Optional[int]:
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _results(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in (
        "results",
        "observations",
        "users",
        "external_links",
        "sequences",
        "images",
        "names",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    result = payload.get("result")
    if isinstance(result, dict):
        return [result]
    return []


def mo_login_of(user: object) -> str:
    """Return an MO user row's login name.

    MO calls it ``login_name`` and has no ``login`` field at all; ``legal_name``
    is a separate display value and is deliberately NOT a fallback, since a
    profile keyed on it would not round-trip through resolve_user. Only present
    at ``detail=high``.
    """
    if not isinstance(user, dict):
        return ""
    return str(user.get("login_name") or "").strip()


def _is_fatal_entry(item: object) -> bool:
    # MO serializes the flag as the STRING "true"/"false"; a real JSON boolean
    # stringifies to "True"/"False", which casefolds to the same value.
    return isinstance(item, dict) and str(item.get("fatal") or "").casefold() == "true"


def _has_fatal_error(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return False
    return any(_is_fatal_entry(item) for item in errors)


def _is_help_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    text = str(payload)
    return "HelpMessage" in text or "parameters" in payload or "help" in payload


def _sanitized_error(payload: object, status: Optional[int]) -> str:
    # Deliberately do not expose server text, which may echo request values.
    code = ""
    if isinstance(payload, dict):
        raw = payload.get("error")
        if isinstance(raw, dict):
            code = str(raw.get("class") or raw.get("type") or "")
    suffix = f" ({code[:80]})" if code else ""
    return f"Mushroom Observer returned HTTP {status or 'unknown'}{suffix}"


def _first_error_code(payload: object) -> str:
    """Return the code of the first FATAL error, falling back to any code.

    A fatal entry is what actually rejected the request, so it must win over an
    advisory notice that happens to be listed first.
    """
    if not isinstance(payload, dict):
        return ""
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return ""
    fallback = ""
    for item in errors:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        code = str(item["code"]).rsplit("::", 1)[-1][:80].casefold()
        if _is_fatal_entry(item):
            return code
        fallback = fallback or code
    return fallback


results_from_payload = _results
