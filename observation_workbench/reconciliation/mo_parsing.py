"""Canonical parsing for current Mushroom Observer API2 representations."""
from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from .normalization import parse_inat_observation_url, public_fingerprint, sequence_digest
from .types import (
    AuthoritativeLinkRow, CoordinatePrivacyState, InventoryObservation, MediaIdentity,
    RemoteRecordKey, RemoteSite,
)

TARGET_UNKNOWN = "target_unknown_due_to_api_capability"

# Mushroom Observer serves every image from a fixed ladder of renditions at
# /images/<size>/<id>.<ext>, smallest first. The images endpoint's own ``files``
# array is exactly this sequence, so the ladder is data, not a guess.
MO_IMAGE_SIZES = ("thumb", "320", "640", "960", "1280", "orig")

# The ``orig`` rendition is available for RECENT images only, and this is a
# property of the image id rather than of anything in the API response.
#
# Mushroom Observer copies every original to cloud archive storage on upload but
# keeps it on the public image server "for as long as we can, deleting them in
# batches" (config/consts.rb). ``next_image_id_to_go_to_cloud`` is the id below
# which the server copies have been deleted; requesting one of those returns
# HTTP 403 from the archive bucket, which denies anonymous reads (403 rather
# than 404 because a caller without list permission cannot be told the
# difference). Above it, ``original_url`` serves the true original -- several
# megabytes, at full camera resolution.
#
# MO does still serve archived originals to PEOPLE, but deliberately rations it:
# ``GET /images/:id/original`` pulls the blob back out of the bucket with the
# server's own Google credentials, caches it for a day, logs an
# OriginalImageRequest row, and charges it against a 100/user/day and
# 10,000/site/day quota. Anonymous callers of that endpoint are handed the 1280
# rendition instead. Draining a rationed human-scale quota from a background
# sync is not a legitimate use of it, so this application never calls that
# endpoint for bytes -- see mo_original_request_url for the one place its URL is
# used, which is handing a human's own browser a working link.
#
# The threshold below was binary-searched against the live server on 2026-08-01
# and landed exactly on a round number, which matches how MO documents it. It
# only ever RISES as more batches are deleted, so a stale value here fails in
# the safe direction: an image that has since been archived returns 403 and the
# download path falls back to the largest fetchable rendition.
MO_ORIGINAL_AVAILABLE_FROM_IMAGE_ID = 1_600_000

# The largest rendition that is always fetchable, for any image, anonymously.
# For a source image smaller than 1280px MO does not upscale, so 960 and 1280
# of such an image are the same picture at the same pixel dimensions; 1280 is
# never WORSE, and is genuinely larger whenever the source exceeds it.
MO_LARGEST_FETCHABLE_SIZE = "1280"
MO_FETCHABLE_IMAGE_SIZES = tuple(
    size for size in MO_IMAGE_SIZES if size != "orig"
)

_MO_IMAGE_PATH = re.compile(
    rf"/images/(?P<size>{'|'.join(MO_IMAGE_SIZES)})/"
)


def mo_image_size(url: str) -> str:
    """Return the ladder rendition an MO image URL points at, or ""."""
    match = _MO_IMAGE_PATH.search((url or "").strip())
    return match.group("size") if match else ""


def mo_image_url(url: str, size: str) -> str:
    """Rewrite an MO image URL to another rendition on the size ladder.

    A URL that does not match the ladder is returned UNCHANGED rather than
    rewritten on a guess: a wrong rewrite is a failed download, and Mushroom
    Observer changing its URL scheme should degrade to "fetch what we were
    given", not to "fetch nothing".
    """
    text = (url or "").strip()
    if not text or size not in MO_IMAGE_SIZES:
        return text
    swapped, count = _MO_IMAGE_PATH.subn(f"/images/{size}/", text, count=1)
    return swapped if count else text


def mo_original_is_public(image_id: object) -> bool:
    """Whether this image's true original can be fetched without credentials.

    An unparseable id answers False, so an unrecognized payload shape falls
    back to the rendition that always works rather than to a certain 403.
    """
    identifier = positive_int(image_id)
    return bool(
        identifier and identifier >= MO_ORIGINAL_AVAILABLE_FROM_IMAGE_ID
    )


def mo_best_downloadable_size(image_id: object) -> str:
    """The highest-fidelity rendition this application may fetch for an image.

    The true original when the image server still holds it, and the largest
    sized rendition otherwise. iNaturalist stores uploads at up to 2048px, so
    for a large photograph the original is meaningfully better than 1280 --
    which is why this is worth deciding per image rather than settling on the
    size that always works.
    """
    return (
        "orig" if mo_original_is_public(image_id)
        else MO_LARGEST_FETCHABLE_SIZE
    )


