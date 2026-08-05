"""Gate 1D coordinate synchronization (Mushroom Observer → iNaturalist).

Mushroom Observer observations are a read-only source in this project, so the
only coordinate write targets iNaturalist. The service mirrors the Gate 1C ITS
state machine (fresh-read preview → journal one reviewed action → preflight →
single confirmed write → mandatory destination re-read verification).

Privacy design: raw latitude/longitude and any exact distance live only in
memory-only fields populated by fresh remote reads. They are never passed to
SQLite, logging, exceptions, settings, payloads, URLs, third-party maps, or the
UI. Crucially, **no coordinate-derived fingerprint is persisted either**: an
unkeyed hash of a low-entropy point is offline-enumerable, so coordinate
freshness is established from the sites' own record/version fingerprints (which
change with ``updated_at``) and equality is checked by comparing freshly-read
points in memory. Only IDs, action type, privacy states, and version
fingerprints are stored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from math import asin, cos, radians, sin, sqrt
from typing import Any, Callable, Optional

from observation_workbench.api.auth import AuthState
from observation_workbench.api.client import INatAPIError, INatClient

from .db import ReconciliationDB
from .inat_reader import INatReconciliationReader
from .mo_client import MOAPIError, MOClient, ReconciliationCancelled
from .mo_parsing import mo_record_fingerprint, parse_mo_coordinate, parse_mo_observation, positive_int
from .normalization import public_fingerprint
from .specimen_state import evaluate_specimen_state
from .types import (
    CoordinateActionOption, CoordinateActionType, CoordinateComparisonPreview,
    CoordinatePrivacyState, CoordinateRecordSnapshot, ReconciliationProfile, RemoteSite,
)

# A copy whose source and destination points differ by more than this is flagged
# for explicit review. The distance is computed in memory only and never persisted.
LARGE_DISCREPANCY_M = 1000.0

# Precision used for in-memory point equality (~1 m). Comparison is in memory
# only; no rounded point is ever stored.
_POINT_PRECISION = 5

# Tolerance (metres) for positional-accuracy equality during verification and
# the no-op check. iNaturalist stores integer metres, so a small slack absorbs
# rounding without hiding a materially different accuracy.
_ACCURACY_TOLERANCE_M = 1.0

# Translate an internal privacy state to the iNaturalist geoprivacy write value.
# iNaturalist represents an openly visible coordinate as "open" (not "public").
_INAT_GEOPRIVACY = {
    CoordinatePrivacyState.PUBLIC.value: "open",
    CoordinatePrivacyState.OBSCURED.value: "obscured",
    CoordinatePrivacyState.PRIVATE.value: "private",
}


class CoordinateSyncError(RuntimeError):
    def __init__(self, message: str, code: str = "coordinate_sync_unavailable") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CoordinateActionResult:
    action_id: int
    state: str
    message: str


@dataclass(frozen=True)
class _LiveCoordinateState:
    profile_id: int
    pair_id: int
    mo_observation_id: int
    inat_observation_id: int
    inat_observation_uuid: str
    inat_record_fingerprint: str
    mo_record_fingerprint: str
    inat_token_marker: str
    auth_generation: int
    mo_key_generation: int
    source: CoordinateRecordSnapshot
    destination: CoordinateRecordSnapshot
    # A non-empty message means unrelated specimen-identity evidence conflicts, so
    # the records may not be the same collection and no coordinate may be copied.
    specimen_conflict: str = ""
    specimen_warnings: tuple[str, ...] = ()
    # Distance is derived from possibly-private points and is kept in memory only.
    distance_m: Optional[float] = field(default=None, repr=False, compare=False)


class CoordinateSyncService:
    """Fresh-read comparison and single-write service for confirmed pairs only."""

    def __init__(
        self, db: ReconciliationDB, inat_client: INatClient, mo_client: MOClient,
        auth_provider: Callable[[], AuthState], mo_key_provider: Callable[[int], str],
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

    def prepare_preview(
        self, profile_id: int, pair_id: int,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> CoordinateComparisonPreview:
        pair = self._eligible_pair(profile_id, pair_id)
        profile = self.db.profile(profile_id)
        live = self._refresh(profile, pair, cancelled)
        warnings: list[str] = list(live.specimen_warnings)
        if live.specimen_conflict:
            warnings.append(
                "Coordinate copy is blocked while specimen-identity evidence conflicts: "
                + live.specimen_conflict
            )
        options = self._options(live, warnings)
        if not options and not live.specimen_conflict and not warnings:
            warnings.append("No safe coordinate copy is proposed for the current remote state.")
        return CoordinateComparisonPreview(
            profile_id=profile_id, pair_id=pair_id,
            auth_generation=live.auth_generation,
            mo_key_generation=live.mo_key_generation,
            source_fingerprint=_pair_fingerprint(pair),
            mo_observation_id=live.mo_observation_id,
            inat_observation_id=live.inat_observation_id,
            inat_observation_uuid=live.inat_observation_uuid,
            inat_record_fingerprint=live.inat_record_fingerprint,
            mo_record_fingerprint=live.mo_record_fingerprint,
            source=_public_snapshot(live.source),
            destination=_public_snapshot(live.destination),
            options=tuple(options), warnings=tuple(warnings),
        )

    def _options(
        self, live: _LiveCoordinateState, warnings: list[str],
    ) -> list[CoordinateActionOption]:
        source = live.source
        destination = live.destination
        # Unrelated specimen-identity conflicts mean the two records may not be the
        # same collection; the coordinate must not be copied across such a pair.
        if live.specimen_conflict:
            return []
        if not source.coordinates_available:
            if source.privacy_state == CoordinatePrivacyState.PRIVATE.value:
                warnings.append(
                    "The Mushroom Observer coordinate is private and not readable here; it cannot be copied."
                )
            else:
                warnings.append("Mushroom Observer exposes no readable coordinate to copy.")
            return []
        if source.privacy_state == CoordinatePrivacyState.UNKNOWN.value:
            warnings.append("The Mushroom Observer coordinate privacy state is unknown; no copy is proposed.")
            return []
        # An unknown or unrecognized destination privacy means we cannot reason
        # about what we would overwrite or whether visibility would change; block.
        if destination.privacy_state == CoordinatePrivacyState.UNKNOWN.value:
            warnings.append(
                "The iNaturalist coordinate privacy state is unknown or unrecognized; no copy is proposed."
            )
            return []
        proposed_privacy = source.privacy_state
        # The observation-level geoprivacy the write would set (distinct from the
        # effective visibility, which the destination taxon geoprivacy may clamp).
        proposed_observation_geoprivacy = _normalize_geoprivacy(_INAT_GEOPRIVACY.get(proposed_privacy))
        # Two independent disclosure concepts (they are NOT the same thing):
        #   * ``sends_nonpublic_source`` — the SOURCE point is private/obscured, so
        #     copying discloses that exact point to another service.
        #   * ``broadens_visibility`` — the copy makes the EXISTING iNaturalist
        #     coordinate more publicly VISIBLE, computed from the predicted
        #     post-write effective visibility (most restrictive of the proposed
        #     observation setting and the current taxon geoprivacy).
        sends_nonpublic_source = source.privacy_state in {
            CoordinatePrivacyState.PRIVATE.value, CoordinatePrivacyState.OBSCURED.value,
        }
        broadens = _broadens_visibility(destination, proposed_observation_geoprivacy)
        if sends_nonpublic_source:
            warnings.append(
                "The source coordinate is private or obscured; copying it sends the exact point to "
                "iNaturalist (its public visibility there is governed by the matching geoprivacy setting). "
                "Review before confirming."
            )
        if broadens:
            warnings.append(
                "This copy makes the existing iNaturalist coordinate more publicly visible (the predicted "
                f"effective visibility becomes less restrictive than the current '{destination.privacy_state}' "
                "setting); confirm the visibility change explicitly."
            )
        # An existing destination coordinate whose exact point cannot be read here
        # must never be replaced automatically: we cannot prove what we would
        # overwrite, and it may hide a real exact coordinate.
        if destination.exact_point_unreadable:
            warnings.append(
                "The existing iNaturalist coordinate cannot be read exactly here (it is obscured or "
                "private without an authorized exact point); automated replacement is blocked. Adjust "
                "it manually on iNaturalist if a copy is intended."
            )
            return []
        # A partial update omits ``positional_accuracy`` when the source has none,
        # so iNaturalist keeps the accuracy that described its OLD point. That
        # value cannot be cleared through this write, and verification (like the
        # no-op check above) requires the accuracies to agree, so such an action
        # could never be proven applied — it would settle 'outcome_unknown' after
        # a successful write and stay unresolvable. Refuse it up front instead.
        if source.accuracy_m is None and destination.accuracy_m is not None:
            warnings.append(
                "The Mushroom Observer coordinate has no positional accuracy while the iNaturalist "
                "coordinate does. This copy cannot clear the existing accuracy, which would then "
                "describe a point it no longer belongs to, so no copy is proposed; clear the accuracy "
                "on iNaturalist first if a copy is intended."
            )
            return []
        large = bool(live.distance_m is not None and live.distance_m > LARGE_DISCREPANCY_M)
        if large:
            warnings.append(
                "The existing iNaturalist coordinate differs substantially from the source; review before replacing."
            )
        replaces = destination.coordinates_available
        # "Already matches" compares the exact point and the observation-level
        # geoprivacy we control (not the effective visibility, which taxon
        # geoprivacy may further restrict) — consistent with _is_satisfied.
        if (
            replaces and _same_point(destination, source)
            and _same_accuracy(source.accuracy_m, destination.accuracy_m)
            and destination.observation_geoprivacy == proposed_observation_geoprivacy
        ):
            warnings.append("The iNaturalist coordinate already matches the source; no copy is needed.")
            return []
        action_type = (
            CoordinateActionType.INAT_COORDINATE_REPLACE if replaces
            else CoordinateActionType.INAT_COORDINATE_SET
        )
        description = (
            f"{'Replace' if replaces else 'Set'} the iNaturalist coordinate from Mushroom Observer "
            f"observation {source.observation_id} "
            f"(source privacy: {source.privacy_state}; proposed destination privacy: {proposed_privacy})"
        )
        option = CoordinateActionOption(
            action_type=action_type, destination_site=RemoteSite.INAT, source_site=RemoteSite.MO,
            source_record_id=source.observation_id, destination_record_id=destination.observation_id,
            source_privacy_state=source.privacy_state, proposed_privacy_state=proposed_privacy,
            accuracy_m=source.accuracy_m, replaces_data=replaces,
            sends_nonpublic_source=sends_nonpublic_source, broadens_visibility=broadens,
            large_discrepancy=large, description=description, destructive=replaces,
            enabled=True, disabled_reason="",
        )
        return [option]

    # Execution --------------------------------------------------------

    def execute_group(
        self, profile_id: int, group_id: int, cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> list[CoordinateActionResult]:
        rows = self.db.action_group_rows(profile_id, group_id)
        if len(rows) != 1 or not _is_coordinate_action(rows[0].get("action_type") if rows else None):
            raise CoordinateSyncError(
                "A coordinate journal group must contain exactly one individually reviewed action.",
                "invalid_coordinate_group",
            )
        row = rows[0]
        state = str(row["state"])
        if state == "outcome_unknown":
            return [self.verify_unknown(profile_id, int(row["action_id"]), cancelled)]
        if state == "succeeded":
            return []
        if state != "pending":
            return [CoordinateActionResult(
                int(row["action_id"]), state,
                "This coordinate action is terminal; create a fresh comparison for another copy.",
            )]
        return [self._execute(row, cancelled, progress)]

    def verify_unknown(
        self, profile_id: int, action_id: int, cancelled: Callable[[], bool],
    ) -> CoordinateActionResult:
        row = self.db.action(profile_id, action_id)
        if not row or not _is_coordinate_action(row.get("action_type")):
            raise CoordinateSyncError("The selected journal row is not a coordinate action.", "invalid_coordinate_action")
        if str(row["state"]) != "outcome_unknown":
            return CoordinateActionResult(action_id, str(row["state"]), "No unknown outcome remains to verify.")
        profile = self.db.profile(profile_id)
        pair = self._pair_from_journal(profile_id, row)
        try:
            live = self._refresh(profile, pair, cancelled, verification_only=True)
        except Exception:
            return CoordinateActionResult(
                action_id, "outcome_unknown",
                "Destination reread is still unavailable; the action was not retried.",
            )
        if self._is_satisfied(row, live):
            self.db.finish_action(
                profile_id, action_id, "succeeded", phase="verification",
                verification_state="verified_after_unknown",
            )
            return CoordinateActionResult(action_id, "succeeded", "Verified the prior submission without retrying it.")
        # The destination iNaturalist record version is unchanged since preview,
        # so the write was not applied.
        if live.inat_record_fingerprint == str(row["preview_inat_record_fingerprint"]):
            self.db.finish_action(
                profile_id, action_id, "failed", phase="verification",
                error_code="verified_not_applied", verification_state="verified_not_applied",
            )
            return CoordinateActionResult(
                action_id, "failed",
                "The iNaturalist observation is unchanged since preview; the write was not applied.",
            )
        self.db.finish_action(
            profile_id, action_id, "outcome_unknown", phase="verification",
            error_code="changed_not_proven", verification_state="changed_not_proven",
        )
        return CoordinateActionResult(
            action_id, "outcome_unknown",
            "The destination changed but does not prove this write; run a fresh coordinate comparison.",
        )

    def _execute(
        self, row: dict[str, Any], cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> CoordinateActionResult:
        profile_id = int(row["profile_id"])
        action_id = int(row["action_id"])
        if not self.db.claim_action(profile_id, action_id, "resource_preflight"):
            current = self.db.action(profile_id, action_id) or row
            return CoordinateActionResult(action_id, str(current["state"]), "The coordinate action is no longer pending.")
        write_started = False
        try:
            if cancelled():
                raise ReconciliationCancelled("Coordinate action cancelled")
            progress(f"Coordinate action {action_id}: rereading source and destination")
            pair = self._eligible_pair(profile_id, int(row["pair_id"]))
            self._require_current_source(row, pair)
            profile = self.db.profile(profile_id)
            live = self._refresh(profile, pair, cancelled)
            if self._is_satisfied(row, live):
                self.db.finish_action(
                    profile_id, action_id, "succeeded", phase="verification",
                    verification_state="already_correct",
                )
                return CoordinateActionResult(action_id, "succeeded", "Destination already matches; no write was sent.")
            # Fresh specimen-identity evidence must still prove the same collection
            # (tolerating only the coordinate being reconciled).
            if live.specimen_conflict:
                raise CoordinateSyncError(
                    "Fresh specimen-identity evidence conflicts; no coordinate was copied: "
                    + live.specimen_conflict,
                    "specimen_conflict",
                )
            # Both observation versions must be exactly as previewed (this detects
            # any source OR destination coordinate change via their updated_at).
            self._require_unchanged_context(row, live)
            if live.source.privacy_state != str(row["source_privacy_state"]):
                raise CoordinateSyncError("The Mushroom Observer coordinate privacy state changed after preview.", "source_privacy_changed")
            if not live.source.coordinates_available or live.source.latitude is None or live.source.longitude is None:
                raise CoordinateSyncError("The Mushroom Observer source coordinate is no longer readable.", "source_changed")

            def begin_write() -> None:
                """Stamp the durable write boundary immediately before the request.

                ``_write`` still rechecks the credential context and resolves the
                destination geoprivacy, both of which can refuse deterministically
                before anything is sent. Stamping the boundary earlier would settle
                those refusals as 'outcome_unknown' ("a write may have been
                submitted") when in fact no request left this process.
                """
                nonlocal write_started
                if not self.db.mark_action_write_started(profile_id, action_id):
                    raise CoordinateSyncError(
                        "This action left its claimed state before the write boundary. "
                        "No write was sent.",
                        "write_boundary_lost",
                    )
                write_started = True

            progress(f"Coordinate action {action_id}: submitting one explicitly confirmed coordinate write")
            write_error: Optional[Exception] = None
            http_status: Optional[int] = None
            try:
                response = self._write(row, live, cancelled, begin_write)
                metadata = getattr(response, "metadata", None)
                http_status = getattr(metadata, "status_code", None)
            except (INatAPIError, MOAPIError) as exc:
                write_error = exc
                http_status = getattr(exc, "status_code", None)

            progress(f"Coordinate action {action_id}: verifying destination coordinate")
            try:
                verified = self._refresh(profile, pair, lambda: False, verification_only=True)
            except Exception:
                self.db.finish_action(
                    profile_id, action_id, "outcome_unknown", phase="verification",
                    error_code="verification_unavailable", http_status=http_status,
                    verification_state="unavailable",
                )
                return CoordinateActionResult(
                    action_id, "outcome_unknown",
                    "The write may have been submitted, but destination verification is unavailable.",
                )
            if self._is_satisfied(row, verified):
                self.db.finish_action(
                    profile_id, action_id, "succeeded", phase="verification",
                    http_status=http_status, verification_state="verified_final_state",
                )
                return CoordinateActionResult(action_id, "succeeded", "Verified the copied iNaturalist coordinate.")
            unchanged = verified.inat_record_fingerprint == str(row["preview_inat_record_fingerprint"])
            if unchanged:
                self.db.finish_action(
                    profile_id, action_id, "failed", phase="verification",
                    error_code="verified_not_applied", http_status=http_status,
                    verification_state="verified_not_applied",
                )
                return CoordinateActionResult(action_id, "failed", "Verification shows that the write was not applied.")
            if write_error is not None and bool(getattr(write_error, "outcome_unknown", False)):
                self.db.finish_action(
                    profile_id, action_id, "outcome_unknown", phase="verification",
                    error_code="write_outcome_unknown", http_status=http_status,
                    verification_state="changed_not_proven",
                )
                return CoordinateActionResult(action_id, "outcome_unknown", "Write outcome remains unknown.")
            self.db.finish_action(
                profile_id, action_id, "outcome_unknown", phase="verification",
                error_code="changed_not_proven", http_status=http_status,
                verification_state="changed_not_proven",
            )
            return CoordinateActionResult(
                action_id, "outcome_unknown",
                "The destination changed but does not prove the requested coordinate; run a fresh comparison.",
            )
        except ReconciliationCancelled:
            self.db.finish_action(
                profile_id, action_id, "cancelled", phase="resource_preflight",
                error_code="user_cancelled",
            )
            return CoordinateActionResult(action_id, "cancelled", "Cancelled before a coordinate write was sent.")
        except CoordinateSyncError as exc:
            self.db.finish_action(
                profile_id, action_id,
                "outcome_unknown" if write_started else "failed",
                phase="verification" if write_started else "resource_preflight",
                error_code=exc.code,
            )
            if write_started:
                return CoordinateActionResult(
                    action_id, "outcome_unknown",
                    "A write may have been submitted; verify before any retry. " + str(exc),
                )
            return CoordinateActionResult(action_id, "failed", str(exc))
        except Exception:
            terminal = "outcome_unknown" if write_started else "failed"
            self.db.finish_action(
                profile_id, action_id, terminal,
                phase="verification" if write_started else "resource_preflight",
                error_code="local_journal_failure" if write_started else "preflight_failed",
            )
            return CoordinateActionResult(
                action_id, terminal,
                "The write outcome must be verified before any retry."
                if write_started else "Coordinate preflight failed before a write was sent.",
            )

    def _write(
        self, row: dict[str, Any], live: _LiveCoordinateState, cancelled: Callable[[], bool],
        on_send: Callable[[], None],
    ) -> object:
        """Validate every local precondition, then send exactly one request.

        ``on_send`` stamps the durable write boundary and must be called
        immediately before the request leaves, so a deterministic local refusal
        below is settled as a definite failure rather than as an ambiguous
        "a write may have been submitted".
        """
        auth = self._recheck_inat_auth(live)
        latitude = live.source.latitude
        longitude = live.source.longitude
        if latitude is None or longitude is None:
            raise CoordinateSyncError("The source no longer contains a readable coordinate.", "source_changed")
        geoprivacy = _INAT_GEOPRIVACY.get(str(row["proposed_privacy_state"]))
        if geoprivacy is None:
            raise CoordinateSyncError("The proposed destination geoprivacy is unrecognised.", "invalid_geoprivacy")
        on_send()
        return self.inat_client.update_observation_coordinates_v2(
            auth.api_token, live.inat_observation_uuid,
            latitude=latitude, longitude=longitude,
            positional_accuracy=live.source.accuracy_m, geoprivacy=geoprivacy,
        )

    def _recheck_inat_auth(self, live: _LiveCoordinateState) -> AuthState:
        auth = self.auth_provider()
        if (
            not auth.api_token
            or self.auth_generation_provider() != live.auth_generation
            or public_fingerprint(auth.api_token) != live.inat_token_marker
        ):
            raise CoordinateSyncError("iNaturalist authentication changed after preflight.", "inat_auth_changed")
        return auth

    def _require_unchanged_context(self, row: dict[str, Any], live: _LiveCoordinateState) -> None:
        if str(row["preview_inat_record_fingerprint"]) != live.inat_record_fingerprint:
            raise CoordinateSyncError("The iNaturalist observation changed after preview.", "inat_record_changed")
        if str(row["preview_mo_record_fingerprint"]) != live.mo_record_fingerprint:
            raise CoordinateSyncError("The Mushroom Observer observation changed after preview.", "mo_record_changed")

    def _is_satisfied(self, row: dict[str, Any], live: _LiveCoordinateState) -> bool:
        # The intended target point is the source point read when the action was
        # journaled, identified by the source record version. If the source
        # changed since then, we cannot prove the destination holds the intended
        # point, so satisfaction is not asserted.
        if live.mo_record_fingerprint != str(row["preview_mo_record_fingerprint"]):
            return False
        destination = live.destination
        # Verify the exact point and the OBSERVATION-LEVEL geoprivacy we actually
        # requested. The effective visibility may read back more restrictive than
        # requested when taxon geoprivacy clamps it (e.g. we set "open" but the
        # taxon forces "obscured"); that is still a correct write, so we do not
        # require effective equality. A LESS restrictive effective result than
        # requested, however, would be wrong and is rejected below.
        requested_geoprivacy = _normalize_geoprivacy(_INAT_GEOPRIVACY.get(str(row["proposed_privacy_state"])))
        if not destination.coordinates_available or not _same_point(destination, live.source):
            return False
        # The accuracy must also round-trip: a right point with the wrong accuracy
        # is not synchronized.
        if not _same_accuracy(live.source.accuracy_m, destination.accuracy_m):
            return False
        if destination.observation_geoprivacy != requested_geoprivacy:
            return False
        # Effective visibility must be at least as restrictive as the PREDICTED
        # post-write visibility, which already folds in the destination taxon
        # geoprivacy clamp (so a taxon-obscured result of an "open" request still
        # verifies, but a wrongly-less-restrictive result does not).
        predicted = _more_restrictive(requested_geoprivacy, destination.taxon_geoprivacy)
        effective_rank = _STATE_RANK.get(destination.privacy_state, 0)
        predicted_rank = _GEOPRIVACY_RANK.get(predicted, 0)
        return effective_rank >= predicted_rank

    # Fresh read -------------------------------------------------------

    def _refresh(
        self, profile: ReconciliationProfile, pair: dict[str, Any],
        cancelled: Callable[[], bool], *, verification_only: bool = False,
    ) -> _LiveCoordinateState:
        if cancelled():
            raise ReconciliationCancelled("Coordinate comparison cancelled")
        auth_generation = self.auth_generation_provider()
        mo_key_generation = self.mo_key_generation_provider()
        auth = self.auth_provider()
        token = auth.api_token if auth.is_authenticated else ""
        if not token:
            raise CoordinateSyncError(
                "iNaturalist authentication must match the selected reconciliation account.",
                "inat_auth_mismatch",
            )
        if not verification_only:
            current = _first_result(self.inat_client.get_current_user_v2(token))
            if positive_int(current.get("id") if current else None) != profile.inat_user_id:
                raise CoordinateSyncError(
                    "The authenticated iNaturalist account does not match the profile.",
                    "inat_auth_mismatch",
                )

        inat_id = int(pair["inat_observation_id"])
        mo_id = int(pair["mo_observation_id"])
        inat_raw = _first_result(self.inat_client.get_reconciliation_detail(inat_id, token, deep=False))
        if not inat_raw or positive_int(inat_raw.get("id")) != inat_id:
            raise CoordinateSyncError("The iNaturalist observation is unavailable.", "inat_unavailable")
        inat_uuid = str(inat_raw.get("uuid") or "").strip()
        inat_user_raw = inat_raw.get("user")
        inat_user = inat_user_raw if isinstance(inat_user_raw, dict) else {}
        if not inat_uuid or positive_int(inat_user.get("id")) != profile.inat_user_id:
            raise CoordinateSyncError("The iNaturalist record identity or owner changed.", "inat_owner_changed")
        if _fungi_status(inat_raw) == "nonfungal":
            raise CoordinateSyncError("The iNaturalist observation is now known to be outside Fungi.", "inat_out_of_scope")

        mo_raw = _first_result(self.mo_client.observation(mo_id, cancelled, detail="high"))
        if not mo_raw or positive_int(mo_raw.get("id")) != mo_id:
            raise CoordinateSyncError("The Mushroom Observer observation is unavailable.", "mo_unavailable")
        mo_observation = parse_mo_observation(mo_raw, profile.mo_user_id)
        if mo_observation.owner_id != profile.mo_user_id:
            raise CoordinateSyncError("The Mushroom Observer record owner changed.", "mo_owner_changed")
        if mo_observation.fungi_status == "nonfungal":
            raise CoordinateSyncError("The Mushroom Observer observation is now known to be outside Fungi.", "mo_out_of_scope")

        inat_date = _parse_date(inat_raw.get("observed_on"))
        mo_date = mo_observation.observed_on
        if inat_date and mo_date and abs((inat_date - mo_date).days) > 1:
            raise CoordinateSyncError(
                "The freshly read observation dates differ by more than one day; review the pair again.",
                "observed_date_conflict",
            )

        source = _mo_coordinate_snapshot(mo_raw, mo_id)
        destination = _inat_coordinate_snapshot(inat_raw, inat_id)
        distance = _distance_m(source, destination)
        # Reuse the shared specimen-identity validator, tolerating only the
        # coordinate being reconciled: an unrelated voucher/collection/owner/date/
        # deletion/scope conflict still blocks the copy. Verification re-reads only
        # confirm the destination point, so the full check is skipped there.
        specimen_conflict = ""
        specimen_warnings: tuple[str, ...] = ()
        if not verification_only:
            reader = INatReconciliationReader(self.inat_client)
            specimen_conflict, _specimen_fp, specimen_warnings = evaluate_specimen_state(
                self.db, profile, pair, inat_raw, mo_raw, reader,
                mo_client=self.mo_client, cancelled=cancelled, include_coordinates=False,
            )
        if not verification_only and (
            auth_generation != self.auth_generation_provider()
            or mo_key_generation != self.mo_key_generation_provider()
        ):
            raise CoordinateSyncError("Credential state changed during coordinate preflight.", "credential_context_changed")
        return _LiveCoordinateState(
            profile_id=profile.profile_id, pair_id=int(pair["pair_id"]),
            mo_observation_id=mo_id, inat_observation_id=inat_id,
            inat_observation_uuid=inat_uuid,
            inat_record_fingerprint=_inat_record_fingerprint(inat_raw),
            mo_record_fingerprint=mo_record_fingerprint(mo_raw),
            inat_token_marker=public_fingerprint(token),
            auth_generation=auth_generation, mo_key_generation=mo_key_generation,
            source=source, destination=destination,
            specimen_conflict=specimen_conflict, specimen_warnings=specimen_warnings,
            distance_m=distance,
        )

    # Eligibility / source anchoring -----------------------------------

    def _eligible_pair(self, profile_id: int, pair_id: int) -> dict[str, Any]:
        pair = self.db.pair_detail(profile_id, pair_id)
        if not pair or pair.get("review_state") != "confirmed" or pair.get("excluded"):
            raise CoordinateSyncError("Only a currently confirmed, non-excluded pair can produce coordinate actions.")
        if self.db.confirmed_pair_conflict(
            profile_id, int(pair["mo_observation_id"]), int(pair["inat_observation_id"]),
        ):
            raise CoordinateSyncError("Another confirmed one-to-one pairing conflicts with this pair.", "one_to_one_conflict")
        return pair

    def _require_current_source(self, row: dict[str, Any], pair: dict[str, Any]) -> None:
        group = self.db.action_group(int(row["profile_id"]), int(row["action_group_id"]))
        if not group or _pair_fingerprint(pair) != str(group["source_fingerprint"]):
            raise CoordinateSyncError("The confirmed pair changed after preview.", "pair_changed")

    def _pair_from_journal(self, profile_id: int, row: dict[str, Any]) -> dict[str, Any]:
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


# Coordinate parsing (memory-only raw values) --------------------------

def _mo_coordinate_snapshot(raw: dict[str, Any], observation_id: int) -> CoordinateRecordSnapshot:
    latitude, longitude, accuracy, privacy = parse_mo_coordinate(raw)
    available = latitude is not None and longitude is not None
    return CoordinateRecordSnapshot(
        site=RemoteSite.MO, observation_id=observation_id, coordinates_available=available,
        privacy_state=privacy, accuracy_m=accuracy,
        latitude=latitude, longitude=longitude,
    )


def _inat_coordinate_snapshot(raw: dict[str, Any], observation_id: int) -> CoordinateRecordSnapshot:
    latitude, longitude, point_source = _inat_point(raw)
    available = latitude is not None and longitude is not None
    # Only the authorized private point proves the exact coordinate; a public
    # ``geojson``/``location`` value on an obscured or private record is the
    # imprecise, deliberately-shifted point, not the true one.
    exact_source = point_source == "private_geojson"
    # Keep the observation-level and taxon-level settings separate. The
    # observation-level value is what a write controls and what verification
    # checks; the taxon-level value can independently clamp effective visibility.
    observation_geoprivacy = _normalize_geoprivacy(raw.get("geoprivacy"))
    taxon_geoprivacy = _normalize_geoprivacy(raw.get("taxon_geoprivacy"))
    # An unrecognized nonempty value on either field is treated as unknown and
    # blocks the action rather than silently degrading to open.
    if observation_geoprivacy == "unknown" or taxon_geoprivacy == "unknown":
        effective = "unknown"
    else:
        effective = _more_restrictive(observation_geoprivacy, taxon_geoprivacy)
    obscured_or_private = effective in {"private", "obscured"}
    # Whenever either field imposes obscuration/private treatment and no
    # authorized private point is available, the exact coordinate is unreadable —
    # even if an imprecise public point is present.
    exact_point_unreadable = obscured_or_private and not exact_source
    if effective == "unknown":
        privacy = CoordinatePrivacyState.UNKNOWN.value
    elif effective == "private":
        privacy = CoordinatePrivacyState.PRIVATE.value
    elif effective == "obscured":
        privacy = CoordinatePrivacyState.OBSCURED.value
    elif available:
        # No obscuration on either field and a point is present: it is exact.
        privacy = CoordinatePrivacyState.PUBLIC.value
    else:
        privacy = CoordinatePrivacyState.ABSENT.value
    try:
        accuracy = float(raw.get("positional_accuracy")) if raw.get("positional_accuracy") is not None else None
    except (TypeError, ValueError):
        accuracy = None
    return CoordinateRecordSnapshot(
        site=RemoteSite.INAT, observation_id=observation_id, coordinates_available=available,
        privacy_state=privacy, observation_geoprivacy=observation_geoprivacy,
        taxon_geoprivacy=taxon_geoprivacy, accuracy_m=accuracy,
        exact_point_unreadable=exact_point_unreadable,
        latitude=latitude, longitude=longitude,
    )


# Restriction order for iNaturalist geoprivacy. ``public`` is the internal state
# name for an ``open`` observation; both rank the same.
_GEOPRIVACY_RANK = {"open": 1, "public": 1, "obscured": 2, "private": 3}


def _normalize_geoprivacy(value: object) -> str:
    """Normalize a raw geoprivacy value to open/obscured/private, or unknown.

    ``None`` and empty mean the iNaturalist default (open). A recognized value is
    returned as-is; any other nonempty value is ``"unknown"`` so the caller blocks
    rather than silently treating it as open.
    """
    text = str(value or "").strip().casefold()
    if text in ("", "open"):
        return "open"
    if text in ("obscured", "private"):
        return text
    return "unknown"


def _more_restrictive(a: str, b: str) -> str:
    """The more restrictive of two known geoprivacy values (private>obscured>open)."""
    return a if _GEOPRIVACY_RANK.get(a, 0) >= _GEOPRIVACY_RANK.get(b, 0) else b


def _inat_point(raw: dict[str, Any]) -> tuple[Optional[float], Optional[float], str]:
    """Read a point, reporting which source it came from.

    The authorized read prefers the true ``private_geojson`` point; the returned
    source key lets the caller tell an exact (private) point from an imprecise,
    obscured public one.
    """
    for key in ("private_geojson", "geojson"):
        coordinates = raw.get(key)
        if isinstance(coordinates, dict):
            point = coordinates.get("coordinates")
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                try:
                    return float(point[1]), float(point[0]), key
                except (TypeError, ValueError):
                    continue
    location = raw.get("location")
    if isinstance(location, str) and "," in location:
        try:
            latitude, longitude = (float(value.strip()) for value in location.split(",", 1))
            return latitude, longitude, "location"
        except ValueError:
            pass
    return None, None, ""


def _same_point(a: CoordinateRecordSnapshot, b: CoordinateRecordSnapshot) -> bool:
    """In-memory point equality at ~1 m precision; no rounded point is stored."""
    if a.latitude is None or a.longitude is None or b.latitude is None or b.longitude is None:
        return False
    return (
        round(a.latitude, _POINT_PRECISION) == round(b.latitude, _POINT_PRECISION)
        and round(a.longitude, _POINT_PRECISION) == round(b.longitude, _POINT_PRECISION)
    )


def _same_accuracy(a: Optional[float], b: Optional[float]) -> bool:
    """Positional-accuracy equality with explicit None semantics.

    Both unspecified → equal; exactly one unspecified → different; both numeric →
    equal within a small meter tolerance (iNaturalist stores integer metres).
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= _ACCURACY_TOLERANCE_M


def _distance_m(
    source: CoordinateRecordSnapshot, destination: CoordinateRecordSnapshot,
) -> Optional[float]:
    if (
        source.latitude is None or source.longitude is None
        or destination.latitude is None or destination.longitude is None
    ):
        return None
    radius = 6371000.0
    lat1, lon1 = radians(source.latitude), radians(source.longitude)
    lat2, lon2 = radians(destination.latitude), radians(destination.longitude)
    d_lat = lat2 - lat1
    d_lon = lon2 - lon1
    a = sin(d_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(d_lon / 2) ** 2
    return 2 * radius * asin(min(1.0, sqrt(a)))


def _public_snapshot(snapshot: CoordinateRecordSnapshot) -> CoordinateRecordSnapshot:
    """Drop the memory-only raw point before the preview leaves the worker.

    The UI never needs raw coordinates; it renders privacy state, availability,
    accuracy, and the add-vs-replace decision. Stripping the point here keeps raw
    latitude/longitude off any Qt signal, dialog, or external map link. No
    coordinate-derived fingerprint is carried either.
    """
    return CoordinateRecordSnapshot(
        site=snapshot.site, observation_id=snapshot.observation_id,
        coordinates_available=snapshot.coordinates_available,
        privacy_state=snapshot.privacy_state,
        observation_geoprivacy=snapshot.observation_geoprivacy,
        taxon_geoprivacy=snapshot.taxon_geoprivacy,
        accuracy_m=snapshot.accuracy_m,
        exact_point_unreadable=snapshot.exact_point_unreadable,
    )


# Effective-visibility rank of an internal privacy_state (public==open).
_STATE_RANK = {
    CoordinatePrivacyState.PUBLIC.value: 1,
    CoordinatePrivacyState.OBSCURED.value: 2,
    CoordinatePrivacyState.PRIVATE.value: 3,
}


def _broadens_visibility(
    destination: CoordinateRecordSnapshot, proposed_observation_geoprivacy: str,
) -> bool:
    """True only when the copy makes the EXISTING destination coordinate more visible.

    Predicted post-write effective visibility is the MOST restrictive of the
    proposed observation-level geoprivacy and the destination's current taxon
    geoprivacy — setting the observation to ``open`` does not broaden anything
    while taxon geoprivacy still clamps it to obscured. An absent or unknown
    current visibility has nothing to broaden (a brand-new coordinate's
    disclosure is governed by ``sends_nonpublic_source`` instead).
    """
    if not destination.coordinates_available and not destination.exact_point_unreadable:
        return False
    current_rank = _STATE_RANK.get(destination.privacy_state)
    if current_rank is None:
        return False
    predicted = _more_restrictive(proposed_observation_geoprivacy, destination.taxon_geoprivacy)
    predicted_rank = _GEOPRIVACY_RANK.get(predicted, 0)
    if predicted_rank == 0:
        return False
    return predicted_rank < current_rank


def _is_coordinate_action(action_type: object) -> bool:
    return str(action_type or "").startswith("inat_coordinate_")


def _first_result(payload: object) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    rows = payload.get("results")
    if isinstance(rows, list):
        return next((item for item in rows if isinstance(item, dict)), None)
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    return payload if payload.get("id") is not None else None


def _parse_date(value: object) -> Optional[date]:
    if isinstance(value, dict):
        value = value.get("date") or value.get("start") or value.get("observed_on")
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _fungi_status(raw: dict[str, Any]) -> str:
    taxon = raw.get("taxon") if isinstance(raw.get("taxon"), dict) else {}
    ancestry = str(taxon.get("ancestry") or "")
    iconic = str(taxon.get("iconic_taxon_name") or "").casefold()
    if iconic == "fungi" or "47170" in ancestry.split("/"):
        return "fungi"
    return "nonfungal" if taxon else "unknown"


def _inat_record_fingerprint(raw: dict[str, Any]) -> str:
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    taxon = raw.get("taxon") if isinstance(raw.get("taxon"), dict) else {}
    return public_fingerprint(
        raw.get("id"), raw.get("uuid"), user.get("id"), raw.get("observed_on"),
        raw.get("updated_at"), taxon.get("id"), taxon.get("ancestry"),
    )


def _pair_fingerprint(pair: dict[str, Any]) -> str:
    return public_fingerprint(
        "pair", pair.get("pair_id"), pair.get("updated_at"), pair.get("review_state"),
        pair.get("link_state"), pair.get("confirmed_by"),
    )
