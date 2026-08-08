"""Gate 2C lossless deletion review and fail-closed deletion saga.

Production deletion is intentionally disabled until a site's complete content
enumeration and identity-safe post-delete verifier have passed controlled live
acceptance.  The saga boundary is injectable so journal ordering, concurrency,
unknown outcomes, cancellation, and tombstone finalization can be proven with
disposable offline fakes without weakening that production guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import re
import uuid as uuidlib
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from observation_workbench.api.auth import AuthState
from observation_workbench.api.client import INatClient

from .consolidation_identity import canonical_stable_identity_fingerprint
from .db import ReconciliationDB
from .mo_client import MOClient, ReconciliationCancelled, results_from_payload
from .normalization import (
    parse_inat_observation_url,
    parse_mo_observation_url,
    public_fingerprint,
)
from .types import ReconciliationProfile, RemoteSite


class CapabilityStatus(str, Enum):
    VERIFIED = "VERIFIED"
    OFFLINE_PROVEN = "OFFLINE-PROVEN"
    NEEDS_LIVE_PROOF = "NEEDS-LIVE-PROOF"
    UNSUPPORTED = "UNSUPPORTED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class SiteDeletionCapability:
    site: RemoteSite
    owned_observation_delete: CapabilityStatus
    stable_remote_identity: CapabilityStatus
    complete_donor_content_read: CapabilityStatus
    third_party_enumeration: CapabilityStatus
    definitive_post_delete_verification: CapabilityStatus
    unknown_outcome_recovery: CapabilityStatus
    safe_for_phase_2c: CapabilityStatus
    endpoint: str
    disabled_reason: str
    execution_enabled: bool = False


PRODUCTION_CAPABILITIES: Mapping[RemoteSite, SiteDeletionCapability] = {
    RemoteSite.MO: SiteDeletionCapability(
        site=RemoteSite.MO,
        owned_observation_delete=CapabilityStatus.NEEDS_LIVE_PROOF,
        stable_remote_identity=CapabilityStatus.UNSUPPORTED,
        complete_donor_content_read=CapabilityStatus.AMBIGUOUS,
        third_party_enumeration=CapabilityStatus.AMBIGUOUS,
        definitive_post_delete_verification=CapabilityStatus.UNSUPPORTED,
        unknown_outcome_recovery=CapabilityStatus.UNSUPPORTED,
        safe_for_phase_2c=CapabilityStatus.UNSUPPORTED,
        endpoint="DELETE https://mushroomobserver.org/api2/observations?id=<id>",
        disabled_reason=(
            "Mushroom Observer has no proven stable observation UUID, complete "
            "third-party inventory, or identity-safe post-delete absence verifier."
        ),
    ),
    RemoteSite.INAT: SiteDeletionCapability(
        site=RemoteSite.INAT,
        owned_observation_delete=CapabilityStatus.VERIFIED,
        stable_remote_identity=CapabilityStatus.VERIFIED,
        complete_donor_content_read=CapabilityStatus.AMBIGUOUS,
        third_party_enumeration=CapabilityStatus.AMBIGUOUS,
        definitive_post_delete_verification=CapabilityStatus.NEEDS_LIVE_PROOF,
        unknown_outcome_recovery=CapabilityStatus.NEEDS_LIVE_PROOF,
        safe_for_phase_2c=CapabilityStatus.NEEDS_LIVE_PROOF,
        endpoint="DELETE https://api.inaturalist.org/v2/observations/{uuid}",
        disabled_reason=(
            "Complete contribution/project enumeration and exact deleted-feed "
            "verification have not passed controlled live acceptance."
        ),
    ),
}


class DeletionError(RuntimeError):
    def __init__(self, message: str, code: str = "deletion_unavailable") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeletionContent:
    content_type: str
    identity: str
    value: str = field(default="", repr=False)
    value_fingerprint: str = ""
    safe_summary: str = ""
    source_media_identity: str = ""
    original_byte_fingerprint: str = ""
    license_label: str = ""
    copyright_holder: str = ""
    attribution: str = ""


@dataclass(frozen=True)
class ThirdPartyContribution:
    contribution_type: str
    remote_identity: str
    contributor_id: int
    safe_summary: str
    fingerprint: str


@dataclass(frozen=True)
class ExternalDependency:
    dependency_type: str
    source_identity: str
    target_identity: str
    safe_summary: str
    proven_points_to_canonical: bool = False


@dataclass(frozen=True)
class RemoteDeletionRecord:
    site: RemoteSite
    observation_id: int
    remote_uuid: str
    owner_id: int
    owner_login: str
    updated_at: str
    record_fingerprint: str
    contents: tuple[DeletionContent, ...]
    third_party: tuple[ThirdPartyContribution, ...] = ()
    dependencies: tuple[ExternalDependency, ...] = ()
    inventory_issues: tuple[str, ...] = ()
    content_enumeration_complete: bool = False
    third_party_enumeration_complete: bool = False
    dependency_search_complete: bool = False
    reciprocal_targets: tuple[tuple[RemoteSite, int], ...] = ()
    exists: bool = True


@dataclass(frozen=True)
class DeletionParityItem:
    content_type: str
    source_identity: str
    canonical_identity: str
    match_method: str
    source_fingerprint: str
    canonical_fingerprint: str
    preserved: bool
    blocking_reason: str
    safe_summary: str


@dataclass(frozen=True)
class DonorDeletionReadiness:
    stable_member_id: int
    site: RemoteSite
    observation_id: int
    remote_uuid: str
    owner_account: str
    admitting_attempt_id: int
    evidence_path: str
    remote_updated_at: str
    content_inventory: tuple[DeletionContent, ...]
    parity_items: tuple[DeletionParityItem, ...]
    third_party: tuple[ThirdPartyContribution, ...]
    dependencies: tuple[ExternalDependency, ...]
    blocking_reasons: tuple[str, ...]
    remote_record_fingerprint: str
    content_inventory_fingerprint: str
    parity_fingerprint: str
    third_party_activity_fingerprint: str
    dependency_fingerprint: str

    @property
    def eligible(self) -> bool:
        return not self.blocking_reasons

    @property
    def status(self) -> str:
        if self.eligible:
            return "Eligible: lossless duplicate"
        return self.blocking_reasons[0]


@dataclass(frozen=True)
class DonorDeletionPreview:
    profile_id: int
    consolidation_id: int
    base_finalized_attempt_id: int
    auth_generation: int
    mo_key_generation: int
    canonical_mo_observation_id: Optional[int]
    canonical_inat_observation_id: Optional[int]
    canonical_stable_identity_fingerprint: str
    canonical_mutable_snapshot_fingerprint: str
    parity_report_fingerprint: str
    donors: tuple[DonorDeletionReadiness, ...]
    warnings: tuple[str, ...] = ()

    def confirmation_fingerprint(self, selected_member_ids: Sequence[int]) -> str:
        selected = tuple(int(value) for value in selected_member_ids)
        lookup = {item.stable_member_id: item for item in self.donors}
        return public_fingerprint(
            "phase_2c_confirmation_v1",
            self.profile_id,
            self.consolidation_id,
            self.base_finalized_attempt_id,
            self.canonical_stable_identity_fingerprint,
            self.canonical_mutable_snapshot_fingerprint,
            self.parity_report_fingerprint,
            self.auth_generation,
            self.mo_key_generation,
            *(
                public_fingerprint(
                    member_id,
                    lookup[member_id].remote_record_fingerprint,
                    lookup[member_id].parity_fingerprint,
                )
                for member_id in selected
            ),
        )

    def typed_phrase(self, selected_member_ids: Sequence[int]) -> str:
        selected = tuple(selected_member_ids)
        if len(selected) == 1:
            item = next(
                donor
                for donor in self.donors
                if donor.stable_member_id == int(selected[0])
            )
            return f"DELETE {item.site.value.upper()} {item.observation_id}"
        return f"DELETE {len(selected)} DONORS"


@dataclass(frozen=True)
class DeletionActionResult:
    action_id: int
    state: str
    message: str


class DeleteDispatch(Protocol):
    """Capability-proof boundary used by the saga."""

    def refresh_record(
        self,
        profile: ReconciliationProfile,
        site: RemoteSite,
        observation_id: int,
        cancelled: Callable[[], bool],
    ) -> RemoteDeletionRecord: ...

    def delete_exact(
        self,
        profile: ReconciliationProfile,
        record: RemoteDeletionRecord,
        request_correlation: str,
        cancelled: Callable[[], bool],
    ) -> None: ...

    def verify_exact_absence(
        self,
        profile: ReconciliationProfile,
        site: RemoteSite,
        observation_id: int,
        remote_uuid: str,
        owner_id: int,
        cancelled: Callable[[], bool],
    ) -> str:
        """Return ``deleted``, ``present``, or ``ambiguous``."""
        ...


class DisabledProductionDeleteDispatch:
    """Fresh reads for review; the destructive boundary is unreachable."""

    def __init__(
        self,
        inat_client: INatClient,
        mo_client: MOClient,
        auth_provider: Callable[[], AuthState],
        mo_key_provider: Callable[[int], str],
    ) -> None:
        self.inat_client = inat_client
        self.mo_client = mo_client
        self.auth_provider = auth_provider
        self.mo_key_provider = mo_key_provider

    def refresh_record(
        self,
        profile: ReconciliationProfile,
        site: RemoteSite,
        observation_id: int,
        cancelled: Callable[[], bool],
    ) -> RemoteDeletionRecord:
        if cancelled():
            raise ReconciliationCancelled("Deletion review cancelled")
        if site is RemoteSite.INAT:
            auth = self.auth_provider()
            if (
                not auth.is_authenticated
                or auth.login.casefold() != profile.inat_login.casefold()
            ):
                raise DeletionError(
                    "Matching iNaturalist authentication is required.",
                    "authentication_mismatch",
                )
            # V2Response is itself the payload dict; it carries only .metadata.
            detail = self.inat_client.get_reconciliation_detail(
                observation_id,
                auth.api_token,
                deep=True,
            )
            raw = dict(_first_result(detail))
            if not raw:
                return RemoteDeletionRecord(
                    site,
                    observation_id,
                    "",
                    0,
                    "",
                    "",
                    "",
                    (),
                    exists=False,
                )
            try:
                activity = self.inat_client.get_observation_v2(
                    raw.get("uuid", ""),
                    auth.api_token,
                )
            except Exception:
                raw["_phase2c_activity_read_failed"] = True
            else:
                raw.update(
                    {
                        key: value
                        for key, value in _first_result(activity).items()
                        if key not in {"id", "uuid"}
                    }
                )
        else:
            raw = dict(
                _first_result(
                    self.mo_client.observation(observation_id, cancelled, detail="high")
                )
            )
            if not raw:
                return RemoteDeletionRecord(
                    site,
                    observation_id,
                    "",
                    0,
                    "",
                    "",
                    "",
                    (),
                    exists=False,
                )
            for key, reader in (
                (
                    "_phase2c_images",
                    lambda: self.mo_client.images_for_observation(
                        observation_id, cancelled
                    ),
                ),
                (
                    "_phase2c_sequences",
                    lambda: self.mo_client.sequences(
                        profile.mo_user_id, (observation_id,), cancelled
                    ),
                ),
                (
                    "_phase2c_links",
                    lambda: self.mo_client.external_links((observation_id,), cancelled),
                ),
            ):
                try:
                    raw[key] = results_from_payload(reader())
                except ReconciliationCancelled:
                    raise
                except Exception:
                    raw[key] = []
                    raw[f"{key}_read_failed"] = True
        if not raw:
            return RemoteDeletionRecord(
                site,
                observation_id,
                "",
                0,
                "",
                "",
                "",
                (),
                exists=False,
            )
        return _record_from_raw(site, observation_id, raw, profile)

    def delete_exact(
        self,
        profile: ReconciliationProfile,
        record: RemoteDeletionRecord,
        request_correlation: str,
        cancelled: Callable[[], bool],
    ) -> None:
        del profile, request_correlation, cancelled
        capability = PRODUCTION_CAPABILITIES[record.site]
        raise DeletionError(
            f"{record.site.value} deletion is disabled: "
            f"{capability.disabled_reason}",
            "deletion_capability_unverified",
        )

    def verify_exact_absence(
        self,
        profile: ReconciliationProfile,
        site: RemoteSite,
        observation_id: int,
        remote_uuid: str,
        owner_id: int,
        cancelled: Callable[[], bool],
    ) -> str:
        del profile, observation_id, remote_uuid, owner_id, cancelled
        if not PRODUCTION_CAPABILITIES[site].execution_enabled:
            return "ambiguous"
        return "ambiguous"


def normalized_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def analyze_lossless_parity(
    donor: RemoteDeletionRecord,
    canonical: RemoteDeletionRecord,
) -> tuple[tuple[DeletionParityItem, ...], tuple[str, ...]]:
    """Compare each donor item by type-specific, exact preservation rules."""
    canonical_by_type: dict[str, list[DeletionContent]] = {}
    for item in canonical.contents:
        canonical_by_type.setdefault(item.content_type, []).append(item)
    parity: list[DeletionParityItem] = []
    reasons: list[str] = []
    used_candidates: dict[str, set[int]] = {}
    strict_scalar_types = {
        "description",
        "notes",
        "voucher",
        "collection_number",
        "accession",
        "observation_field",
        "date",
        "coordinates",
        "positional_accuracy",
        "locality",
        "geoprivacy",
        "taxon",
        "license",
        "copyright_holder",
        "attribution",
        "external_url",
        "project_association",
        "sound",
    }
    for source in donor.contents:
        candidates = canonical_by_type.get(source.content_type, [])
        match: Optional[DeletionContent] = None
        match_index: Optional[int] = None
        method = ""
        if source.content_type == "photo":
            if source.source_media_identity:
                matched = next(
                    (
                        (index, candidate)
                        for index, candidate in enumerate(candidates)
                        if index
                        not in used_candidates.setdefault(source.content_type, set())
                        if candidate.source_media_identity
                        == source.source_media_identity
                        and _photo_metadata_equal(source, candidate)
                    ),
                    None,
                )
                if matched is not None:
                    match_index, match = matched
                method = "stable_source_media_identity"
            if match is None and source.original_byte_fingerprint:
                matched = next(
                    (
                        (index, candidate)
                        for index, candidate in enumerate(candidates)
                        if index
                        not in used_candidates.setdefault(source.content_type, set())
                        if candidate.original_byte_fingerprint
                        == source.original_byte_fingerprint
                        and _photo_metadata_equal(source, candidate)
                    ),
                    None,
                )
                if matched is not None:
                    match_index, match = matched
                method = "full_original_byte_fingerprint"
            if match is None:
                reason = "Blocked: unique photo"
                if (
                    not source.original_byte_fingerprint
                    and not source.source_media_identity
                ):
                    reason = "Blocked: original photo bytes unavailable"
            else:
                reason = ""
        elif source.content_type == "sequence":
            matched = next(
                (
                    (index, candidate)
                    for index, candidate in enumerate(candidates)
                    if index
                    not in used_candidates.setdefault(source.content_type, set())
                    if candidate.value_fingerprint
                    and candidate.value_fingerprint == source.value_fingerprint
                    and candidate.identity == source.identity
                ),
                None,
            )
            if matched is not None:
                match_index, match = matched
            method = "exact_accession_and_sequence_identity"
            reason = "" if match else "Blocked: different or unavailable sequence"
        elif source.content_type.startswith("owner_"):
            matched = next(
                (
                    (index, candidate)
                    for index, candidate in enumerate(candidates)
                    if index
                    not in used_candidates.setdefault(source.content_type, set())
                    if candidate.value_fingerprint == source.value_fingerprint
                    and normalized_text(candidate.value)
                    == normalized_text(source.value)
                ),
                None,
            )
            if matched is not None:
                match_index, match = matched
            method = "exact_owner_authored_activity"
            reason = "" if match else f"Blocked: unpreserved {source.content_type}"
        elif source.content_type in strict_scalar_types:
            if not source.value:
                match = source
                method = "genuinely_empty"
            else:
                matched = next(
                    (
                        (index, candidate)
                        for index, candidate in enumerate(candidates)
                        if index
                        not in used_candidates.setdefault(source.content_type, set())
                        if candidate.identity == source.identity
                        and normalized_text(candidate.value)
                        == normalized_text(source.value)
                    ),
                    None,
                )
                if matched is not None:
                    match_index, match = matched
                method = "exact_normalized_typed_value"
            label = source.content_type.replace("_", " ")
            reason = "" if match else f"Blocked: different {label}"
        else:
            matched = next(
                (
                    (index, candidate)
                    for index, candidate in enumerate(candidates)
                    if index
                    not in used_candidates.setdefault(source.content_type, set())
                    if candidate.identity == source.identity
                    and candidate.value_fingerprint == source.value_fingerprint
                ),
                None,
            )
            if matched is not None:
                match_index, match = matched
            method = "exact_typed_fingerprint"
            reason = "" if match else f"Blocked: unpreserved {source.content_type}"
        preserved = match is not None
        if match_index is not None:
            used_candidates.setdefault(source.content_type, set()).add(match_index)
        parity.append(
            DeletionParityItem(
                content_type=source.content_type,
                source_identity=source.identity,
                canonical_identity=match.identity if match else "",
                match_method=method,
                source_fingerprint=source.value_fingerprint,
                canonical_fingerprint=match.value_fingerprint if match else "",
                preserved=preserved,
                blocking_reason=reason,
                safe_summary=source.safe_summary,
            )
        )
        if reason:
            reasons.append(reason)
    return tuple(parity), tuple(dict.fromkeys(reasons))


def _photo_metadata_equal(left: DeletionContent, right: DeletionContent) -> bool:
    return (
        normalized_text(left.license_label).casefold()
        == normalized_text(right.license_label).casefold()
        and normalized_text(left.copyright_holder)
        == normalized_text(right.copyright_holder)
        and normalized_text(left.attribution) == normalized_text(right.attribution)
    )


class DeletionService:
    def __init__(
        self,
        db: ReconciliationDB,
        inat_client: INatClient,
        mo_client: MOClient,
        auth_provider: Callable[[], AuthState],
        mo_key_provider: Callable[[int], str],
        auth_generation_provider: Callable[[], int],
        mo_key_generation_provider: Callable[[], int],
        *,
        dispatch: Optional[DeleteDispatch] = None,
        capabilities: Mapping[
            RemoteSite, SiteDeletionCapability
        ] = PRODUCTION_CAPABILITIES,
        phase_2b_closed_provider: Callable[[], bool] = lambda: False,
    ) -> None:
        self.db = db
        self.inat_client = inat_client
        self.mo_client = mo_client
        self.auth_provider = auth_provider
        self.mo_key_provider = mo_key_provider
        self.auth_generation_provider = auth_generation_provider
        self.mo_key_generation_provider = mo_key_generation_provider
        self.dispatch = dispatch or DisabledProductionDeleteDispatch(
            inat_client,
            mo_client,
            auth_provider,
            mo_key_provider,
        )
        self.capabilities = capabilities
        self.phase_2b_closed_provider = phase_2b_closed_provider

    def prepare_preview(
        self,
        profile_id: int,
        consolidation_id: int,
        cancelled: Callable[[], bool] = lambda: False,
        *,
        allowed_deletion_attempt_id: Optional[int] = None,
    ) -> DonorDeletionPreview:
        profile = self.db.profile(profile_id)
        consolidation = self.db.get_consolidation(profile_id, consolidation_id)
        if not consolidation or str(consolidation["state"]) != "finalized":
            raise DeletionError(
                "Phase 2C requires a finalized Phase 2B consolidation.",
                "phase_2b_not_finalized",
            )
        base_attempt = int(consolidation.get("current_finalized_attempt_id") or 0)
        if base_attempt <= 0:
            raise DeletionError(
                "The finalized canonical baseline is missing.", "canonical_drift"
            )
        members = self.db.list_consolidation_members(profile_id, consolidation_id)
        if not members:
            raise DeletionError("The consolidation has no stable members.")
        unresolved = self._unresolved_specimen_activity(
            profile_id,
            consolidation_id,
            allowed_deletion_attempt_id=allowed_deletion_attempt_id,
        )
        records: dict[tuple[str, int], RemoteDeletionRecord] = {}
        for member in members:
            if str(member.get("remote_state") or "online") == "deleted":
                continue
            key = (str(member["site"]), int(member["observation_id"]))
            records[key] = self.dispatch.refresh_record(
                profile,
                RemoteSite(key[0]),
                key[1],
                cancelled,
            )
        canonical: dict[RemoteSite, RemoteDeletionRecord] = {}
        for site, field_name in (
            (RemoteSite.MO, "canonical_mo_observation_id"),
            (RemoteSite.INAT, "canonical_inat_observation_id"),
        ):
            value = consolidation.get(field_name)
            if value is not None:
                record = records.get((site.value, int(value)))
                if record is None or not record.exists:
                    raise DeletionError(
                        "A canonical observation no longer exists.", "canonical_drift"
                    )
                expected_owner = (
                    profile.mo_user_id
                    if site is RemoteSite.MO
                    else profile.inat_user_id
                )
                if record.owner_id != expected_owner:
                    raise DeletionError(
                        "Canonical ownership changed.", "canonical_drift"
                    )
                stable_member = next(
                    (
                        member
                        for member in members
                        if str(member["site"]) == site.value
                        and int(member["observation_id"]) == int(value)
                        and str(member["role"]) == "canonical"
                    ),
                    None,
                )
                if (
                    stable_member is None
                    or int(stable_member.get("stable_owner_account_id") or 0)
                    != expected_owner
                    or str(stable_member.get("remote_uuid") or "") != record.remote_uuid
                    or str(stable_member.get("local_state")) != "canonical"
                ):
                    raise DeletionError(
                        "Canonical stable identity changed.", "canonical_drift"
                    )
                canonical[site] = record
        if len(canonical) == 2:
            pair = self.db.pair_by_records(
                profile_id,
                canonical[RemoteSite.MO].observation_id,
                canonical[RemoteSite.INAT].observation_id,
            )
            if (
                not pair
                or str(pair.get("review_state")) != "confirmed"
                or int(pair.get("pair_id") or 0)
                != int(consolidation.get("canonical_pair_id") or 0)
                or (
                    RemoteSite.INAT,
                    canonical[RemoteSite.INAT].observation_id,
                )
                not in canonical[RemoteSite.MO].reciprocal_targets
                or (
                    RemoteSite.MO,
                    canonical[RemoteSite.MO].observation_id,
                )
                not in canonical[RemoteSite.INAT].reciprocal_targets
            ):
                raise DeletionError(
                    "The confirmed canonical reciprocal pair drifted.",
                    "canonical_drift",
                )
        stable_identity = public_fingerprint(
            "phase_2c_canonical_stable_v1",
            *(
                canonical_stable_identity_fingerprint(
                    site,
                    record.observation_id,
                    record.remote_uuid,
                    record.owner_id,
                )
                for site, record in sorted(
                    canonical.items(), key=lambda item: item[0].value
                )
            ),
        )
        mutable_snapshot = public_fingerprint(
            "phase_2c_canonical_mutable_v1",
            *(
                record.record_fingerprint
                for _, record in sorted(
                    canonical.items(), key=lambda item: item[0].value
                )
            ),
        )
        donors: list[DonorDeletionReadiness] = []
        for member in members:
            if str(member["role"]) != "donor":
                continue
            if str(member.get("remote_state") or "online") == "deleted":
                continue
            site = RemoteSite(str(member["site"]))
            donor = records.get((site.value, int(member["observation_id"])))
            reasons = self._stable_donor_reasons(
                profile,
                consolidation,
                member,
                donor,
                unresolved,
            )
            parity_items: tuple[DeletionParityItem, ...] = ()
            dependencies: tuple[ExternalDependency, ...] = ()
            if donor is not None and donor.exists and site in canonical:
                dependencies = (
                    *donor.dependencies,
                    *self._local_dependencies(
                        profile_id,
                        consolidation_id,
                        site,
                        int(member["observation_id"]),
                    ),
                )
                parity_items, parity_reasons = analyze_lossless_parity(
                    donor,
                    canonical[site],
                )
                reasons.extend(parity_reasons)
                if not donor.content_enumeration_complete:
                    reasons.append("Blocked: complete donor content read unavailable")
                for issue in donor.inventory_issues:
                    reasons.append(f"Blocked: incomplete donor inventory ({issue})")
                if not donor.third_party_enumeration_complete:
                    reasons.append("Blocked: third-party enumeration incomplete")
                if donor.third_party:
                    for contribution in donor.third_party:
                        reasons.append(
                            "Blocked: third-party "
                            + contribution.contribution_type.replace("_", " ")
                        )
                if not donor.dependency_search_complete:
                    reasons.append("Blocked: external dependency search incomplete")
                for dependency in dependencies:
                    if not dependency.proven_points_to_canonical:
                        reasons.append("Blocked: external dependency")
            elif donor is None or not donor.exists:
                reasons.append("Blocked: donor unavailable")
            else:
                # The donor is readable, but this site has no canonical record
                # to compare it against, so lossless parity cannot be proven.
                reasons.append(
                    f"Blocked: no canonical {site.value} observation to preserve "
                    "this donor's content"
                )
            capability = self.capabilities[site]
            if not capability.execution_enabled:
                reasons.append("Blocked: deletion capability unverified")
            reasons = list(dict.fromkeys(reasons))
            contents = donor.contents if donor else ()
            third_party = donor.third_party if donor else ()
            if donor is None:
                dependencies = ()
            inventory_fp = _content_inventory_fingerprint(contents)
            parity_fp = _parity_fingerprint(parity_items)
            third_party_fp = public_fingerprint(
                "phase_2c_third_party_v1",
                *(item.fingerprint for item in third_party),
            )
            dependency_fp = public_fingerprint(
                "phase_2c_dependencies_v1",
                *(
                    public_fingerprint(
                        item.dependency_type,
                        item.source_identity,
                        item.target_identity,
                        item.proven_points_to_canonical,
                    )
                    for item in dependencies
                ),
            )
            admitting_attempt = int(
                member.get("added_by_attempt_id")
                or member.get("superseded_by_attempt_id")
                or 0
            )
            donors.append(
                DonorDeletionReadiness(
                    stable_member_id=int(member["consolidation_member_id"]),
                    site=site,
                    observation_id=int(member["observation_id"]),
                    remote_uuid=str(member.get("remote_uuid") or ""),
                    owner_account=(
                        f"{donor.owner_login} (id {donor.owner_id})"
                        if donor
                        else "unavailable"
                    ),
                    admitting_attempt_id=admitting_attempt,
                    evidence_path=f"Phase 2B attempt #{admitting_attempt} evidence graph",
                    remote_updated_at=donor.updated_at if donor else "",
                    content_inventory=contents,
                    parity_items=parity_items,
                    third_party=third_party,
                    dependencies=dependencies,
                    blocking_reasons=tuple(reasons),
                    remote_record_fingerprint=donor.record_fingerprint if donor else "",
                    content_inventory_fingerprint=inventory_fp,
                    parity_fingerprint=parity_fp,
                    third_party_activity_fingerprint=third_party_fp,
                    dependency_fingerprint=dependency_fp,
                )
            )
        report_fp = public_fingerprint(
            "phase_2c_report_v1",
            stable_identity,
            mutable_snapshot,
            *(
                public_fingerprint(
                    item.stable_member_id,
                    item.remote_record_fingerprint,
                    item.content_inventory_fingerprint,
                    item.parity_fingerprint,
                    item.third_party_activity_fingerprint,
                    item.dependency_fingerprint,
                    *item.blocking_reasons,
                )
                for item in donors
            ),
        )
        warnings = []
        if not self.phase_2b_closed_provider():
            warnings.append(
                "Phase 2B is not formally closed; live Phase 2C deletion remains blocked."
            )
        return DonorDeletionPreview(
            profile_id=profile_id,
            consolidation_id=consolidation_id,
            base_finalized_attempt_id=base_attempt,
            auth_generation=self.auth_generation_provider(),
            mo_key_generation=self.mo_key_generation_provider(),
            canonical_mo_observation_id=consolidation.get(
                "canonical_mo_observation_id"
            ),
            canonical_inat_observation_id=consolidation.get(
                "canonical_inat_observation_id"
            ),
            canonical_stable_identity_fingerprint=stable_identity,
            canonical_mutable_snapshot_fingerprint=mutable_snapshot,
            parity_report_fingerprint=report_fp,
            donors=tuple(donors),
            warnings=tuple(warnings),
        )

    def execute_group(
        self,
        profile_id: int,
        group_id: int,
        cancelled: Callable[[], bool] = lambda: False,
        progress: Callable[[str], None] = lambda _message: None,
    ) -> list[DeletionActionResult]:
        attempt = self.db.deletion_attempt_for_group(profile_id, group_id)
        if not attempt:
            raise DeletionError("Deletion attempt not found.")
        attempt_id = int(attempt["deletion_attempt_id"])
        results: list[DeletionActionResult] = []
        for item in self.db.deletion_items(profile_id, attempt_id):
            state = str(item["state"])
            if state == "succeeded":
                continue
            if state == "running":
                action_id = int(item["action_id"] or 0)
                normalized = self.db.normalize_running_deletion_action(
                    profile_id,
                    action_id,
                    error_code="resume_discovered_running",
                )
                refreshed = next(
                    (
                        candidate
                        for candidate in self.db.deletion_items(profile_id, attempt_id)
                        if int(candidate["deletion_item_id"])
                        == int(item["deletion_item_id"])
                    ),
                    None,
                )
                if refreshed is None:
                    raise DeletionError(
                        "The running deletion item disappeared during recovery."
                    )
                item = refreshed
                state = str(normalized or item["state"])
            if state == "outcome_unknown":
                action_id = int(item["action_id"])
                result = self.verify_unknown(profile_id, action_id, cancelled)
                results.append(result)
                if result.state != "succeeded":
                    return results
                continue
            if state != "pending":
                return results
            if cancelled():
                self.db.cancel_deletion_tail(
                    profile_id,
                    attempt_id,
                    int(item["ordinal"]),
                )
                return results
            result = self._execute_one(profile_id, attempt, item, cancelled, progress)
            results.append(result)
            if result.state != "succeeded":
                return results
        return results

    def verify_unknown(
        self,
        profile_id: int,
        action_id: int,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> DeletionActionResult:
        action = self.db.deletion_action(profile_id, action_id)
        if not action or str(action["state"]) != "outcome_unknown":
            raise DeletionError("Only an unknown delete action can be verified.")
        profile = self.db.profile(profile_id)
        try:
            verdict = self.dispatch.verify_exact_absence(
                profile,
                RemoteSite(str(action["site"])),
                int(action["observation_id"]),
                str(action["remote_uuid"] or ""),
                (
                    profile.inat_user_id
                    if str(action["site"]) == "inat"
                    else profile.mo_user_id
                ),
                cancelled,
            )
        except Exception:
            return DeletionActionResult(
                action_id,
                "outcome_unknown",
                "Authenticated verification was interrupted or failed; "
                "the action remains unknown and no delete was resent.",
            )
        if verdict == "deleted":
            if not self.db.mark_deletion_verified(
                profile_id, action_id, "verified_deleted"
            ):
                raise DeletionError("The verified action changed concurrently.")
            self.db.settle_deletion_success(profile_id, action_id)
            return DeletionActionResult(
                action_id,
                "succeeded",
                "The exact reviewed donor is verified deleted; tombstone finalized.",
            )
        if verdict == "present":
            self.db.finish_deletion_action(
                profile_id,
                action_id,
                "retry_required",
                verification_state="verified_still_present",
                error_code="fresh_explicit_retry_required",
            )
            return DeletionActionResult(
                action_id,
                "retry_required",
                "The exact donor remains online. A fresh explicit review is required.",
            )
        return DeletionActionResult(
            action_id,
            "outcome_unknown",
            "Authenticated verification remains ambiguous; no request was resent.",
        )

    def _execute_one(
        self,
        profile_id: int,
        attempt: Mapping[str, Any],
        item: Mapping[str, Any],
        cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> DeletionActionResult:
        if not self.phase_2b_closed_provider():
            raise DeletionError(
                "Phase 2B is not formally closed; live deletion is blocked.",
                "phase_2b_not_closed",
            )
        site = RemoteSite(str(item["site"]))
        capability = self.capabilities[site]
        if not capability.execution_enabled:
            raise DeletionError(
                f"{site.value} deletion is disabled: {capability.disabled_reason}",
                "deletion_capability_unverified",
            )
        profile = self.db.profile(profile_id)
        progress(
            f"Revalidating {site.value} #{int(item['observation_id'])} "
            "before journaling one delete…"
        )
        preview = self.prepare_preview(
            profile_id,
            int(attempt["consolidation_id"]),
            cancelled,
            allowed_deletion_attempt_id=int(attempt["deletion_attempt_id"]),
        )
        fresh = next(
            (
                donor
                for donor in preview.donors
                if donor.stable_member_id == int(item["stable_member_id"])
            ),
            None,
        )
        if (
            fresh is None
            or not fresh.eligible
            or preview.base_finalized_attempt_id
            != int(attempt["base_finalized_attempt_id"])
            or preview.canonical_stable_identity_fingerprint
            != str(attempt["canonical_stable_identity_fingerprint"])
            or preview.canonical_mutable_snapshot_fingerprint
            != str(attempt["canonical_mutable_snapshot_fingerprint"])
            or fresh.remote_record_fingerprint
            != str(item["reviewed_remote_record_fingerprint"])
            or fresh.content_inventory_fingerprint
            != str(item["reviewed_content_inventory_fingerprint"])
            or fresh.parity_fingerprint != str(item["reviewed_parity_fingerprint"])
            or fresh.third_party_activity_fingerprint
            != str(item["reviewed_third_party_activity_fingerprint"])
            or fresh.dependency_fingerprint
            != str(item["reviewed_dependency_fingerprint"])
        ):
            raise DeletionError(
                "The reviewed deletion plan drifted; no delete was journaled.",
                "review_fingerprint_drift",
            )
        if self.auth_generation_provider() != int(
            attempt["reviewed_auth_generation"]
        ) or self.mo_key_generation_provider() != int(
            attempt["reviewed_mo_key_generation"]
        ):
            raise DeletionError(
                "Authentication changed after review.", "authentication_drift"
            )
        if not self._authenticated_owner(profile, site, cancelled):
            raise DeletionError(
                "The active credential no longer proves the reviewed owner.",
                "authentication_owner_mismatch",
            )
        identity_fp = canonical_stable_identity_fingerprint(
            site,
            fresh.observation_id,
            fresh.remote_uuid,
            profile.inat_user_id if site is RemoteSite.INAT else profile.mo_user_id,
        )
        request_correlation = str(uuidlib.uuid4())
        action_id = int(item["action_id"] or 0)
        if action_id > 0:
            existing = self.db.deletion_action(profile_id, action_id)
            if (
                existing is None
                or str(existing["site"]) != site.value
                or int(existing["observation_id"]) != fresh.observation_id
                or str(existing["remote_uuid"] or "") != fresh.remote_uuid
                or str(existing["reviewed_identity_fingerprint"]) != identity_fp
            ):
                raise DeletionError(
                    "The existing deletion action has a different identity.",
                    "existing_identity_conflict",
                )
            request_correlation = str(existing["request_correlation"])
        else:
            try:
                action_id = self.db.mint_deletion_action(
                    profile_id,
                    int(attempt["deletion_attempt_id"]),
                    int(item["deletion_item_id"]),
                    site=site.value,
                    observation_id=fresh.observation_id,
                    remote_uuid=fresh.remote_uuid,
                    reviewed_identity_fingerprint=identity_fp,
                    request_correlation=request_correlation,
                )
            except ValueError:
                # A concurrent worker may have committed the exact journal row
                # after this worker read the item. Resolve that row; never mint
                # or send a second deletion.
                refreshed = next(
                    (
                        candidate
                        for candidate in self.db.deletion_items(
                            profile_id, int(attempt["deletion_attempt_id"])
                        )
                        if int(candidate["deletion_item_id"])
                        == int(item["deletion_item_id"])
                    ),
                    None,
                )
                if refreshed is None or not refreshed.get("action_id"):
                    raise
                action_id = int(refreshed["action_id"])
                existing = self.db.deletion_action(profile_id, action_id)
                if (
                    existing is None
                    or str(existing["site"]) != site.value
                    or int(existing["observation_id"]) != fresh.observation_id
                    or str(existing["remote_uuid"] or "") != fresh.remote_uuid
                    or str(existing["reviewed_identity_fingerprint"]) != identity_fp
                ):
                    raise DeletionError(
                        "A concurrent deletion action has a different identity.",
                        "concurrent_identity_conflict",
                    )
                request_correlation = str(existing["request_correlation"])
        if cancelled():
            self.db.cancel_pending_deletion_action(profile_id, action_id)
            return DeletionActionResult(
                action_id, "cancelled", "Cancelled before the delete request."
            )
        if not self.db.claim_deletion_action(profile_id, action_id):
            row = self.db.deletion_action(profile_id, action_id) or {}
            return DeletionActionResult(
                action_id,
                str(row.get("state") or "failed"),
                "Another worker already claimed this exact delete action.",
            )
        try:
            record = self.dispatch.refresh_record(
                profile,
                site,
                fresh.observation_id,
                cancelled,
            )
        except Exception:
            if cancelled():
                self.db.finish_deletion_action(
                    profile_id,
                    action_id,
                    "cancelled",
                    error_code="cancelled_during_prewrite_refresh",
                )
                return DeletionActionResult(
                    action_id,
                    "cancelled",
                    "Cancelled while refreshing the donor before any write.",
                )
            self.db.normalize_running_deletion_action(
                profile_id,
                action_id,
                error_code="prewrite_refresh_failed",
            )
            return DeletionActionResult(
                action_id,
                "pending",
                "The pre-write refresh failed safely; no request was sent.",
            )
        if (
            not record.exists
            or record.remote_uuid != fresh.remote_uuid
            or record.owner_id
            != (profile.inat_user_id if site is RemoteSite.INAT else profile.mo_user_id)
            or record.record_fingerprint != fresh.remote_record_fingerprint
        ):
            self.db.finish_deletion_action(
                profile_id,
                action_id,
                "failed",
                error_code="last_moment_identity_drift",
            )
            return DeletionActionResult(
                action_id, "failed", "The donor changed before the request."
            )
        if cancelled():
            self.db.finish_deletion_action(
                profile_id,
                action_id,
                "cancelled",
                error_code="cancelled_before_write",
            )
            return DeletionActionResult(
                action_id, "cancelled", "Cancelled before the delete request."
            )
        if not self.db.mark_deletion_write_started(profile_id, action_id):
            raise DeletionError("The delete action could not enter its write boundary.")
        try:
            self.dispatch.delete_exact(
                profile,
                record,
                request_correlation,
                cancelled,
            )
        except Exception as exc:
            self.db.finish_deletion_action(
                profile_id,
                action_id,
                "outcome_unknown",
                error_code=(
                    "cancelled_after_write_started"
                    if cancelled()
                    else "delete_response_ambiguous"
                ),
                http_status=getattr(exc, "status_code", None),
            )
            return DeletionActionResult(
                action_id,
                "outcome_unknown",
                "An exception occurred after the durable write boundary; "
                "the request will not be resent blindly.",
            )
        try:
            verdict = self.dispatch.verify_exact_absence(
                profile,
                site,
                record.observation_id,
                record.remote_uuid,
                record.owner_id,
                cancelled,
            )
        except Exception:
            self.db.finish_deletion_action(
                profile_id,
                action_id,
                "outcome_unknown",
                error_code=(
                    "cancelled_during_post_delete_verification"
                    if cancelled()
                    else "post_delete_verifier_exception"
                ),
            )
            return DeletionActionResult(
                action_id,
                "outcome_unknown",
                "Post-delete verification failed after the write boundary; "
                "the action remains unknown.",
            )
        if verdict != "deleted":
            target = "retry_required" if verdict == "present" else "outcome_unknown"
            self.db.finish_deletion_action(
                profile_id,
                action_id,
                target,
                verification_state=(
                    "verified_still_present" if verdict == "present" else ""
                ),
                error_code=(
                    "fresh_explicit_retry_required"
                    if verdict == "present"
                    else "post_delete_ambiguous"
                ),
            )
            return DeletionActionResult(
                action_id,
                target,
                "Post-delete verification did not prove exact deletion; tail stopped.",
            )
        try:
            if not self.db.mark_deletion_verified(
                profile_id, action_id, "verified_deleted"
            ):
                raise DeletionError("The verified action changed before finalization.")
            self.db.settle_deletion_success(profile_id, action_id)
        except Exception:
            self.db.normalize_running_deletion_action(
                profile_id,
                action_id,
                error_code="local_finalize_failed",
            )
            raise
        return DeletionActionResult(
            action_id,
            "succeeded",
            "The exact donor was verified deleted and locally tombstoned.",
        )

    def _stable_donor_reasons(
        self,
        profile: ReconciliationProfile,
        consolidation: Mapping[str, Any],
        member: Mapping[str, Any],
        donor: Optional[RemoteDeletionRecord],
        unresolved: Sequence[str],
    ) -> list[str]:
        reasons: list[str] = list(unresolved)
        site = RemoteSite(str(member["site"]))
        observation_id = int(member["observation_id"])
        if str(member["role"]) != "donor":
            reasons.append("Blocked: donor became canonical")
        if str(member["local_state"]) != "superseded":
            reasons.append("Blocked: donor is not locally superseded")
        if not member.get("added_by_attempt_id"):
            reasons.append("Blocked: Phase 2B admitting attempt unavailable")
        canonical_id = (
            consolidation.get("canonical_inat_observation_id")
            if site is RemoteSite.INAT
            else consolidation.get("canonical_mo_observation_id")
        )
        if canonical_id is not None and int(canonical_id) == observation_id:
            reasons.append("Blocked: donor became canonical")
        if donor is None or not donor.exists:
            reasons.append("Blocked: donor unavailable")
            return reasons
        expected_owner = (
            profile.inat_user_id if site is RemoteSite.INAT else profile.mo_user_id
        )
        if donor.owner_id != expected_owner:
            reasons.append("Blocked: donor no longer owned")
        if donor.remote_uuid != str(member.get("remote_uuid") or ""):
            reasons.append("Blocked: stable remote identity changed")
        return reasons

    def _authenticated_owner(
        self,
        profile: ReconciliationProfile,
        site: RemoteSite,
        cancelled: Callable[[], bool],
    ) -> bool:
        if site is RemoteSite.INAT:
            auth = self.auth_provider()
            if (
                not auth.is_authenticated
                or auth.login.casefold() != profile.inat_login.casefold()
            ):
                return False
            current = _first_result(
                self.inat_client.get_current_user_v2(auth.api_token)
            )
            return (
                _positive_int(current.get("id")) == profile.inat_user_id
                and str(current.get("login") or "").casefold()
                == profile.inat_login.casefold()
            )
        api_key = self.mo_key_provider(profile.profile_id)
        if not api_key:
            return False
        return (
            self.mo_client.authenticated_user_id(
                api_key,
                profile.mo_user_id,
                cancelled,
            )
            == profile.mo_user_id
        )

    def _unresolved_specimen_activity(
        self,
        profile_id: int,
        consolidation_id: int,
        *,
        allowed_deletion_attempt_id: Optional[int] = None,
    ) -> tuple[str, ...]:
        conn = self.db.connection()
        reasons: list[str] = []
        row = conn.execute(
            "SELECT 1 FROM sync_consolidation_attempts WHERE profile_id=? "
            "AND consolidation_id=? AND state IN ('pending','outcome_unknown') "
            "LIMIT 1",
            (profile_id, consolidation_id),
        ).fetchone()
        if row:
            reasons.append("Blocked: unresolved consolidation action")
        deletion_attempt = conn.execute(
            "SELECT deletion_attempt_id,state FROM sync_deletion_attempts "
            "WHERE profile_id=? "
            "AND consolidation_id=? "
            "AND state IN ('pending','partial','outcome_unknown') "
            "AND (? IS NULL OR deletion_attempt_id!=?) LIMIT 1",
            (
                profile_id,
                consolidation_id,
                allowed_deletion_attempt_id,
                allowed_deletion_attempt_id,
            ),
        ).fetchone()
        settled_retryable_partial = False
        if deletion_attempt and str(deletion_attempt["state"]) == "partial":
            active_item = conn.execute(
                "SELECT 1 FROM sync_deletion_items "
                "WHERE deletion_attempt_id=? "
                "AND state IN ('pending','running','outcome_unknown') LIMIT 1",
                (int(deletion_attempt["deletion_attempt_id"]),),
            ).fetchone()
            settled_retryable_partial = active_item is None
        if deletion_attempt and not settled_retryable_partial:
            reasons.append("Blocked: unresolved deletion attempt")
        member_keys = conn.execute(
            "SELECT site,observation_id FROM sync_consolidation_members "
            "WHERE profile_id=? AND consolidation_id=?",
            (profile_id, consolidation_id),
        ).fetchall()
        for member in member_keys:
            site = str(member["site"])
            observation_id = int(member["observation_id"])
            deletion_action = conn.execute(
                "SELECT 1 FROM sync_deletion_actions WHERE profile_id=? "
                "AND site=? AND observation_id=? "
                "AND state IN ('pending','running','outcome_unknown') "
                "AND (? IS NULL OR deletion_attempt_id!=?) LIMIT 1",
                (
                    profile_id,
                    site,
                    observation_id,
                    allowed_deletion_attempt_id,
                    allowed_deletion_attempt_id,
                ),
            ).fetchone()
            if deletion_action:
                reasons.append("Blocked: unresolved remote action")
                break
            action = conn.execute(
                "SELECT 1 FROM sync_actions WHERE profile_id=? "
                "AND state IN ('pending','running','outcome_unknown') AND ("
                "(?='mo' AND (mo_observation_id=? OR source_site='mo' "
                "AND source_record_id=?)) OR "
                "(?='inat' AND (inat_observation_id=? OR source_site='inat' "
                "AND source_record_id=?))) LIMIT 1",
                (
                    profile_id,
                    site,
                    observation_id,
                    observation_id,
                    site,
                    observation_id,
                    observation_id,
                ),
            ).fetchone()
            if action:
                reasons.append("Blocked: unresolved remote action")
                break
        return tuple(dict.fromkeys(reasons))

    def _local_dependencies(
        self,
        profile_id: int,
        consolidation_id: int,
        site: RemoteSite,
        observation_id: int,
    ) -> tuple[ExternalDependency, ...]:
        conn = self.db.connection()
        dependencies: list[ExternalDependency] = []
        pairs = conn.execute(
            "SELECT pair_id,mo_observation_id,inat_observation_id,review_state "
            "FROM sync_pairs WHERE profile_id=? AND ("
            "(?='mo' AND mo_observation_id=?) OR "
            "(?='inat' AND inat_observation_id=?))",
            (
                profile_id,
                site.value,
                observation_id,
                site.value,
                observation_id,
            ),
        ).fetchall()
        for pair in pairs:
            dependencies.append(
                ExternalDependency(
                    dependency_type="local_pair",
                    source_identity=f"sync_pair:{int(pair['pair_id'])}",
                    target_identity=(
                        f"mo:{int(pair['mo_observation_id'])}|"
                        f"inat:{int(pair['inat_observation_id'])}"
                    ),
                    safe_summary=(
                        f"Local pair #{int(pair['pair_id'])} still references "
                        "this donor"
                    ),
                    proven_points_to_canonical=False,
                )
            )
        other = conn.execute(
            "SELECT consolidation_id FROM sync_consolidation_members "
            "WHERE profile_id=? AND site=? AND observation_id=? "
            "AND consolidation_id!=?",
            (profile_id, site.value, observation_id, consolidation_id),
        ).fetchall()
        for row in other:
            dependencies.append(
                ExternalDependency(
                    "other_consolidation",
                    f"consolidation:{int(row['consolidation_id'])}",
                    f"{site.value}:{observation_id}",
                    "Another consolidation identity references this donor",
                    False,
                )
            )
        return tuple(dependencies)


def _first_result(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    results = payload.get("results")
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0]
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    observations = payload.get("observations")
    if (
        isinstance(observations, list)
        and observations
        and isinstance(observations[0], dict)
    ):
        return observations[0]
    return payload if payload.get("id") else {}


def _record_from_raw(
    site: RemoteSite,
    observation_id: int,
    raw: Mapping[str, Any],
    profile: ReconciliationProfile,
) -> RemoteDeletionRecord:
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
    owner_id = _positive_int(
        user.get("id") or owner.get("id") or raw.get("owner_id") or raw.get("user_id")
    )
    owner_login = str(
        user.get("login")
        or owner.get("login")
        or owner.get("name")
        or raw.get("owner_login")
        or ""
    )
    contents: list[DeletionContent] = []
    inventory_issues: list[str] = []

    def add(content_type: str, identity: str, value: object, summary: str) -> None:
        normalized = normalized_text(value)
        contents.append(
            DeletionContent(
                content_type,
                identity,
                normalized,
                public_fingerprint(
                    "phase_2c_content_v1", content_type, identity, normalized
                ),
                summary,
            )
        )

    def collection(*keys: str) -> tuple[object, bool]:
        for key in keys:
            if key in raw:
                return raw.get(key), True
        return [], False

    # A read that failed leaves an empty collection behind. Without these
    # markers an unreachable endpoint is indistinguishable from a genuinely
    # empty donor, so record them as inventory gaps: they block eligibility
    # and enter the record fingerprint below.
    for marker, issue in (
        ("_phase2c_activity_read_failed", "owner and third-party activity read failed"),
        ("_phase2c_images_read_failed", "photo read failed"),
        ("_phase2c_sequences_read_failed", "sequence read failed"),
        ("_phase2c_links_read_failed", "external link read failed"),
    ):
        if raw.get(marker):
            inventory_issues.append(issue)

    add(
        "date",
        "observed_on",
        raw.get("observed_on") or raw.get("date") or raw.get("when"),
        "Observation date",
    )
    add(
        "locality",
        "locality",
        raw.get("place_guess") or raw.get("location_name") or raw.get("where"),
        "Locality",
    )
    add("description", "description", raw.get("description"), "Description")
    add("notes", "notes", raw.get("notes"), "Notes")
    add(
        "positional_accuracy",
        "positional_accuracy",
        raw.get("positional_accuracy"),
        "Positional accuracy",
    )
    add(
        "geoprivacy",
        "geoprivacy",
        raw.get("geoprivacy") or raw.get("gps_hidden"),
        "Geoprivacy",
    )
    taxon = raw.get("taxon") if isinstance(raw.get("taxon"), dict) else {}
    consensus = raw.get("consensus") if isinstance(raw.get("consensus"), dict) else {}
    add(
        "taxon",
        "taxon",
        f"{taxon.get('id') or consensus.get('id') or raw.get('name_id') or ''}|"
        f"{taxon.get('name') or consensus.get('name') or raw.get('name') or ''}",
        "Taxon and identification state",
    )
    coordinates = raw.get("private_geojson") or raw.get("geojson")
    add(
        "coordinates",
        "coordinates",
        json.dumps(coordinates, sort_keys=True) if coordinates else "",
        "Coordinates",
    )
    add(
        "license",
        "observation_license",
        raw.get("license_code") or raw.get("license"),
        "Observation license",
    )
    for name, kind in (
        ("voucher_number", "voucher"),
        ("herbarium", "voucher"),
        ("accession_number", "accession"),
        ("collection_number", "collection_number"),
    ):
        if name in raw:
            add(kind, name, raw.get(name), kind.replace("_", " ").title())
    ofvs, ofvs_present = collection("ofvs", "observation_field_values")
    if isinstance(ofvs, list):
        for ordinal, row in enumerate(ofvs, 1):
            if not isinstance(row, dict):
                inventory_issues.append(f"malformed observation field row {ordinal}")
                continue
            field_value = row.get("value")
            field_id = row.get("field_id") or (
                row.get("observation_field", {}).get("id")
                if isinstance(row.get("observation_field"), dict)
                else ""
            )
            if not field_id:
                inventory_issues.append(
                    f"observation field row {ordinal} lacks stable identity"
                )
                continue
            add(
                "observation_field",
                f"field:{field_id}",
                field_value,
                f"Observation field {field_id}",
            )
    elif ofvs_present:
        inventory_issues.append("observation fields are not a collection")
    photo_rows, photos_present = collection(
        "observation_photos",
        "_phase2c_images",
        "images",
    )
    if isinstance(photo_rows, list):
        for ordinal, row in enumerate(photo_rows, 1):
            if not isinstance(row, dict):
                inventory_issues.append(f"malformed photo row {ordinal}")
                continue
            photo = row.get("photo") if isinstance(row.get("photo"), dict) else row
            photo_id = str(photo.get("id") or "")
            if not photo_id:
                inventory_issues.append(f"photo row {ordinal} lacks stable identity")
                continue
            license_label = str(photo.get("license_code") or photo.get("license") or "")
            attribution = str(photo.get("attribution") or "")
            holder = str(photo.get("copyright_holder") or "")
            contents.append(
                DeletionContent(
                    "photo",
                    f"{site.value}:photo:{photo_id}",
                    "",
                    public_fingerprint(
                        "phase_2c_photo_v1",
                        site.value,
                        photo_id,
                        license_label,
                        holder,
                        attribution,
                    ),
                    f"Photo {photo_id}",
                    f"{site.value}:photo:{photo_id}",
                    "",
                    license_label,
                    holder,
                    attribution,
                )
            )
    elif photos_present:
        inventory_issues.append("photos are not a collection")
    sequence_rows, sequences_present = collection(
        "_phase2c_sequences",
        "sequences",
    )
    if isinstance(sequence_rows, list):
        for ordinal, row in enumerate(sequence_rows, 1):
            if not isinstance(row, dict):
                inventory_issues.append(f"malformed sequence row {ordinal}")
                continue
            sequence_id = str(row.get("id") or "")
            accession = normalized_text(row.get("accession"))
            bases = re.sub(r"[^A-Za-z]", "", str(row.get("bases") or "")).upper()
            if not sequence_id and not accession and not bases:
                inventory_issues.append(
                    f"sequence row {ordinal} lacks enumerable content"
                )
                continue
            contents.append(
                DeletionContent(
                    "sequence",
                    f"sequence:{sequence_id}:{accession}",
                    accession,
                    public_fingerprint("phase_2c_sequence_v1", accession, bases),
                    f"Sequence {sequence_id or accession or 'unknown'}",
                )
            )
    elif sequences_present:
        inventory_issues.append("sequences are not a collection")
    link_rows, links_present = collection("_phase2c_links", "external_links")
    if isinstance(link_rows, list):
        for ordinal, row in enumerate(link_rows, 1):
            if not isinstance(row, dict):
                inventory_issues.append(f"malformed external link row {ordinal}")
                continue
            if not row.get("url"):
                inventory_issues.append(f"external link row {ordinal} lacks URL")
                continue
            add(
                "external_url",
                f"external_link:{row.get('id') or ordinal}",
                row.get("url"),
                "External URL",
            )
    elif links_present:
        inventory_issues.append("external links are not a collection")
    owner_activity, third_party, activity_issues = _activity_inventory_from_raw(
        raw, owner_id
    )
    contents.extend(owner_activity)
    inventory_issues.extend(activity_issues)
    record_fp = public_fingerprint(
        "phase_2c_remote_record_v1",
        site.value,
        observation_id,
        raw.get("uuid") or "",
        owner_id,
        raw.get("updated_at") or "",
        _content_inventory_fingerprint(contents),
        *(item.fingerprint for item in third_party),
        *inventory_issues,
    )
    # The currently available serializers omit categories such as annotations,
    # project curation, subscriptions, and reverse links. Keep all three
    # completeness flags false in the production parser.
    return RemoteDeletionRecord(
        site=site,
        observation_id=observation_id,
        remote_uuid=str(raw.get("uuid") or ""),
        owner_id=owner_id,
        owner_login=owner_login,
        updated_at=str(raw.get("updated_at") or ""),
        record_fingerprint=record_fp,
        contents=tuple(contents),
        third_party=third_party,
        dependencies=(),
        inventory_issues=tuple(dict.fromkeys(inventory_issues)),
        content_enumeration_complete=False,
        third_party_enumeration_complete=False,
        dependency_search_complete=False,
        reciprocal_targets=_reciprocal_targets(site, raw),
    )


def _activity_inventory_from_raw(
    raw: Mapping[str, Any],
    owner_id: int,
) -> tuple[
    tuple[DeletionContent, ...],
    tuple[ThirdPartyContribution, ...],
    tuple[str, ...],
]:
    owner_contents: list[DeletionContent] = []
    third_party: list[ThirdPartyContribution] = []
    issues: list[str] = []
    for key, label in (
        ("identifications", "identification"),
        ("comments", "comment"),
        ("faves", "favorite"),
        ("quality_metrics", "vote"),
        ("annotations", "annotation"),
        ("votes", "vote"),
        ("namings", "identification"),
        ("project_observations", "project curation"),
    ):
        rows = raw.get(key) if key in raw else []
        if not isinstance(rows, list):
            if key in raw:
                issues.append(f"{key} are not a collection")
            continue
        for ordinal, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                issues.append(f"malformed {label} row {ordinal}")
                continue
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            contributor_id = _positive_int(
                user.get("id") or row.get("user_id") or row.get("owner_id")
            )
            remote_identity = str(row.get("uuid") or row.get("id") or "")
            if not remote_identity:
                issues.append(f"{label} row {ordinal} lacks stable identity")
            if contributor_id <= 0:
                third_party.append(
                    ThirdPartyContribution(
                        contribution_type=f"unknown_author_{label}",
                        remote_identity=remote_identity or f"row:{ordinal}",
                        contributor_id=0,
                        safe_summary=f"{label.title()} with unknown authorship",
                        fingerprint=public_fingerprint(
                            "phase_2c_unknown_author_activity_v1",
                            key,
                            remote_identity,
                            ordinal,
                        ),
                    )
                )
                continue
            if contributor_id == owner_id:
                value = _owner_activity_value(label, row)
                identity = f"{label}:{remote_identity or ordinal}"
                owner_contents.append(
                    DeletionContent(
                        content_type=f"owner_{label.replace(' ', '_')}",
                        identity=identity,
                        value=value,
                        value_fingerprint=public_fingerprint(
                            "phase_2c_owner_activity_v1",
                            label,
                            value,
                        ),
                        safe_summary=f"Owner-authored {label}",
                    )
                )
                continue
            third_party.append(
                ThirdPartyContribution(
                    contribution_type=label,
                    remote_identity=remote_identity,
                    contributor_id=contributor_id,
                    safe_summary=f"Third-party {label} by account {contributor_id}",
                    fingerprint=public_fingerprint(
                        "phase_2c_third_party_item_v1",
                        key,
                        remote_identity,
                        contributor_id,
                    ),
                )
            )
    return tuple(owner_contents), tuple(third_party), tuple(issues)


def _owner_activity_value(label: str, row: Mapping[str, Any]) -> str:
    """Return exact normalized owner-authored activity without unrelated data."""
    if label == "comment":
        return normalized_text(row.get("body") or row.get("comment"))
    if label == "identification":
        taxon = row.get("taxon") if isinstance(row.get("taxon"), dict) else {}
        return normalized_text(
            json.dumps(
                {
                    "taxon_id": taxon.get("id")
                    or row.get("taxon_id")
                    or row.get("name_id"),
                    "taxon_name": taxon.get("name") or row.get("name"),
                    "body": row.get("body"),
                    "current": row.get("current"),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    safe = {
        key: value
        for key, value in row.items()
        if key not in {"user", "owner"} and not key.startswith("_")
    }
    return normalized_text(
        json.dumps(
            safe,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def _content_inventory_fingerprint(
    contents: Sequence[DeletionContent],
) -> str:
    return public_fingerprint(
        "phase_2c_inventory_v1",
        *(
            public_fingerprint(
                item.content_type,
                item.identity,
                item.value_fingerprint,
                item.source_media_identity,
                item.original_byte_fingerprint,
                item.license_label,
                item.copyright_holder,
                item.attribution,
            )
            for item in contents
        ),
    )


def _parity_fingerprint(items: Sequence[DeletionParityItem]) -> str:
    return public_fingerprint(
        "phase_2c_parity_v1",
        *(
            public_fingerprint(
                item.content_type,
                item.source_identity,
                item.canonical_identity,
                item.match_method,
                item.source_fingerprint,
                item.canonical_fingerprint,
                item.preserved,
                item.blocking_reason,
            )
            for item in items
        ),
    )


def _positive_int(value: object) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _reciprocal_targets(
    site: RemoteSite,
    raw: Mapping[str, Any],
) -> tuple[tuple[RemoteSite, int], ...]:
    targets: list[tuple[RemoteSite, int]] = []
    if site is RemoteSite.INAT:
        rows = raw.get("ofvs") or raw.get("observation_field_values") or []
        key, target_site, parse = "value", RemoteSite.MO, parse_mo_observation_url
    else:
        rows = raw.get("_phase2c_links") or raw.get("external_links") or []
        key, target_site, parse = "url", RemoteSite.INAT, parse_inat_observation_url
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            # The same strict, whole-value URL parsers the rest of
            # reconciliation uses to establish a link establish it here.
            target = parse(row.get(key))
            if target:
                targets.append((target_site, target))
    return tuple(dict.fromkeys(targets))