def mo_image_page_url(image_id: object) -> str:
    """Mushroom Observer's own page for one image.

    This is where an operator must be sent to see an ARCHIVED original. The
    storage URL returns 403 to everyone, including a signed-in operator's
    browser, and MO's ``/images/:id/original`` route is not usable on its own
    either -- its HTML branch redirects straight to the one-day
    ``/orig_cache`` path whether or not the retrieval that fills it has run, so
    it 404s unless something already warmed the cache. Only MO's page drives
    the full flow: sign-in, the quota-checked pull from the archive bucket, and
    the poll until the blob has landed. Reimplementing that state machine here
    to spend a rationed human allowance from a sync tool would be the wrong
    thing to build; handing the operator MO's own page is the right one.
    """
    identifier = positive_int(image_id)
    return f"https://mushroomobserver.org/images/{identifier}" if identifier else ""


def parse_mo_observation(raw: dict[str, Any], account_id: int) -> InventoryObservation:
    """Parse both current low-detail and richer observation shapes consistently."""
    observation_id = positive_int(raw.get("id") or raw.get("observation_id"))
    if not observation_id:
        raise ValueError("MO observation has no positive ID")

    owner = _mapping(raw.get("owner")) or _mapping(raw.get("user"))
    owner_id = positive_int(raw.get("owner_id")) or positive_int(owner) or positive_int(raw.get("user_id"))
    owner_login = str(
        owner.get("login") or owner.get("name") or raw.get("owner_login") or ""
    ).strip()

    consensus = (
        _mapping(raw.get("consensus"))
        or _mapping(raw.get("name"))
        or _mapping(raw.get("consensus_name"))
    )
    consensus_id = (
        positive_int(raw.get("consensus_id"))
        or positive_int(consensus)
        or positive_int(raw.get("name_id"))
    )
    raw_consensus_name = raw.get("consensus_name")
    consensus_name = str(
        consensus.get("text_name")
        or consensus.get("name")
        or (raw_consensus_name if isinstance(raw_consensus_name, str) else "")
        or (raw.get("name") if isinstance(raw.get("name"), str) else "")
        or ""
    ).strip()
    rank = str(consensus.get("rank") or raw.get("consensus_rank") or "").strip()

    location = _mapping(raw.get("location"))
    location_id = positive_int(raw.get("location_id")) or positive_int(location)
    location_name = str(
        raw.get("location_name") or location.get("name") or location.get("display_name") or ""
    ).strip()
    observed = parse_mo_date(raw.get("date") or raw.get("observed_on") or raw.get("when"))
    updated = parse_mo_datetime(raw.get("updated_at") or raw.get("modified"))
    fungi_status = mo_fungi_status(raw, consensus)
    media = _observation_media(raw)
    fingerprint = public_fingerprint(
        observation_id, owner_id, observed, consensus_id, consensus_name, rank,
        location_id, location_name, fungi_status, updated,
        *((item.photo_id, item.source_site, item.source_photo_id) for item in media),
    )
    return InventoryObservation(
        key=RemoteRecordKey(RemoteSite.MO, observation_id), account_id=account_id,
        owner_id=owner_id, owner_login=owner_login, observed_on=observed,
        taxon_id=consensus_id, taxon_name=consensus_name, taxon_rank=rank,
        public_locality=location_name, fungi_status=fungi_status,
        updated_at=updated, content_fingerprint=fingerprint, media=media,
    )


def parse_mo_external_link(
    raw: dict[str, Any], required_site_id: int,
) -> Optional[tuple[int, AuthoritativeLinkRow]]:
    """Parse one link without treating an API capability gap as malformed data."""
    source_id = positive_int(raw.get("observation_id") or raw.get("observation") or raw.get("target"))
    site_id = positive_int(
        raw.get("external_site_id") or raw.get("external_site") or raw.get("site")
    )
    if not source_id or site_id != required_site_id:
        return None
    row_id = str(raw.get("id") or "").strip()
    external_id = positive_int(raw.get("external_id"))
    url_value = str(
        raw.get("url") or raw.get("link_url") or raw.get("derived_url")
        or raw.get("external_url") or ""
    ).strip()
    target_id = external_id or parse_inat_observation_url(url_value)
    if target_id:
        parse_state = "valid"
    elif not url_value:
        # Current API serializers omit external_id and URL-backed imports can
        # legitimately serialize a nil URL. That is unreadable, not malformed.
        parse_state = TARGET_UNKNOWN
    else:
        parse_state = "malformed"
    identity = row_id or f"external:{source_id}:unidentified"
    return source_id, AuthoritativeLinkRow(
        identity, site_id, RemoteSite.INAT, target_id, parse_state,
        public_fingerprint(identity, site_id, target_id, parse_state, url_value),
    )


def mo_record_fingerprint(raw: dict[str, Any]) -> str:
    """Fingerprint all low-detail identity and eligibility fields."""
    record = parse_mo_observation(raw, account_id=0)
    return record.content_fingerprint


def parse_mo_sequence_record(raw: dict[str, Any]) -> Optional[tuple[int, str]]:
    """Return the current observation identity and non-reversible sequence hash."""
    observation_id = positive_int(raw.get("observation_id") or raw.get("observation"))
    digest = sequence_digest(raw.get("sequence") or raw.get("bases") or raw.get("dna_sequence"))
    return (observation_id, digest) if observation_id and digest else None


