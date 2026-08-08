"""
Parsers: raw iNaturalist API JSON → typed dataclasses.

All parsers are defensive — they use .get() throughout and
tolerate missing or None fields gracefully.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from observation_workbench.models import (
    StudyComment,
    StudyIdentification,
    StudyObservation,
    StudyPhoto,
    StudyTaxon,
    StudyVote,
    TaxonCount,
    TaxonSummary,
)

log = logging.getLogger(__name__)


def _parse_optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 0:
            return False
        if value == 1:
            return True
        return None
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def parse_taxon(raw: Optional[Dict]) -> Optional[StudyTaxon]:
    """Parse a taxon object from API JSON. Returns None if raw is None/empty."""
    if not raw:
        return None
    try:
        # `or 0` handles both a missing key AND an explicit null value from the API
        taxon_id = int(raw.get("id") or 0)

        # Fallback chain: each step handles explicit null via `or`
        name = (
            raw.get("name")
            or raw.get("matched_term")
            or raw.get("preferred_common_name")
            or raw.get("english_common_name")
            or ""
        )
        if not name:
            name = "Unknown"

        return StudyTaxon(
            taxon_id=taxon_id,
            name=name,
            common_name=raw.get("preferred_common_name")
            or raw.get("english_common_name")
            or "",
            rank=raw.get("rank") or "",
            iconic_taxon_name=raw.get("iconic_taxon_name") or "",
            ancestry=raw.get("ancestry") or "",
        )
    except Exception as exc:
        log.debug("parse_taxon error: %s | raw=%s", exc, raw)
        return None


def parse_photo(raw: Optional[Dict]) -> Optional[StudyPhoto]:
    """
    Parse a photo object from API JSON.

    iNat API may return photos with:
      - 'url': square URL (most common in observation.photos[])
      - 'square_url': explicit square URL
      - 'original_url': sometimes present

    We prefer 'url' as the square URL for size derivation.
    NOTE: Flickr-hosted photos may use a different URL structure.
    """
    if not raw:
        return None
    try:
        photo_id = int(raw.get("id", 0) or 0)
        if not photo_id:
            return None

        # Try to find a usable square URL for size derivation
        url_square = raw.get("url") or raw.get("square_url") or ""
        if not url_square:
            return None

        return StudyPhoto(
            photo_id=photo_id,
            url_square=url_square,
            attribution=raw.get("attribution", "") or "",
            license_code=raw.get("license_code", "") or "",
        )
    except Exception as exc:
        log.debug("parse_photo error: %s | raw=%s", exc, raw)
        return None


def parse_identification(
    raw: Optional[Dict], target_user_login: str = ""
) -> Optional[StudyIdentification]:
    """
    Parse an identification object from API JSON.

    Fields of note:
      - 'is_leading': bool — whether this ID is currently leading the community
      - 'disagreement': bool or null — whether this ID explicitly disagrees
      - 'body': comment text attached to the identification
      - 'current': bool — whether this is still the user's current ID
    """
    if not raw:
        return None
    try:
        taxon = parse_taxon(raw.get("taxon"))
        if not taxon:
            return None

        user_obj = raw.get("user") or {}
        user_login = user_obj.get("login", "") or raw.get("user_login", "")
        user_id = user_obj.get("id") or raw.get("user_id")
        category = raw.get("category", "") or ""
        is_leading_raw = raw.get("is_leading")
        is_leading = None if is_leading_raw is None else bool(is_leading_raw)
        if is_leading is None and category:
            is_leading = category == "leading"

        return StudyIdentification(
            ident_id=int(raw.get("id", 0)),
            taxon=taxon,
            user_login=user_login,
            user_id=int(user_id) if user_id else None,
            body=raw.get("body", "") or "",
            is_leading=is_leading,
            category=category,
            disagreement=raw.get("disagreement"),  # can be True/False/None
            created_at=raw.get("created_at", "") or "",
            current=bool(raw.get("current", True)),
            own_observation=(
                None
                if raw.get("own_observation") is None
                else bool(raw.get("own_observation"))
            ),
        )
    except Exception as exc:
        log.debug("parse_identification error: %s | raw=%s", exc, raw)
        return None


def parse_comment(raw: Optional[Dict]) -> Optional[StudyComment]:
    """Parse an observation-level comment from iNaturalist JSON."""
    if not raw:
        return None
    try:
        user_obj = raw.get("user") or {}
        return StudyComment(
            comment_id=int(raw.get("id") or 0),
            user_login=user_obj.get("login", "") or raw.get("user_login", "") or "",
            body=raw.get("body", "") or "",
            created_at=raw.get("created_at", "") or "",
            hidden=bool(raw.get("hidden", False)),
        )
    except Exception as exc:
        log.debug("parse_comment error: %s | raw=%s", exc, raw)
        return None


def parse_vote(raw: Optional[Dict]) -> Optional[StudyVote]:
    """Parse an observation vote, including scoped DQA votes such as needs_id."""
    if not raw:
        return None
    try:
        user_obj = raw.get("user") or {}
        vote_flag = raw.get("vote_flag")
        return StudyVote(
            vote_id=int(raw.get("id") or 0),
            user_login=user_obj.get("login", "") or raw.get("user_login", "") or "",
            vote_scope=raw.get("vote_scope", "") or "",
            vote_flag=_parse_optional_bool(vote_flag),
        )
    except Exception as exc:
        log.debug("parse_vote error: %s | raw=%s", exc, raw)
        return None


def _observation_field_value(raw_obs: Dict, field_name: str) -> str:
    """Return an observation field value by field name, across common payload shapes."""
    wanted = field_name.casefold()
    raw_values = (
        raw_obs.get("ofvs")
        or raw_obs.get("observation_field_values")
        or raw_obs.get("observation_fields")
        or []
    )
    for item in raw_values:
        if not isinstance(item, dict):
            continue
        field = item.get("observation_field") or item.get("field") or {}
        name = item.get("name") or item.get("field_name") or field.get("name") or ""
        if name.casefold() != wanted:
            continue
        value = (
            item.get("value")
            or item.get("display_value")
            or item.get("value_text")
            or ""
        )
        if value:
            return str(value)
    return ""


def _enrich_ident_taxon(
    ident: StudyIdentification,
    raw_ident: Dict,
    raw_obs: Dict,
) -> StudyIdentification:
    """
    Try to find a richer taxon object for an identification whose top-level
    taxon parsed to "Unknown".

    The iNat API often includes a more complete taxon object in one of:
      1. raw_obs["identifications"][i] — the same identification nested inside
         the observation (matched by identification id).
      2. raw_obs["taxon"] / raw_obs["community_taxon"] — when the identifier
         used the same taxon as the observation-level taxon.

    Returns the original ident unchanged if no improvement is found.
    """
    ident_id = int(raw_ident.get("id") or 0)
    taxon_id = ident.taxon.taxon_id

    # 1. Look for the same identification inside the observation's nested list.
    for ri in raw_obs.get("identifications") or []:
        if int(ri.get("id") or 0) == ident_id:
            enriched = parse_taxon(ri.get("taxon"))
            if enriched and enriched.name != "Unknown":
                ident.taxon = enriched
            return ident

    # 2. If taxon_id matches the obs-level or community taxon, use that richer copy.
    if taxon_id:
        for raw_tax in (raw_obs.get("taxon"), raw_obs.get("community_taxon")):
            if raw_tax and int(raw_tax.get("id") or 0) == taxon_id:
                enriched = parse_taxon(raw_tax)
                if enriched and enriched.name != "Unknown":
                    ident.taxon = enriched
                return ident

    log.debug(
        "ident %d: taxon still Unknown after enrichment attempt (taxon_id=%s)",
        ident_id,
        taxon_id,
    )
    return ident


def parse_observation(
    raw_obs: Dict, target_identification: Optional[StudyIdentification] = None
) -> Optional[StudyObservation]:
    """
    Parse a StudyObservation from a raw /observations result.

    target_identification is populated only for username identification study
    mode. URL observation queries leave it unset and display the community or
    observation taxon instead.
    """
    if not raw_obs:
        return None
    try:
        obs_id = int(raw_obs.get("id", 0) or 0)
        if not obs_id:
            return None

        # Observer
        observer_obj = raw_obs.get("user") or {}
        observer_login = observer_obj.get("login", "") or ""

        # Observation-level taxon (the taxon as filed by the observer)
        obs_taxon = parse_taxon(raw_obs.get("taxon"))

        # Community taxon (consensus of identifiers)
        community_taxon = parse_taxon(raw_obs.get("community_taxon"))

        # Photos
        raw_photos = raw_obs.get("photos") or []
        photos = [p for raw_p in raw_photos if (p := parse_photo(raw_p)) is not None]

        # All identifications on the observation (may be absent on index results)
        raw_idents = raw_obs.get("identifications") or []
        all_idents = []
        for ri in raw_idents:
            ident = parse_identification(ri)
            if ident:
                all_idents.append(ident)
        if target_identification and not any(
            ident.ident_id == target_identification.ident_id for ident in all_idents
        ):
            all_idents.append(target_identification)

        # Observation-level comments can appear under different keys depending
        # on endpoint/version and privacy/moderation filtering.
        raw_comments = (
            raw_obs.get("comments")
            or raw_obs.get("observation_comments")
            or raw_obs.get("comments_without_flags")
            or []
        )
        comments = []
        for raw_comment in raw_comments:
            comment = parse_comment(raw_comment)
            if comment:
                comments.append(comment)

        votes = []
        for raw_vote in raw_obs.get("votes") or []:
            vote = parse_vote(raw_vote)
            if vote:
                votes.append(vote)

        latitude, longitude = _parse_public_coordinates(raw_obs)

        return StudyObservation(
            obs_id=obs_id,
            observer_login=observer_login,
            uuid=raw_obs.get("uuid", "") or "",
            observed_on=raw_obs.get("observed_on", "")
            or raw_obs.get("observed_on_string", "")
            or "",
            place_guess=raw_obs.get("place_guess", "") or "",
            taxon=obs_taxon,
            community_taxon=community_taxon,
            photos=photos,
            target_identification=target_identification,
            all_identifications=all_idents,
            comments=comments,
            votes=votes,
            provisional_species_name=_observation_field_value(
                raw_obs,
                "Provisional Species Name",
            ),
            species_name_override=_observation_field_value(
                raw_obs,
                "Species Name Override",
            ),
            dna_barcode_its=_observation_field_value(
                raw_obs,
                "DNA Barcode ITS",
            ),
            quality_grade=raw_obs.get("quality_grade", "") or "",
            obscured=bool(raw_obs.get("obscured", False)),
            num_identification_agreements=int(
                raw_obs.get("num_identification_agreements", 0) or 0
            ),
            num_identification_disagreements=int(
                raw_obs.get("num_identification_disagreements", 0) or 0
            ),
            created_at=raw_obs.get("created_at", "") or "",
            latitude=latitude,
            longitude=longitude,
            positional_accuracy=_as_float(raw_obs.get("positional_accuracy")),
            description=raw_obs.get("description", "") or "",
            captive=_parse_optional_bool(raw_obs.get("captive")),
            reviewed_by=_parse_user_ids(raw_obs.get("reviewed_by")),
        )
    except Exception as exc:
        log.warning("parse_observation error: %s", exc)
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_public_coordinates(
    raw_obs: Dict[str, Any],
) -> tuple[Optional[float], Optional[float]]:
    """Return validated public ``(latitude, longitude)`` from a v1 record."""
    geojson = raw_obs.get("geojson")
    if (
        isinstance(geojson, dict)
        and str(geojson.get("type") or "").casefold() == "point"
    ):
        coordinates = geojson.get("coordinates")
        if isinstance(coordinates, (list, tuple)) and len(coordinates) >= 2:
            validated = _validated_coordinates(coordinates[0], coordinates[1])
            if validated is not None:
                return validated

    # Older v1 shapes can expose the same public point as "lat,lon".  Never
    # inspect private_geojson here: the Info tab must show only the public
    # coordinates returned for the observation.
    location = raw_obs.get("location")
    if isinstance(location, str):
        parts = [part.strip() for part in location.split(",")]
        if len(parts) == 2:
            validated = _validated_coordinates(parts[1], parts[0])
            if validated is not None:
                return validated
    return None, None


def _validated_coordinates(
    raw_longitude: Any,
    raw_latitude: Any,
) -> Optional[tuple[float, float]]:
    if isinstance(raw_longitude, bool) or isinstance(raw_latitude, bool):
        return None
    try:
        longitude = float(raw_longitude)
        latitude = float(raw_latitude)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        return None
    if not -180.0 <= longitude <= 180.0 or not -90.0 <= latitude <= 90.0:
        return None
    return latitude, longitude


def _parse_user_ids(value: Any) -> List[int]:
    if not isinstance(value, list):
        return []
    ids: List[int] = []
    for item in value:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return ids


def parse_observation_from_ident(
    raw_ident: Dict, target_user_login: str
) -> Optional[StudyObservation]:
    """
    Parse a StudyObservation from an identification API result.

    The /identifications endpoint returns each identification with a nested
    'observation' object containing full observation data including photos.
    This parser extracts everything we need in one pass.
    """
    if not raw_ident:
        return None
    try:
        target_ident = parse_identification(raw_ident, target_user_login)
        if not target_ident:
            return None

        raw_obs = raw_ident.get("observation")
        if not raw_obs:
            return None

        # The top-level identification's taxon object is sometimes a minimal stub
        # (name=null) while the same identification's taxon nested inside
        # raw_obs["identifications"] tends to be fully populated.  When the
        # first parse produced "Unknown", scan the nested list for a richer copy.
        if target_ident.taxon.name == "Unknown":
            target_ident = _enrich_ident_taxon(target_ident, raw_ident, raw_obs)
        return parse_observation(raw_obs, target_identification=target_ident)
    except Exception as exc:
        log.warning("parse_observation_from_ident error: %s", exc)
        return None


def parse_taxon_summary(raw: Dict) -> TaxonSummary:
    """
    Parse /observations/species_counts response into TaxonSummary.

    Response shape: { 'total_results': N, 'results': [{'count': N, 'taxon': {...}}, ...] }
    """
    results = raw.get("results") or []
    counts = []
    for item in results:
        taxon = parse_taxon(item.get("taxon"))
        if taxon:
            counts.append(TaxonCount(taxon=taxon, count=int(item.get("count", 0))))
    total = int(raw.get("total_results", sum(c.count for c in counts)))
    return TaxonSummary(counts=counts, total=total)
