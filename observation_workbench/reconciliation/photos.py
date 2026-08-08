"""Gate 1E-B photo transfer: Mushroom Observer -> iNaturalist.

One reviewed photo per journal group. Never a batch, never automatic.

The shape mirrors Gate 1D (`coordinates.py`): fresh-read preview -> journal one
reviewed action -> preflight re-read -> a single write -> a mandatory
destination re-read that alone decides success.

Three findings from the Gate 1E-A live proof constrain this module
(`docs/gate_1e_capability_report.md`):

* **One call, not two.** The write is the multipart ``POST /observation_photos``,
  which uploads and attaches together. The two-call path leaves a photo attached
  to nothing if the second call fails, and iNaturalist has no
  ``DELETE /photos/{id}`` -- such a photo can never be removed (§2.2, §12).
* **Verify by the observation_photo uuid, not the photo uuid.** A bare photo's
  uuid is not readable back at all; the observation_photo uuid we generate is
  returned by a destination re-read, so it is the only durable recovery key
  (§12.1a). It is journaled *before* the request is sent.
* **The destination license is the account default.** iNaturalist accepts no
  license on upload and this app never re-licenses a photo. The source license
  is carried for display only.
"""

from __future__ import annotations

import logging
import re
import time
import uuid as uuidlib
from dataclasses import dataclass
from typing import Any, Callable, Optional

from observation_workbench.api.auth import AuthState
from observation_workbench.api.client import INatAPIError, INatClient
from .db import ReconciliationDB
from .inat_reader import INatReconciliationReader
from .mo_client import (
    MOAPIError,
    MOClient,
    ReconciliationCancelled,
    results_from_payload,
)
from .mo_parsing import (
    MO_FETCHABLE_IMAGE_SIZES,
    MO_LARGEST_FETCHABLE_SIZE,
    mo_best_downloadable_size,
    mo_image_size,
    mo_image_url,
    mo_observation_photo_count,
    parse_mo_observation,
    positive_int,
)
from .normalization import public_fingerprint
from .photo_license import (
    map_mo_license_to_inat,
    normalized_pixel_fingerprint,
    photo_byte_fingerprint,
    photo_md5,
    pixel_fingerprints_match,
)
from .specimen_state import evaluate_specimen_state
from .types import (
    PhotoActionOption,
    PhotoActionType,
    PhotoComparisonPreview,
    PhotoIdentityPreview,
    PhotoRecordSnapshot,
    ReconciliationProfile,
    RemoteSite,
)

log = logging.getLogger(__name__)

# A source photo larger than this is not downloaded into memory for transfer.
# iNaturalist's own upload limit is well under this; the cap exists so a
# pathological source cannot exhaust memory in a worker thread.
MAX_PHOTO_BYTES = 40 * 1024 * 1024

# Preview downloads each candidate image to fingerprint it, so the digest the
# user reviews is the digest that gets uploaded. This caps how many images one
# observation may cost us.
MAX_PREVIEW_DIGESTS = 12

# iNaturalist processes uploads asynchronously, so an attachment can be accepted
# and still not appear on the very next read. Verification therefore polls a
# bounded number of times before giving any verdict, and a photo never absent
# "yet" is reported unknown rather than failed.
VERIFY_ATTEMPTS = 4
VERIFY_DELAY_S = 2.0


class PhotoSyncError(RuntimeError):
    def __init__(self, message: str, code: str = "photo_sync_unavailable") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class PhotoActionResult:
    action_id: int
    state: str
    message: str


@dataclass(frozen=True)
class _DestinationScan:
    """Result of fingerprinting every photo already on the destination.

    ``complete`` is the safety property: when False, some destination photo
    could not be compared, so we cannot prove the source is not already there
    and no transfer may be enabled.
    """

    fingerprints: tuple[tuple[str, str], ...] = ()
    unchecked: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unchecked

    def matching_photo_id(self, pixels: str) -> str:
        if not pixels:
            return ""
        return next(
            (
                pid
                for pid, fp in self.fingerprints
                if pixel_fingerprints_match(pixels, fp)
            ),
            "",
        )


@dataclass
class _LivePhotoState:
    profile_id: int
    auth_generation: int
    mo_key_generation: int
    inat_token_marker: str
    # Public digest of the Mushroom Observer API key as it stood when the source
    # was read, or "" when no key was configured then. Held so the write
    # boundary can prove the SAME credential context, including a key that
    # appeared or disappeared without the generation counter being observed.
    mo_key_marker: str
    mo_observation_id: int
    inat_observation_id: int
    inat_observation_uuid: str
    inat_record_fingerprint: str
    mo_record_fingerprint: str
    source_photos: tuple[PhotoRecordSnapshot, ...]
    destination_photos: tuple[PhotoRecordSnapshot, ...]
    # observation_photo uuids present at the destination, the verification key.
    destination_observation_photo_uuids: frozenset[str]
    # Blocking message from the shared specimen-identity validator. Non-empty
    # means the two records may no longer describe the same collection, so no
    # photo may cross between them.
    specimen_conflict: str = ""
    specimen_warnings: tuple[str, ...] = ()