def parse_mo_coordinate(
    raw: dict[str, Any],
) -> tuple[Optional[float], Optional[float], Optional[float], str]:
    """Extract an MO observation's precise GPS point and privacy state.

    Returns ``(latitude, longitude, accuracy_m, privacy_state)``. Only an
    explicit observation-level GPS point is returned; a named ``location`` area
    (north/south/east/west bounds) is deliberately *not* treated as a copyable
    point, because copying an area centroid as a precise coordinate would be
    misleading. The returned values are for in-memory use only and must never be
    persisted, logged, or placed in payloads/URLs.
    """
    latitude = _coord_float(raw.get("latitude"), raw.get("lat"))
    longitude = _coord_float(raw.get("longitude"), raw.get("lng"), raw.get("long"))
    accuracy = _coord_float(
        raw.get("gps_accuracy"), raw.get("accuracy"), raw.get("positional_accuracy")
    )
    hidden = _is_truthy(
        raw.get("gps_hidden"), raw.get("hidden"), raw.get("location_hidden"),
    )
    readable = (
        latitude is not None and longitude is not None
        and -90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0
    )
    if not readable:
        # ``gps_hidden`` proves a private point exists even though this reader
        # cannot see it — distinct from a genuinely absent coordinate. Either way
        # no readable point is returned, so no copy can be proposed.
        privacy = (
            CoordinatePrivacyState.PRIVATE.value if hidden
            else CoordinatePrivacyState.ABSENT.value
        )
        return None, None, None, privacy
    privacy = (
        CoordinatePrivacyState.PRIVATE.value if hidden
        else CoordinatePrivacyState.PUBLIC.value
    )
    return latitude, longitude, accuracy, privacy


def _coord_float(*values: object) -> Optional[float]:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return None


def _is_truthy(*values: object) -> bool:
    for value in values:
        if isinstance(value, bool):
            if value:
                return True
        elif isinstance(value, (int, float)):
            if value:
                return True
        elif isinstance(value, str):
            if value.strip().casefold() in {"1", "true", "yes", "hidden", "private"}:
                return True
    return False


def positive_int(value: object) -> Optional[int]:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def parse_mo_date(value: object) -> Optional[date]:
    if isinstance(value, dict):
        value = value.get("date") or value.get("start") or value.get("observed_on")
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def parse_mo_datetime(value: object) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def mo_fungi_status(raw: dict[str, Any], consensus: Optional[dict[str, Any]] = None) -> str:
    name = consensus or _mapping(raw.get("consensus")) or _mapping(raw.get("name"))
    classification = name.get("classification") or raw.get("classification")
    if isinstance(classification, dict):
        kingdom = str(classification.get("kingdom") or "").casefold()
    else:
        kingdom = str(classification or raw.get("kingdom") or "").casefold()
    if not kingdom:
        return "unknown"
    return "fungi" if "fungi" in kingdom else "nonfungal"


def _observation_media(raw: dict[str, Any]) -> tuple[MediaIdentity, ...]:
    values: list[MediaIdentity] = []
    images = raw.get("images") or raw.get("image") or []
    if isinstance(images, dict):
        images = [images]
    for item in images if isinstance(images, list) else []:
        if isinstance(item, dict):
            identity = parse_mo_media_identity(item)
            if identity:
                values.append(identity)
    primary = raw.get("primary_image")
    if isinstance(primary, dict):
        identity = parse_mo_media_identity(primary)
        if identity:
            values.append(identity)
    primary_id = positive_int(raw.get("primary_image_id"))
    if primary_id:
        values.append(MediaIdentity(RemoteSite.MO, str(primary_id), "display"))
    unique = {(item.site, item.photo_id, item.rendition): item for item in values}
    return tuple(sorted(unique.values(), key=lambda item: (item.photo_id, item.rendition)))


def mo_observation_photo_count(raw: dict[str, Any]) -> int:
    """Count every distinct photo represented by an MO observation payload.

    Mushroom Observer serializes the primary photo separately from ``images``
    in high-detail observation responses.  Use the same normalized media
    identities as inventory parsing so a primary photo is included without
    being counted twice when an endpoint also includes it in the gallery.
    """
    return len(_observation_media(raw))


def parse_mo_media_identity(raw: dict[str, Any]) -> Optional[MediaIdentity]:
    photo_id = positive_int(raw)
    if not photo_id:
        return None
    source = _mapping(raw.get("source"))
    source_name = " ".join(str(
        raw.get("source_site") or raw.get("original_site")
        or source.get("site") or source.get("name") or ""
    ).casefold().split())
    source_url = str(raw.get("source_url") or raw.get("original_url") or source.get("url") or "")
    source_site: Optional[RemoteSite] = None
    if source_name in {"inaturalist", "inaturalist.org", "i naturalist"}:
        source_site = RemoteSite.INAT
    else:
        try:
            if urlsplit(source_url).hostname in {"inaturalist.org", "www.inaturalist.org"}:
                source_site = RemoteSite.INAT
        except ValueError:
            pass
    source_photo_id = positive_int(
        raw.get("source_photo_id") or raw.get("original_photo_id") or source.get("photo_id")
    )
    return MediaIdentity(
        RemoteSite.MO, str(photo_id), "display", source_site,
        str(source_photo_id) if source_site and source_photo_id else "",
    )


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
