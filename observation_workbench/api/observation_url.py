"""Utilities for turning iNaturalist observation URLs into API queries."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

QueryParams = List[Tuple[str, str]]

_INAT_HOSTS = {
    "inaturalist.org",
    "www.inaturalist.org",
    "api.inaturalist.org",
}
_PAGE_PARAM_NAMES = {"page", "per_page"}

# Observation field whose value identifies the source provisional name when a
# bulk-disagree URL filters by it instead of a numeric taxon_id.
PROVISIONAL_SPECIES_FIELD_NAME = "Provisional Species Name"


class ObservationURLParseError(ValueError):
    """Raised when text is a URL, but not a supported iNaturalist observations URL."""


@dataclass(frozen=True)
class ObservationURLQuery:
    display_url: str
    source_key: str
    params: QueryParams
    # Kept optional/defaulted so existing callers constructing this class work.
    source_kind: str = "observations"
    parameter_sources: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Keep provenance positional and backward-compatible for old callers."""
        params = list(self.params)
        sources = list(self.parameter_sources[:len(params)])
        sources.extend(["URL"] * (len(params) - len(sources)))
        object.__setattr__(self, "params", params)
        object.__setattr__(self, "parameter_sources", tuple(sources))


def is_probable_url_input(text: str) -> bool:
    value = text.strip().lower()
    return bool(
        value.startswith(("http://", "https://"))
        or value.startswith(("www.inaturalist.org/", "inaturalist.org/", "api.inaturalist.org/"))
    )


def parse_observations_url(text: str) -> Optional[ObservationURLQuery]:
    """Return an observation API query for a supported iNaturalist URL.

    Non-URL input returns None so existing username behavior can continue.
    iNaturalist URLs are intentionally restricted to observation index/detail
    URLs because those map cleanly to /v1/observations.
    """
    value = text.strip()
    if not value:
        return None
    if not is_probable_url_input(value):
        return None

    display_url = _with_scheme(value)
    parsed = urlparse(display_url)
    host = (parsed.hostname or "").lower()
    if host not in _INAT_HOSTS:
        raise ObservationURLParseError("Only iNaturalist observation URLs are supported.")

    path = _normalise_path(parsed.path)
    params = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in _PAGE_PARAM_NAMES
    ]

    detail_match = re.fullmatch(r"/observations/(\d+)", path)
    is_identify = path == "/observations/identify"
    # Bound once, before the branches, so every path provably has a provenance
    # entry per parameter and adding a branch cannot leave it unassigned.
    sources = ["URL"] * len(params)
    if path == "/observations":
        pass
    elif is_identify:
        # Identify normally supplies these per-user defaults.  Keep them visible
        # and ordered after pasted filters, without overriding an explicit URL.
        existing = {key.casefold() for key, _ in params}
        for key, val in (("quality_grade", "needs_id"), ("reviewed", "false")):
            if key.casefold() not in existing:
                params.append((key, val))
                sources.append("Identify default")
    elif detail_match:
        params.insert(0, ("id", detail_match.group(1)))
        sources.insert(0, "URL")
    else:
        raise ObservationURLParseError(
            "Only iNaturalist /observations URLs can be loaded from this field."
        )

    source_key = _canonical_source_key(params)
    return ObservationURLQuery(
        display_url=display_url, source_key=source_key, params=params,
        source_kind="identify" if is_identify else ("single" if detail_match else "observations"),
        parameter_sources=tuple(sources),
    )


def with_taxon_filter(
    query: ObservationURLQuery,
    taxon_id: Optional[int],
) -> ObservationURLQuery:
    """Return a copy of an observations query narrowed to a taxon subtree."""
    if taxon_id is None:
        return query

    sources = list(query.parameter_sources)
    pairs = [(param, source) for param, source in zip(query.params, sources)
             if param[0].lower() != "taxon_id"]
    params = [param for param, _ in pairs]
    new_sources = [source for _, source in pairs]
    params.append(("taxon_id", str(int(taxon_id))))
    new_sources.append("Applied filter")
    return ObservationURLQuery(
        display_url=query.display_url,
        source_key=_canonical_source_key(params),
        params=params,
        source_kind=query.source_kind,
        parameter_sources=tuple(new_sources),
    )


def extract_single_taxon_id_from_observation_query(query: ObservationURLQuery) -> int:
    """Return the single numeric ``taxon_id`` required by bulk disagree.

    The observations URL parser preserves repeated query parameters, so this
    helper can distinguish a single source taxon from ambiguous multi-taxon
    searches.
    """
    values = [
        val.strip()
        for key, val in query.params
        if key.lower() == "taxon_id"
    ]
    if not values:
        raise ValueError(
            "This bulk disagree workflow requires a URL with a numeric taxon_id."
        )
    if len(values) != 1 or "," in values[0]:
        raise ValueError(
            "This workflow currently supports exactly one source taxon_id. "
            "Please use a URL with a single numeric taxon_id."
        )
    if not values[0].isdigit():
        raise ValueError(
            "This bulk disagree workflow requires a URL with a numeric taxon_id."
        )
    return int(values[0])


def extract_optional_single_taxon_id_from_observation_query(
    query: ObservationURLQuery,
) -> Optional[int]:
    """Return the single numeric ``taxon_id``, or ``None`` when the URL has none.

    Like :func:`extract_single_taxon_id_from_observation_query`, but a URL with
    no ``taxon_id`` is allowed (returns ``None``) so the bulk-disagree workflow
    can run against URLs filtered by something else (e.g. an observation field).
    Ambiguous multi-taxon or non-numeric ``taxon_id`` values are still rejected.
    """
    values = [
        val.strip()
        for key, val in query.params
        if key.lower() == "taxon_id"
    ]
    if not values:
        return None
    if len(values) != 1 or "," in values[0]:
        raise ValueError(
            "This workflow currently supports at most one source taxon_id. "
            "Please use a URL with a single numeric taxon_id, or none."
        )
    if not values[0].isdigit():
        raise ValueError(
            "The taxon_id in this URL is not numeric. Use a single numeric "
            "taxon_id, or a URL with no taxon_id."
        )
    return int(values[0])


def extract_provisional_species_name_from_observation_query(
    query: ObservationURLQuery,
) -> Optional[str]:
    """Return the ``field:Provisional Species Name`` value carried by the URL.

    Bulk disagree URLs can identify their source by a provisional species name
    observation field instead of a numeric ``taxon_id`` (e.g.
    ``field:Provisional Species Name=Hygrocybe sp. 'flavescens-PNW06'``). The
    field-name match is case-insensitive; ``None`` is returned when no such
    filter is present or its value is blank.
    """
    wanted = f"field:{PROVISIONAL_SPECIES_FIELD_NAME}".casefold()
    for key, val in query.params:
        if key.strip().casefold() == wanted:
            value = val.strip()
            if value:
                return value
    return None


def _with_scheme(value: str) -> str:
    if value.lower().startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def _normalise_path(path: str) -> str:
    # The /v1/ branch used to return early WITHOUT stripping the trailing
    # slash, so `.../v1/observations/?taxon_id=47170` normalised to
    # "/observations/" and matched none of the supported shapes below — a
    # supported URL rejected as unsupported. Both branches strip it now.
    if path.startswith("/v1/observations"):
        path = path[3:]
    return path.rstrip("/") or "/"


def _canonical_source_key(params: QueryParams) -> str:
    query = urlencode(params)
    return urlunparse(("https", "www.inaturalist.org", "/observations", "", query, ""))