class PhotoSyncService:
    """Read-only pair review plus single-write transfer for confirmed pairs."""

    def __init__(
        self,
        db: ReconciliationDB,
        inat_client: INatClient,
        mo_client: MOClient,
        auth_provider: Callable[[], AuthState],
        mo_key_provider: Callable[[int], str],
        auth_generation_provider: Callable[[], int],
        mo_key_generation_provider: Callable[[], int],
    ) -> None:
        self.db = db
        self.inat_client = inat_client
        self.mo_client = mo_client
        self.auth_provider = auth_provider
        self.mo_key_provider = mo_key_provider
        self.auth_generation_provider = auth_generation_provider
        self.mo_key_generation_provider = mo_key_generation_provider

    # Preview ----------------------------------------------------------

    def prepare_identity_preview(
        self,
        profile_id: int,
        pair_id: int,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> PhotoIdentityPreview:
        """Read both complete photo sets without authorizing any write.

        Candidate review must happen before confirmation, while transfer
        planning must happen after it. This path deliberately performs no
        digesting, duplicate scan, journaling, or write eligibility checks.
        """
        pair = self.db.pair_detail(profile_id, pair_id)
        if (
            not pair
            or str(pair.get("review_state") or "") not in {"candidate", "confirmed"}
            or pair.get("excluded")
        ):
            raise PhotoSyncError(
                "Select a candidate or confirmed, non-excluded pair for photo review.",
                "pair_not_reviewable",
            )
        if cancelled():
            raise ReconciliationCancelled("Photo comparison cancelled")
        profile = self.db.profile(profile_id)
        mo_observation_id = int(pair["mo_observation_id"])
        inat_observation_id = int(pair["inat_observation_id"])

        auth = self.auth_provider()
        token = (
            auth.api_token
            if auth.is_authenticated
            and auth.login.casefold() == profile.inat_login.casefold()
            else ""
        )
        inat_raw = _first_result(
            self.inat_client.get_reconciliation_detail(
                inat_observation_id, token, deep=True
            )
        )
        if not inat_raw or positive_int(inat_raw.get("id")) != inat_observation_id:
            raise PhotoSyncError(
                "The iNaturalist observation could not be read for photo review.",
                "inat_read_failed",
            )
        mo_raw = _first_result(
            self.mo_client.observation(mo_observation_id, cancelled, detail="high")
        )
        if not mo_raw or positive_int(mo_raw.get("id")) != mo_observation_id:
            raise PhotoSyncError(
                "The Mushroom Observer observation could not be read for photo review.",
                "mo_read_failed",
            )
        mo_payload = self.mo_client.images_for_observation(mo_observation_id, cancelled)
        mo_photos = _mo_photo_snapshots(mo_payload, mo_observation_id)
        inat_photos = _inat_photo_snapshots(inat_raw, inat_observation_id)
        warnings: list[str] = []
        if not mo_photos:
            warnings.append("Mushroom Observer returned no displayable photos.")
        if not inat_photos:
            warnings.append("iNaturalist returned no displayable photos.")
        # The observation read above is not spent solely on validating the id:
        # the observation and the images endpoint are separate queries, and a
        # gallery that returns fewer photos than the observation itself claims
        # means this comparison is being made against an incomplete set. Say so
        # rather than letting the reviewer read a short set as the whole set.
        expected_photos = mo_observation_photo_count(mo_raw)
        if expected_photos > len(mo_photos):
            warnings.append(
                f"Mushroom Observer lists {expected_photos} photos for this "
                f"observation but returned {len(mo_photos)}. The comparison "
                "below is incomplete."
            )
        return PhotoIdentityPreview(
            profile_id=profile_id,
            pair_id=pair_id,
            review_state=str(pair["review_state"]),
            mo_observation_id=mo_observation_id,
            inat_observation_id=inat_observation_id,
            mo_photos=mo_photos,
            inat_photos=inat_photos,
            warnings=tuple(warnings),
        )

    def prepare_preview(
        self,
        profile_id: int,
        pair_id: int,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> PhotoComparisonPreview:
        pair = self._eligible_pair(profile_id, pair_id)
        profile = self.db.profile(profile_id)
        live = self._refresh(profile, pair, cancelled)
        warnings: list[str] = list(live.specimen_warnings)
        # Fingerprint the candidate images HERE, during the explicit preview, so
        # the digest the user reviews is the digest that is later uploaded and
        # so duplicate evidence can actually be shown before anything is sent.
        digests = self._preview_digests(live, warnings, cancelled)
        scan = self._destination_pixel_fingerprints(live, cancelled)
        options = self._options(profile_id, live, warnings, digests, scan)
        if not options and not warnings:
            warnings.append(
                "No photo on Mushroom Observer needs transferring to iNaturalist."
            )
        return PhotoComparisonPreview(
            profile_id=profile_id,
            pair_id=pair_id,
            auth_generation=live.auth_generation,
            mo_key_generation=live.mo_key_generation,
            source_fingerprint=_pair_fingerprint(pair),
            mo_observation_id=live.mo_observation_id,
            inat_observation_id=live.inat_observation_id,
            inat_observation_uuid=live.inat_observation_uuid,
            inat_record_fingerprint=live.inat_record_fingerprint,
            mo_record_fingerprint=live.mo_record_fingerprint,
            source_photos=live.source_photos,
            destination_photos=live.destination_photos,
            options=tuple(options),
            warnings=tuple(warnings),
        )

    def _destination_pixel_fingerprints(
        self,
        live: _LivePhotoState,
        cancelled: Callable[[], bool],
    ) -> _DestinationScan:
        """The third duplicate signal: what is ALREADY on the destination.

        The ledger only knows about transfers this application performed, so it
        cannot see a photo added manually, by another tool, or before the ledger
        existed. Only enumerating the destination catches those.

        A byte digest is useless here because iNaturalist re-encodes everything
        it stores, so each destination photo is compared by normalized pixel
        fingerprint instead. A medium rendition is fetched rather than the
        square one, which is *cropped* and would not correspond to the source.
        That fingerprint is best-effort: it handles iNaturalist's re-encoding and
        rescaling, but a cropped or heavily edited copy can still evade it, which
        is why it is one of three duplicate signals rather than the only one.

        **This scan must be complete or the transfer is refused.** Every
        destination photo is checked -- there is no cap, because an unchecked
        destination photo may already BE the image we are about to upload, and
        the resulting duplicate cannot be deleted. Any photo that cannot be
        fetched or decoded makes the scan incomplete, which disables the
        transfer rather than merely warning about it.
        """
        fingerprints: list[tuple[str, str]] = []
        unchecked: list[str] = []
        for photo in live.destination_photos:
            if cancelled():
                raise ReconciliationCancelled("Photo comparison cancelled")
            url = _inat_comparable_url(photo.source_url)
            if not url:
                unchecked.append(f"{photo.photo_id} (no comparable image URL)")
                continue
            try:
                data = self.inat_client.download_image(url)
            except Exception:
                unchecked.append(f"{photo.photo_id} (could not be downloaded)")
                continue
            digest = normalized_pixel_fingerprint(data)
            if not digest:
                unchecked.append(f"{photo.photo_id} (could not be decoded)")
                continue
            fingerprints.append((photo.photo_id, digest))
        return _DestinationScan(tuple(fingerprints), tuple(unchecked))

    def _preview_digests(
        self,
        live: _LivePhotoState,
        warnings: list[str],
        cancelled: Callable[[], bool],
    ) -> dict[str, tuple[str, str]]:
        """Download each candidate image during the preview and digest it twice.

        Returns ``photo_id -> (byte_fingerprint, pixel_fingerprint)``. The byte
        digest pins the exact reviewed content for the write; the pixel digest is
        what can be compared against a re-encoded copy already at the destination.

        The bytes are held only long enough to hash them and are never persisted.
        A download failure is not fatal: the affected option is disabled later,
        because an image we cannot fingerprint is an image whose content and
        duplicate status we cannot show the user.
        """
        if live.specimen_conflict:
            return {}
        candidates = [p for p in live.source_photos if p.source_url]
        if len(candidates) > MAX_PREVIEW_DIGESTS:
            warnings.append(
                f"This observation has {len(candidates)} images; only the first "
                f"{MAX_PREVIEW_DIGESTS} were fingerprinted for review."
            )
            candidates = candidates[:MAX_PREVIEW_DIGESTS]
        digests: dict[str, tuple[str, str]] = {}
        for photo in candidates:
            if cancelled():
                raise ReconciliationCancelled("Photo comparison cancelled")
            try:
                data = self._download(photo)
                digests[photo.photo_id] = (
                    photo_byte_fingerprint(data),
                    normalized_pixel_fingerprint(data),
                )
            except PhotoSyncError as exc:
                warnings.append(
                    f"Image {photo.photo_id} could not be fingerprinted: {exc}"
                )
        return digests

    def _options(
        self,
        profile_id: int,
        live: _LivePhotoState,
        warnings: list[str],
        digests: dict[str, tuple[str, str]],
        scan: _DestinationScan,
    ) -> list[PhotoActionOption]:
        # A photo must never cross between records that may no longer describe
        # the same physical collection.
        if live.specimen_conflict:
            warnings.append(
                "Photo transfer is blocked while specimen-identity evidence conflicts: "
                + live.specimen_conflict
            )
            return []
        if not live.source_photos:
            warnings.append(
                "Mushroom Observer exposes no image on this observation that is owned by "
                "your account."
            )
            return []
        # Fail closed: without a complete picture of the destination we cannot
        # prove a photo is not already there, and a duplicate upload is
        # irreversible. No option is offered at all.
        if not scan.complete:
            warnings.append(
                "Photo transfer is blocked because these photos already on the iNaturalist "
                "observation could not be compared: "
                + "; ".join(scan.unchecked)
                + ". Until every existing photo can be checked, a transfer could silently "
                "duplicate one of them."
            )
            return []
        account_holder = self._account_holder(profile_id)
        # Signal 1, authoritative for our own prior work: the local ledger.
        already_sent = self.db.transferred_source_photo_ids(
            profile_id, RemoteSite.MO, live.inat_observation_id
        )
        # Signal 2: content digests of what we previously sent TO THIS
        # observation. Scoped to the destination on purpose -- the same image
        # legitimately belongs to two different confirmed specimen pairs, so a
        # profile-wide prohibition would block correct transfers.
        known_digests = self.db.transferred_photo_digests(
            profile_id, destination_observation_id=live.inat_observation_id
        )
        # Signal 2b, advisory only: the same content sent to a DIFFERENT
        # observation. Surfaced so the user can notice a mis-paired record, but
        # never used to disable.
        elsewhere = self.db.transferred_photo_digests(profile_id)
        options: list[PhotoActionOption] = []
        for photo in live.source_photos:
            if photo.photo_id in already_sent:
                warnings.append(
                    f"Mushroom Observer image {photo.photo_id} has already been transferred to this "
                    "iNaturalist observation; it is not offered again."
                )
                continue
            if not photo.source_url:
                warnings.append(
                    f"Mushroom Observer image {photo.photo_id} exposes no readable file URL."
                )
                continue
            holder = photo.copyright_holder.strip()
            # Authorship is proven, not assumed. The transfer is authorized only
            # when a verified profile login and a recorded holder both exist and
            # match; a missing login and a missing holder are unproven claims,
            # not absent objections, so each refuses on its own.
            holder_differs = not (
                holder
                and account_holder
                and holder.casefold() == account_holder.casefold()
            )
            verdict = map_mo_license_to_inat(photo.license_label)
            digest, pixels = digests.get(photo.photo_id, ("", ""))
            duplicate_of = known_digests.get(digest, "") if digest else ""
            # Signal 3: content already ON the destination, whoever put it
            # there. This is the only signal that sees a photo added manually,
            # by another tool, or before this ledger existed.
            already_there = scan.matching_photo_id(pixels)
            disabled_reason = ""
            # Accepting the destination account's default license does NOT grant
            # permission to republish an all-rights-reserved or unlicensed image,
            # so an unsupported source license disables the transfer outright.
            if not verdict.eligible:
                disabled_reason = (
                    f"The Mushroom Observer license '{photo.license_label or 'none recorded'}' is "
                    "all-rights-reserved or unrecognised. Uploading it under your iNaturalist "
                    "default license would republish it without permission."
                )
            elif not account_holder:
                # Irreversible write authorization must never rest on an
                # assumption the UI happens to enforce. Without a verified
                # Mushroom Observer login on the profile there is nothing to
                # compare the holder against, so every photo -- foreign holder
                # or empty holder alike -- is refused.
                disabled_reason = (
                    "The reconciliation profile has no verified Mushroom Observer login, so this "
                    "image's authorship cannot be confirmed before republishing it under your "
                    "iNaturalist account."
                )
            elif not holder:
                disabled_reason = (
                    "This image records no copyright holder, so its authorship cannot be "
                    "confirmed before republishing it under your account."
                )
            elif holder.casefold() != account_holder.casefold():
                # Uploading under this account credits it as the photographer.
                # With no attribution-note workflow yet, that can breach the
                # source license's attribution term, so a mismatch is refused
                # outright rather than confirmed away by the user.
                #
                # The comparison is exact against the profile's Mushroom Observer
                # login, which is deliberately strict: a holder recorded as a
                # real name rather than a login will not match and will be
                # refused. Relaxing that needs explicitly configured, verified
                # holder aliases -- not a looser string match.
                disabled_reason = (
                    f"Mushroom Observer credits this image to '{holder}', not '{account_holder}'. "
                    "Uploading it would attribute it to your iNaturalist account and drop that "
                    "credit, which the source license may not permit."
                )
            elif already_there:
                disabled_reason = (
                    f"This image already appears on the destination observation as iNaturalist "
                    f"photo {already_there}; transferring it would duplicate it."
                )
            elif duplicate_of:
                disabled_reason = (
                    f"This image is identical to a photo already transferred here "
                    f"({duplicate_of}); transferring it again would duplicate it."
                )
            elif not digest:
                disabled_reason = (
                    "The image could not be downloaded to fingerprint it, so neither its content "
                    "nor its duplicate status can be confirmed."
                )
            elif not pixels:
                disabled_reason = (
                    "The image could not be decoded, so it cannot be compared against the photos "
                    "already on the destination observation."
                )
            elif digest in elsewhere:
                warnings.append(
                    f"Image {photo.photo_id} has the same content as a photo already transferred "
                    f"to a different observation ({elsewhere[digest]}). Confirm the pairing."
                )
            option = PhotoActionOption(
                action_type=PhotoActionType.INAT_PHOTO_ATTACH,
                source_site=RemoteSite.MO,
                destination_site=RemoteSite.INAT,
                source_photo_id=photo.photo_id,
                source_record_id=live.mo_observation_id,
                destination_record_id=live.inat_observation_id,
                source_license_label=photo.license_label,
                # iNaturalist accepts no license on upload, so state plainly what
                # the photo will actually be licensed as at the destination.
                destination_license_note=(
                    "Uploads take your iNaturalist account's default photo license. "
                    f"The Mushroom Observer license is '{photo.license_label or 'unknown'}'"
                    + (
                        f" (equivalent to {verdict.inat_license_code})."
                        if verdict.inat_license_code
                        else "."
                    )
                ),
                source_copyright_holder=holder,
                holder_differs=holder_differs,
                byte_fingerprint=digest,
                description=(
                    f"Attach Mushroom Observer image {photo.photo_id} to iNaturalist "
                    f"observation {live.inat_observation_id}"
                ),
                enabled=not disabled_reason,
                disabled_reason=disabled_reason,
            )
            if disabled_reason:
                warnings.append(f"Image {photo.photo_id}: {disabled_reason}")
            options.append(option)
        # One reviewed photo per journal group; the caller picks which.
        return options

    # Execution --------------------------------------------------------

    def execute_group(
        self,
        profile_id: int,
        group_id: int,
        cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> list[PhotoActionResult]:
        rows = self.db.action_group_rows(profile_id, group_id)
        if len(rows) != 1 or not _is_photo_action(
            rows[0].get("action_type") if rows else None
        ):
            raise PhotoSyncError(
                "A photo journal group must contain exactly one individually reviewed action.",
                "invalid_photo_group",
            )
        row = rows[0]
        state = str(row["state"])
        if state == "outcome_unknown":
            return [self.verify_unknown(profile_id, int(row["action_id"]), cancelled)]
        if state == "succeeded":
            return []
        if state != "pending":
            return [
                PhotoActionResult(
                    int(row["action_id"]),
                    state,
                    "This photo action is terminal; create a fresh comparison for another transfer.",
                )
            ]
        return [self._execute(row, cancelled, progress)]

    def verify_unknown(
        self,
        profile_id: int,
        action_id: int,
        cancelled: Callable[[], bool],
    ) -> PhotoActionResult:
        """Resolve a lost response WITHOUT re-uploading.

        A re-upload would create a second photo, so the only safe move is to look
        for the planned observation_photo uuid at the destination.
        """
        row = self.db.action(profile_id, action_id)
        if not row or not _is_photo_action(row.get("action_type")):
            raise PhotoSyncError(
                "The selected journal row is not a photo action.",
                "invalid_photo_action",
            )
        if str(row["state"]) != "outcome_unknown":
            return PhotoActionResult(
                action_id, str(row["state"]), "No unknown outcome remains to verify."
            )
        pair = self._pair_from_journal(profile_id, row)
        profile = self.db.profile(profile_id)
        planned = str(row["planned_observation_photo_uuid"])
        try:
            live = self._await_attachment(profile, pair, planned, cancelled)
        except Exception:
            return PhotoActionResult(
                action_id,
                "outcome_unknown",
                "Destination reread is still unavailable; the upload was not retried.",
            )
        if planned and planned in live.destination_observation_photo_uuids:
            landed = next(
                (
                    p
                    for p in live.destination_photos
                    if p.observation_photo_uuid == planned
                ),
                None,
            )
            self.db.finish_action(
                profile_id,
                action_id,
                "succeeded",
                phase="verification",
                verification_state="verified_after_unknown",
            )
            self.db.finish_photo_transfer(
                profile_id,
                action_id,
                "succeeded",
                destination_photo_id=landed.photo_id if landed else "",
                destination_license_code=landed.license_label if landed else "",
            )
            return PhotoActionResult(
                action_id,
                "succeeded",
                "Verified the prior upload landed; it was not sent again.",
            )
        # Deliberately NOT resolved to "failed". This path has no response in
        # hand -- there is no evidence the request was rejected before
        # acceptance, only that the photo is not visible yet. Marking it failed
        # would offer a retry that could duplicate an accepted upload, and the
        # duplicate could never be deleted. It stays unknown until the photo
        # appears or the operator resolves it deliberately.
        self.db.finish_action(
            profile_id,
            action_id,
            "outcome_unknown",
            phase="verification",
            error_code="not_yet_visible",
            verification_state="changed_not_proven",
        )
        return PhotoActionResult(
            action_id,
            "outcome_unknown",
            "The uploaded photo is still not visible on the iNaturalist observation. This does "
            "not prove it was rejected — check the observation on iNaturalist before retrying, "
            "because a second upload would create a duplicate that cannot be deleted.",
        )

    def _execute(
        self,
        row: dict[str, Any],
        cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> PhotoActionResult:
        profile_id = int(row["profile_id"])
        action_id = int(row["action_id"])
        action_type = str(row.get("action_type") or "")
        if action_type != PhotoActionType.INAT_PHOTO_ATTACH.value:
            # Only the proven MO->iNat direction has a working executor.
            # ``mo_photo_attach`` (Gate 2A's placeholder for the unimplemented
            # iNat->MO direction) must NEVER reach here and fall through to
            # ``_write``, which unconditionally calls
            # ``create_observation_photo_v2`` (an iNat upload) regardless of
            # the row's actual direction — that would misdirect a write at
            # the wrong site. Rejected before any claim, read, or network
            # call, so a legacy, malformed, or manually repaired row can
            # never dispatch to the wrong write method. This is a hard
            # backstop independent of whichever caller reached ``_execute``
            # (Gate 2A's ``execute_group``, or a direct resume/verify call).
            self.db.finish_action(
                profile_id,
                action_id,
                "failed",
                phase="preview",
                error_code="unsupported_photo_direction",
            )
            return PhotoActionResult(
                action_id,
                "failed",
                f"'{action_type}' has no supported photo-transfer executor; no network request was made.",
            )
        if not self.db.claim_action(profile_id, action_id, "resource_preflight"):
            current = self.db.action(profile_id, action_id) or row
            return PhotoActionResult(
                action_id,
                str(current["state"]),
                "The photo action is no longer pending.",
            )
        write_started = False
        try:
            if cancelled():
                raise ReconciliationCancelled("Photo action cancelled")
            progress(f"Photo action {action_id}: rereading source and destination")
            pair = self._eligible_pair(profile_id, int(row["pair_id"]))
            self._require_current_source(row, pair)
            profile = self.db.profile(profile_id)
            live = self._refresh(profile, pair, cancelled)

            planned = str(row["planned_observation_photo_uuid"])
            if not planned:
                raise PhotoSyncError(
                    "This action has no planned observation_photo uuid; it cannot be verified after a write.",
                    "missing_planned_uuid",
                )
            # Already landed (e.g. a resumed crash): never upload a second copy.
            if planned in live.destination_observation_photo_uuids:
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "succeeded",
                    phase="verification",
                    verification_state="already_correct",
                )
                self.db.finish_photo_transfer(profile_id, action_id, "succeeded")
                return PhotoActionResult(
                    action_id,
                    "succeeded",
                    "The photo is already attached; no upload was sent.",
                )
            self._require_unchanged_context(row, live)

            source_photo_id = str(row["source_photo_id"])
            source = next(
                (p for p in live.source_photos if p.photo_id == source_photo_id), None
            )
            if source is None or not source.source_url:
                raise PhotoSyncError(
                    "The Mushroom Observer image is no longer readable on the source observation.",
                    "source_changed",
                )

            if live.specimen_conflict:
                raise PhotoSyncError(
                    "Fresh specimen-identity evidence conflicts; no photo was transferred: "
                    + live.specimen_conflict,
                    "specimen_conflict",
                )

            progress(f"Photo action {action_id}: downloading source image")
            image_bytes = self._download(source)
            fingerprint = photo_byte_fingerprint(image_bytes)
            digest = photo_md5(image_bytes)
            # The bytes must be the exact bytes reviewed. A stable Mushroom
            # Observer id whose file changed after preview would otherwise upload
            # content the user never saw.
            reviewed = str(row["reviewed_byte_fingerprint"] or "")
            if reviewed and reviewed != fingerprint:
                raise PhotoSyncError(
                    "The Mushroom Observer image content changed after it was reviewed; "
                    "nothing was uploaded.",
                    "source_content_changed",
                )

            # Downloading a full-size image takes real time, during which the
            # pair, either record, the destination photo set, or authentication
            # can all change -- and the user may have cancelled. Everything
            # validated before the download is therefore revalidated here,
            # immediately before the only irreversible step.
            if cancelled():
                raise ReconciliationCancelled("Photo action cancelled")
            progress(f"Photo action {action_id}: final pre-write check")
            pair = self._eligible_pair(profile_id, int(row["pair_id"]))
            self._require_current_source(row, pair)
            live = self._refresh(profile, pair, cancelled)
            if live.specimen_conflict:
                raise PhotoSyncError(
                    "Fresh specimen-identity evidence conflicts; no photo was transferred: "
                    + live.specimen_conflict,
                    "specimen_conflict",
                )
            self._require_unchanged_context(row, live)
            if planned in live.destination_observation_photo_uuids:
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "succeeded",
                    phase="verification",
                    verification_state="already_correct",
                )
                self.db.finish_photo_transfer(
                    profile_id,
                    action_id,
                    "succeeded",
                    byte_fingerprint=fingerprint,
                    md5=digest,
                )
                return PhotoActionResult(
                    action_id,
                    "succeeded",
                    "The photo is already attached; no upload was sent.",
                )
            # Re-run the duplicate signals against the just-read destination:
            # the same photo may have arrived by another route while we
            # downloaded, and a duplicate cannot be undone.
            self._require_not_duplicate(
                profile_id, action_id, live, image_bytes, fingerprint, cancelled
            )
            if cancelled():
                raise ReconciliationCancelled("Photo action cancelled")

            # The duplicate scan itself performs one download per destination
            # photo, which is another window in which the pair, either record or
            # the destination photo set can change. Re-read once more and
            # require the state to be EXACTLY the state that was just checked --
            # in particular the destination photo-set fingerprint, so a photo
            # attached during the scan cannot slip past unexamined.
            pair = self._eligible_pair(profile_id, int(row["pair_id"]))
            self._require_current_source(row, pair)
            settled = self._refresh(profile, pair, cancelled)
            if settled.specimen_conflict:
                raise PhotoSyncError(
                    "Fresh specimen-identity evidence conflicts; no photo was transferred: "
                    + settled.specimen_conflict,
                    "specimen_conflict",
                )
            if (
                settled.inat_record_fingerprint != live.inat_record_fingerprint
                or settled.mo_record_fingerprint != live.mo_record_fingerprint
            ):
                raise PhotoSyncError(
                    "The observations changed while duplicate checks were running; nothing was "
                    "uploaded. Run a fresh photo comparison.",
                    "state_changed_during_checks",
                )
            self._require_unchanged_context(row, settled)
            if planned in settled.destination_observation_photo_uuids:
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "succeeded",
                    phase="verification",
                    verification_state="already_correct",
                )
                self.db.finish_photo_transfer(
                    profile_id,
                    action_id,
                    "succeeded",
                    byte_fingerprint=fingerprint,
                    md5=digest,
                )
                return PhotoActionResult(
                    action_id,
                    "succeeded",
                    "The photo is already attached; no upload was sent.",
                )
            live = settled
            if cancelled():
                raise ReconciliationCancelled("Photo action cancelled")

            if not self.db.mark_action_write_started(profile_id, action_id):
                raise PhotoSyncError(
                    "This action left its claimed state before the write boundary. "
                    "No photo was uploaded.",
                    "write_boundary_lost",
                )
            write_started = True
            progress(
                f"Photo action {action_id}: uploading one explicitly confirmed photo"
            )
            write_error: Optional[Exception] = None
            http_status: Optional[int] = None
            try:
                response = self._write(live, source, image_bytes, planned)
                metadata = getattr(response, "metadata", None)
                http_status = getattr(metadata, "status_code", None)
            except (INatAPIError, MOAPIError) as exc:
                write_error = exc
                http_status = getattr(exc, "status_code", None)

            progress(f"Photo action {action_id}: verifying the attachment")
            try:
                verified = self._await_attachment(profile, pair, planned)
            except Exception:
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "outcome_unknown",
                    phase="verification",
                    error_code="verification_unavailable",
                    http_status=http_status,
                    verification_state="unavailable",
                )
                self.db.finish_photo_transfer(
                    profile_id,
                    action_id,
                    "outcome_unknown",
                    byte_fingerprint=fingerprint,
                    md5=digest,
                )
                return PhotoActionResult(
                    action_id,
                    "outcome_unknown",
                    "The upload may have been submitted, but destination verification is unavailable.",
                )
            if planned in verified.destination_observation_photo_uuids:
                landed = next(
                    (
                        p
                        for p in verified.destination_photos
                        if p.observation_photo_uuid == planned
                    ),
                    None,
                )
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "succeeded",
                    phase="verification",
                    http_status=http_status,
                    verification_state="verified_final_state",
                )
                self.db.finish_photo_transfer(
                    profile_id,
                    action_id,
                    "succeeded",
                    destination_photo_id=landed.photo_id if landed else "",
                    byte_fingerprint=fingerprint,
                    md5=digest,
                    destination_license_code=landed.license_label if landed else "",
                )
                return PhotoActionResult(
                    action_id, "succeeded", "Verified the attached iNaturalist photo."
                )
            # "Failed" is only safe when the server positively REJECTED the
            # request before accepting it: a definite client-error status with a
            # response in hand. Everything else -- lost transport, 5xx, or simply
            # not visible yet after the polling window -- stays unknown, because
            # iNaturalist accepts uploads asynchronously and turning an ambiguous
            # upload into a retryable failure invites a duplicate photo that can
            # never be deleted.
            definitely_rejected = (
                write_error is not None
                and bool(getattr(write_error, "response_received", False))
                and not bool(getattr(write_error, "outcome_unknown", False))
                and _is_client_rejection(getattr(write_error, "status_code", None))
            )
            if definitely_rejected:
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "failed",
                    phase="verification",
                    error_code="verified_not_applied",
                    http_status=http_status,
                    verification_state="verified_not_applied",
                )
                self.db.finish_photo_transfer(
                    profile_id,
                    action_id,
                    "failed",
                    byte_fingerprint=fingerprint,
                    md5=digest,
                )
                return PhotoActionResult(
                    action_id,
                    "failed",
                    f"iNaturalist rejected the upload (HTTP {http_status}); nothing was attached.",
                )
            code = (
                "write_outcome_unknown"
                if write_error is not None
                and bool(getattr(write_error, "outcome_unknown", False))
                else "changed_not_proven"
            )
            self.db.finish_action(
                profile_id,
                action_id,
                "outcome_unknown",
                phase="verification",
                error_code=code,
                http_status=http_status,
                verification_state="changed_not_proven",
            )
            self.db.finish_photo_transfer(
                profile_id,
                action_id,
                "outcome_unknown",
                byte_fingerprint=fingerprint,
                md5=digest,
            )
            return PhotoActionResult(
                action_id,
                "outcome_unknown",
                "The upload result could not be proven within the verification window. "
                "Verify it before any retry — re-uploading would create a duplicate photo "
                "that cannot be deleted.",
            )
        except ReconciliationCancelled:
            self.db.finish_action(
                profile_id,
                action_id,
                "cancelled",
                phase="resource_preflight",
                error_code="user_cancelled",
            )
            self.db.finish_photo_transfer(profile_id, action_id, "failed")
            return PhotoActionResult(
                action_id, "cancelled", "Cancelled before a photo was uploaded."
            )
        except PhotoSyncError as exc:
            self.db.finish_action(
                profile_id,
                action_id,
                "outcome_unknown" if write_started else "failed",
                phase="verification" if write_started else "resource_preflight",
                error_code=exc.code,
            )
            self.db.finish_photo_transfer(
                profile_id,
                action_id,
                "outcome_unknown" if write_started else "failed",
            )
            if write_started:
                return PhotoActionResult(
                    action_id,
                    "outcome_unknown",
                    "An upload may have been submitted; verify before any retry. "
                    + str(exc),
                )
            return PhotoActionResult(action_id, "failed", str(exc))
        except Exception:
            terminal = "outcome_unknown" if write_started else "failed"
            self.db.finish_action(
                profile_id,
                action_id,
                terminal,
                phase="verification" if write_started else "resource_preflight",
                error_code=(
                    "local_journal_failure" if write_started else "preflight_failed"
                ),
            )
            self.db.finish_photo_transfer(profile_id, action_id, terminal)
            return PhotoActionResult(
                action_id,
                terminal,
                (
                    "The upload outcome must be verified before any retry."
                    if write_started
                    else "Photo preflight failed before an upload was sent."
                ),
            )

    def _write(
        self,
        live: _LivePhotoState,
        source: PhotoRecordSnapshot,
        image_bytes: bytes,
        planned_uuid: str,
    ) -> object:
        """The single multipart upload+attach call. Never the two-call path."""
        auth = self._recheck_credentials(live)
        filename = _safe_filename(source)
        return self.inat_client.create_observation_photo_v2(
            auth.api_token,
            live.inat_observation_uuid,
            image_bytes=image_bytes,
            filename=filename,
            content_type=_content_type(filename),
            client_uuid=planned_uuid,
        )

    def _download(self, source: PhotoRecordSnapshot) -> bytes:
        try:
            data = self.inat_client.download_image(source.source_url)
        except Exception as exc:
            # MO deletes originals from the image server in batches, so
            # MO_ORIGINAL_AVAILABLE_FROM_IMAGE_ID can only ever be too LOW --
            # an image archived since it was recorded now answers 403. Retry
            # once at the rendition that is always served rather than failing
            # the transfer over a threshold that has moved.
            retry_url = (
                mo_image_url(source.source_url, MO_LARGEST_FETCHABLE_SIZE)
                if source.site == RemoteSite.MO
                and mo_image_size(source.source_url) == "orig"
                else ""
            )
            if not retry_url or retry_url == source.source_url:
                raise PhotoSyncError(
                    f"The Mushroom Observer image could not be downloaded: {type(exc).__name__}",
                    "source_download_failed",
                ) from exc
            try:
                data = self.inat_client.download_image(retry_url)
            except Exception as retry_exc:
                raise PhotoSyncError(
                    f"The Mushroom Observer image could not be downloaded: {type(retry_exc).__name__}",
                    "source_download_failed",
                ) from retry_exc
        if not data:
            raise PhotoSyncError(
                "The Mushroom Observer image was empty.", "source_empty"
            )
        if len(data) > MAX_PHOTO_BYTES:
            raise PhotoSyncError(
                f"The source image is larger than the {MAX_PHOTO_BYTES // (1024 * 1024)} MB transfer limit.",
                "source_too_large",
            )
        return data

    # Reads ------------------------------------------------------------

    def _refresh(
        self,
        profile: ReconciliationProfile,
        pair: dict[str, Any],
        cancelled: Callable[[], bool],
        *,
        verification_only: bool = False,
    ) -> _LivePhotoState:
        if cancelled():
            raise ReconciliationCancelled("Photo comparison cancelled")
        # Capture the credential identity BEFORE any read, and require it to be
        # unchanged after. Recording it afterwards would let an A->B->A
        # transition finish with the original token marker but the newest
        # generation, which the pre-write recheck would then wave through.
        auth_generation = self.auth_generation_provider()
        mo_key_generation = self.mo_key_generation_provider()
        # The Mushroom Observer key is not used to upload -- the write is
        # iNaturalist-only -- but it is part of the credential context the source
        # was read under, so it is captured and rechecked exactly like the
        # iNaturalist token. Only its one-way marker is kept; the key itself is
        # never held, logged or persisted here.
        mo_key_marker = self._mo_key_marker(profile.profile_id)
        auth = self.auth_provider()
        token = auth.api_token if auth.is_authenticated else ""
        if not token:
            raise PhotoSyncError(
                "iNaturalist authentication must match the selected reconciliation account.",
                "inat_auth_mismatch",
            )
        token_marker = public_fingerprint(token)
        mo_observation_id = int(pair["mo_observation_id"])
        inat_observation_id = int(pair["inat_observation_id"])

        # Prove the token actually belongs to the profile's account before any
        # write path is entered. Checking only that the *observation* belongs to
        # the expected user would let a token for another account reach the
        # submission step and rely on the server to refuse it.
        if not verification_only:
            self._require_authenticated_account(profile, token)

        # ``sync_pairs`` stores only the numeric iNaturalist id, but every photo
        # endpoint is keyed by UUID, so the uuid is resolved from a fresh read --
        # which is also where destination ownership is proven. Uploading into
        # somebody else's observation must be impossible.
        detail = _first_result(
            self.inat_client.get_reconciliation_detail(
                inat_observation_id, token, deep=False
            )
        )
        if not detail or positive_int(detail.get("id")) != inat_observation_id:
            raise PhotoSyncError(
                "The iNaturalist observation is unavailable.", "inat_unavailable"
            )
        inat_uuid = str(detail.get("uuid") or "").strip()
        owner_raw = detail.get("user")
        owner = owner_raw if isinstance(owner_raw, dict) else {}
        if not inat_uuid or positive_int(owner.get("id")) != profile.inat_user_id:
            raise PhotoSyncError(
                "The iNaturalist observation identity or owner changed; no photo may be attached.",
                "inat_owner_changed",
            )

        inat_raw = self.inat_client.get_observation_photos_v2(inat_uuid, token)
        inat_obs = _first_result(inat_raw)
        if not inat_obs:
            raise PhotoSyncError(
                "The iNaturalist observation could not be reread.", "inat_read_failed"
            )
        destination = _inat_photo_snapshots(inat_obs, inat_observation_id)

        source: tuple[PhotoRecordSnapshot, ...] = ()
        mo_fingerprint = ""
        specimen_conflict = ""
        specimen_warnings: tuple[str, ...] = ()
        if not verification_only:
            mo_raw = _first_result(
                self.mo_client.observation(mo_observation_id, cancelled, detail="high")
            )
            if not mo_raw or positive_int(mo_raw.get("id")) != mo_observation_id:
                raise PhotoSyncError(
                    "The Mushroom Observer observation is unavailable.",
                    "mo_unavailable",
                )
            mo_observation = parse_mo_observation(mo_raw, profile.mo_user_id)
            if mo_observation.owner_id != profile.mo_user_id:
                raise PhotoSyncError(
                    "The Mushroom Observer observation owner changed; its photos are not yours to send.",
                    "mo_owner_changed",
                )

            # The same shared specimen-identity validator used by ITS, coordinate
            # copying and name proposals. A photo must never cross between two
            # records that no longer describe the same physical collection, so
            # unlike Gate 1D nothing is tolerated here -- including coordinates.
            reader = INatReconciliationReader(self.inat_client)
            specimen_conflict, _evidence, warnings = evaluate_specimen_state(
                self.db,
                profile,
                pair,
                detail,
                mo_raw,
                reader,
                mo_client=self.mo_client,
                cancelled=cancelled,
                include_coordinates=True,
            )
            specimen_warnings = tuple(warnings)

            mo_payload = self.mo_client.images_for_observation(
                mo_observation_id, cancelled
            )
            source = _mo_photo_snapshots(mo_payload, mo_observation_id)
            # Fail CLOSED on ownership: only an image positively owned by the
            # profile's Mushroom Observer account is transferable. An image whose
            # owner is missing or unparsable is not ours to republish.
            source = tuple(p for p in source if p.owner_id == profile.mo_user_id)
            mo_fingerprint = _mo_photos_fingerprint(source)
        # The credential must be the same one the reads were made with.
        after = self.auth_provider()
        if (
            self.auth_generation_provider() != auth_generation
            or self.mo_key_generation_provider() != mo_key_generation
            or self._mo_key_marker(profile.profile_id) != mo_key_marker
            or not after.api_token
            or public_fingerprint(after.api_token) != token_marker
        ):
            raise PhotoSyncError(
                "Authentication changed while the photo comparison was being read.",
                "inat_auth_changed",
            )
        return _LivePhotoState(
            profile_id=profile.profile_id,
            auth_generation=auth_generation,
            mo_key_generation=mo_key_generation,
            inat_token_marker=token_marker,
            mo_key_marker=mo_key_marker,
            mo_observation_id=mo_observation_id,
            inat_observation_id=inat_observation_id,
            inat_observation_uuid=inat_uuid,
            inat_record_fingerprint=_inat_photos_fingerprint(destination),
            mo_record_fingerprint=mo_fingerprint,
            source_photos=source,
            destination_photos=destination,
            destination_observation_photo_uuids=frozenset(
                p.observation_photo_uuid
                for p in destination
                if p.observation_photo_uuid
            ),
            specimen_conflict=specimen_conflict,
            specimen_warnings=specimen_warnings,
        )

    def _require_not_duplicate(
        self,
        profile_id: int,
        action_id: int,
        live: _LivePhotoState,
        image_bytes: bytes,
        fingerprint: str,
        cancelled: Callable[[], bool],
    ) -> None:
        """Last-moment duplicate check against the freshly read destination.

        Fails closed exactly as the preview does: an unreadable or undecodable
        destination photo aborts the transfer rather than being skipped.
        """
        known = self.db.transferred_photo_digests(
            profile_id,
            destination_observation_id=live.inat_observation_id,
            exclude_action_id=action_id,
        )
        if fingerprint and fingerprint in known:
            raise PhotoSyncError(
                f"This image was already transferred to this observation ({known[fingerprint]}); "
                "nothing was uploaded.",
                "duplicate_ledger",
            )
        pixels = normalized_pixel_fingerprint(image_bytes)
        if not pixels:
            raise PhotoSyncError(
                "The source image could not be decoded, so it cannot be compared against the "
                "photos already on the destination; nothing was uploaded.",
                "source_undecodable",
            )
        scan = self._destination_pixel_fingerprints(live, cancelled)
        if not scan.complete:
            raise PhotoSyncError(
                "These photos already on the iNaturalist observation could not be compared: "
                + "; ".join(scan.unchecked)
                + ". Nothing was uploaded, because it could have duplicated one of them.",
                "destination_scan_incomplete",
            )
        match = scan.matching_photo_id(pixels)
        if match:
            raise PhotoSyncError(
                f"This image already appears on the destination observation as iNaturalist "
                f"photo {match}; nothing was uploaded.",
                "duplicate_destination",
            )

    def _await_attachment(
        self,
        profile: ReconciliationProfile,
        pair: dict[str, Any],
        planned_uuid: str,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> _LivePhotoState:
        """Re-read the destination until the attachment appears, or the window ends.

        iNaturalist can accept an upload and not surface it on the very next
        read, so a single re-read is not evidence of anything. The last state is
        returned either way; the caller decides, and treats "still absent" as
        unknown rather than failed.
        """
        live = self._refresh(profile, pair, lambda: False, verification_only=True)
        for _ in range(max(0, VERIFY_ATTEMPTS - 1)):
            if planned_uuid in live.destination_observation_photo_uuids or cancelled():
                return live
            time.sleep(VERIFY_DELAY_S)
            live = self._refresh(profile, pair, lambda: False, verification_only=True)
        return live

    def _require_authenticated_account(
        self, profile: ReconciliationProfile, token: str
    ) -> None:
        current = _first_result(self.inat_client.get_current_user_v2(token))
        if positive_int(current.get("id") if current else None) != profile.inat_user_id:
            raise PhotoSyncError(
                "The authenticated iNaturalist account does not match the profile.",
                "inat_auth_mismatch",
            )

    # Guards -----------------------------------------------------------

    def _mo_key_marker(self, profile_id: int) -> str:
        """Current MO key marker, or a hard failure -- never a silent "".

        A provider that cannot answer is not evidence that nothing changed, so
        the credential context is treated as unreadable and the caller refuses.
        """
        try:
            return _key_marker(self.mo_key_provider(profile_id))
        except Exception as exc:
            raise PhotoSyncError(
                "The Mushroom Observer credential state could not be read.",
                "mo_key_unavailable",
            ) from exc

    def _recheck_credentials(self, live: _LivePhotoState) -> AuthState:
        """The final credential-context check, immediately before the upload.

        Both halves of the context are required, as at every other
        reconciliation write gate: the iNaturalist token that performs the write
        AND the Mushroom Observer key the source was read under. A MO credential
        transition between the last source read and the upload means the images,
        licensing and ownership under review may no longer be what this account
        can see, so it must not be waved through just because the write itself
        happens to be iNaturalist-only.
        """
        auth = self.auth_provider()
        if (
            not auth.api_token
            or self.auth_generation_provider() != live.auth_generation
            or public_fingerprint(auth.api_token) != live.inat_token_marker
        ):
            raise PhotoSyncError(
                "iNaturalist authentication changed after preflight.",
                "inat_auth_changed",
            )
        if (
            self.mo_key_generation_provider() != live.mo_key_generation
            or self._mo_key_marker(live.profile_id) != live.mo_key_marker
        ):
            raise PhotoSyncError(
                "The Mushroom Observer API key changed after preflight.",
                "mo_key_changed",
            )
        return auth

    def _require_unchanged_context(
        self, row: dict[str, Any], live: _LivePhotoState
    ) -> None:
        if str(row["preview_inat_record_fingerprint"]) != live.inat_record_fingerprint:
            raise PhotoSyncError(
                "The iNaturalist observation's photos changed after preview.",
                "inat_record_changed",
            )
        if str(row["preview_mo_record_fingerprint"]) != live.mo_record_fingerprint:
            raise PhotoSyncError(
                "The Mushroom Observer observation's images changed after preview.",
                "mo_record_changed",
            )

    def _account_holder(self, profile_id: int) -> str:
        try:
            profile = self.db.profile(profile_id)
        except Exception:
            return ""
        return str(getattr(profile, "mo_login", "") or "")

    def _eligible_pair(self, profile_id: int, pair_id: int) -> dict[str, Any]:
        pair = self.db.pair_detail(profile_id, pair_id)
        if not pair or pair.get("review_state") != "confirmed" or pair.get("excluded"):
            raise PhotoSyncError(
                "Only a currently confirmed, non-excluded pair can transfer photos.",
                "pair_not_confirmed",
            )
        return pair

    def _require_current_source(
        self, row: dict[str, Any], pair: dict[str, Any]
    ) -> None:
        profile_id = int(row["profile_id"])
        group = self.db.action_group(profile_id, int(row["action_group_id"]))
        if not group:
            raise PhotoSyncError(
                "The confirmed pair changed after the photo was reviewed.",
                "pair_changed",
            )
        if str(group.get("source_kind")) == "creation":
            # Gate 2A (section 10): a creation-saga group's own
            # source_fingerprint is the SOURCE RECORD fingerprint captured at
            # journal time (see journal_observation_creation_actions), never
            # a pair fingerprint -- comparing it against _pair_fingerprint(pair)
            # would ALWAYS mismatch (the pair did not even exist yet when that
            # fingerprint was captured), so every Gate 2A photo item would
            # fail here unconditionally. Validate against the creation ledger
            # instead, mirroring LinkRepairService's equally explicit,
            # narrow exemption (actions.py's
            # _require_valid_creation_link_exemption): the action's own
            # pair_id must match the ledger's pair, and both mo/inat ids must
            # agree across the pair, the action row, and the action group.
            # _eligible_pair has already required review_state=='confirmed'
            # and not-excluded before this method is ever reached, so no
            # separate provisional/confirmed distinction is needed here --
            # every photo item is population, never a bootstrap step.
            ledger = self.db.creation_ledger_for_group(
                profile_id, int(row["action_group_id"])
            )
            if not ledger or int(ledger.get("profile_id") or 0) != profile_id:
                raise PhotoSyncError(
                    "The creation ledger row is missing.", "source_missing"
                )
            if int(row.get("pair_id") or -1) != int(ledger.get("pair_id") or -2):
                raise PhotoSyncError(
                    "The action's pair does not match the creation ledger's pair.",
                    "pair_changed",
                )
            mo_id = int(row["mo_observation_id"])
            inat_id = int(row["inat_observation_id"])
            if (
                int(pair.get("mo_observation_id") or 0) != mo_id
                or int(pair.get("inat_observation_id") or 0) != inat_id
                or int(group.get("mo_observation_id") or 0) != mo_id
                or int(group.get("inat_observation_id") or 0) != inat_id
            ):
                raise PhotoSyncError(
                    "The confirmed pair changed after the photo was reviewed.",
                    "pair_changed",
                )
            # Round-3 finding 8: pair+group id agreement alone is broader
            # than necessary — it says nothing about whether THIS action is
            # actually the reviewed photo item it claims to be. A manually
            # malformed inat_photo_attach row inside a genuine creation
            # group (right pair, right ids, but never actually minted from a
            # reviewed sync_creation_items row) must not ride this
            # exemption. Require exact item/attempt/identity/transfer-ledger
            # correlation.
            action_id = int(row["action_id"])
            if str(row.get("action_type") or "") != "inat_photo_attach":
                raise PhotoSyncError(
                    "This action type is not permitted to use the creation-saga photo exemption.",
                    "creation_exemption_type_not_permitted",
                )
            item = self.db.creation_item_for_action(profile_id, action_id)
            if not item or str(item.get("item_type")) != "photo":
                raise PhotoSyncError(
                    "This action is not linked to a reviewed photo item.",
                    "creation_item_missing",
                )
            if int(item.get("attempt_id") or -1) != int(ledger.get("attempt_id") or -2):
                raise PhotoSyncError(
                    "This action does not belong to the current creation attempt.",
                    "attempt_mismatch",
                )
            identity_destination = ledger.get("destination_observation_id")
            if (
                identity_destination is not None
                and int(identity_destination) != inat_id
            ):
                raise PhotoSyncError(
                    "The action's destination does not match the creation identity's recorded "
                    "destination.",
                    "pair_changed",
                )
            transfer = (
                self.db.connection()
                .execute(
                    "SELECT action_id FROM sync_photo_transfers WHERE profile_id=? AND source_site=? "
                    "AND source_photo_id=? AND destination_site='inat' AND destination_observation_id=?",
                    (
                        profile_id,
                        str(row.get("source_site") or ""),
                        str(row.get("source_photo_id") or ""),
                        inat_id,
                    ),
                )
                .fetchone()
            )
            if not transfer or int(transfer["action_id"] or -1) != action_id:
                raise PhotoSyncError(
                    "This action does not match its own transfer-ledger row.",
                    "transfer_mismatch",
                )
            return
        # The reviewed pair version lives on the action GROUP, not on the action
        # row; comparing against a missing action column would silently compare
        # the current fingerprint to itself and never fire.
        if _pair_fingerprint(pair) != str(group["source_fingerprint"]):
            raise PhotoSyncError(
                "The confirmed pair changed after the photo was reviewed.",
                "pair_changed",
            )

    def _pair_from_journal(
        self, profile_id: int, row: dict[str, Any]
    ) -> dict[str, Any]:
        """Build a pair view from immutable journaled IDs, not current eligibility.

        Recovering the outcome of a write that was already submitted must stay
        possible even if the pair was since reopened, excluded or unconfirmed.
        Eligibility gates *new* writes; it must never block mandatory recovery.
        """
        current = self.db.pair_detail(profile_id, int(row["pair_id"])) or {}
        return {
            "pair_id": int(row["pair_id"]),
            "mo_observation_id": int(row["mo_observation_id"]),
            "inat_observation_id": int(row["inat_observation_id"]),
            "link_state": current.get("link_state", ""),
            "review_state": current.get("review_state", ""),
            "confirmed_by": current.get("confirmed_by", ""),
            "updated_at": current.get("updated_at", ""),
        }


# Parsing ---------------------------------------------------------------


def _mo_photo_snapshots(
    payload: object,
    observation_id: int,
) -> tuple[PhotoRecordSnapshot, ...]:
    snapshots: list[PhotoRecordSnapshot] = []
    for raw in results_from_payload(payload):
        photo_id = str(raw.get("id") or "").strip()
        if not photo_id:
            continue
        owner_raw = raw.get("owner")
        owner = owner_raw if isinstance(owner_raw, dict) else {}
        snapshots.append(
            PhotoRecordSnapshot(
                site=RemoteSite.MO,
                photo_id=photo_id,
                observation_id=observation_id,
                license_label=str(raw.get("license") or ""),
                copyright_holder=str(raw.get("copyright_holder") or ""),
                owner_id=positive_int(owner.get("id")) or 0,
                source_url=_mo_best_url(raw),
            )
        )
    return tuple(snapshots)


def _mo_best_url(raw: dict[str, Any]) -> str:
    """Choose the highest-fidelity rendition that can actually be DOWNLOADED.

    Not simply the largest one Mushroom Observer lists. ``original_url`` and the
    trailing ``files`` entries both point at the ``orig`` rendition, which is
    served only for images the image server still holds -- for older ids it is
    403 from the archive bucket. See
    :data:`MO_ORIGINAL_AVAILABLE_FROM_IMAGE_ID`. Returning an archived original
    made every photo transfer of an older image fail with
    ``source_download_failed`` and every candidate photo comparison show
    "download failed", because the URL this function picks is the only one
    either path ever asks for.

    Selection is driven by ``files`` when it is present, since that array is the
    API's own statement of which renditions exist, and falls back to rewriting
    ``original_url`` onto the ladder. An MO URL that matches no known rendition
    is passed through untouched rather than guessed at.

    MO's URLs are public CDN addresses with a cache-busting query string; they
    carry no credential and are safe to hold in memory. They are never persisted.
    """
    wanted = mo_best_downloadable_size(positive_int(raw.get("id")))
    listed = (
        [
            entry.strip()
            for entry in (raw.get("files") or [])
            if isinstance(entry, str) and entry.strip()
        ]
        if isinstance(raw.get("files"), list)
        else []
    )
    by_size = {mo_image_size(entry): entry for entry in listed if mo_image_size(entry)}
    for size in (wanted, *reversed(MO_FETCHABLE_IMAGE_SIZES)):
        if size in by_size:
            return by_size[size]
    original = mo_image_url(str(raw.get("original_url") or "").strip(), wanted)
    # Nothing on the ladder was recognized. Prefer whatever the API named over
    # returning nothing at all: if MO ever changes its URL scheme, "download
    # the URL we were handed" degrades to a possible 403, while "" guarantees
    # the photo is dropped from every comparison and transfer silently.
    return original or (listed[-1] if listed else "")


def _inat_photo_snapshots(
    observation: dict[str, Any],
    observation_id: int,
) -> tuple[PhotoRecordSnapshot, ...]:
    snapshots: list[PhotoRecordSnapshot] = []
    for row in observation.get("observation_photos") or []:
        if not isinstance(row, dict):
            continue
        raw_photo = row.get("photo")
        photo: dict[str, Any] = raw_photo if isinstance(raw_photo, dict) else {}
        photo_id = str(photo.get("id") or "").strip()
        snapshots.append(
            PhotoRecordSnapshot(
                site=RemoteSite.INAT,
                photo_id=photo_id or f"observation_photo:{row.get('uuid')}",
                observation_id=observation_id,
                license_label=str(photo.get("license_code") or ""),
                copyright_holder=str(photo.get("attribution") or ""),
                source_url=str(photo.get("url") or ""),
                observation_photo_uuid=str(row.get("uuid") or ""),
            )
        )
    return tuple(snapshots)


def _inat_photos_fingerprint(photos: tuple[PhotoRecordSnapshot, ...]) -> str:
    """Version marker for the destination's photo set.

    Deliberately derived from the observation_photo rows rather than the
    observation's ``updated_at``: attaching or removing a photo is exactly what
    must be detected, and this changes precisely when the photo set changes.

    The join-row uuid alone would miss content changing BEHIND a stable
    attachment, which would silently invalidate the duplicate scan that was run
    against the old image, so the photo's own identity is folded in as well:
    photo id, licence, attribution and file URL (whose cache-busting query is
    iNaturalist's own version marker). The v2 ``Photo`` schema exposes no
    ``updated_at``, so the URL is the closest available revision signal.
    """
    parts = sorted(
        "|".join(
            (
                p.observation_photo_uuid,
                p.photo_id,
                p.license_label,
                p.copyright_holder,
                p.source_url,
            )
        )
        for p in photos
    )
    return public_fingerprint("inat_photos", *parts)


def _mo_photos_fingerprint(photos: tuple[PhotoRecordSnapshot, ...]) -> str:
    """Version marker for the source image set.

    Photo IDs alone are not enough: Mushroom Observer can change an image's
    license, copyright holder, owner or file behind a stable id, and uploading
    content or licensing the user never reviewed would be exactly the failure
    this gate exists to prevent. Every displayed attribute is therefore folded
    in, so any change to a reviewed image invalidates the preview.
    """
    parts = sorted(
        "|".join(
            (
                p.photo_id,
                p.license_label,
                p.copyright_holder,
                str(p.owner_id),
                p.source_url,
            )
        )
        for p in photos
    )
    return public_fingerprint("mo_photos", *parts)


def _key_marker(key: str) -> str:
    """One-way marker for a Mushroom Observer API key; "" when none is set.

    Only the digest is ever compared, held or passed around -- never the key.
    An absent key deliberately markers as "" rather than as a digest of the
    empty string, so a key appearing or disappearing between the read and the
    write is a visible change rather than two indistinguishable states.
    """
    return public_fingerprint(key) if key else ""


def _pair_fingerprint(pair: dict[str, Any]) -> str:
    return public_fingerprint(
        "pair",
        pair.get("pair_id"),
        pair.get("updated_at"),
        pair.get("review_state"),
        pair.get("link_state"),
        pair.get("confirmed_by"),
    )


def _is_client_rejection(status: object) -> bool:
    """True only for a status that means the server refused before accepting.

    5xx and missing statuses are excluded on purpose: they leave open whether the
    upload was taken, and a photo upload must never be retried on a maybe.
    """
    try:
        code = int(status)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return code in {400, 401, 403, 404, 409, 422}


def _is_photo_action(action_type: object) -> bool:
    return str(action_type or "") == PhotoActionType.INAT_PHOTO_ATTACH.value


def _first_result(payload: object) -> Optional[dict[str, Any]]:
    # v2 reads come back wrapped in a V2Response; unwrap before inspecting.
    payload = getattr(payload, "data", payload)
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0]
    if isinstance(results, dict):
        return results
    return None


