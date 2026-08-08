"""Gate 1B reciprocal-link preview, preflight, execution, and verification.

This module is deliberately separate from inventory scanning.  It performs no
write until a caller has journaled an explicitly selected preview.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger(__name__)

from observation_workbench.api.auth import AuthState
from observation_workbench.api.client import INatAPIError, INatClient

from .db import ReconciliationDB, pair_source_fingerprint
from .inat_reader import INatReconciliationReader, MO_FIELD_NAME, inat_fungi_status
from .mo_client import (
    MOAPIError,
    MOClient,
    ReconciliationCancelled,
    results_from_payload,
)
from .mo_parsing import (
    TARGET_UNKNOWN,
    mo_record_fingerprint,
    parse_mo_external_link,
    parse_mo_observation,
)
from .normalization import parse_mo_observation_url, public_fingerprint
from .types import (
    AuthoritativeLinkSnapshot,
    LinkActionType,
    LinkRepairOption,
    LinkRepairPreview,
    ReconciliationProfile,
    RemoteSite,
)

INAT_MO_URL = "https://mushroomobserver.org/obs/{mo_id}"
MO_INAT_URL = "https://www.inaturalist.org/observations/{inat_id}"


class LinkRepairError(RuntimeError):
    """A safe, user-displayable Gate 1B precondition or verification error."""

    def __init__(self, message: str, code: str = "link_repair_unavailable") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LinkActionResult:
    action_id: int
    state: str
    message: str


@dataclass(frozen=True)
class _LiveState:
    profile_id: int
    mo_observation_id: int
    inat_observation_id: int
    inat_observation_uuid: str
    inat_field_id: int
    mo_external_site_id: int
    inat_record_fingerprint: str
    mo_record_fingerprint: str
    inat_links_fingerprint: str
    mo_links_fingerprint: str
    inat_token_marker: str
    mo_key_marker: str
    auth_generation: int
    mo_key_generation: int
    inat_rows: tuple[AuthoritativeLinkSnapshot, ...]
    mo_rows: tuple[AuthoritativeLinkSnapshot, ...]


def simulate_link_repair_final_state(
    preview: LinkRepairPreview,
    options: Sequence[LinkRepairOption],
    *,
    enforce: bool = True,
) -> dict[RemoteSite, tuple[tuple[str, Optional[int]], ...]]:
    """Apply selected primitives in memory and enforce the approved aggregate intent."""
    if not options:
        raise LinkRepairError(
            "Select at least one link repair action.", "empty_selection"
        )
    allowed = set(preview.options)
    if any(option not in allowed or not option.enabled for option in options):
        raise LinkRepairError(
            "The selection contains an unavailable preview action.", "invalid_selection"
        )
    rows: dict[RemoteSite, dict[str, tuple[str, Optional[int]]]] = {
        RemoteSite.INAT: {
            item.row_uuid or item.row_id: (item.parse_state, item.target_observation_id)
            for item in preview.inat_rows
        },
        RemoteSite.MO: {
            item.row_uuid or item.row_id: (item.parse_state, item.target_observation_id)
            for item in preview.mo_rows
        },
    }
    touched: set[tuple[RemoteSite, str]] = set()
    for index, option in enumerate(options, 1):
        operation = option.action_type.value.rsplit("_", 1)[-1]
        if preview.review_intent == "remove_only" and operation != "remove":
            raise LinkRepairError(
                "A remove-only review cannot authorize additions or substitutions.",
                "intent_mismatch",
            )
        identity = option.remote_row_uuid or option.remote_row_id
        if operation == "add":
            identity = f"new:{option.site.value}:{index}"
            rows[option.site][identity] = ("valid", option.desired_target_id)
            continue
        key = (option.site, identity)
        if not identity or key in touched or identity not in rows[option.site]:
            raise LinkRepairError(
                "Multiple or stale operations target the same exact row.",
                "invalid_selection",
            )
        touched.add(key)
        if rows[option.site][identity][0] == TARGET_UNKNOWN:
            raise LinkRepairError(
                "An imported MO link with an unreadable target cannot be changed safely.",
                "mo_target_unreadable",
            )
        if operation == "remove":
            del rows[option.site][identity]
        elif operation == "repair":
            rows[option.site][identity] = ("valid", option.desired_target_id)
        else:
            raise LinkRepairError(
                "Unsupported link action in preview.", "invalid_selection"
            )

    result: dict[RemoteSite, tuple[tuple[str, Optional[int]], ...]] = {}
    for site, values in rows.items():
        normalized = [
            (
                ("valid", target)
                if state in {"duplicate", "conflicting", "ambiguous"}
                and target is not None
                else (state, target)
            )
            for state, target in values.values()
        ]
        result[site] = tuple(
            sorted(normalized, key=lambda item: (item[0], item[1] or 0))
        )
    if enforce and preview.review_intent == "reciprocal":
        expected = {
            RemoteSite.INAT: preview.mo_observation_id,
            RemoteSite.MO: preview.inat_observation_id,
        }
        for site in (RemoteSite.INAT, RemoteSite.MO):
            final = result[site]
            if final != (("valid", expected[site]),):
                raise LinkRepairError(
                    "The selected actions do not produce exactly one clean reciprocal link on each site.",
                    "incomplete_final_state",
                )
    return result


class LinkRepairService:
    """Synchronous service intended to run only in the action worker pool."""

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

    def prepare_preview(
        self,
        profile_id: int,
        *,
        pair_id: Optional[int] = None,
        issue_id: Optional[int] = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> LinkRepairPreview:
        """Refresh both records/resources and construct a memory-only preview."""
        if (pair_id is None) == (issue_id is None):
            raise LinkRepairError(
                "Select exactly one confirmed pair or reviewed link issue."
            )
        profile = self.db.profile(profile_id)
        pair: Optional[dict[str, Any]] = None
        issue: Optional[dict[str, Any]] = None
        source_kind: str
        source_fingerprint: str
        allow_changes = False
        intent = "reciprocal"
        if pair_id is not None:
            pair = self.db.pair_detail(profile_id, pair_id)
            if not pair or pair.get("review_state") != "confirmed":
                raise LinkRepairError(
                    "Only a currently confirmed pair can produce link additions."
                )
            if pair.get("excluded"):
                raise LinkRepairError(
                    "An excluded pair cannot produce a remote link action."
                )
            mo_id = int(pair["mo_observation_id"])
            inat_id = int(pair["inat_observation_id"])
            source_kind = "pair"
            source_fingerprint = pair_source_fingerprint(pair)
        else:
            issue = self.db.issue_detail(profile_id, int(issue_id))
            review = self.db.link_issue_review(profile_id, int(issue_id))
            if not issue or not review:
                raise LinkRepairError(
                    "This link issue must be explicitly reviewed again before it can produce actions."
                )
            mo_id = _positive_int(review.get("mo_observation_id")) or 0
            inat_id = _positive_int(review.get("inat_observation_id")) or 0
            if not mo_id or not inat_id:
                raise LinkRepairError(
                    "The reviewed issue does not identify both exact observations."
                )
            source_kind = "issue"
            source_fingerprint = _review_fingerprint(review)
            allow_changes = True
            intent = str(review.get("review_intent") or "remove_only")

        if intent == "reciprocal":
            conflict = self.db.confirmed_pair_conflict(profile_id, mo_id, inat_id)
            if conflict:
                raise LinkRepairError(
                    "A different confirmed pair already uses one of these records; resolve the one-to-one conflict first.",
                    "one_to_one_conflict",
                )

        state = self._refresh_state(
            profile, mo_id, inat_id, cancelled, require_mo_key=False
        )
        warnings: list[str] = []
        options = self._options(
            state, allow_changes=allow_changes, intent=intent, warnings=warnings
        )
        if not options and not warnings:
            # Only claim this when nothing was BLOCKED. _options() also produces
            # no options when a site has incorrect, duplicate, or malformed rows
            # it refuses to touch without an explicit issue review; announcing
            # "already correct" there contradicted the warning printed directly
            # above it and told the user a broken link was fine.
            warnings.append(
                "Both authoritative resources already have the requested reciprocal final state."
            )
        return LinkRepairPreview(
            profile_id=profile_id,
            source_kind=source_kind,
            review_intent=intent,
            auth_generation=state.auth_generation,
            mo_key_generation=state.mo_key_generation,
            pair_id=pair_id,
            issue_id=issue_id,
            source_fingerprint=source_fingerprint,
            mo_observation_id=mo_id,
            inat_observation_id=inat_id,
            inat_observation_uuid=state.inat_observation_uuid,
            inat_field_id=state.inat_field_id,
            mo_external_site_id=state.mo_external_site_id,
            inat_record_fingerprint=state.inat_record_fingerprint,
            mo_record_fingerprint=state.mo_record_fingerprint,
            inat_links_fingerprint=state.inat_links_fingerprint,
            mo_links_fingerprint=state.mo_links_fingerprint,
            inat_rows=state.inat_rows,
            mo_rows=state.mo_rows,
            options=tuple(options),
            warnings=tuple(warnings),
        )

    def prepare_consolidation_add(
        self,
        profile_id: int,
        group_id: int,
        site: RemoteSite,
        cancelled: Callable[[], bool],
    ) -> tuple[LinkRepairOption, dict[str, Any]]:
        """Public Gate 2B bridge for one additive canonical-link action.

        Existing noncanonical rows are deliberately retained. The returned
        snapshot is complete enough for the owning consolidation service to
        journal exactly one normal Gate 1B action before calling
        :meth:`execute_journaled_action`.
        """
        ledger = self.db.consolidation_ledger_for_group(profile_id, group_id)
        group = self.db.action_group(profile_id, group_id)
        if not ledger or not group:
            raise LinkRepairError(
                "The consolidation ledger is missing.", "source_missing"
            )
        mo_id = _positive_int(ledger.get("canonical_mo_observation_id"))
        inat_id = _positive_int(ledger.get("canonical_inat_observation_id"))
        if not mo_id or not inat_id:
            raise LinkRepairError(
                "A reciprocal-link action requires both canonical observations.",
                "canonical_pair_incomplete",
            )
        self._require_consolidation_identity(
            profile_id,
            ledger,
            group,
            mo_id,
            inat_id,
        )
        profile = self.db.profile(profile_id)
        live = self._refresh_state(
            profile,
            mo_id,
            inat_id,
            cancelled,
            require_mo_key=site is RemoteSite.MO,
        )
        rows = live.inat_rows if site is RemoteSite.INAT else live.mo_rows
        desired = mo_id if site is RemoteSite.INAT else inat_id
        already_present = any(
            row.parse_state != "malformed" and row.target_observation_id == desired
            for row in rows
        )
        option = self._option(site, "add", live, None, desired)
        return option, {
            "already_present": already_present,
            "inat_observation_uuid": live.inat_observation_uuid,
            "inat_record_fingerprint": live.inat_record_fingerprint,
            "mo_record_fingerprint": live.mo_record_fingerprint,
            "inat_links_fingerprint": live.inat_links_fingerprint,
            "mo_links_fingerprint": live.mo_links_fingerprint,
            "inat_field_id": live.inat_field_id,
            "mo_external_site_id": live.mo_external_site_id,
        }

    def execute_journaled_action(
        self,
        profile_id: int,
        row: dict[str, Any],
        cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> LinkActionResult:
        """Execute one already-journaled link row through the full Gate 1B contract."""
        return self._execute_action(profile_id, row, cancelled, progress)

    def execute_group(
        self,
        profile_id: int,
        group_id: int,
        cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> list[LinkActionResult]:
        """Run pending actions serially, stopping on failure or uncertainty."""
        results: list[LinkActionResult] = []
        group_preflight_complete = False
        for ordered in self.db.action_group_rows(profile_id, group_id):
            # Re-read each row instead of executing the snapshot taken before
            # the first write. Every action in this loop mutates its pending
            # siblings: advance_pending_action_fingerprints() rewrites their
            # preview_* fingerprints so the next action can observe the verified
            # result of the previous one, and verify_unknown() settles states.
            # Reading the pre-run snapshot threw all of that away, so the second
            # action of an ordinary two-link reciprocal repair compared live
            # remote state against fingerprints predating its own predecessor's
            # write and died with "The authoritative remote state changed after
            # preview" — the MO link was never sent. ConsolidationService's
            # execute_group already re-reads per step, which is why only the
            # plain Gate 1B path was affected.
            row = self.db.action(profile_id, int(ordered["action_id"])) or ordered
            state = str(row["state"])
            if state == "succeeded":
                continue
            if cancelled() and state == "pending":
                self.db.cancel_action_group_tail(
                    profile_id,
                    group_id,
                    int(row["ordinal"]) - 1,
                    "credential_or_user_cancellation",
                )
                results.append(
                    LinkActionResult(
                        int(row["action_id"]),
                        "cancelled",
                        "Authentication, credentials, or user cancellation prevented all remaining writes.",
                    )
                )
                self._mark_group_stale_if_written(profile_id, group_id)
                break
            if state in {"failed", "cancelled"}:
                self.db.cancel_action_group_tail(
                    profile_id,
                    group_id,
                    int(row["ordinal"]),
                    (
                        "predecessor_failed"
                        if state == "failed"
                        else "predecessor_cancelled"
                    ),
                )
                results.append(
                    LinkActionResult(
                        int(row["action_id"]),
                        state,
                        "A failed or cancelled predecessor blocks all later actions; create a fresh preview.",
                    )
                )
                self._mark_group_stale_if_written(profile_id, group_id)
                break
            if state == "outcome_unknown":
                result = self.verify_unknown(
                    profile_id, int(row["action_id"]), cancelled
                )
            elif state == "pending":
                if not group_preflight_complete:
                    try:
                        self._preflight_group(profile_id, group_id, cancelled, progress)
                    except ReconciliationCancelled:
                        self.db.cancel_action_group_tail(
                            profile_id,
                            group_id,
                            int(row["ordinal"]) - 1,
                            "credential_or_user_cancellation",
                        )
                        results.append(
                            LinkActionResult(
                                int(row["action_id"]),
                                "cancelled",
                                "Cancelled during whole-group preflight; no write was sent.",
                            )
                        )
                        self._mark_group_stale_if_written(profile_id, group_id)
                        break
                    except Exception as exc:
                        code = str(getattr(exc, "code", "") or "group_preflight_failed")
                        self.db.fail_pending_action_and_cancel_tail(
                            profile_id,
                            group_id,
                            int(row["action_id"]),
                            int(row["ordinal"]),
                            code,
                        )
                        message = (
                            str(exc)
                            if isinstance(
                                exc, (LinkRepairError, MOAPIError, INatAPIError)
                            )
                            else "Whole-group preflight failed before any new write was sent."
                        )
                        results.append(
                            LinkActionResult(int(row["action_id"]), "failed", message)
                        )
                        self._mark_group_stale_if_written(profile_id, group_id)
                        break
                    group_preflight_complete = True
                result = self._execute_action(profile_id, row, cancelled, progress)
            else:
                continue
            results.append(result)
            if result.state != "succeeded":
                if result.state in {"failed", "cancelled"}:
                    self.db.cancel_action_group_tail(
                        profile_id,
                        group_id,
                        int(row["ordinal"]),
                        (
                            "predecessor_failed"
                            if result.state == "failed"
                            else "predecessor_cancelled"
                        ),
                    )
                self._mark_group_stale_if_written(profile_id, group_id)
                break
        if any(
            item.get("write_started_at")
            for item in self.db.action_group_rows(profile_id, group_id)
        ):
            self._mark_group_stale_if_written(profile_id, group_id)
            if (
                results
                and results[-1].state == "succeeded"
                and "read-only scan" not in results[-1].message
            ):
                final = results[-1]
                results[-1] = LinkActionResult(
                    final.action_id,
                    final.state,
                    final.message
                    + " Run a read-only scan to refresh local reconciliation state.",
                )
        return results

    def _preflight_group(
        self,
        profile_id: int,
        group_id: int,
        cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> None:
        """Validate every deterministic prerequisite before the first pending write."""
        rows = self.db.action_group_rows(profile_id, group_id)
        pending = [item for item in rows if str(item["state"]) == "pending"]
        if not pending:
            return
        anchor = pending[0]
        self._require_current_source(profile_id, anchor)
        profile = self.db.profile(profile_id)
        require_mo_key = any(str(item["site"]) == "mo" for item in pending)
        progress(
            "Validating both accounts, bindings, owners, credentials, and link resources"
        )
        live = self._refresh_state(
            profile,
            int(anchor["mo_observation_id"]),
            int(anchor["inat_observation_id"]),
            cancelled,
            require_mo_key=require_mo_key,
        )
        if any(
            int(item["mo_observation_id"]) != live.mo_observation_id
            or int(item["inat_observation_id"]) != live.inat_observation_id
            for item in pending
        ):
            raise LinkRepairError(
                "The journal group contains inconsistent observation identities.",
                "journal_identity_mismatch",
            )
        if all(self._matches_preview(item, live) for item in pending):
            return
        # Preserve idempotency when the complete selected final state was
        # reached independently after preview. Per-action execution below will
        # record each operation as already correct without submitting writes.
        if all(self._action_satisfied(profile_id, item, live) for item in pending):
            return
        raise LinkRepairError(
            "The authoritative remote state changed after preview. No write was sent.",
            "stale_preview",
        )

    def _mark_group_stale_if_written(self, profile_id: int, group_id: int) -> None:
        rows = self.db.action_group_rows(profile_id, group_id)
        if not any(item.get("write_started_at") for item in rows):
            return
        group = self.db.action_group(profile_id, group_id)
        if group:
            self.db.mark_link_reconciliation_stale(
                profile_id,
                int(group["mo_observation_id"]),
                int(group["inat_observation_id"]),
            )

    def verify_unknown(
        self,
        profile_id: int,
        action_id: int,
        cancelled: Callable[[], bool],
    ) -> LinkActionResult:
        row = self.db.action(profile_id, action_id)
        if not row or row.get("state") != "outcome_unknown":
            raise LinkRepairError("Only an outcome-unknown action can be verified.")
        try:
            profile = self.db.profile(profile_id)
            live = self._refresh_state(
                profile,
                int(row["mo_observation_id"]),
                int(row["inat_observation_id"]),
                cancelled,
                require_mo_key=False,
                verification_only=True,
            )
            if self._action_satisfied(profile_id, row, live):
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "succeeded",
                    phase="verification",
                    verification_state="verified_final_state",
                )
                local_ok = self._record_verified_local(
                    profile_id, row, live, remote_write=True
                )
                return LinkActionResult(
                    action_id,
                    "succeeded",
                    "Final remote state is verified."
                    + (
                        ""
                        if local_ok
                        else " Run a read-only scan to refresh local presentation state."
                    ),
                )
            unchanged = (
                live.inat_links_fingerprint == row["preview_inat_links_fingerprint"]
                and live.mo_links_fingerprint == row["preview_mo_links_fingerprint"]
            )
            if unchanged:
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "failed",
                    phase="verification",
                    error_code="verified_not_applied",
                    verification_state="verified_not_applied",
                )
                return LinkActionResult(
                    action_id,
                    "failed",
                    "Verification shows that the write was not applied.",
                )
            return LinkActionResult(
                action_id,
                "outcome_unknown",
                "Remote state changed but does not prove this action's result; create a fresh preview.",
            )
        finally:
            # Direct verification can target a non-final action and may run
            # without execute_group()'s normal final cleanup.
            self._mark_group_stale_if_written(profile_id, int(row["action_group_id"]))

    def _execute_action(
        self,
        profile_id: int,
        row: dict[str, Any],
        cancelled: Callable[[], bool],
        progress: Callable[[str], None],
    ) -> LinkActionResult:
        action_id = int(row["action_id"])
        write_started = False
        if not self.db.claim_action(profile_id, action_id, "account_preflight"):
            current = self.db.action(profile_id, action_id) or row
            return LinkActionResult(
                action_id,
                str(current["state"]),
                "Action was not pending.",
            )
        try:
            if cancelled():
                raise ReconciliationCancelled("Link action cancelled")
            profile = self.db.profile(profile_id)
            self._require_current_source(profile_id, row)
            progress(f"Action {action_id}: refreshing both records and link resources")
            live = self._refresh_state(
                profile,
                int(row["mo_observation_id"]),
                int(row["inat_observation_id"]),
                cancelled,
                require_mo_key=str(row["site"]) == "mo",
            )
            if self._action_satisfied(profile_id, row, live):
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "succeeded",
                    phase="verification",
                    verification_state="already_correct",
                )
                local_ok = self._record_verified_local(
                    profile_id, row, live, remote_write=False
                )
                return LinkActionResult(
                    action_id,
                    "succeeded",
                    "Already correct; no write was sent."
                    + (
                        ""
                        if local_ok
                        else " Run a read-only scan to refresh local presentation state."
                    ),
                )
            if not self._matches_preview(row, live):
                raise LinkRepairError(
                    "The authoritative remote state changed after preview. No write was sent.",
                    "stale_preview",
                )
            if not self.db.mark_action_write_started(profile_id, action_id):
                raise LinkRepairError(
                    "This action left its claimed state before the write boundary. "
                    "No write was sent.",
                    "write_boundary_lost",
                )
            write_started = True
            progress(
                f"Action {action_id}: submitting one explicitly confirmed link write"
            )
            write_error: Optional[Exception] = None
            http_status: Optional[int] = None
            try:
                response = self._write(row, profile, live, cancelled)
                metadata = getattr(response, "metadata", None)
                http_status = getattr(metadata, "status_code", None)
            except ReconciliationCancelled:
                # MOClient._write does its rate-limit wait (up to five seconds)
                # and its final cancellation check strictly BEFORE issuing the
                # request, so this can only mean no write left the process --
                # and the iNaturalist client never raises it at all. Hand the
                # never-written marker back before the outer handler settles
                # this 'cancelled', otherwise the row claims a write began and
                # _mark_group_stale_if_written marks the pair stale and opens a
                # "read-only link refresh required" issue for a request that was
                # never sent.
                if self.db.clear_action_write_boundary(profile_id, action_id):
                    write_started = False
                raise
            except (INatAPIError, MOAPIError) as exc:
                write_error = exc
                http_status = getattr(exc, "status_code", None)

            progress(f"Action {action_id}: verifying authoritative final state")
            try:
                verified = self._refresh_state(
                    profile,
                    int(row["mo_observation_id"]),
                    int(row["inat_observation_id"]),
                    # Once a write has begun, cancellation may stop later
                    # actions but must not interrupt mandatory verification.
                    lambda: False,
                    require_mo_key=False,
                    verification_only=True,
                )
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
                return LinkActionResult(
                    action_id,
                    "outcome_unknown",
                    "The write may have been submitted, but final-state verification is unavailable.",
                )
            if self._action_satisfied(profile_id, row, verified):
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "succeeded",
                    phase="verification",
                    http_status=http_status,
                    verification_state="verified_final_state",
                )
                local_ok = self._record_verified_local(
                    profile_id, row, verified, remote_write=True
                )
                return LinkActionResult(
                    action_id,
                    "succeeded",
                    "Verified final state."
                    + (
                        ""
                        if local_ok
                        else " Run a read-only scan to refresh local presentation state."
                    ),
                )
            verified_unchanged = (
                verified.inat_links_fingerprint == live.inat_links_fingerprint
                and verified.mo_links_fingerprint == live.mo_links_fingerprint
            )
            if verified_unchanged:
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "failed",
                    phase="verification",
                    error_code="verified_not_applied",
                    http_status=http_status,
                    verification_state="verified_not_applied",
                )
                return LinkActionResult(
                    action_id,
                    "failed",
                    "Verification shows that the write was not applied.",
                )
            if write_error is not None and bool(
                getattr(write_error, "outcome_unknown", False)
            ):
                self.db.finish_action(
                    profile_id,
                    action_id,
                    "outcome_unknown",
                    phase="verification",
                    error_code="write_outcome_unknown",
                    http_status=http_status,
                    verification_state="not_proven",
                )
                return LinkActionResult(
                    action_id, "outcome_unknown", "Write outcome remains unknown."
                )
            code = str(
                getattr(write_error, "error_code", "") or "verification_mismatch"
            )
            self.db.finish_action(
                profile_id,
                action_id,
                "failed",
                phase="verification",
                error_code=code,
                http_status=http_status,
                verification_state="final_state_not_achieved",
            )
            return LinkActionResult(
                action_id, "failed", "The requested final state was not achieved."
            )
        except ReconciliationCancelled:
            self.db.finish_action(
                profile_id,
                action_id,
                "cancelled",
                phase="resource_preflight",
                error_code="user_cancelled",
            )
            return LinkActionResult(
                action_id, "cancelled", "Cancelled before the write was sent."
            )
        except LinkRepairError as exc:
            self.db.finish_action(
                profile_id,
                action_id,
                "failed",
                phase="resource_preflight",
                error_code=exc.code,
            )
            return LinkActionResult(action_id, "failed", str(exc))
        except Exception:
            # Previously fully silent: the caller only ever saw the generic
            # "Preflight failed" message below, with no way to diagnose a real
            # bug versus a genuine remote-state problem. Log the traceback
            # (metadata-only modules elsewhere in this app avoid logging
            # payload content, but this except clause never had payload data
            # in scope to begin with).
            log.exception(
                "Link action %s: unexpected error during preflight/verification",
                action_id,
            )
            current = self.db.action(profile_id, action_id)
            if current and current.get("state") == "succeeded":
                return LinkActionResult(
                    action_id,
                    "succeeded",
                    "Remote final state was verified; run a read-only scan to refresh local presentation state.",
                )
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
            return LinkActionResult(
                action_id,
                terminal,
                (
                    "The write outcome must be verified before any retry."
                    if write_started
                    else "Preflight failed before a write was sent."
                ),
            )

    def _record_verified_local(
        self,
        profile_id: int,
        row: dict[str, Any],
        live: _LiveState,
        *,
        remote_write: bool,
    ) -> bool:
        """Best-effort presentation refresh after the durable terminal outcome."""
        try:
            self.db.advance_pending_action_fingerprints(
                profile_id, int(row["action_group_id"]), live
            )
            self.db.refresh_authoritative_link_rows(profile_id, live)
            if remote_write:
                group_rows = self.db.action_group_rows(
                    profile_id, int(row["action_group_id"])
                )
                is_last = bool(group_rows) and int(row["ordinal"]) == max(
                    int(item["ordinal"]) for item in group_rows
                )
                if is_last:
                    self.db.mark_link_reconciliation_stale(
                        profile_id, live.mo_observation_id, live.inat_observation_id
                    )
                    return False
            return True
        except Exception:
            # The remote outcome is already durably verified. A normal
            # read-only scan can rebuild this presentation state safely.
            return False

    def _require_current_source(self, profile_id: int, row: dict[str, Any]) -> None:
        group = self.db.action_group(profile_id, int(row["action_group_id"]))
        if not group:
            raise LinkRepairError(
                "The journal action group no longer exists.", "source_missing"
            )
        consolidation = self.db.consolidation_ledger_for_group(
            profile_id, int(row["action_group_id"])
        )
        if consolidation:
            self._require_valid_consolidation_link(
                profile_id,
                row,
                group,
                consolidation,
            )
            return
        if str(group.get("source_kind")) == "creation":
            # Gate 2A (section 8): a creation-saga group's own pair_id/
            # issue_id columns are always NULL (the group is journaled
            # before any destination id — hence any pair — exists at all;
            # see _migration_v9's conditional CHECK), so neither branch
            # below applies and without this explicit branch the confirmed-
            # pair/staleness check below would silently no-op for every
            # creation-saga link action. This is the narrow, explicit
            # exemption instead — every one of the following must hold, not
            # a broad "any pair is fine":
            self._require_valid_creation_link_exemption(profile_id, row, group)
            return
        if group.get("pair_id") is not None:
            pair = self.db.pair_detail(profile_id, int(group["pair_id"]))
            if (
                not pair
                or pair.get("review_state") != "confirmed"
                or pair.get("excluded")
            ):
                raise LinkRepairError(
                    "The source pair is no longer confirmed and eligible.",
                    "pair_changed",
                )
            if pair_source_fingerprint(pair) != group.get("source_fingerprint"):
                raise LinkRepairError(
                    "The confirmed pair changed after preview.", "pair_changed"
                )
        elif group.get("issue_id") is not None:
            review = self.db.link_issue_review(profile_id, int(group["issue_id"]))
            if not review:
                raise LinkRepairError(
                    "The reviewed link issue changed after preview.", "issue_changed"
                )
            if (
                _review_fingerprint(review) != group.get("source_fingerprint")
                or int(review.get("mo_observation_id") or 0)
                != int(group["mo_observation_id"])
                or int(review.get("inat_observation_id") or 0)
                != int(group["inat_observation_id"])
            ):
                raise LinkRepairError(
                    "The exact issue identity decision changed after preview.",
                    "issue_changed",
                )
            if str(
                review.get("review_intent")
            ) == "reciprocal" and self.db.confirmed_pair_conflict(
                profile_id,
                int(group["mo_observation_id"]),
                int(group["inat_observation_id"]),
            ):
                raise LinkRepairError(
                    "A new one-to-one pair conflict appeared after preview.",
                    "one_to_one_conflict",
                )

    def _require_consolidation_identity(
        self,
        profile_id: int,
        ledger: dict[str, Any],
        group: dict[str, Any],
        mo_id: int,
        inat_id: int,
    ) -> None:
        if (
            int(ledger.get("profile_id") or 0) != profile_id
            or str(ledger.get("state") or "") != "pending"
            or str(ledger.get("consolidation_state") or "")
            not in {"draft", "confirmed", "finalized"}
            or int(group.get("mo_observation_id") or 0) != mo_id
            or int(group.get("inat_observation_id") or 0) != inat_id
            or str(group.get("source_fingerprint") or "")
            != str(ledger.get("canonical_pair_fingerprint") or "")
        ):
            raise LinkRepairError(
                "The immutable consolidation identity changed after approval.",
                "consolidation_changed",
            )
        pair_id = ledger.get("canonical_pair_id")
        if pair_id is None:
            raise LinkRepairError(
                "The canonical pair has not been prepared.", "pair_changed"
            )
        pair = self.db.pair_detail(profile_id, int(pair_id))
        if (
            not pair
            or int(pair.get("mo_observation_id") or 0) != mo_id
            or int(pair.get("inat_observation_id") or 0) != inat_id
            or str(pair.get("review_state") or "") not in {"provisional", "confirmed"}
            or pair.get("excluded")
        ):
            raise LinkRepairError(
                "The canonical pair changed after approval.", "pair_changed"
            )
        members = self.db.list_consolidation_members(
            profile_id, int(ledger["consolidation_id"])
        )
        member_mo_ids = {
            int(member["observation_id"])
            for member in members
            if member["site"] == "mo"
        }
        member_inat_ids = {
            int(member["observation_id"])
            for member in members
            if member["site"] == "inat"
        }
        for confirmed_mo, confirmed_inat in self.db.confirmed_pair_keys(profile_id):
            if confirmed_mo != mo_id and confirmed_inat != inat_id:
                continue
            if (
                confirmed_mo not in member_mo_ids
                or confirmed_inat not in member_inat_ids
            ):
                raise LinkRepairError(
                    "A confirmed pair outside this duplicate set now conflicts with the "
                    "canonical pair.",
                    "one_to_one_conflict",
                )

    def _require_valid_consolidation_link(
        self,
        profile_id: int,
        row: dict[str, Any],
        group: dict[str, Any],
        ledger: dict[str, Any],
    ) -> None:
        if str(row.get("action_type") or "") not in (
            LinkActionType.MO_EXTERNAL_LINK_ADD.value,
            LinkActionType.INAT_OFV_ADD.value,
        ):
            raise LinkRepairError(
                "A consolidation may only use additive reciprocal-link actions.",
                "consolidation_action_not_permitted",
            )
        mo_id = int(row["mo_observation_id"])
        inat_id = int(row["inat_observation_id"])
        if (
            mo_id != int(ledger.get("canonical_mo_observation_id") or 0)
            or inat_id != int(ledger.get("canonical_inat_observation_id") or 0)
            or row.get("pair_id") is None
            or int(row["pair_id"]) != int(ledger.get("canonical_pair_id") or -1)
        ):
            raise LinkRepairError(
                "The action does not target the approved canonical pair.",
                "pair_changed",
            )
        self._require_consolidation_identity(
            profile_id,
            ledger,
            group,
            mo_id,
            inat_id,
        )

    def _require_valid_creation_link_exemption(
        self,
        profile_id: int,
        row: dict[str, Any],
        group: dict[str, Any],
    ) -> None:
        """Section 8: the Gate 2A creation-saga narrow exemption from the
        ordinary confirmed-pair check, made explicit and checked in full —
        never a broad early return. Every condition below must hold.
        """
        action_type = str(row.get("action_type") or "")
        # Only the two reciprocal-link action types are ever minted inside a
        # creation saga's action group (both as the bootstrap links AND, for
        # inat_ofv_add, as identifier population items) — no other type may
        # ever claim this exemption, even if a row somehow carries
        # source_kind='creation'.
        if action_type not in (
            LinkActionType.MO_EXTERNAL_LINK_ADD.value,
            LinkActionType.INAT_OFV_ADD.value,
        ):
            raise LinkRepairError(
                f"Action type '{action_type}' is not permitted to use the creation-saga link "
                f"exemption.",
                "creation_exemption_type_not_permitted",
            )
        ledger = self.db.creation_ledger_for_group(
            profile_id, int(row["action_group_id"])
        )
        if not ledger or int(ledger.get("profile_id") or 0) != profile_id:
            raise LinkRepairError(
                "The creation ledger row is missing.", "source_missing"
            )
        row_pair_id = row.get("pair_id")
        if row_pair_id is None or int(row_pair_id) != int(ledger.get("pair_id") or -1):
            raise LinkRepairError(
                "The action's pair does not match the creation ledger's pair.",
                "pair_changed",
            )
        pair = self.db.pair_detail(profile_id, int(row_pair_id))
        if not pair:
            raise LinkRepairError(
                "The creation saga's pair no longer exists.", "pair_changed"
            )
        mo_id = int(row["mo_observation_id"])
        inat_id = int(row["inat_observation_id"])
        # IDs must agree across the action row, the action group, AND the
        # creation ledger/identity -- not just the pair.
        if (
            int(pair.get("mo_observation_id") or 0) != mo_id
            or int(pair.get("inat_observation_id") or 0) != inat_id
            or int(group.get("mo_observation_id") or 0) != mo_id
            or int(group.get("inat_observation_id") or 0) != inat_id
        ):
            raise LinkRepairError(
                "The creation saga's pair changed since this action was minted.",
                "pair_changed",
            )
        source_site = str(ledger.get("source_site") or "")
        destination_id = mo_id if source_site == "inat" else inat_id
        expected_destination = ledger.get("destination_observation_id")
        # Once the identity has a real destination id on record, every
        # action must agree with it -- a stale/legacy row pointing at a
        # different (e.g. superseded) destination is never permitted.
        if (
            expected_destination is not None
            and int(expected_destination) != destination_id
        ):
            raise LinkRepairError(
                "The action's destination id does not match the creation identity's recorded "
                "destination.",
                "pair_changed",
            )
        if pair.get("excluded"):
            raise LinkRepairError(
                "The creation saga's pair is excluded.", "pair_changed"
            )
        if self.db.confirmed_pair_conflict(profile_id, mo_id, inat_id):
            raise LinkRepairError(
                "A different confirmed pair already claims one of these records.",
                "one_to_one_conflict",
            )
        review_state = pair.get("review_state")
        # Distinguish the two reciprocal-link BOOTSTRAP actions (which
        # necessarily run while the pair is still provisional -- finalize
        # cannot succeed before they exist) from a POPULATION item that
        # happens to reuse the same action_type (inat_ofv_add for an
        # identifier item): population requires the pair already confirmed.
        is_population_item = (
            self.db.creation_item_for_action(profile_id, int(row["action_id"]))
            is not None
        )
        if is_population_item:
            if review_state != "confirmed":
                raise LinkRepairError(
                    "This population item cannot execute until the creation saga's pair is confirmed.",
                    "pair_not_confirmed",
                )
        elif review_state not in ("provisional", "confirmed"):
            raise LinkRepairError(
                "The creation saga's pair is neither provisional nor confirmed.",
                "pair_changed",
            )

    def _refresh_state(
        self,
        profile: ReconciliationProfile,
        mo_id: int,
        inat_id: int,
        cancelled: Callable[[], bool],
        *,
        require_mo_key: bool,
        verification_only: bool = False,
    ) -> _LiveState:
        if cancelled():
            raise ReconciliationCancelled("Link action cancelled")
        auth = self.auth_provider()
        auth_generation = self.auth_generation_provider()
        mo_key_generation = self.mo_key_generation_provider()
        token = auth.api_token if auth.is_authenticated else ""
        if not token and not verification_only:
            raise LinkRepairError(
                "iNaturalist authentication must match the selected reconciliation account.",
                "inat_auth_mismatch",
            )
        if not verification_only:
            current_user = _first_result(self.inat_client.get_current_user_v2(token))
            if (
                _positive_int(current_user.get("id") if current_user else None)
                != profile.inat_user_id
            ):
                raise LinkRepairError(
                    "The authenticated iNaturalist numeric account does not match the profile.",
                    "inat_auth_mismatch",
                )

        reader = INatReconciliationReader(self.inat_client)
        definitions = reader.resolve_field_definitions(MO_FIELD_NAME)
        stored = self.db.field_binding(profile.profile_id, "mo_url")
        if stored is None or str(stored["verification_state"]) != "verified":
            raise LinkRepairError(
                "Verify the exact Mushroom Observer URL field binding before previewing repairs.",
                "inat_field_unverified",
            )
        field_id = int(stored["field_id"])
        matches = [
            item for item in definitions if _positive_int(item.get("id")) == field_id
        ]
        if len(matches) != 1:
            raise LinkRepairError(
                "The stored iNaturalist field binding is no longer valid.",
                "inat_field_changed",
            )

        inat_raw = _first_result(
            self.inat_client.get_reconciliation_detail(inat_id, token, deep=False)
        )
        if not inat_raw or _positive_int(inat_raw.get("id")) != inat_id:
            raise LinkRepairError(
                "The iNaturalist observation is unavailable.",
                "inat_observation_unavailable",
            )
        inat_uuid = str(inat_raw.get("uuid") or "").strip()
        if not inat_uuid:
            raise LinkRepairError(
                "The iNaturalist observation UUID is unavailable.",
                "inat_uuid_unavailable",
            )
        inat_user = (
            inat_raw.get("user") if isinstance(inat_raw.get("user"), dict) else {}
        )
        if _positive_int(inat_user.get("id")) != profile.inat_user_id:
            raise LinkRepairError(
                "The iNaturalist observation is not owned by the selected account.",
                "inat_owner_changed",
            )
        if _fungi_status(inat_raw) == "nonfungal":
            raise LinkRepairError(
                "The iNaturalist observation is now known to be outside Fungi.",
                "inat_out_of_scope",
            )

        sites = [
            row
            for row in results_from_payload(self.mo_client.external_sites(cancelled))
            if _is_inat_site(row)
        ]
        site_ids = {_positive_int(row.get("id")) for row in sites} - {None}
        if len(site_ids) != 1:
            raise LinkRepairError(
                "Mushroom Observer has no unique iNaturalist external-site definition.",
                "mo_site_unavailable",
            )
        external_site_id = int(next(iter(site_ids)))
        mo_raw = _first_result(
            self.mo_client.observation(mo_id, cancelled, detail="low")
        )
        if not mo_raw or _positive_int(mo_raw.get("id")) != mo_id:
            raise LinkRepairError(
                "The Mushroom Observer observation is unavailable.",
                "mo_observation_unavailable",
            )
        mo_record = parse_mo_observation(mo_raw, profile.mo_user_id)
        if mo_record.owner_id != profile.mo_user_id:
            raise LinkRepairError(
                "The Mushroom Observer observation is not owned by the selected account.",
                "mo_owner_changed",
            )
        if mo_record.fungi_status == "nonfungal":
            raise LinkRepairError(
                "The Mushroom Observer observation is now known to be outside Fungi.",
                "mo_out_of_scope",
            )
        inat_date = _parse_date(inat_raw.get("observed_on"))
        mo_date = mo_record.observed_on
        if inat_date and mo_date and abs((inat_date - mo_date).days) > 1:
            raise LinkRepairError(
                "The freshly read observation dates differ by more than one day; review the pair again.",
                "observed_date_conflict",
            )

        key = self.mo_key_provider(profile.profile_id) if require_mo_key else ""
        if require_mo_key:
            if not key:
                raise LinkRepairError(
                    "A Mushroom Observer API key is required for this selected action.",
                    "mo_key_missing",
                )
            user_id = self.mo_client.authenticated_user_id(
                key, profile.mo_user_id, cancelled
            )
            if user_id != profile.mo_user_id:
                raise LinkRepairError(
                    "The Mushroom Observer API key does not match this profile.",
                    "mo_key_mismatch",
                )

        inat_rows = _inat_rows(inat_raw, field_id, inat_id)
        mo_payload = self.mo_client.external_links((mo_id,), cancelled)
        mo_rows = _mo_rows(mo_payload, external_site_id, mo_id)
        if not verification_only and (
            auth_generation != self.auth_generation_provider()
            or mo_key_generation != self.mo_key_generation_provider()
        ):
            raise LinkRepairError(
                "Authentication or credential state changed during preflight.",
                "credential_context_changed",
            )
        return _LiveState(
            profile_id=profile.profile_id,
            mo_observation_id=mo_id,
            inat_observation_id=inat_id,
            inat_observation_uuid=inat_uuid,
            inat_field_id=field_id,
            mo_external_site_id=external_site_id,
            inat_record_fingerprint=_inat_record_fingerprint(inat_raw),
            mo_record_fingerprint=mo_record_fingerprint(mo_raw),
            inat_links_fingerprint=_rows_fingerprint(inat_rows),
            mo_links_fingerprint=_rows_fingerprint(mo_rows),
            inat_token_marker=public_fingerprint(token),
            mo_key_marker=public_fingerprint(key) if key else "",
            auth_generation=auth_generation,
            mo_key_generation=mo_key_generation,
            inat_rows=inat_rows,
            mo_rows=mo_rows,
        )

    def _options(
        self,
        state: _LiveState,
        *,
        allow_changes: bool,
        intent: str,
        warnings: list[str],
    ) -> list[LinkRepairOption]:
        result: list[LinkRepairOption] = []
        desired = {
            RemoteSite.INAT: state.mo_observation_id,
            RemoteSite.MO: state.inat_observation_id,
        }
        for site, rows in (
            (RemoteSite.INAT, state.inat_rows),
            (RemoteSite.MO, state.mo_rows),
        ):
            expected = desired[site]
            correct = [
                row
                for row in rows
                if row.parse_state != "malformed"
                and row.target_observation_id == expected
            ]
            if not rows and intent == "reciprocal":
                result.append(self._option(site, "add", state, None, expected))
                continue
            if intent == "reciprocal" and len(rows) == 1 and len(correct) == 1:
                continue
            if rows and not allow_changes:
                warnings.append(
                    f"{site.value}: existing incorrect, duplicate, or malformed rows require explicit issue review."
                )
                continue
            for row in rows:
                is_correct = row in correct
                if not is_correct and intent == "reciprocal":
                    result.append(self._option(site, "repair", state, row, expected))
                # A remove-only review rejects the reviewed correspondence.
                # Its sole one-way row must therefore remain an explicit,
                # selectable removal even though it points at the reviewed
                # opposite observation.
                removable = (
                    intent == "remove_only" or not is_correct or len(correct) > 1
                )
                if removable:
                    result.append(self._option(site, "remove", state, row, None))
            if not rows and intent == "remove_only":
                warnings.append(
                    f"{site.value}: no authoritative row remains to remove."
                )
        return result

    def _option(
        self,
        site: RemoteSite,
        operation: str,
        state: _LiveState,
        row: Optional[AuthoritativeLinkSnapshot],
        desired: Optional[int],
    ) -> LinkRepairOption:
        prefix = "inat_ofv" if site is RemoteSite.INAT else "mo_external_link"
        action_type = LinkActionType(f"{prefix}_{operation}")
        current = row.target_observation_id if row else None
        if operation == "add":
            description = f"Add the missing {site.value} reciprocal link"
        else:
            row_identity = row.row_uuid or row.row_id if row else ""
            description = (
                f"{'Repair' if operation == 'repair' else 'Remove'} exact "
                f"{site.value} row {row_identity}"
            )
        enabled = True
        reason = ""
        if site is RemoteSite.INAT and operation != "add" and row and not row.row_uuid:
            enabled = False
            reason = "The API did not return the field-value UUID required for an exact write."
        if (
            site is RemoteSite.MO
            and operation != "add"
            and row
            and not _positive_int(row.row_id)
        ):
            enabled = False
            reason = "The API did not return the numeric external-link row ID required for an exact write."
        if site is RemoteSite.MO and row and row.parse_state == TARGET_UNKNOWN:
            enabled = False
            reason = "API2 does not expose this imported link's target identity; it cannot be repaired or removed safely."
        if site is RemoteSite.MO and not self.mo_key_provider(state.profile_id):
            enabled = False
            reason = "Enter a Mushroom Observer API key before selecting an MO write."
        return LinkRepairOption(
            action_type=action_type,
            site=site,
            description=description,
            destructive=operation in {"repair", "remove"},
            mo_observation_id=state.mo_observation_id,
            inat_observation_id=state.inat_observation_id,
            remote_row_id=row.row_id if row else "",
            remote_row_uuid=row.row_uuid if row else "",
            binding_id=(
                state.inat_field_id
                if site is RemoteSite.INAT
                else state.mo_external_site_id
            ),
            current_target_id=current,
            desired_target_id=desired,
            enabled=enabled,
            disabled_reason=reason,
        )

    def _write(
        self,
        row: dict[str, Any],
        profile: ReconciliationProfile,
        live: _LiveState,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        action = LinkActionType(str(row["action_type"]))
        auth = self.auth_provider()
        self._require_current_inat_auth(auth, live)
        if action is LinkActionType.INAT_OFV_ADD:
            return self.inat_client.create_reconciliation_field_value_v2(
                auth.api_token,
                str(row["inat_observation_uuid"]),
                int(row["binding_id"]),
                INAT_MO_URL.format(mo_id=int(row["mo_observation_id"])),
            )
        if action is LinkActionType.INAT_OFV_REPAIR:
            return self.inat_client.update_reconciliation_field_value_v2(
                auth.api_token,
                str(row["remote_row_uuid"]),
                str(row["inat_observation_uuid"]),
                int(row["binding_id"]),
                INAT_MO_URL.format(mo_id=int(row["mo_observation_id"])),
            )
        if action is LinkActionType.INAT_OFV_REMOVE:
            return self.inat_client.delete_reconciliation_field_value_v2(
                auth.api_token,
                str(row["remote_row_uuid"]),
            )
        key = self.mo_key_provider(profile.profile_id)
        if (
            not key
            or self.mo_key_generation_provider() != live.mo_key_generation
            or public_fingerprint(key) != live.mo_key_marker
        ):
            raise LinkRepairError(
                "The Mushroom Observer API key is no longer available.",
                "mo_key_missing",
            )
        if action is LinkActionType.MO_EXTERNAL_LINK_ADD:
            return self.mo_client.create_external_link(
                key,
                int(row["mo_observation_id"]),
                int(row["binding_id"]),
                MO_INAT_URL.format(inat_id=int(row["inat_observation_id"])),
                cancelled,
            )
        if action is LinkActionType.MO_EXTERNAL_LINK_REPAIR:
            return self.mo_client.update_external_link(
                key,
                int(row["remote_row_id"]),
                MO_INAT_URL.format(inat_id=int(row["inat_observation_id"])),
                cancelled,
            )
        return self.mo_client.delete_external_link(
            key, int(row["remote_row_id"]), cancelled
        )

    def _require_current_inat_auth(self, auth: AuthState, live: _LiveState) -> None:
        if (
            not auth.api_token
            or self.auth_generation_provider() != live.auth_generation
            or public_fingerprint(auth.api_token) != live.inat_token_marker
        ):
            raise LinkRepairError(
                "iNaturalist authentication changed after preflight. No write was sent.",
                "inat_auth_changed",
            )

    @staticmethod
    def _matches_preview(row: dict[str, Any], live: _LiveState) -> bool:
        identity_matches = live.inat_observation_uuid == str(
            row["inat_observation_uuid"]
        ) and (
            live.inat_field_id == int(row["binding_id"])
            if str(row["site"]) == "inat"
            else live.mo_external_site_id == int(row["binding_id"])
        )
        return identity_matches and (
            live.inat_record_fingerprint == row["preview_inat_record_fingerprint"]
            and live.mo_record_fingerprint == row["preview_mo_record_fingerprint"]
            and live.inat_links_fingerprint == row["preview_inat_links_fingerprint"]
            and live.mo_links_fingerprint == row["preview_mo_links_fingerprint"]
        )

    @staticmethod
    def _is_satisfied(row: dict[str, Any], live: _LiveState) -> bool:
        action = LinkActionType(str(row["action_type"]))
        rows = live.inat_rows if str(row["site"]) == "inat" else live.mo_rows
        if action in {LinkActionType.INAT_OFV_ADD, LinkActionType.MO_EXTERNAL_LINK_ADD}:
            return len(rows) == 1 and any(
                item.parse_state != "malformed"
                and item.target_observation_id == int(row["desired_target_id"])
                for item in rows
            )
        target = str(row["remote_row_uuid"] or row["remote_row_id"])
        exact = next(
            (item for item in rows if str(item.row_uuid or item.row_id) == target), None
        )
        if action in {
            LinkActionType.INAT_OFV_REMOVE,
            LinkActionType.MO_EXTERNAL_LINK_REMOVE,
        }:
            return exact is None
        return (
            exact is not None
            and exact.parse_state != "malformed"
            and exact.target_observation_id == int(row["desired_target_id"])
        )

    def _action_satisfied(
        self,
        profile_id: int,
        row: dict[str, Any],
        live: _LiveState,
    ) -> bool:
        group = self.db.action_group(profile_id, int(row["action_group_id"])) or {}
        is_consolidation = (
            self.db.consolidation_ledger_for_group(
                profile_id, int(row["action_group_id"])
            )
            is not None
        )
        if is_consolidation and LinkActionType(str(row["action_type"])) in {
            LinkActionType.INAT_OFV_ADD,
            LinkActionType.MO_EXTERNAL_LINK_ADD,
        }:
            rows = live.inat_rows if str(row["site"]) == "inat" else live.mo_rows
            satisfied = any(
                item.parse_state != "malformed"
                and item.target_observation_id == int(row["desired_target_id"])
                for item in rows
            )
        else:
            satisfied = self._is_satisfied(row, live)
        if not satisfied:
            return False
        group_rows = self.db.action_group_rows(profile_id, int(row["action_group_id"]))
        if not group_rows or int(row["ordinal"]) != max(
            int(item["ordinal"]) for item in group_rows
        ):
            return True
        # Gate 2B is additive. Donor rows may continue to point at an older
        # counterpart until a separately reviewed Phase 2C; requiring exactly
        # one row here would violate that boundary.
        if is_consolidation:
            return True
        reciprocal = group.get("pair_id") is not None
        if group.get("issue_id") is not None:
            review = self.db.link_issue_review(profile_id, int(group["issue_id"]))
            reciprocal = bool(review and review.get("review_intent") == "reciprocal")
        if not reciprocal:
            return True
        return (
            len(live.inat_rows) == 1
            and live.inat_rows[0].parse_state == "valid"
            and live.inat_rows[0].target_observation_id == live.mo_observation_id
            and len(live.mo_rows) == 1
            and live.mo_rows[0].parse_state == "valid"
            and live.mo_rows[0].target_observation_id == live.inat_observation_id
        )


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


def _positive_int(value: object) -> Optional[int]:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _parse_date(value: object) -> Optional[date]:
    if isinstance(value, dict):
        value = value.get("date") or value.get("start") or value.get("observed_on")
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _inat_rows(
    raw: dict[str, Any], field_id: int, observation_id: int
) -> tuple[AuthoritativeLinkSnapshot, ...]:
    rows: list[AuthoritativeLinkSnapshot] = []
    count = 0
    for item in raw.get("ofvs") or raw.get("observation_field_values") or []:
        if not isinstance(item, dict):
            continue
        field = (
            item.get("observation_field")
            if isinstance(item.get("observation_field"), dict)
            else {}
        )
        if (
            _positive_int(
                item.get("field_id")
                or item.get("observation_field_id")
                or field.get("id")
            )
            != field_id
        ):
            continue
        count += 1
        raw_value = str(item.get("value") or "")
        target = parse_mo_observation_url(raw_value)
        row_id = str(item.get("id") or "")
        row_uuid = str(item.get("uuid") or "")
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        state = "valid" if target else "malformed"
        rows.append(
            AuthoritativeLinkSnapshot(
                site=RemoteSite.INAT,
                observation_id=observation_id,
                row_id=row_id or f"unidentified:{count}",
                row_uuid=row_uuid,
                binding_id=field_id,
                target_observation_id=target,
                parse_state=state,
                row_fingerprint=public_fingerprint(
                    row_id, row_uuid, field_id, target, state, raw_value
                ),
                added_by_user_id=_positive_int(user.get("id")),
                display_value=raw_value,
            )
        )
    return _mark_duplicates(tuple(rows))


def _mo_rows(
    payload: object, site_id: int, observation_id: int
) -> tuple[AuthoritativeLinkSnapshot, ...]:
    rows: list[AuthoritativeLinkSnapshot] = []
    count = 0
    for item in results_from_payload(payload):
        parsed = parse_mo_external_link(item, site_id)
        if parsed is None or parsed[0] != observation_id:
            continue
        _source_id, link = parsed
        count += 1
        raw_value = str(
            item.get("url")
            or item.get("link_url")
            or item.get("derived_url")
            or item.get("external_url")
            or ""
        )
        rows.append(
            AuthoritativeLinkSnapshot(
                site=RemoteSite.MO,
                observation_id=observation_id,
                row_id=link.row_id or f"unidentified:{count}",
                binding_id=site_id,
                target_observation_id=link.target_observation_id,
                parse_state=link.parse_state,
                row_fingerprint=link.fingerprint,
                display_value=raw_value,
            )
        )
    return _mark_duplicates(tuple(rows))


def _mark_duplicates(
    rows: tuple[AuthoritativeLinkSnapshot, ...],
) -> tuple[AuthoritativeLinkSnapshot, ...]:
    if len(rows) <= 1:
        return rows
    targets = {
        item.target_observation_id
        for item in rows
        if item.target_observation_id is not None
    }
    state = (
        "ambiguous"
        if any(item.parse_state == TARGET_UNKNOWN for item in rows)
        else "conflicting" if len(targets) > 1 else "duplicate"
    )
    return tuple(
        AuthoritativeLinkSnapshot(
            site=item.site,
            observation_id=item.observation_id,
            row_id=item.row_id,
            row_uuid=item.row_uuid,
            binding_id=item.binding_id,
            target_observation_id=item.target_observation_id,
            parse_state=item.parse_state if item.parse_state != "valid" else state,
            row_fingerprint=public_fingerprint(item.row_fingerprint, state),
            added_by_user_id=item.added_by_user_id,
            display_value=item.display_value,
        )
        for item in rows
    )


def _rows_fingerprint(rows: Sequence[AuthoritativeLinkSnapshot]) -> str:
    return public_fingerprint(
        *(
            item.row_fingerprint
            for item in sorted(rows, key=lambda row: (row.row_id, row.row_uuid))
        )
    )


def _review_fingerprint(review: dict[str, Any]) -> str:
    return public_fingerprint(
        review.get("issue_fingerprint"),
        review.get("review_intent"),
        review.get("mo_observation_id"),
        review.get("inat_observation_id"),
        review.get("reviewed_at"),
    )


def _inat_record_fingerprint(raw: dict[str, Any]) -> str:
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    taxon = raw.get("taxon") if isinstance(raw.get("taxon"), dict) else {}
    return public_fingerprint(
        raw.get("id"),
        raw.get("uuid"),
        user.get("id"),
        raw.get("observed_on"),
        raw.get("updated_at"),
        taxon.get("id"),
        taxon.get("ancestry"),
    )


def _fungi_status(raw: dict[str, Any]) -> str:
    # Delegates to the scan's own classifier instead of re-deriving the rule.
    # The re-derived copy disagreed with it in two ways that hard-block writes:
    # it missed the Fungi kingdom taxon itself, and it called any taxon with
    # neither an iconic name nor an ancestry "nonfungal" -- so a pair whose
    # iNaturalist record carries only a coarse ID (for example "State of Matter
    # Life", which the deliberately unfiltered delta scan does record) failed
    # every preview and every post-write verification with inat_out_of_scope.
    raw_taxon = raw.get("taxon")
    return inat_fungi_status(raw_taxon if isinstance(raw_taxon, dict) else {})


def _is_inat_site(raw: dict[str, Any]) -> bool:
    text = " ".join(
        str(raw.get(key) or "") for key in ("name", "site", "url", "base_url")
    ).casefold()
    return "inaturalist" in text or "inaturalist.org" in text
