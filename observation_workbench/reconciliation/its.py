"""Gate 1C ITS comparison and individually reviewed synchronization.

Raw sequence bodies live only in memory-only fields of ``MOSequenceRecord`` and
``ITSRecordSnapshot`` returned by fresh remote reads. They are never passed to
SQLite, logging, exceptions, settings, or durable action metadata.

Each Mushroom Observer ``Sequence`` row is modelled as one composite record so
that bases, archive, and accession — which belong to the same MO record — are
never split apart when proposing or submitting a repair.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable, Optional, Sequence

from observation_workbench.api.auth import AuthState
from observation_workbench.api.client import INatAPIError, INatClient

from .db import ReconciliationDB
from .inat_reader import (
    ACCESSION_FIELD_NAME, INatReconciliationReader, ITS_FIELD_NAME,
)
from .mo_client import MOAPIError, MOClient, ReconciliationCancelled, results_from_payload
from .mo_parsing import mo_record_fingerprint, parse_mo_observation, positive_int
from .normalization import (
    MO_GENBANK_ARCHIVE, accession_namespace, is_genbank_accession,
    is_mo_writable_archive, normalize_accession, normalize_archive,
    normalize_sequence, public_fingerprint, sequence_digest,
)
from .specimen_state import evaluate_specimen_state
from .types import (
    HydratedObservation, InventoryObservation, ITSActionOption, ITSActionType,
    ITSComparisonPreview, ITSRecordSnapshot, MOSequenceRecord, ReconciliationProfile,
    RemoteSite,
)


class ITSSyncError(RuntimeError):
    def __init__(self, message: str, code: str = "its_sync_unavailable") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ITSActionResult:
    action_id: int
    state: str
    message: str


@dataclass(frozen=True)
class _WriteSource:
    """Memory-only source value resolved immediately before a write."""

    raw_sequence: str = field(default="", repr=False, compare=False)
    normalized_sequence: str = field(default="", repr=False, compare=False)
    sequence_fingerprint: str = ""
    normalized_accession: str = ""
    archive: str = ""


@dataclass(frozen=True)
class _LiveITSState:
    profile_id: int
    pair_id: int
    mo_observation_id: int
    inat_observation_id: int
    inat_observation_uuid: str
    inat_field_id: int
    inat_accession_field_id: Optional[int]
    inat_record_fingerprint: str
    mo_record_fingerprint: str
    inat_values_fingerprint: str
    mo_values_fingerprint: str
    specimen_state_fingerprint: str
    inat_token_marker: str
    mo_key_marker: str
    auth_generation: int
    mo_key_generation: int
    specimen_conflict: str
    specimen_warnings: tuple[str, ...]
    inat_records: tuple[ITSRecordSnapshot, ...]
    mo_records: tuple[ITSRecordSnapshot, ...]
    mo_sequences: tuple[MOSequenceRecord, ...]
    # MO sequence rows attached to this observation whose locus or row id could
    # not be read. Their real content is unknown, so the MO side of the
    # comparison is not provable and no ITS write may be proposed.
    mo_unreadable_rows: int = 0


class ITSSyncService:
    """Fresh-read comparison and write service for confirmed pairs only."""

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

    def prepare_preview(
        self, profile_id: int, pair_id: int,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> ITSComparisonPreview:
        pair = self._eligible_pair(profile_id, pair_id)
        profile = self.db.profile(profile_id)
        live = self._refresh(profile, pair, cancelled, require_mo_key=False)
        # A fresh comparison rebuilds the persistent safe derivatives (sequence
        # hashes, archive-qualified accessions, pair score/classification) and only
        # then resolves the ITS refresh-required issue. If persistence fails the
        # issue stays open even though this memory-only dialog can still be shown.
        if self._persist_evidence(profile_id, live):
            # refresh_its_evidence bumped the pair's updated_at, which participates
            # in the source fingerprint; re-read so the journaled fingerprint
            # matches what execution will observe.
            pair = self.db.pair_detail(profile_id, pair_id) or pair
        states = _comparison_states(live)
        warnings: list[str] = list(live.specimen_warnings)
        if live.specimen_conflict:
            warnings.append(
                "ITS writes are blocked while specimen evidence conflicts: " + live.specimen_conflict
            )
        options = self._options(profile, live, warnings)
        if not options and not live.specimen_conflict:
            warnings.append("No safe ITS write is proposed for the current remote state.")
        return ITSComparisonPreview(
            profile_id=profile_id, pair_id=pair_id,
            auth_generation=live.auth_generation,
            mo_key_generation=live.mo_key_generation,
            source_fingerprint=_pair_fingerprint(pair),
            mo_observation_id=live.mo_observation_id,
            inat_observation_id=live.inat_observation_id,
            inat_observation_uuid=live.inat_observation_uuid,
            inat_field_id=live.inat_field_id,
            inat_accession_field_id=live.inat_accession_field_id,
            inat_record_fingerprint=live.inat_record_fingerprint,
            mo_record_fingerprint=live.mo_record_fingerprint,
            specimen_state_fingerprint=live.specimen_state_fingerprint,
            states=states, inat_records=live.inat_records, mo_records=live.mo_records,
            options=tuple(options), warnings=tuple(warnings),
        )

    def execute_group(
        self, profile_id: int, group_id: int, cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> list[ITSActionResult]:
        rows = self.db.action_group_rows(profile_id, group_id)
        if len(rows) != 1 or not _is_its_action(rows[0].get("action_type") if rows else None):
            raise ITSSyncError(
                "An ITS journal group must contain exactly one individually reviewed action.",
                "invalid_its_group",
            )
        row = rows[0]
        state = str(row["state"])
        if state == "outcome_unknown":
            return [self.verify_unknown(profile_id, int(row["action_id"]), cancelled)]
        if state == "succeeded":
            return []
        if state != "pending":
            return [ITSActionResult(
                int(row["action_id"]), state,
                "This ITS action is terminal; create a fresh comparison for another write.",
            )]
        return [self._execute(row, cancelled, progress)]

    def verify_unknown(
        self, profile_id: int, action_id: int, cancelled: Callable[[], bool],
    ) -> ITSActionResult:
        row = self.db.action(profile_id, action_id)
        if not row or not _is_its_action(row.get("action_type")):
            raise ITSSyncError("The selected journal row is not an ITS action.", "invalid_its_action")
        if str(row["state"]) != "outcome_unknown":
            return ITSActionResult(action_id, str(row["state"]), "No unknown outcome remains to verify.")
        profile = self.db.profile(profile_id)
        # Verifying an already-submitted write uses the immutable journaled
        # observation identities, not current pair eligibility. A pair that was
        # since reopened, excluded, or marked conflicting must still be verifiable;
        # eligibility gates new writes, not mandatory outcome recovery.
        pair = self._pair_from_journal(profile_id, row)
        try:
            live = self._refresh(profile, pair, cancelled, require_mo_key=False, verification_only=True)
        except Exception:
            return ITSActionResult(
                action_id, "outcome_unknown",
                "Destination reread is still unavailable; the action was not retried.",
            )
        outcome = self._classify_unknown(row, live)
        if outcome == "succeeded":
            # _finish_success rebuilds evidence and resolves the stale requirement.
            self._finish_success(row, live, verification_state="verified_after_unknown")
            return ITSActionResult(action_id, "succeeded", "Verified the prior submission without retrying it.")
        # A possibly written group whose result is not a verified success keeps a
        # visible ITS refresh requirement.
        self.db.mark_its_reconciliation_stale(profile_id, int(row["action_group_id"]))
        if outcome == "failed":
            self.db.finish_action(
                profile_id, action_id, "failed", phase="verification",
                error_code="verified_not_applied", verification_state="verified_not_applied",
            )
            return ITSActionResult(
                action_id, "failed",
                "The destination was reread; it is exactly unchanged and the write was not applied.",
            )
        # Destination changed but the requested result cannot be proven.
        self.db.finish_action(
            profile_id, action_id, "outcome_unknown", phase="verification",
            error_code="changed_not_proven", verification_state="changed_not_proven",
        )
        return ITSActionResult(
            action_id, "outcome_unknown",
            "The destination changed but does not prove this write; run a fresh ITS comparison.",
        )

    def _pair_from_journal(self, profile_id: int, row: dict[str, Any]) -> dict[str, Any]:
        """Build a pair view from immutable journaled IDs, not current eligibility."""
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

    def _classify_unknown(self, row: dict[str, Any], live: _LiveITSState) -> str:
        """Return one of ``succeeded``/``failed``/``unknown`` per the pre-write snapshot."""
        if self._is_satisfied(row, live):
            return "succeeded"
        pre_write = str(row["destination_preflight_fingerprint"])
        if self._destination_fingerprint(row, live) == pre_write:
            return "failed"
        return "unknown"

    def _execute(
        self, row: dict[str, Any], cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> ITSActionResult:
        profile_id = int(row["profile_id"])
        action_id = int(row["action_id"])
        if not self.db.claim_action(profile_id, action_id, "resource_preflight"):
            current = self.db.action(profile_id, action_id) or row
            return ITSActionResult(action_id, str(current["state"]), "The ITS action is no longer pending.")
        write_started = False
        resolved_success = False
        try:
            if cancelled():
                raise ReconciliationCancelled("ITS action cancelled")
            progress(f"ITS action {action_id}: rereading source and destination")
            pair = self._eligible_pair(profile_id, int(row["pair_id"]))
            self._require_current_source(row, pair)
            profile = self.db.profile(profile_id)
            live = self._refresh(
                profile, pair, cancelled,
                require_mo_key=str(row["site"]) == RemoteSite.MO.value,
            )
            if self._is_satisfied(row, live):
                self._finish_success(row, live, verification_state="already_correct")
                return ITSActionResult(action_id, "succeeded", "Destination was already correct; no write was sent.")
            if live.specimen_conflict:
                raise ITSSyncError(
                    "Fresh specimen evidence conflicts; no ITS write was sent: " + live.specimen_conflict,
                    "specimen_conflict",
                )
            if live.mo_unreadable_rows:
                raise ITSSyncError(
                    "Mushroom Observer returned a sequence row for this observation whose locus or row "
                    "id could not be read, so its ITS state cannot be established. No write was sent.",
                    "mo_sequence_unreadable",
                )
            self._require_unchanged_context(row, live)
            source = self._current_source(row, live)
            if source is None:
                raise ITSSyncError("The exact source ITS evidence changed after preview.", "source_changed")
            if self._destination_fingerprint(row, live) != str(row["destination_preflight_fingerprint"]):
                raise ITSSyncError(
                    "The destination ITS state changed after preview. No write was sent.",
                    "stale_destination",
                )

            def begin_write() -> None:
                """Stamp the durable write boundary immediately before the request.

                ``_write`` performs several purely local preconditions (credential
                recheck, destination binding, the GenBank-field rule, MO row
                ownership, deposit writability, retained-component compatibility)
                that raise before any network call. Stamping the boundary earlier
                would settle those deterministic refusals as 'outcome_unknown'
                ("a write may have been submitted") even though nothing was sent.
                """
                nonlocal write_started
                if not self.db.mark_action_write_started(profile_id, action_id):
                    raise ITSSyncError(
                        "This action left its claimed state before the write boundary. "
                        "No write was sent.",
                        "write_boundary_lost",
                    )
                write_started = True

            progress(f"ITS action {action_id}: submitting one explicitly confirmed write")
            write_error: Optional[Exception] = None
            http_status: Optional[int] = None
            server_id = ""
            server_uuid = ""
            try:
                response = self._write(row, profile, live, source, cancelled, begin_write)
                metadata = getattr(response, "metadata", None)
                http_status = getattr(metadata, "status_code", None)
                server_id, server_uuid = _response_identity(response)
            except (INatAPIError, MOAPIError) as exc:
                write_error = exc
                http_status = getattr(exc, "status_code", None)

            progress(f"ITS action {action_id}: verifying destination state")
            try:
                verified = self._refresh(
                    profile, pair, lambda: False, require_mo_key=False,
                    verification_only=True,
                )
            except Exception:
                self.db.finish_action(
                    profile_id, action_id, "outcome_unknown", phase="verification",
                    error_code="verification_unavailable", http_status=http_status,
                    verification_state="unavailable", server_row_id=server_id,
                    server_row_uuid=server_uuid,
                )
                return ITSActionResult(
                    action_id, "outcome_unknown",
                    "The write may have been submitted, but destination verification is unavailable.",
                )
            if self._is_satisfied(row, verified):
                self._finish_success(
                    row, verified, verification_state="verified_final_state",
                    http_status=http_status, server_id=server_id, server_uuid=server_uuid,
                )
                # _finish_success rebuilt evidence and resolved (or re-raised) the
                # stale requirement; the finally block must not re-open it.
                resolved_success = True
                return ITSActionResult(action_id, "succeeded", "Verified the normalized ITS final state.")
            unchanged = self._destination_fingerprint(row, verified) == str(row["destination_preflight_fingerprint"])
            if unchanged:
                self.db.finish_action(
                    profile_id, action_id, "failed", phase="verification",
                    error_code="verified_not_applied", http_status=http_status,
                    verification_state="verified_not_applied",
                )
                return ITSActionResult(action_id, "failed", "Verification shows that the write was not applied.")
            if write_error is not None and bool(getattr(write_error, "outcome_unknown", False)):
                self.db.finish_action(
                    profile_id, action_id, "outcome_unknown", phase="verification",
                    error_code="write_outcome_unknown", http_status=http_status,
                    verification_state="changed_not_proven",
                )
                return ITSActionResult(action_id, "outcome_unknown", "Write outcome remains unknown.")
            # The destination changed but does not prove this action; do not mark
            # it failed merely because the desired result is currently absent.
            self.db.finish_action(
                profile_id, action_id, "outcome_unknown", phase="verification",
                error_code="changed_not_proven", http_status=http_status,
                verification_state="changed_not_proven",
            )
            return ITSActionResult(
                action_id, "outcome_unknown",
                "The destination changed but does not prove the requested ITS state; run a fresh comparison.",
            )
        except ReconciliationCancelled:
            self.db.finish_action(
                profile_id, action_id, "cancelled", phase="resource_preflight",
                error_code="user_cancelled",
            )
            return ITSActionResult(action_id, "cancelled", "Cancelled before an ITS write was sent.")
        except ITSSyncError as exc:
            self.db.finish_action(
                profile_id, action_id,
                "outcome_unknown" if write_started else "failed",
                phase="verification" if write_started else "resource_preflight",
                error_code=exc.code,
            )
            if write_started:
                return ITSActionResult(
                    action_id, "outcome_unknown",
                    "A write may have been submitted; verify before any retry. " + str(exc),
                )
            return ITSActionResult(action_id, "failed", str(exc))
        except Exception:
            terminal = "outcome_unknown" if write_started else "failed"
            self.db.finish_action(
                profile_id, action_id, terminal,
                phase="verification" if write_started else "resource_preflight",
                error_code="local_journal_failure" if write_started else "preflight_failed",
            )
            return ITSActionResult(
                action_id, terminal,
                "The write outcome must be verified before any retry."
                if write_started else "ITS preflight failed before a write was sent.",
            )
        finally:
            # A write was started but did not end in a verified success with
            # rebuilt evidence: keep a visible ITS refresh requirement.
            if write_started and not resolved_success:
                self.db.mark_its_reconciliation_stale(profile_id, int(row["action_group_id"]))

    def _require_unchanged_context(self, row: dict[str, Any], live: _LiveITSState) -> None:
        """Stop before an unsafe write if observation identity or specimen result moved."""
        if str(row["preview_inat_record_fingerprint"]) != live.inat_record_fingerprint:
            raise ITSSyncError("The iNaturalist observation changed after preview.", "inat_record_changed")
        if str(row["preview_mo_record_fingerprint"]) != live.mo_record_fingerprint:
            raise ITSSyncError("The Mushroom Observer observation changed after preview.", "mo_record_changed")
        if str(row["preview_inat_links_fingerprint"]) != live.specimen_state_fingerprint:
            raise ITSSyncError(
                "The specimen-identity validation changed after preview.", "specimen_state_changed",
            )

    def _finish_success(
        self, row: dict[str, Any], live: _LiveITSState, *, verification_state: str,
        http_status: Optional[int] = None, server_id: str = "", server_uuid: str = "",
    ) -> None:
        profile_id = int(row["profile_id"])
        if ITSActionType(str(row["action_type"])) is not ITSActionType.INAT_ITS_REMOVE:
            verified_id, verified_uuid = self._verified_result_identity(row, live)
            server_id = server_id or verified_id
            server_uuid = server_uuid or verified_uuid
        self.db.finish_action(
            profile_id, int(row["action_id"]), "succeeded", phase="verification",
            http_status=http_status, verification_state=verification_state,
            server_row_id=server_id, server_row_uuid=server_uuid,
        )
        if not self._persist_evidence(profile_id, live):
            # The verified remote result is already durable, but local evidence
            # was not rebuilt, so keep a visible refresh requirement.
            self.db.mark_its_reconciliation_stale(profile_id, int(row["action_group_id"]))

    def _persist_evidence(self, profile_id: int, live: _LiveITSState) -> bool:
        """Rebuild persistent safe ITS derivatives and resolve the stale issue.

        Returns True only when the whole persistence transaction (evidence, pair
        score, and stale-issue resolution) succeeded.
        """
        try:
            self.db.refresh_its_evidence(
                profile_id, live.pair_id,
                mo_observation_id=live.mo_observation_id,
                inat_observation_id=live.inat_observation_id,
                mo_hashes=_mo_sequence_fingerprints(live.mo_sequences),
                inat_hashes=_sequence_fingerprints(live.inat_records),
                mo_accessions=_mo_accessions(live.mo_sequences),
                inat_accessions=_accessions(live.inat_records),
            )
            # Only after the derivatives and pair score are durably refreshed do
            # we resolve the refresh requirement.
            self.db.resolve_its_reconciliation_stale(
                profile_id, live.mo_observation_id, live.inat_observation_id,
            )
            return True
        except Exception:
            return False

    def _verified_result_identity(
        self, row: dict[str, Any], live: _LiveITSState,
    ) -> tuple[str, str]:
        desired_hash = str(row["sequence_fingerprint"] or "")
        desired_accession = str(row["normalized_accession"] or "")
        desired_archive = str(row["normalized_archive"] or "")
        target = str(row["remote_row_id"] or "")

        def matches(seq_fp: str, archive: str, accession: str) -> bool:
            if desired_hash:
                return seq_fp == desired_hash
            return bool(desired_accession) and archive == desired_archive and accession == desired_accession

        if str(row["site"]) == "mo":
            for item in live.mo_sequences:
                if target and str(item.sequence_id) != target:
                    continue
                if matches(item.sequence_fingerprint, item.archive, item.normalized_accession):
                    return str(item.sequence_id), ""
            return "", ""
        for item in live.inat_records:
            if target and item.remote_id != target:
                continue
            if matches(item.sequence_fingerprint, item.archive, item.normalized_accession):
                return item.remote_id, item.remote_uuid
        return "", ""

    def _write(
        self, row: dict[str, Any], profile: ReconciliationProfile,
        live: _LiveITSState, source: _WriteSource,
        cancelled: Callable[[], bool], on_send: Callable[[], None],
    ) -> object:
        """Validate every local precondition, then send exactly one request.

        ``on_send`` stamps the durable write boundary and must be called
        immediately before the request leaves — never earlier, so a deterministic
        local refusal below is settled as a definite failure rather than as an
        ambiguous "a write may have been submitted".
        """
        action = ITSActionType(str(row["action_type"]))
        targets_mo = action in {ITSActionType.MO_SEQUENCE_ADD, ITSActionType.MO_SEQUENCE_REPAIR}
        # Recheck BOTH credential contexts before every write. Even an MO write
        # was reviewed under the selected iNaturalist account context.
        auth = self._recheck_inat_auth(live)
        if not targets_mo:
            if action is ITSActionType.INAT_ITS_REMOVE:
                on_send()
                return self.inat_client.delete_reconciliation_field_value_v2(
                    auth.api_token, str(row["remote_row_uuid"]),
                )
            binding_id = positive_int(row["binding_id"])
            if not binding_id:
                raise ITSSyncError("The exact iNaturalist destination field is unavailable.", "binding_missing")
            if binding_id == live.inat_accession_field_id and (
                source.archive != MO_GENBANK_ARCHIVE
                or not is_genbank_accession(source.normalized_accession)
            ):
                raise ITSSyncError(
                    "Only GenBank accessions may be written to the iNaturalist GenBank field; "
                    f"the source archive is {source.archive or 'unspecified'}.",
                    "non_genbank_accession",
                )
            # Write the NORMALIZED sequence to iNaturalist's DNA field so a FASTA
            # header, digits, dashes/dots, or stray whitespace from the MO source
            # never lands in the iNaturalist value. Raw text stays for display only.
            value = source.normalized_sequence if source.sequence_fingerprint else source.normalized_accession
            if not value:
                raise ITSSyncError("The source no longer contains the selected ITS value.", "source_changed")
            if action is ITSActionType.INAT_ITS_ADD:
                on_send()
                return self.inat_client.create_reconciliation_field_value_v2(
                    auth.api_token, live.inat_observation_uuid, binding_id, value,
                )
            on_send()
            return self.inat_client.update_reconciliation_field_value_v2(
                auth.api_token, str(row["remote_row_uuid"]),
                live.inat_observation_uuid, binding_id, value,
            )

        key = self._recheck_mo_key(profile, live)
        if action is ITSActionType.MO_SEQUENCE_ADD:
            bases = source.raw_sequence if source.sequence_fingerprint else ""
            archive, accession = self._mo_deposit_fields(source)
            if not bases and not (archive and accession):
                raise ITSSyncError("The source no longer contains a writable MO sequence value.", "source_changed")
            on_send()
            return self.mo_client.create_sequence(
                key, live.mo_observation_id, "ITS",
                cancelled, bases=bases, archive=archive, accession=accession,
                notes="Synchronized from confirmed iNaturalist pair",
            )

        # MO_SEQUENCE_REPAIR: operate on the complete composite MO row.
        sequence_id = positive_int(row["remote_row_id"])
        if not sequence_id:
            raise ITSSyncError("The exact MO sequence ID is unavailable.", "mo_sequence_id_missing")
        composite = next((item for item in live.mo_sequences if item.sequence_id == sequence_id), None)
        if composite is None:
            raise ITSSyncError("The exact MO sequence row is no longer present.", "mo_sequence_missing")
        self._require_mo_edit_permission(profile, composite)
        if source.sequence_fingerprint:
            # Replace bases; the existing deposit is retained by MO. Only allow it
            # when that retained deposit is proven compatible with the source pair.
            self._require_retained_deposit_compatible(composite, live)
            on_send()
            return self.mo_client.update_sequence(
                key, sequence_id, cancelled, bases=source.raw_sequence,
            )
        archive, accession = self._mo_deposit_fields(source)
        if not (archive and accession):
            raise ITSSyncError("The source does not provide a complete archive-plus-accession deposit.", "incomplete_deposit")
        # Replace the deposit; existing bases are retained by MO. Only allow it
        # when those retained bases are proven compatible with the source pair.
        self._require_retained_bases_compatible(composite, live)
        on_send()
        return self.mo_client.update_sequence(
            key, sequence_id, cancelled, archive=archive, accession=accession,
        )

    def _recheck_inat_auth(self, live: _LiveITSState) -> AuthState:
        auth = self.auth_provider()
        if (
            not auth.api_token
            or self.auth_generation_provider() != live.auth_generation
            or public_fingerprint(auth.api_token) != live.inat_token_marker
        ):
            raise ITSSyncError("iNaturalist authentication changed after preflight.", "inat_auth_changed")
        return auth

    def _recheck_mo_key(self, profile: ReconciliationProfile, live: _LiveITSState) -> str:
        key = self.mo_key_provider(profile.profile_id)
        if (
            not key or self.mo_key_generation_provider() != live.mo_key_generation
            or public_fingerprint(key) != live.mo_key_marker
        ):
            raise ITSSyncError("The Mushroom Observer API key changed after preflight.", "mo_key_changed")
        return key

    @staticmethod
    def _require_mo_edit_permission(profile: ReconciliationProfile, composite: MOSequenceRecord) -> None:
        if composite.creator_user_id is None:
            raise ITSSyncError(
                "The Mushroom Observer sequence creator is unknown; row edit permission cannot be proven.",
                "mo_sequence_owner_unknown",
            )
        if composite.creator_user_id != profile.mo_user_id:
            raise ITSSyncError(
                "The authenticated Mushroom Observer account does not own this sequence row.",
                "mo_sequence_owner_mismatch",
            )

    @staticmethod
    def _mo_deposit_fields(source: _WriteSource) -> tuple[str, str]:
        accession = source.normalized_accession
        if not accession:
            return "", ""
        archive = normalize_archive(source.archive, accession)
        if not is_mo_writable_archive(archive):
            raise ITSSyncError(
                f"Mushroom Observer does not accept the {archive or 'unspecified'} archive as a deposit.",
                "non_writable_archive",
            )
        return archive, accession

    def _require_retained_deposit_compatible(self, composite: MOSequenceRecord, live: _LiveITSState) -> None:
        """A bases repair may retain the deposit only if it is empty or proven-valid.

        A nonempty-invalid deposit (incomplete or unparseable) must never be left
        untouched on the row, and a valid deposit must be proven-compatible with the
        confirmed pair before it is retained.
        """
        if composite.accession_validation == "empty":
            return
        if composite.has_deposit and composite.accession_identity in self._inat_accession_identities(live):
            return
        raise ITSSyncError(
            "This MO row carries a mixed, invalid, or unproven deposit alongside its bases; "
            "it requires manual review before a bases repair.",
            "mixed_mo_record",
        )

    def _require_retained_bases_compatible(self, composite: MOSequenceRecord, live: _LiveITSState) -> None:
        """An accession repair may retain the bases only if they are empty or proven-valid."""
        if composite.sequence_validation == "empty":
            return
        if composite.has_bases and composite.sequence_fingerprint in _sequence_fingerprints(live.inat_records):
            return
        raise ITSSyncError(
            "This MO row carries mixed, malformed, or unproven bases alongside its deposit; "
            "it requires manual review before an accession repair.",
            "mixed_mo_record",
        )

    @staticmethod
    def _inat_accession_identities(live: _LiveITSState) -> set[tuple[str, str]]:
        return {
            (item.archive, item.normalized_accession)
            for item in live.inat_records
            if item.value_kind == "accession" and item.validation_state == "valid"
            and item.normalized_accession
        }

    def _refresh(
        self, profile: ReconciliationProfile, pair: dict[str, Any],
        cancelled: Callable[[], bool], *, require_mo_key: bool,
        verification_only: bool = False,
    ) -> _LiveITSState:
        if cancelled():
            raise ReconciliationCancelled("ITS comparison cancelled")
        auth_generation = self.auth_generation_provider()
        mo_key_generation = self.mo_key_generation_provider()
        auth = self.auth_provider()
        token = auth.api_token if auth.is_authenticated else ""
        if not token:
            raise ITSSyncError(
                "iNaturalist authentication must match the selected reconciliation account.",
                "inat_auth_mismatch",
            )
        if not verification_only:
            current = _first_result(self.inat_client.get_current_user_v2(token))
            if positive_int(current.get("id") if current else None) != profile.inat_user_id:
                raise ITSSyncError(
                    "The authenticated iNaturalist account does not match the profile.",
                    "inat_auth_mismatch",
                )

        reader = INatReconciliationReader(self.inat_client)
        stored = self.db.field_binding(profile.profile_id, "its")
        definitions = reader.resolve_field_definitions(ITS_FIELD_NAME, ("dna",))
        if stored is None or str(stored["verification_state"]) != "verified":
            raise ITSSyncError("Verify the exact DNA Barcode ITS field binding first.", "its_field_unverified")
        field_id = int(stored["field_id"])
        if len([item for item in definitions if positive_int(item.get("id")) == field_id]) != 1:
            raise ITSSyncError("The stored DNA Barcode ITS field binding is no longer valid.", "its_field_changed")
        accession_field_id: Optional[int] = None
        accession_binding = self.db.field_binding(profile.profile_id, "its_accession")
        if accession_binding is not None and str(accession_binding["verification_state"]) == "verified":
            candidate_id = int(accession_binding["field_id"])
            accession_definitions = reader.resolve_field_definitions(ACCESSION_FIELD_NAME)
            if len([
                item for item in accession_definitions
                if positive_int(item.get("id")) == candidate_id
            ]) == 1:
                accession_field_id = candidate_id

        inat_id = int(pair["inat_observation_id"])
        mo_id = int(pair["mo_observation_id"])
        inat_raw = _first_result(self.inat_client.get_reconciliation_detail(inat_id, token, deep=True))
        if not inat_raw or positive_int(inat_raw.get("id")) != inat_id:
            raise ITSSyncError("The iNaturalist observation is unavailable.", "inat_unavailable")
        inat_uuid = str(inat_raw.get("uuid") or "").strip()
        inat_user_raw = inat_raw.get("user")
        inat_user = inat_user_raw if isinstance(inat_user_raw, dict) else {}
        if not inat_uuid or positive_int(inat_user.get("id")) != profile.inat_user_id:
            raise ITSSyncError("The iNaturalist record identity or owner changed.", "inat_owner_changed")

        mo_raw = _first_result(self.mo_client.observation(mo_id, cancelled, detail="high"))
        # Prove the payload is the observation we asked for: MO reports a rejected
        # filter as a fatal error inside HTTP 200, and every check below (owner,
        # record fingerprint, specimen evidence) would otherwise be computed from
        # a different record than the one this action writes to.
        if not mo_raw or positive_int(mo_raw.get("id")) != mo_id:
            raise ITSSyncError("The Mushroom Observer observation is unavailable.", "mo_unavailable")
        mo_observation = parse_mo_observation(mo_raw, profile.mo_user_id)
        if mo_observation.owner_id != profile.mo_user_id:
            raise ITSSyncError("The Mushroom Observer record owner changed.", "mo_owner_changed")
        sequence_payload = self.mo_client.sequences(
            profile.mo_user_id, (mo_id,), cancelled,
        )
        inat_records = _inat_records(inat_raw, field_id, accession_field_id, inat_id)
        mo_sequences = _mo_composites(sequence_payload, mo_id)
        mo_unreadable_rows = _mo_unreadable_rows(sequence_payload, mo_id)
        mo_records = _mo_display_records(mo_sequences)

        specimen_conflict, specimen_state, specimen_warnings = self._specimen_state(
            profile, pair, inat_raw, mo_raw, reader, cancelled,
        )

        key = self.mo_key_provider(profile.profile_id) if require_mo_key else ""
        if require_mo_key:
            if not key:
                raise ITSSyncError("A Mushroom Observer API key is required.", "mo_key_missing")
            if self.mo_client.authenticated_user_id(key, profile.mo_user_id, cancelled) != profile.mo_user_id:
                raise ITSSyncError("The Mushroom Observer API key does not match the profile.", "mo_key_mismatch")
        if not verification_only and (
            auth_generation != self.auth_generation_provider()
            or mo_key_generation != self.mo_key_generation_provider()
        ):
            raise ITSSyncError("Credential state changed during ITS preflight.", "credential_context_changed")
        return _LiveITSState(
            profile_id=profile.profile_id, pair_id=int(pair["pair_id"]),
            mo_observation_id=mo_id, inat_observation_id=inat_id,
            inat_observation_uuid=inat_uuid, inat_field_id=field_id,
            inat_accession_field_id=accession_field_id,
            inat_record_fingerprint=_inat_record_fingerprint(inat_raw),
            mo_record_fingerprint=mo_record_fingerprint(mo_raw),
            inat_values_fingerprint=_records_fingerprint(inat_records),
            mo_values_fingerprint=_mo_records_fingerprint(mo_sequences),
            specimen_state_fingerprint=specimen_state,
            inat_token_marker=public_fingerprint(token),
            mo_key_marker=public_fingerprint(key) if key else "",
            auth_generation=auth_generation, mo_key_generation=mo_key_generation,
            specimen_conflict=specimen_conflict, specimen_warnings=specimen_warnings,
            inat_records=inat_records, mo_records=mo_records, mo_sequences=mo_sequences,
            mo_unreadable_rows=mo_unreadable_rows,
        )

    def _specimen_state(
        self, profile: ReconciliationProfile, pair: dict[str, Any],
        inat_raw: dict[str, Any], mo_raw: dict[str, Any],
        reader: INatReconciliationReader, cancelled: Callable[[], bool],
    ) -> tuple[str, str, tuple[str, ...]]:
        """Full fresh specimen-identity validation reusing the shared validator.

        Returns ``(blocking_message, evidence_fingerprint, soft_warnings)``. ITS
        writes reconcile no specimen field, so every conflict — coordinates
        included — blocks. The shared implementation lives in ``specimen_state``
        so Gate 1C/1D cannot drift apart (it also resolves the MO fungal scope,
        which the observation payload never carries).
        """
        return evaluate_specimen_state(
            self.db, profile, pair, inat_raw, mo_raw, reader,
            mo_client=self.mo_client, cancelled=cancelled,
        )

    def _options(
        self, profile: ReconciliationProfile, live: _LiveITSState, warnings: list[str],
    ) -> list[ITSActionOption]:
        if live.specimen_conflict:
            return []
        # An MO row we cannot classify may already hold the very bases or deposit
        # a transfer would "add". Proposing anything here risks a duplicate MO row
        # or an overwrite presented as filling an empty slot, so nothing —
        # including the iNaturalist-side removals — is offered until the row is
        # readable.
        if live.mo_unreadable_rows:
            warnings.append(
                "Mushroom Observer returned sequence rows for this observation whose locus or row id "
                "could not be read, so its ITS state cannot be established. No ITS write is proposed."
            )
            return []
        result: list[ITSActionOption] = []
        result.extend(self._sequence_options(profile, live, warnings))
        result.extend(self._accession_options(profile, live, warnings))
        result.extend(self._removal_options(live))
        return result

    def _sequence_options(
        self, profile: ReconciliationProfile, live: _LiveITSState, warnings: list[str],
    ) -> list[ITSActionOption]:
        result: list[ITSActionOption] = []
        mo_row = _single_mo_row(live)
        inat_its_rows = [item for item in live.inat_records if item.binding_id == live.inat_field_id]
        if len(live.mo_sequences) > 1 or len(inat_its_rows) > 1:
            warnings.append("Multiple ITS sequence records require manual review; no sequence transfer is proposed.")
            return result
        inat_valid = next((item for item in inat_its_rows if item.validation_state == "valid"), None)
        inat_invalid = next((item for item in inat_its_rows if item.validation_state != "valid"), None)
        mo_has_bases = bool(mo_row and mo_row.has_bases)

        # MO valid bases → iNaturalist ITS field.
        if mo_has_bases and mo_row is not None:
            if inat_valid is None and inat_invalid is None:
                result.append(self._mo_to_inat_sequence(live, mo_row, destination=None))
            elif inat_invalid is not None and inat_valid is None:
                # The field is occupied by exactly one invalid value: repair, not add.
                result.append(self._mo_to_inat_sequence(live, mo_row, destination=inat_invalid))
            elif inat_valid is not None and inat_valid.sequence_fingerprint != mo_row.sequence_fingerprint:
                result.append(self._mo_to_inat_sequence(live, mo_row, destination=inat_valid, manual=True))
        # iNaturalist valid sequence → Mushroom Observer.
        if inat_valid is not None:
            if mo_row is None:
                result.append(self._inat_to_mo_sequence(profile, live, inat_valid, destination=None))
            elif not mo_row.has_bases:
                result.append(self._inat_to_mo_sequence(profile, live, inat_valid, destination=mo_row))
            elif mo_row.sequence_fingerprint != inat_valid.sequence_fingerprint:
                result.append(self._inat_to_mo_sequence(profile, live, inat_valid, destination=mo_row, manual=True))
        return result

    def _accession_options(
        self, profile: ReconciliationProfile, live: _LiveITSState, warnings: list[str],
    ) -> list[ITSActionOption]:
        result: list[ITSActionOption] = []
        mo_row = _single_mo_row(live)
        acc_field = live.inat_accession_field_id
        inat_acc_rows = [
            item for item in live.inat_records
            if acc_field is not None and item.binding_id == acc_field
        ]
        if len(live.mo_sequences) > 1 or len(inat_acc_rows) > 1:
            warnings.append("Multiple ITS accession records require manual review; no accession transfer is proposed.")
            return result
        inat_valid = next((item for item in inat_acc_rows if item.validation_state == "valid"), None)
        inat_invalid = next((item for item in inat_acc_rows if item.validation_state != "valid"), None)
        mo_has_deposit = bool(mo_row and mo_row.has_deposit)

        # MO deposit → iNaturalist GenBank field. Only a GenBank-archive deposit
        # qualifies; an ENA/UNITE/BOLD deposit is display/compare-only here.
        if mo_has_deposit and mo_row is not None and acc_field is not None:
            genbank_ok = (
                mo_row.archive == MO_GENBANK_ARCHIVE
                and is_genbank_accession(mo_row.normalized_accession)
            )
            if inat_valid is None and inat_invalid is None:
                result.append(self._mo_to_inat_accession(live, mo_row, destination=None, genbank_ok=genbank_ok))
            elif inat_invalid is not None and inat_valid is None:
                result.append(self._mo_to_inat_accession(live, mo_row, destination=inat_invalid, genbank_ok=genbank_ok))
            elif inat_valid is not None and (mo_row.archive, mo_row.normalized_accession) != (inat_valid.archive, inat_valid.normalized_accession):
                result.append(self._mo_to_inat_accession(live, mo_row, destination=inat_valid, genbank_ok=genbank_ok, manual=True))
        elif mo_row is not None and mo_row.normalized_accession and not mo_row.has_deposit:
            warnings.append(
                "The Mushroom Observer accession is not a complete, recognised deposit; transfer is disabled."
            )
        # iNaturalist GenBank accession → Mushroom Observer deposit.
        if inat_valid is not None:
            if mo_row is None:
                result.append(self._inat_to_mo_accession(profile, live, inat_valid, destination=None))
            elif not mo_row.has_deposit:
                result.append(self._inat_to_mo_accession(profile, live, inat_valid, destination=mo_row))
            elif mo_row.accession_identity != (inat_valid.archive, inat_valid.normalized_accession):
                result.append(self._inat_to_mo_accession(profile, live, inat_valid, destination=mo_row, manual=True))
        return result

    def _removal_options(self, live: _LiveITSState) -> list[ITSActionOption]:
        result: list[ITSActionOption] = []
        for record in live.inat_records:
            if record.validation_state == "valid":
                continue
            invalid_text = record.raw_value or record.raw_sequence or record.normalized_accession or "(empty)"
            result.append(ITSActionOption(
                action_type=ITSActionType.INAT_ITS_REMOVE,
                destination_site=RemoteSite.INAT,
                description=(
                    f"Remove explicitly selected invalid iNaturalist {record.label or 'ITS'} row "
                    f"{record.remote_uuid or record.remote_id} containing: {invalid_text}"
                ),
                destructive=True, source_site=RemoteSite.INAT,
                source_record_id=live.inat_observation_id,
                destination_record_id=live.inat_observation_id,
                source_remote_id=record.remote_id,
                destination_remote_id=record.remote_id,
                destination_remote_uuid=record.remote_uuid,
                destination_binding_id=record.binding_id,
                source_metadata_fingerprint=record.metadata_fingerprint,
                destination_preflight_fingerprint=live.inat_values_fingerprint,
                enabled=bool(record.remote_uuid),
                disabled_reason="The field-value UUID required for exact removal is unavailable."
                if not record.remote_uuid else "",
            ))
        return result

    def _mo_to_inat_sequence(
        self, live: _LiveITSState, mo_row: MOSequenceRecord,
        destination: Optional[ITSRecordSnapshot], *, manual: bool = False,
    ) -> ITSActionOption:
        action = ITSActionType.INAT_ITS_REPAIR if destination else ITSActionType.INAT_ITS_ADD
        enabled, reason = True, ""
        if destination is not None and not destination.remote_uuid:
            enabled, reason = False, "The exact iNaturalist field-value UUID is unavailable."
        target = "the occupied invalid ITS field row" if destination else "the empty ITS field"
        return ITSActionOption(
            action_type=action, destination_site=RemoteSite.INAT,
            description=(
                ("Manual conflict choice: " if manual else "")
                + f"copy MO sequence {mo_row.sequence_id} bases to iNaturalist {target}"
            ),
            destructive=destination is not None,
            source_site=RemoteSite.MO, source_record_id=mo_row.observation_id,
            destination_record_id=live.inat_observation_id,
            source_remote_id=str(mo_row.sequence_id),
            destination_remote_id=destination.remote_id if destination else "",
            destination_remote_uuid=destination.remote_uuid if destination else "",
            destination_binding_id=live.inat_field_id,
            sequence_fingerprint=mo_row.sequence_fingerprint,
            source_metadata_fingerprint=mo_row.record_fingerprint,
            destination_preflight_fingerprint=live.inat_values_fingerprint,
            enabled=enabled, disabled_reason=reason,
        )

    def _mo_to_inat_accession(
        self, live: _LiveITSState, mo_row: MOSequenceRecord,
        destination: Optional[ITSRecordSnapshot], *, genbank_ok: bool, manual: bool = False,
    ) -> ITSActionOption:
        action = ITSActionType.INAT_ITS_REPAIR if destination else ITSActionType.INAT_ITS_ADD
        enabled, reason = True, ""
        if not genbank_ok:
            enabled, reason = False, (
                f"The Mushroom Observer archive is {mo_row.archive or 'unspecified'}; only GenBank "
                "accessions may be written to the iNaturalist GenBank field."
            )
        elif destination is not None and not destination.remote_uuid:
            enabled, reason = False, "The exact iNaturalist field-value UUID is unavailable."
        target = "the occupied invalid GenBank field row" if destination else "the empty GenBank field"
        return ITSActionOption(
            action_type=action, destination_site=RemoteSite.INAT,
            description=(
                ("Manual conflict choice: " if manual else "")
                + f"copy MO accession {mo_row.normalized_accession} ({mo_row.archive or 'unspecified'}) "
                f"to iNaturalist {target}"
            ),
            destructive=destination is not None,
            source_site=RemoteSite.MO, source_record_id=mo_row.observation_id,
            destination_record_id=live.inat_observation_id,
            source_remote_id=str(mo_row.sequence_id),
            destination_remote_id=destination.remote_id if destination else "",
            destination_remote_uuid=destination.remote_uuid if destination else "",
            destination_binding_id=live.inat_accession_field_id,
            normalized_accession=mo_row.normalized_accession, archive=mo_row.archive,
            source_metadata_fingerprint=mo_row.record_fingerprint,
            destination_preflight_fingerprint=live.inat_values_fingerprint,
            enabled=enabled, disabled_reason=reason,
        )

    def _inat_to_mo_sequence(
        self, profile: ReconciliationProfile, live: _LiveITSState,
        source: ITSRecordSnapshot, destination: Optional[MOSequenceRecord],
        *, manual: bool = False,
    ) -> ITSActionOption:
        action = ITSActionType.MO_SEQUENCE_REPAIR if destination else ITSActionType.MO_SEQUENCE_ADD
        enabled, reason = self._mo_write_gate(profile, destination)
        # A bases repair must not silently retain a nonempty-invalid or unproven
        # deposit on the same composite row.
        if enabled and destination is not None and destination.accession_validation != "empty":
            if not (destination.has_deposit
                    and destination.accession_identity in self._inat_accession_identities(live)):
                enabled, reason = False, (
                    "This MO row carries a mixed, invalid, or unproven deposit; replacing its bases "
                    "would leave conflicting sequence/deposit data. Manual review is required."
                )
        target = f"MO sequence row {destination.sequence_id}" if destination else "a new MO sequence"
        return ITSActionOption(
            action_type=action, destination_site=RemoteSite.MO,
            description=(
                ("Manual conflict choice: " if manual else "")
                + f"copy iNaturalist sequence {source.remote_id} bases to {target}"
            ),
            destructive=destination is not None,
            source_site=RemoteSite.INAT, source_record_id=source.observation_id,
            destination_record_id=live.mo_observation_id,
            source_remote_id=source.remote_id,
            destination_remote_id=str(destination.sequence_id) if destination else "",
            destination_binding_id=None,
            sequence_fingerprint=source.sequence_fingerprint,
            source_metadata_fingerprint=source.metadata_fingerprint,
            destination_preflight_fingerprint=live.mo_values_fingerprint,
            enabled=enabled, disabled_reason=reason,
        )

    def _inat_to_mo_accession(
        self, profile: ReconciliationProfile, live: _LiveITSState,
        source: ITSRecordSnapshot, destination: Optional[MOSequenceRecord],
        *, manual: bool = False,
    ) -> ITSActionOption:
        action = ITSActionType.MO_SEQUENCE_REPAIR if destination else ITSActionType.MO_SEQUENCE_ADD
        enabled, reason = self._mo_write_gate(profile, destination)
        archive = normalize_archive(source.archive, source.normalized_accession)
        if enabled and not (archive and source.normalized_accession):
            enabled, reason = False, "Mushroom Observer requires a complete archive-plus-accession deposit."
        elif enabled and not is_mo_writable_archive(archive):
            enabled, reason = False, (
                f"Mushroom Observer does not accept the {archive or 'unspecified'} archive as a deposit."
            )
        # An accession repair must not silently retain nonempty-invalid or
        # unproven bases on the same composite row.
        if enabled and destination is not None and destination.sequence_validation != "empty":
            if not (destination.has_bases
                    and destination.sequence_fingerprint in _sequence_fingerprints(live.inat_records)):
                enabled, reason = False, (
                    "This MO row carries mixed, malformed, or unproven bases; replacing its deposit "
                    "would leave conflicting sequence/deposit data. Manual review is required."
                )
        target = f"MO sequence row {destination.sequence_id}" if destination else "a new MO sequence"
        return ITSActionOption(
            action_type=action, destination_site=RemoteSite.MO,
            description=(
                ("Manual conflict choice: " if manual else "")
                + f"copy iNaturalist accession {source.normalized_accession} to {target} "
                f"as archive {archive or 'unspecified'}"
            ),
            destructive=destination is not None,
            source_site=RemoteSite.INAT, source_record_id=source.observation_id,
            destination_record_id=live.mo_observation_id,
            source_remote_id=source.remote_id,
            destination_remote_id=str(destination.sequence_id) if destination else "",
            destination_binding_id=None,
            normalized_accession=source.normalized_accession, archive=archive,
            source_metadata_fingerprint=source.metadata_fingerprint,
            destination_preflight_fingerprint=live.mo_values_fingerprint,
            enabled=enabled, disabled_reason=reason,
        )

    def _mo_write_gate(
        self, profile: ReconciliationProfile, destination: Optional[MOSequenceRecord],
    ) -> tuple[bool, str]:
        if not self.mo_key_provider(profile.profile_id):
            return False, "Enter a Mushroom Observer API key before selecting this write."
        if destination is None:
            return True, ""
        if not positive_int(destination.sequence_id):
            return False, "The exact Mushroom Observer sequence ID is unavailable."
        if destination.creator_user_id is None:
            return False, (
                "The Mushroom Observer sequence creator is unknown; row edit permission cannot be proven."
            )
        if destination.creator_user_id != profile.mo_user_id:
            return False, (
                "The authenticated Mushroom Observer account does not own this sequence row."
            )
        return True, ""

    def _eligible_pair(self, profile_id: int, pair_id: int) -> dict[str, Any]:
        pair = self.db.pair_detail(profile_id, pair_id)
        if not pair or pair.get("review_state") != "confirmed" or pair.get("excluded"):
            raise ITSSyncError("Only a currently confirmed, non-excluded pair can produce ITS actions.")
        return pair

    def _require_current_source(self, row: dict[str, Any], pair: dict[str, Any]) -> None:
        group = self.db.action_group(int(row["profile_id"]), int(row["action_group_id"]))
        if not group or _pair_fingerprint(pair) != str(group["source_fingerprint"]):
            raise ITSSyncError("The confirmed pair changed after preview.", "pair_changed")

    def _current_source(
        self, row: dict[str, Any], live: _LiveITSState,
    ) -> Optional[_WriteSource]:
        desired_hash = str(row["sequence_fingerprint"] or "")
        desired_accession = str(row["normalized_accession"] or "")
        desired_archive = str(row["normalized_archive"] or "")
        metadata = str(row["source_metadata_fingerprint"])
        remote_id = str(row["source_sequence_remote_id"])

        def accession_matches(archive: str, accession: str) -> bool:
            return bool(desired_accession) and archive == desired_archive and accession == desired_accession

        if str(row["source_site"]) == "mo":
            for item in live.mo_sequences:
                if str(item.sequence_id) != remote_id or item.record_fingerprint != metadata:
                    continue
                if desired_hash and item.sequence_fingerprint == desired_hash:
                    return _WriteSource(
                        raw_sequence=item.raw_bases, normalized_sequence=item.normalized_sequence,
                        sequence_fingerprint=item.sequence_fingerprint,
                    )
                if accession_matches(item.archive, item.normalized_accession):
                    return _WriteSource(normalized_accession=item.normalized_accession, archive=item.archive)
            return None
        for item in live.inat_records:
            if item.remote_id != remote_id or item.metadata_fingerprint != metadata:
                continue
            if desired_hash and item.sequence_fingerprint == desired_hash:
                return _WriteSource(
                    raw_sequence=item.raw_sequence, normalized_sequence=item.normalized_sequence,
                    sequence_fingerprint=item.sequence_fingerprint,
                )
            if accession_matches(item.archive, item.normalized_accession):
                return _WriteSource(normalized_accession=item.normalized_accession, archive=item.archive)
        return None

    @staticmethod
    def _destination_fingerprint(row: dict[str, Any], live: _LiveITSState) -> str:
        return live.inat_values_fingerprint if str(row["site"]) == "inat" else live.mo_values_fingerprint

    def _is_satisfied(self, row: dict[str, Any], live: _LiveITSState) -> bool:
        action = ITSActionType(str(row["action_type"]))
        if action is ITSActionType.INAT_ITS_REMOVE:
            target = str(row["remote_row_uuid"] or row["remote_row_id"])
            return not any(
                (item.remote_uuid or item.remote_id) == target for item in live.inat_records
            )
        desired_hash = str(row["sequence_fingerprint"] or "")
        desired_accession = str(row["normalized_accession"] or "")
        desired_archive = str(row["normalized_archive"] or "")
        if str(row["site"]) == "mo":
            return self._mo_satisfied(row, live, desired_hash, desired_archive, desired_accession)
        return self._inat_satisfied(row, live, action, desired_hash, desired_archive, desired_accession)

    def _mo_satisfied(
        self, row: dict[str, Any], live: _LiveITSState,
        desired_hash: str, desired_archive: str, desired_accession: str,
    ) -> bool:
        # An unreadable row could itself be the destination, or could carry the
        # component we believe we retained; neither the "one row" count nor the
        # retained-component proof below holds while one exists.
        if len(live.mo_sequences) != 1 or live.mo_unreadable_rows:
            return False
        composite = live.mo_sequences[0]
        target = str(row["remote_row_id"] or "")
        if target and str(composite.sequence_id) != target and str(row["action_type"]).endswith("repair"):
            return False
        # The written component must be present and correct, the whole row must be
        # structurally valid, AND the retained component must still be empty or
        # semantically proven-compatible with the fresh iNaturalist record — the
        # same requirement enforced before the write. This prevents marking a row
        # "succeeded" when it now carries our bases plus an unrelated valid deposit
        # (or vice-versa) introduced concurrently.
        if not composite.is_valid_row:
            return False
        if desired_hash:
            if not (composite.has_bases and composite.sequence_fingerprint == desired_hash):
                return False
            retained_deposit_ok = (
                composite.accession_validation == "empty"
                or composite.accession_identity in self._inat_accession_identities(live)
            )
            return retained_deposit_ok
        if not (
            composite.has_deposit
            and composite.archive == desired_archive
            and composite.normalized_accession == desired_accession
        ):
            return False
        retained_bases_ok = (
            composite.sequence_validation == "empty"
            or composite.sequence_fingerprint in _sequence_fingerprints(live.inat_records)
        )
        return retained_bases_ok

    def _inat_satisfied(
        self, row: dict[str, Any], live: _LiveITSState, action: ITSActionType,
        desired_hash: str, desired_archive: str, desired_accession: str,
    ) -> bool:
        kind = "sequence" if desired_hash else "accession"
        relevant = [item for item in live.inat_records if item.value_kind == kind]
        if len({item.remote_id for item in relevant}) != 1:
            return False
        if action is ITSActionType.INAT_ITS_REPAIR:
            candidates = [item for item in relevant if item.remote_id == str(row["remote_row_id"])]
        else:
            candidates = relevant

        def matches(item: ITSRecordSnapshot) -> bool:
            if desired_hash:
                return item.sequence_fingerprint == desired_hash
            return (
                bool(desired_accession)
                and item.archive == desired_archive
                and item.normalized_accession == desired_accession
            )

        exact = any(matches(item) for item in candidates)
        no_conflict = all(matches(item) for item in relevant)
        return exact and no_conflict


def _inat_records(
    raw: dict[str, Any], field_id: int, accession_field_id: Optional[int],
    observation_id: int,
) -> tuple[ITSRecordSnapshot, ...]:
    records: list[ITSRecordSnapshot] = []
    for index, item in enumerate(raw.get("ofvs") or raw.get("observation_field_values") or (), 1):
        if not isinstance(item, dict):
            continue
        field = item.get("observation_field") if isinstance(item.get("observation_field"), dict) else {}
        binding_id = positive_int(
            item.get("field_id") or item.get("observation_field_id") or field.get("id")
        )
        # ``binding_id`` is None when the row exposes no readable field id. Without
        # the explicit guard it would match a ``None`` ``accession_field_id`` (the
        # normal state when no GenBank binding is verified) and any unrelated
        # observation field would be modelled — and offered for removal — as an
        # invalid ITS/GenBank value.
        if binding_id is None or binding_id not in {field_id, accession_field_id}:
            continue
        value = str(item.get("value") or "").strip()
        sequence_like = _looks_like_sequence(value)
        archive = ""
        if binding_id == field_id:
            normalized = normalize_sequence(value)
            accession = ""
            if normalized:
                kind, validation = "sequence", "valid"
            elif normalize_accession(value):
                kind, validation = "invalid", "accession_in_sequence_field"
            elif sequence_like:
                kind = "invalid"
                validation = "sequence_too_short" if _sequence_symbol_count(value) < 20 else "malformed_sequence"
            else:
                kind, validation = "invalid", "invalid_sequence_value"
        else:
            normalized = ""
            accession = normalize_accession(value)
            # The iNaturalist GenBank field may only hold GenBank accessions; a
            # non-GenBank namespace value is an invalid field value, not a deposit.
            if is_genbank_accession(value):
                kind, validation, archive = "accession", "valid", MO_GENBANK_ARCHIVE
            elif accession:
                kind, validation = "invalid", "non_genbank_accession"
                archive = accession_namespace(value)
            elif sequence_like:
                kind, validation = "invalid", "accession_contains_sequence"
            else:
                kind, validation = "invalid", "invalid_accession"
        row_id = str(item.get("id") or f"unidentified:{index}")
        row_uuid = str(item.get("uuid") or "")
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        digest = sequence_digest(value)
        metadata = public_fingerprint(
            "inat", observation_id, row_id, row_uuid, binding_id, kind,
            digest, archive, accession, validation, positive_int(user.get("id")),
        )
        records.append(ITSRecordSnapshot(
            site=RemoteSite.INAT, observation_id=observation_id,
            remote_id=row_id, remote_uuid=row_uuid, binding_id=binding_id,
            value_kind=kind, raw_sequence=value if binding_id == field_id else "",
            raw_value=value,
            normalized_sequence=normalized, sequence_fingerprint=digest,
            normalized_accession=accession, archive=archive,
            label=ITS_FIELD_NAME if binding_id == field_id else ACCESSION_FIELD_NAME,
            public_metadata=(("added_by_user_id", str(positive_int(user.get("id")) or "unknown")),),
            metadata_fingerprint=metadata, validation_state=validation,
            added_by_user_id=positive_int(user.get("id")),
        ))
    return tuple(records)


def _norm_specimen_id(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _normalized_locality(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _round_coord(value: Optional[float]) -> str:
    return f"{value:.5f}" if isinstance(value, (int, float)) else ""


def _specimen_evidence_fingerprint(
    mo_inv: InventoryObservation, inat_inv: InventoryObservation,
    mo_hyd: HydratedObservation, inat_hyd: HydratedObservation,
    mo_loc: str, inat_loc: str, conflicts: Sequence[str],
) -> str:
    """Non-reversible fingerprint of the normalized specimen evidence itself.

    Detects material changes (owner, date, taxon, fungal scope, vouchers,
    collection numbers, locality, coordinates, availability) independently of the
    derived conflict messages. Coordinates and other semi-private values are fed
    into the hash only; nothing raw is stored.
    """
    parts: list[object] = ["specimen_evidence"]
    for inv, hyd, loc in ((mo_inv, mo_hyd, mo_loc), (inat_inv, inat_hyd, inat_loc)):
        parts.extend([
            inv.key.site.value, inv.owner_id, inv.account_id,
            inv.observed_on, inv.taxon_id, (inv.taxon_name or "").casefold(),
            inv.fungi_status, int(inv.deleted), inv.availability_state, "|",
            *sorted(hyd.voucher_identifiers), "|",
            *sorted(hyd.collection_identifiers), "|",
            loc, int(hyd.coordinates_available),
            _round_coord(hyd.latitude), _round_coord(hyd.longitude), _round_coord(hyd.accuracy_m),
            int(hyd.required_values_available), "||",
        ])
    parts.append("conflicts")
    parts.extend(sorted(conflicts))
    return public_fingerprint(*parts)


def _hydrate_mo_specimen(inventory: InventoryObservation, raw: dict[str, Any]) -> HydratedObservation:
    """Parse MO high-detail specimen structures for the shared conflict check.

    MO's serializer exposes ``collection_numbers`` as ``{collector, number}``
    objects, ``herbarium_records`` as voucher deposits, ``field_slip`` as an
    object with a ``code``, and public ``latitude``/``longitude`` plus a
    ``location`` object with bounding coordinates. The generic iNaturalist
    hydrator does not read these, so MO needs its own.
    """
    vouchers: set[str] = set()
    collections: set[str] = set()

    def _seq(value: object) -> list[Any]:
        if isinstance(value, list):
            return value
        return [value] if value not in (None, "") else []

    for item in _seq(raw.get("collection_numbers")):
        if isinstance(item, dict):
            collector = str(item.get("collector") or item.get("name") or "").strip()
            number = str(item.get("number") or item.get("value") or "").strip()
            for candidate in (number, f"{collector} {number}"):
                normalized = _norm_specimen_id(candidate)
                if normalized:
                    collections.add(normalized)
        else:
            normalized = _norm_specimen_id(item)
            if normalized:
                collections.add(normalized)
    field_slip = raw.get("field_slip")
    for slip in _seq(field_slip):
        code = slip.get("code") if isinstance(slip, dict) else slip
        normalized = _norm_specimen_id(code)
        if normalized:
            collections.add(normalized)
    for record in _seq(raw.get("herbarium_records")):
        if not isinstance(record, dict):
            normalized = _norm_specimen_id(record)
            if normalized:
                vouchers.add(normalized)
            continue
        herbarium_raw = record.get("herbarium")
        herbarium = herbarium_raw if isinstance(herbarium_raw, dict) else {}
        code = str(herbarium.get("code") or herbarium.get("name") or "").strip()
        accession = str(
            record.get("accession_number") or record.get("accession")
            or record.get("initial_det") or record.get("label") or ""
        ).strip()
        for candidate in (accession, f"{code} {accession}"):
            normalized = _norm_specimen_id(candidate)
            if normalized:
                vouchers.add(normalized)

    latitude = longitude = accuracy = None
    coordinates_available = False
    coordinate_source = ""
    coordinate_privacy_state = "unknown"
    lat_raw, lon_raw = raw.get("latitude"), raw.get("longitude")
    if lat_raw is not None and lon_raw is not None:
        try:
            latitude, longitude = float(lat_raw), float(lon_raw)
            coordinates_available = True
            coordinate_source = "explicit_public"
            coordinate_privacy_state = (
                "private"
                if any(raw.get(key) for key in (
                    "gps_hidden", "hidden", "location_hidden",
                ))
                else "public"
            )
            for key in ("gps_accuracy", "accuracy", "positional_accuracy"):
                if raw.get(key) is not None:
                    accuracy = float(raw[key])
                    break
        except (TypeError, ValueError):
            latitude = longitude = None
            accuracy = None
            coordinates_available = False
            coordinate_source = ""
    # A named MO location box is a fallback ONLY. When an explicit observation
    # point exists we keep it precise: a broad named location must never widen the
    # accuracy tolerance and hide a real explicit-coordinate conflict. MO's
    # serializer names the bounds latitude_north/south and longitude_east/west.
    if not coordinates_available:
        location_raw = raw.get("location")
        location = location_raw if isinstance(location_raw, dict) else {}
        bounds: dict[str, float] = {}
        for key in ("latitude_north", "latitude_south", "longitude_east", "longitude_west"):
            try:
                if location.get(key) is not None:
                    bounds[key] = float(location[key])
            except (TypeError, ValueError):
                pass
        if len(bounds) == 4:
            north, south = bounds["latitude_north"], bounds["latitude_south"]
            east, west = bounds["longitude_east"], bounds["longitude_west"]
            latitude = (north + south) / 2
            longitude = (east + west) / 2
            coordinates_available = True
            coordinate_source = "named_location_centroid"
            coordinate_privacy_state = "public"
            # Half the bounding-box diagonal is a conservative accuracy radius.
            accuracy = _distance_m(north, west, south, east) / 2

    owner_raw = raw.get("owner")
    owner = owner_raw if isinstance(owner_raw, dict) else {}

    return HydratedObservation(
        inventory,
        voucher_identifiers=tuple(sorted(vouchers)),
        collection_identifiers=tuple(sorted(collections)),
        latitude=latitude, longitude=longitude, accuracy_m=accuracy,
        coordinates_available=coordinates_available,
        coordinate_privacy_state=coordinate_privacy_state,
        coordinate_source=coordinate_source,
        # MO public specimen data is not auth-gated the way iNaturalist private
        # coordinates are; the fields we need are present in the public payload.
        required_values_available=True,
        # Gate 2A only: surfaced for a creation preview. MO's ``notes`` is the
        # same field name used on both read and create (confirmed live,
        # docs/gate_2a_capability_note.md) — a Gate 2A marker embedded here on
        # a MO destination would round-trip through this same key.
        description=str(raw.get("notes") or ""),
        attribution_name=str(owner.get("login_name") or ""),
        specimen_available=bool(raw["has_specimen"]) if "has_specimen" in raw else None,
    )


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi, d_lambda = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 6371000.0 * 2 * asin(sqrt(a))


def _mo_composites(payload: object, observation_id: int) -> tuple[MOSequenceRecord, ...]:
    """Model each MO sequence row as one composite record kept together."""
    records: list[MOSequenceRecord] = []
    for item in results_from_payload(payload):
        if positive_int(item.get("observation_id") or item.get("observation")) != observation_id:
            continue
        locus = str(item.get("locus") or "").strip()
        if "its" not in locus.casefold():
            continue
        sequence_id = positive_int(item.get("id"))
        if not sequence_id:
            continue
        bases = str(item.get("bases") or item.get("sequence") or item.get("dna_sequence") or "")
        normalized = normalize_sequence(bases)
        digest = sequence_digest(bases)
        accession_raw = str(item.get("accession") or "").strip()
        accession = normalize_accession(accession_raw)
        archive_raw = str(item.get("archive") or "").strip()
        # The archive is taken ONLY from what MO actually stored. Deriving it from
        # the accession format would silently label every INSDC-shaped value
        # "GenBank" — the format cannot distinguish GenBank from ENA — and would
        # also make the incomplete_deposit classification below unreachable, so an
        # archive-less accession would be transferred to iNaturalist's GenBank
        # field as a deposit MO never claimed.
        archive = normalize_archive(archive_raw)
        user_raw = item.get("user")
        user = user_raw if isinstance(user_raw, dict) else {}
        creator_id = positive_int(user.get("id")) or positive_int(item.get("user_id"))
        created_at = str(item.get("created_at") or "")
        updated_at = str(item.get("updated_at") or "")
        notes = str(item.get("notes") or "")
        notes_fingerprint = public_fingerprint(notes)

        if bases:
            sequence_validation = "valid" if normalized else (
                "sequence_too_short" if _looks_like_sequence(bases) and _sequence_symbol_count(bases) < 20
                else "malformed_sequence"
            )
        else:
            sequence_validation = "empty"
        # A valid MO deposit requires BOTH a complete archive and a valid
        # accession (MO's both-present-or-both-absent invariant). Any half-filled
        # or unparseable deposit is a nonempty-invalid component, not empty.
        if accession_raw or archive_raw:
            if accession and archive:
                accession_validation = "valid"
            elif accession and not archive:
                accession_validation = "incomplete_deposit"
            elif archive_raw and not accession:
                accession_validation = (
                    "accession_contains_sequence" if _looks_like_sequence(accession_raw)
                    else "incomplete_deposit"
                )
            elif _looks_like_sequence(accession_raw):
                accession_validation = "accession_contains_sequence"
            else:
                accession_validation = "invalid_accession"
        else:
            accession_validation = "empty"
        record_fingerprint = public_fingerprint(
            "mo", observation_id, sequence_id, locus, digest, sequence_validation,
            archive, accession, accession_validation, notes_fingerprint,
            creator_id, created_at, updated_at,
        )
        public_metadata = tuple(
            (key, str(value)) for key, value in (
                ("locus", locus or "unspecified"),
                ("archive", archive or "not specified"),
                ("creator_user_id", creator_id or "unknown"),
                ("created_at", created_at or "unknown"),
                ("updated_at", updated_at or "unknown"),
                ("notes_fingerprint", notes_fingerprint[:12]),
            ) if str(value)
        )
        records.append(MOSequenceRecord(
            observation_id=observation_id, sequence_id=sequence_id, locus=locus,
            raw_bases=bases, normalized_sequence=normalized, sequence_fingerprint=digest,
            archive=archive, normalized_accession=accession, raw_accession=accession_raw,
            creator_user_id=creator_id,
            created_at=created_at, updated_at=updated_at, notes_fingerprint=notes_fingerprint,
            sequence_validation=sequence_validation, accession_validation=accession_validation,
            record_fingerprint=record_fingerprint, public_metadata=public_metadata,
        ))
    return tuple(records)


def _mo_unreadable_rows(payload: object, observation_id: int) -> int:
    """Count this observation's MO sequence rows we cannot classify.

    ``_mo_composites`` must skip a row whose locus is unreadable (it may be LSU,
    RPB2, …, not ITS) and a row with no usable id (it cannot be repaired). Those
    skips are indistinguishable from "MO has no ITS sequence", which would turn a
    repair into a duplicating add or make an overwrite look like filling an empty
    slot — so they are counted and reported instead of silently dropped.
    """
    unreadable = 0
    for item in results_from_payload(payload):
        if positive_int(item.get("observation_id") or item.get("observation")) != observation_id:
            continue
        if not positive_int(item.get("id")) or not str(item.get("locus") or "").strip():
            unreadable += 1
    return unreadable


def _mo_display_records(composites: Sequence[MOSequenceRecord]) -> tuple[ITSRecordSnapshot, ...]:
    """Derive memory-only display snapshots; writes still use the composite rows."""
    records: list[ITSRecordSnapshot] = []
    for composite in composites:
        if composite.sequence_validation != "empty":
            records.append(ITSRecordSnapshot(
                site=RemoteSite.MO, observation_id=composite.observation_id,
                remote_id=str(composite.sequence_id),
                value_kind="sequence" if composite.has_bases else "invalid",
                raw_sequence=composite.raw_bases, raw_value=composite.raw_bases,
                normalized_sequence=composite.normalized_sequence,
                sequence_fingerprint=composite.sequence_fingerprint, archive=composite.archive,
                label=composite.locus, public_metadata=composite.public_metadata,
                metadata_fingerprint=composite.record_fingerprint,
                validation_state=composite.sequence_validation,
                added_by_user_id=composite.creator_user_id,
            ))
        if composite.accession_validation != "empty":
            records.append(ITSRecordSnapshot(
                site=RemoteSite.MO, observation_id=composite.observation_id,
                remote_id=str(composite.sequence_id),
                value_kind="accession" if composite.has_deposit else "invalid",
                normalized_accession=composite.normalized_accession, archive=composite.archive,
                # Show the exact raw MO accession text (memory only), never just
                # the archive name, so a malformed value is visible.
                raw_value=(
                    composite.raw_accession
                    or f"{composite.archive} {composite.normalized_accession}".strip()
                ),
                label=composite.locus, public_metadata=composite.public_metadata,
                metadata_fingerprint=composite.record_fingerprint,
                validation_state=composite.accession_validation,
                added_by_user_id=composite.creator_user_id,
            ))
    return tuple(records)


def _single_mo_row(live: _LiveITSState) -> Optional[MOSequenceRecord]:
    return live.mo_sequences[0] if len(live.mo_sequences) == 1 else None


def _comparison_states(live: _LiveITSState) -> tuple[str, ...]:
    states: list[str] = []
    inat_seq = _valid_kind(live.inat_records, "sequence")
    mo_seq = [item for item in live.mo_sequences if item.has_bases]
    inat_acc = _valid_kind(live.inat_records, "accession")
    mo_acc = [item for item in live.mo_sequences if item.has_deposit]
    inat_sequence_rows = {
        item.remote_id for item in live.inat_records
        if item.binding_id == live.inat_field_id
    }
    if len(inat_sequence_rows) > 1 or len(live.mo_sequences) > 1:
        states.append("multiple_sequences")
    mo_seq_hashes = {item.sequence_fingerprint for item in mo_seq}
    inat_seq_hashes = _sequence_fingerprints(inat_seq)
    if inat_seq and mo_seq:
        if inat_seq_hashes.intersection(mo_seq_hashes):
            exact = any(
                left.normalized_sequence == right.normalized_sequence
                for left in inat_seq for right in mo_seq
            )
            states.append("same_normalized_sequence" if exact else "reverse_complement_equivalent_sequence")
        else:
            states.append("different_sequences")
    elif mo_seq:
        states.append("sequence_present_only_on_mo")
    elif inat_seq:
        states.append("sequence_present_only_on_inaturalist")
    mo_identities = {item.accession_identity for item in mo_acc}
    inat_identities = {(item.archive, item.normalized_accession) for item in inat_acc}
    if inat_acc and mo_acc:
        states.append(
            "same_accession" if mo_identities.intersection(inat_identities)
            else "conflicting_accessions"
        )
    elif mo_acc:
        states.append("accession_present_only_on_mo")
    elif inat_acc:
        states.append("accession_present_only_on_inaturalist")
    invalid = [
        item.validation_state for item in live.inat_records if item.validation_state != "valid"
    ] + [
        item.sequence_validation for item in live.mo_sequences
        if item.sequence_validation not in {"valid", "empty"}
    ] + [
        item.accession_validation for item in live.mo_sequences
        if item.accession_validation not in {"valid", "empty"}
    ]
    if any(value in {
        "sequence_too_short", "malformed_sequence", "invalid_sequence_value",
    } for value in invalid):
        states.append("sequence_too_short_or_malformed")
    if any(value in {
        "invalid_accession", "accession_in_sequence_field", "accession_contains_sequence",
        "non_genbank_accession",
    } for value in invalid):
        states.append("invalid_accession_or_sequence_text")
    if live.specimen_conflict and inat_seq_hashes.intersection(mo_seq_hashes):
        states.append("sequence_equivalence_with_specimen_conflict")
    return tuple(states or ("no_its_data",))


def _valid_kind(
    records: Sequence[ITSRecordSnapshot], kind: str,
) -> tuple[ITSRecordSnapshot, ...]:
    return tuple(item for item in records if item.value_kind == kind and item.validation_state == "valid")


def _sequence_fingerprints(records: Sequence[ITSRecordSnapshot]) -> set[str]:
    return {item.sequence_fingerprint for item in records if item.sequence_fingerprint}


def _accessions(records: Sequence[ITSRecordSnapshot]) -> set[tuple[str, str]]:
    """Archive-qualified identities of valid iNaturalist accession values."""
    return {
        (item.archive, item.normalized_accession) for item in records
        if item.value_kind == "accession" and item.validation_state == "valid"
        and item.normalized_accession
    }


def _mo_sequence_fingerprints(records: Sequence[MOSequenceRecord]) -> set[str]:
    return {item.sequence_fingerprint for item in records if item.has_bases}


def _mo_accessions(records: Sequence[MOSequenceRecord]) -> set[tuple[str, str]]:
    """Archive-qualified identities of valid MO deposits."""
    return {
        item.accession_identity for item in records
        if item.has_deposit and item.accession_identity is not None
    }


def _records_fingerprint(records: Sequence[ITSRecordSnapshot]) -> str:
    return public_fingerprint(*(sorted(item.metadata_fingerprint for item in records)))


def _mo_records_fingerprint(records: Sequence[MOSequenceRecord]) -> str:
    return public_fingerprint(*(sorted(item.record_fingerprint for item in records)))


def _looks_like_sequence(value: str) -> bool:
    body = "".join(
        line for line in value.splitlines() if not line.lstrip().startswith(">")
    )
    compact = re.sub(r"[\s\d.\-]+", "", body).upper()
    return bool(compact) and re.fullmatch(r"[ACGTRYSWKMBDHVN]+", compact) is not None


def _sequence_symbol_count(value: str) -> int:
    body = "\n".join(
        line for line in value.splitlines() if not line.lstrip().startswith(">")
    )
    return len(re.sub(r"[^A-Za-z]", "", body))


def _pair_fingerprint(pair: dict[str, Any]) -> str:
    return public_fingerprint(
        "pair", pair.get("pair_id"), pair.get("updated_at"), pair.get("review_state"),
        pair.get("link_state"), pair.get("confirmed_by"),
    )


def _inat_record_fingerprint(raw: dict[str, Any]) -> str:
    user_raw = raw.get("user")
    user = user_raw if isinstance(user_raw, dict) else {}
    taxon_raw = raw.get("taxon")
    taxon = taxon_raw if isinstance(taxon_raw, dict) else {}
    return public_fingerprint(
        raw.get("id"), raw.get("uuid"), user.get("id"), raw.get("observed_on"),
        raw.get("updated_at"), taxon.get("id"), taxon.get("ancestry"),
    )


def _first_result(payload: object) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    values = payload.get("results")
    if isinstance(values, list):
        return next((item for item in values if isinstance(item, dict)), None)
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    return payload if payload.get("id") is not None else None


def _response_identity(payload: object) -> tuple[str, str]:
    row = _first_result(payload)
    if not row:
        return "", ""
    return str(row.get("id") or ""), str(row.get("uuid") or "")


def _is_its_action(value: object) -> bool:
    try:
        ITSActionType(str(value))
    except ValueError:
        return False
    return True