def _inat_comparable_url(url: str) -> str:
    """Derive a SCALED iNaturalist rendition from whatever size we were given.

    The default ``url`` is the *square* rendition, which is centre-CROPPED. A
    crop cannot be compared against an uncropped source, so the size token is
    rewritten to ``medium``, which is scaled. Returns "" when the URL does not
    follow iNaturalist's size-token scheme (e.g. legacy Flickr-hosted photos).
    """
    if not url.strip():
        return ""
    swapped, count = re.subn(
        r"/(square|thumb|small|medium|large|original)\.(jpe?g|png|gif|webp)",
        r"/medium.\2",
        url.strip(),
        flags=re.IGNORECASE,
    )
    return swapped if count else ""


def _safe_filename(source: PhotoRecordSnapshot) -> str:
    """A neutral filename. MO's original filename is a private field we do not read."""
    url = source.source_url.split("?", 1)[0]
    suffix = url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else "jpg"
    if suffix not in {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff"}:
        suffix = "jpg"
    return f"mo-{source.photo_id}.{suffix}"


def _content_type(filename: str) -> str:
    suffix = filename.rsplit(".", 1)[-1].lower()
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "tif": "image/tiff",
        "tiff": "image/tiff",
    }.get(suffix, "image/jpeg")


def new_observation_photo_uuid() -> str:
    """Generate the client-side observation_photo uuid.

    Journal this BEFORE sending the request: it is the only key by which a lost
    response can later be resolved without uploading a second copy.
    """
    return str(uuidlib.uuid4())
