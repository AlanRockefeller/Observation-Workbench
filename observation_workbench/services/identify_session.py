"""Read-only, fixed-order sessions for the local Identify workflow."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from observation_workbench.api.client import INatClient
from observation_workbench.api.parsers import parse_observation
from observation_workbench.models import StudyObservation


_USER_SPECIFIC_PARAM_KEYS = frozenset({"reviewed", "viewer_id"})
_REVIEWED_PARAM_KEY = "reviewed"
_VIEWER_ID_PARAM_KEY = "viewer_id"
MAX_OBSERVATION_SCAN = 999_999

# One entry, keyed by the exact token it was resolved with, so a different
# login (or a refreshed token) can never reuse the previous account's ID.
_viewer_id_cache: tuple[str, int] | None = None


def query_requires_authentication(params: Iterable[tuple[str, str]]) -> bool:
    """Return whether an observations query has user-specific filtering.

    Other viewer-scoped filters (beyond `reviewed`) would otherwise run
    unauthenticated and quietly return different results than an
    authenticated identical query.
    """
    return any(key.strip().casefold() in _USER_SPECIFIC_PARAM_KEYS for key, _ in params)


def _param_keys(params: Iterable[tuple[str, str]]) -> set[str]:
    return {str(key).strip().casefold() for key, _ in params}


def requires_viewer_id(params: Iterable[tuple[str, str]]) -> bool:
    """Return whether `reviewed` was supplied without the `viewer_id` it needs.

    The API pairs these two: `reviewed` filters by the user whose ID is given
    in `viewer_id`, and the documentation states `viewer_id` "must be combined
    with the `reviewed` parameter".  Sending `reviewed` alone is not an
    authentication problem the `Authorization` header can solve -- the filter
    is simply not applied, and the query silently returns observations the user
    has already reviewed.
    """
    keys = _param_keys(params)
    return _REVIEWED_PARAM_KEY in keys and _VIEWER_ID_PARAM_KEY not in keys


def resolve_viewer_scoped_params(
    client: INatClient,
    params: Iterable[tuple[str, str]],
    api_token: str,
) -> tuple[tuple[str, str], ...]:
    """Return `params` with the authenticated account's `viewer_id` supplied.

    Callers must route every request that may carry `reviewed` through this so
    a previewed count and the session it previews are filtered identically.
    Unrelated queries are returned unchanged and cost no extra request.
    """
    resolved = tuple((str(key), str(value)) for key, value in params)
    if not requires_viewer_id(resolved):
        return resolved
    if not api_token:
        raise ValueError(
            "Filtering by reviewed requires authentication so the viewer can be identified"
        )
    return (*resolved, (_VIEWER_ID_PARAM_KEY, str(_resolve_viewer_id(client, api_token))))


def _resolve_viewer_id(client: INatClient, api_token: str) -> int:
    global _viewer_id_cache
    cached = _viewer_id_cache
    if cached is not None and cached[0] == api_token:
        return cached[1]
    raw = client.get_current_user_v2(api_token)
    user = raw.get("results") if isinstance(raw, dict) else None
    record = user[0] if isinstance(user, list) and user and isinstance(user[0], dict) else raw
    viewer_id = 0
    if isinstance(record, dict):
        try:
            viewer_id = int(record.get("id") or 0)
        except (TypeError, ValueError):
            viewer_id = 0
    if viewer_id <= 0:
        raise ValueError(
            "Could not identify the authenticated account, so reviewed filtering "
            "cannot be applied faithfully"
        )
    _viewer_id_cache = (api_token, viewer_id)
    return viewer_id


class IdentifySessionCancelled(RuntimeError):
    """Raised when a caller cancels a session build before it is complete."""


@dataclass(frozen=True)
class IdentifyQueryPlan:
    """An immutable snapshot of the query that creates one Identify queue."""

    params: tuple[tuple[str, str], ...]
    sources: tuple[str, ...]
    session_limit: int = 200
    prefetch_radius: int = 3
    source_kind: str = "identify"
    display_url: str = ""

    def __post_init__(self) -> None:
        params = tuple((str(key), str(value)) for key, value in self.params)
        sources = tuple(str(source) for source in self.sources)
        object.__setattr__(self, "params", params)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "session_limit", max(1, int(self.session_limit)))
        object.__setattr__(self, "prefetch_radius", max(0, int(self.prefetch_radius)))
        if len(params) != len(sources):
            raise ValueError("Identify query parameters and sources must remain aligned")

    @property
    def requires_authentication(self) -> bool:
        return query_requires_authentication(self.params)


@dataclass
class IdentifySessionItem:
    observation_id: int
    observation: StudyObservation


@dataclass
class IdentifySession:
    plan: IdentifyQueryPlan
    items: list[IdentifySessionItem] = field(default_factory=list)
    total_results: int = 0

    def replace(self, observation: StudyObservation) -> bool:
        """Replace an observation by stable API ID, never by a list position."""
        for item in self.items:
            if item.observation_id == observation.obs_id:
                item.observation = observation
                return True
        return False

    @property
    def observations(self) -> list[StudyObservation]:
        return [item.observation for item in self.items]


def load_identify_session(
    client: INatClient,
    plan: IdentifyQueryPlan,
    api_token: str = "",
    is_cancelled: Callable[[], bool] | None = None,
) -> IdentifySession:
    """Fetch a bounded, ordered, de-duplicated Identify queue.

    Cancellation is intentionally all-or-nothing.  A user who closes or
    supersedes the planning dialog must never get a partial queue opened later.
    """
    if plan.requires_authentication and not api_token:
        raise ValueError("This Identify query requires authentication")

    # `reviewed` is keyed off `viewer_id`, not off the Authorization header, so
    # resolve the viewer once up front rather than sending a filter the server
    # will ignore.
    _raise_if_cancelled(is_cancelled)
    query_params = resolve_viewer_scoped_params(client, plan.params, api_token)

    items: list[IdentifySessionItem] = []
    seen_ids: set[int] = set()
    page = 1
    total_results = 0
    limit = min(plan.session_limit, MAX_OBSERVATION_SCAN)
    page_size = min(200, limit)

    while len(items) < limit:
        _raise_if_cancelled(is_cancelled)
        raw = client.get_observations(
            query_params,
            page=page,
            per_page=page_size,
            api_token=api_token,
        )
        _raise_if_cancelled(is_cancelled)

        total_results = int(raw.get("total_results") or total_results)
        results = raw.get("results") or []
        if not results:
            break

        new_ids = 0
        for record in results:
            _raise_if_cancelled(is_cancelled)
            observation = parse_observation(record) if isinstance(record, dict) else None
            if observation is None or observation.obs_id in seen_ids:
                continue
            seen_ids.add(observation.obs_id)
            items.append(IdentifySessionItem(observation.obs_id, observation))
            new_ids += 1
            if len(items) >= limit:
                break

        if len(results) < page_size:
            break
        if new_ids == 0:
            break
        if total_results and page * page_size >= total_results:
            break
        if page >= MAX_OBSERVATION_SCAN // page_size:
            break
        page += 1

    _raise_if_cancelled(is_cancelled)
    return IdentifySession(plan=plan, items=items, total_results=total_results)


def _raise_if_cancelled(is_cancelled: Callable[[], bool] | None) -> None:
    if is_cancelled is not None and is_cancelled():
        raise IdentifySessionCancelled("Identify session construction was cancelled")
