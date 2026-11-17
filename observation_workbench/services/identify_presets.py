"""Ready-made Identify searches and plain-language descriptions of queries.

Everything here is read-only. The two "my observations" lookups use public,
unauthenticated reads: they only need to know *which* account to look at.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from urllib.parse import urlencode

from observation_workbench.api.client import INatClient
from observation_workbench.dna_linking.types import FUNGI_TAXON_ID
from observation_workbench.services.dna_vote_review import (
    KIND_CONTESTED,
    KIND_OUTVOTED,
    DNAVoteReview,
    VoteReviewConfig,
)

IDENTIFY_URL = "https://www.inaturalist.org/observations/identify"
OBSERVATIONS_URL = "https://www.inaturalist.org/observations"
MAVERICK_PAGE_SIZE = 200
# A scan reads up to this many DNA-barcoded observations you identified.
DNA_SCAN_MAX_OBSERVATIONS = 1_000


class PresetCancelled(RuntimeError):
    """A preset lookup was abandoned because its dialog moved on."""


def fungi_near_url(
    place_id: int, *, only_kingdom: bool = False, signed_in: bool = True
) -> str:
    """URL for fungi in a place that still need identifying.

    Signed in, this is an Identify-page URL, which adds ``quality_grade=needs_id``
    and ``reviewed=false`` itself (see ``parse_observations_url``). Signed out,
    "reviewed" cannot be applied, so an observations URL with only
    ``quality_grade=needs_id`` is used instead of one that would be refused.
    """
    params: list[tuple[str, str]] = [
        ("taxon_id", str(FUNGI_TAXON_ID)),
        ("place_id", str(int(place_id))),
    ]
    if only_kingdom:
        params.append(("rank", "kingdom"))
    if signed_in:
        return f"{IDENTIFY_URL}?{urlencode(params)}"
    params.append(("quality_grade", "needs_id"))
    return f"{OBSERVATIONS_URL}?{urlencode(params)}"


def id_list_url(observation_ids: Sequence[int]) -> str:
    """A real iNaturalist observations URL for an explicit list of IDs."""
    ids = ",".join(str(int(obs_id)) for obs_id in observation_ids)
    return f"{OBSERVATIONS_URL}?{urlencode([('id', ids)], safe=',')}"


def query_params_url(params: Sequence[tuple[str, str]]) -> str:
    """An observations URL that parses back to exactly these parameters."""
    return f"{OBSERVATIONS_URL}?{urlencode(list(params), safe=',')}"


def find_maverick_observation_ids(
    client: INatClient,
    login: str,
    *,
    limit: int,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> list[int]:
    """Observations where ``login``'s current identification is a maverick.

    Maverick means the identification disagrees with the community taxon.
    Newest identifications come first.
    """
    ids: list[int] = []
    seen: set[int] = set()
    page = 1
    limit = max(1, int(limit))
    while len(ids) < limit:
        if is_cancelled():
            raise PresetCancelled()
        raw = client.get_identifications(
            login,
            current_only=True,
            category="maverick",
            page=page,
            per_page=MAVERICK_PAGE_SIZE,
        )
        results = raw.get("results") if isinstance(raw, dict) else None
        if not isinstance(results, list) or not results:
            break
        for record in results:
            obs_id = _identification_observation_id(record)
            if obs_id and obs_id not in seen:
                seen.add(obs_id)
                ids.append(obs_id)
                if len(ids) >= limit:
                    break
        total = raw.get("total_results") if isinstance(raw, dict) else None
        if len(results) < MAVERICK_PAGE_SIZE:
            break
        if isinstance(total, int) and page * MAVERICK_PAGE_SIZE >= total:
            break
        page += 1
    return ids


def find_dna_disagreement_ids(
    client: INatClient,
    login: str,
    user_id: int,
    *,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> list[int]:
    """DNA-barcoded observations where others' identifications oppose yours.

    Uses the same scan as "Review DNA-contested identifications", keeping
    only the "Outvoted" and "Contested" findings, most worth a look first.
    """
    from observation_workbench.services.dna_vote_review import VoteReviewCancelled

    config = VoteReviewConfig(
        login=login,
        login_user_id=int(user_id) if user_id else None,
        max_observations=DNA_SCAN_MAX_OBSERVATIONS,
        include_refinements=False,
        verify_sequence=True,
    )
    try:
        result = DNAVoteReview(client).scan(config, is_cancelled=is_cancelled)
    except VoteReviewCancelled as exc:
        raise PresetCancelled() from exc
    return [
        finding.observation_id
        for finding in result.findings
        if finding.kind in (KIND_OUTVOTED, KIND_CONTESTED) and finding.observation_id
    ]


def _identification_observation_id(record: object) -> int:
    if not isinstance(record, dict):
        return 0
    value = record.get("observation_id")
    if value is None:
        observation = record.get("observation")
        value = observation.get("id") if isinstance(observation, dict) else None
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------------------
# Plain-language decoding

_QUALITY_GRADES = {
    "needs_id": "Needs ID",
    "research": "Research Grade",
    "casual": "Casual",
}
_BOOLEAN_MEANINGS = {
    "reviewed": ("Already reviewed by you", "Not yet reviewed by you"),
    "captive": ("Captive / cultivated only", "Wild only"),
    "photos": ("Has photos", "No photos"),
    "sounds": ("Has sounds", "No sounds"),
    "verifiable": ("Verifiable", "Not verifiable"),
    "identified": ("Has an identification", "No identification yet"),
    "geo": ("Has coordinates", "No coordinates"),
    "popular": ("Has favorites", "No favorites"),
    "mappable": ("Mappable", "Not mappable"),
    "threatened": ("Threatened taxa", "Not threatened taxa"),
    "introduced": ("Introduced taxa", "Not introduced taxa"),
    "native": ("Native taxa", "Not native taxa"),
    "endemic": ("Endemic taxa", "Not endemic taxa"),
}
_USER_PARAMS = {
    "user_id": "Observed by",
    "user_login": "Observed by",
    "ident_user_id": "Identified by",
    "not_user_id": "Not observed by",
    "without_ident_user_id": "Not identified by",
}
_SIMPLE_LABELS = {
    "rank": "Identified only to {value}",
    "hrank": "Identified at {value} or broader",
    "lrank": "Identified at {value} or finer",
    "d1": "Observed on or after {value}",
    "d2": "Observed on or before {value}",
    "created_d1": "Uploaded on or after {value}",
    "created_d2": "Uploaded on or before {value}",
    "month": "Observed in month(s) {value}",
    "year": "Observed in {value}",
    "project_id": "In project {value}",
    "iconic_taxa": "Group: {value}",
    "q": "Matching text “{value}”",
    "term_id": "Annotation term {value}",
    "acc_below": "Location accuracy under {value} m",
    "radius": "Within {value} km",
    "nelat": "Inside a map area",
    "order_by": "Sorted by {value}",
}
_HIDDEN_FROM_LABEL = frozenset(
    {"viewer_id", "swlat", "swlng", "nelng", "subview", "order", "lat", "lng", "locale"}
)


def static_meaning(name: str, value: str) -> str:
    """Meaning of a parameter that needs no lookup, or '' when unknown."""
    key = name.strip().casefold()
    value = value.strip()
    if key == "quality_grade":
        grades = [
            _QUALITY_GRADES.get(part.strip(), part.strip()) for part in value.split(",")
        ]
        return " or ".join(grade for grade in grades if grade)
    if key in _BOOLEAN_MEANINGS:
        yes, no = _BOOLEAN_MEANINGS[key]
        if value.casefold() in {"true", "1", "yes"}:
            return yes
        if value.casefold() in {"false", "0", "no"}:
            return no
        return ""
    if key in _USER_PARAMS and value:
        return f"{_USER_PARAMS[key]} {value}"
    if key == "id" and value:
        count = len([part for part in value.split(",") if part.strip()])
        return (
            "1 specific observation" if count == 1 else f"{count} specific observations"
        )
    if key.startswith("field:"):
        field_name = name.split(":", 1)[1]
        return (
            f"Has a “{field_name}” value" if not value else f"“{field_name}” is {value}"
        )
    if key in _SIMPLE_LABELS and value:
        return _SIMPLE_LABELS[key].format(value=value)
    return ""


def describe_query(
    records: Iterable[tuple[str, str, str]],
    *,
    max_parts: int = 5,
) -> str:
    """One-line description built from (name, API value, resolved meaning) rows.

    ``resolved meaning`` is whatever the setup table shows (taxon and place
    names once resolved); a static meaning is used when it is blank.
    """
    parts: list[str] = []
    hidden = 0
    for name, value, meaning in records:
        key = name.strip().casefold()
        if not key or key in _HIDDEN_FROM_LABEL:
            continue
        text = meaning.strip()
        if text in {"Resolving…", "Unresolved"} or text.startswith("Failed:"):
            text = ""
        if not text:
            text = static_meaning(name, value)
        if key == "without_taxon_id":
            text = f"Excluding {text}" if text else "Excluding some taxa"
        elif key == "place_id" and text:
            text = f"In {text.split(',')[0]}" if len(text) > 40 else f"In {text}"
        if not text:
            text = f"{name}={value}" if value else name
        if len(parts) < max_parts:
            parts.append(text)
        else:
            hidden += 1
    if hidden:
        parts.append(f"+{hidden} more")
    return " · ".join(parts) if parts else "All observations"
