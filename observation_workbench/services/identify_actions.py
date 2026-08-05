"""Durable, conservative backend for later Identify write UI work.

This module deliberately owns no Identify controls.  It journals intent,
serializes unsafe requests, and requires an explicit per-action or
current-account batch authorization before any queued request can leave the
machine.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional
from uuid import UUID

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from observation_workbench.api.client import INatAPIError, INatClient, UnsafeWriteOutcomeUnknown
from observation_workbench.storage.cache_db import (
    CacheDB,
    IdentifyEnqueueResult,
    _coerce_desired_state,
    make_identify_deduplication_key,
)

log = logging.getLogger(__name__)

UNFINISHED = (
    "queued",
    "submitting",
    "submitted_unverified",
    "failed_retryable",
    "ambiguous",
)
ATTENTION = (
    "submitted_unverified",
    "failed_retryable",
    "failed_terminal",
    "ambiguous",
)
HISTORY = ("confirmed", "cancelled", "tracking_cancelled")
VERIFYABLE_STATES = ("submitted_unverified", "ambiguous")
_CLOCK_TOLERANCE_SECONDS = 120.0


class ActionPhase(str, Enum):
    ACCOUNT_PREFLIGHT = "account_preflight"
    UNSAFE_WRITE = "unsafe_write"
    VERIFICATION_READ = "verification_read"


class ActionOutcome(str, Enum):
    CONFIRMED = "confirmed"
    PREFLIGHT_FAILED = "preflight_failed"
    WRITE_DEFINITELY_REJECTED = "write_definitely_rejected"
    WRITE_OUTCOME_UNKNOWN = "write_outcome_unknown"
    SUBMITTED_UNVERIFIED = "submitted_unverified"
    VERIFICATION_MISMATCH = "verification_mismatch"


class VerificationStatus(str, Enum):
    CONFIRMED = "confirmed"
    NOT_FOUND = "not_found"
    MISMATCHED = "mismatched"
    INSUFFICIENT_FIELDS = "insufficient_fields"
    READ_FAILED = "read_failed"


@dataclass(frozen=True)
class AccountIdentity:
    user_id: int
    uuid: str
    login: str


@dataclass(frozen=True)
class IdentifyAuthSnapshot:
    """Credential-free authentication state for Identify presentation.

    This intentionally exposes neither the API token nor its in-memory
    fingerprint.  Callers can safely use it to decide whether a journal row
    belongs to the account currently authenticated in the application.
    """

    login: str
    authenticated: bool


@dataclass(frozen=True)
class IdentifyActionSummary:
    """Centralized durable-journal state counts for Identify UI surfaces."""

    queued: int = 0
    submitting: int = 0
    submitted_unverified: int = 0
    ambiguous: int = 0
    failed_retryable: int = 0
    failed_terminal: int = 0
    confirmed: int = 0
    cancelled: int = 0
    tracking_cancelled: int = 0

    @property
    def unfinished_count(self) -> int:
        return (
            self.queued
            + self.submitting
            + self.submitted_unverified
            + self.ambiguous
            + self.failed_retryable
        )

    @property
    def attention_count(self) -> int:
        return (
            self.submitted_unverified
            + self.ambiguous
            + self.failed_retryable
            + self.failed_terminal
        )

    @property
    def pending_menu_count(self) -> int:
        """Rows which remain unresolved or require a deliberate review."""
        return self.unfinished_count + self.failed_terminal

    def as_dict(self) -> dict[str, int]:
        return {
            "queued": self.queued,
            "submitting": self.submitting,
            "submitted_unverified": self.submitted_unverified,
            "ambiguous": self.ambiguous,
            "failed_retryable": self.failed_retryable,
            "failed_terminal": self.failed_terminal,
            "confirmed": self.confirmed,
            "cancelled": self.cancelled,
            "tracking_cancelled": self.tracking_cancelled,
        }


@dataclass(frozen=True)
class ExistingIdentification:
    """A prior journaled identification found by dedup, with its reuse disposition.

    ``disposition`` is one of ``reusable`` (still unresolved), ``completed``
    (confirmed), ``needs_recovery`` (outcome unknown), or ``needs_retry``
    (failed/cancelled terminal — no live identification).
    """

    action_id: int
    state: str
    disposition: str


@dataclass(frozen=True)
class IdentifyQueueSummary:
    """Queued-row eligibility without exposing the authorization set itself."""

    current_login: str = ""
    eligible_for_current_account: int = 0
    queued_for_other_accounts: int = 0
    other_account_logins: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    diagnostic: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status is VerificationStatus.CONFIRMED


@dataclass(frozen=True)
class WorkerResult:
    """Terminal result of one phased durable-action operation.

    It carries only non-sensitive metadata.  Request bodies, response bodies,
    and credentials never enter this result or the durable journal.
    """

    local_action_id: int
    phase: ActionPhase
    outcome: ActionOutcome
    write_started: bool = False
    write_response_received: bool = False
    response_metadata: dict[str, Any] = field(default_factory=dict)
    server_object_id: str = ""
    server_object_uuid: str = ""
    verification: Optional[VerificationResult] = None
    exception: Optional[BaseException] = None
    diagnostic: str = ""
    identity: Optional[AccountIdentity] = None
    credential_fingerprint: str = ""
    verify_only: bool = False


@dataclass(frozen=True)
class ObservationUUIDResolution:
    """One immutable UUID-resolution result, correlated by request ID.

    Observation IDs are deliberately not sufficient correlation: callers can
    resolve the same observation more than once, and a closed dialog must be
    able to ignore a late result for its own request.
    """

    request_id: int
    observation_id: int
    observation_uuid: str = ""
    diagnostic: str = ""
    exception: Optional[BaseException] = None

    @property
    def resolved(self) -> bool:
        return bool(self.observation_uuid)


class _ActionSignals(QObject):
    done = Signal(object)


class _UUIDSignals(QObject):
    done = Signal(object)


class _ActionWorker(QRunnable):
    def __init__(
        self,
        manager: "IdentifyActionManager",
        action: dict[str, Any],
        token: str,
        credential_fingerprint: str,
        cached_identity: AccountIdentity | None,
        *,
        verify_only: bool,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._manager = manager
        self._action = action
        self._token = token
        self._credential_fingerprint = credential_fingerprint
        self._cached_identity = cached_identity
        self._verify_only = verify_only
        self.signals = _ActionSignals()

    def run(self) -> None:
        self.signals.done.emit(
            self._manager._run_action_operation(
                self._action,
                self._token,
                self._credential_fingerprint,
                self._cached_identity,
                verify_only=self._verify_only,
            )
        )


class _UUIDResolutionWorker(QRunnable):
    def __init__(
        self,
        client: INatClient,
        request_id: int,
        observation_id: int,
        token: str,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._client = client
        self._request_id = int(request_id)
        self._observation_id = observation_id
        self._token = token
        self.signals = _UUIDSignals()

    def run(self) -> None:
        try:
            raw = self._client.get_observation_identity_v2(
                self._observation_id,
                self._token,
            )
            observation = _first_v2_resource(raw)
            if not isinstance(observation, dict):
                raise ValueError("v2 observation identity response had no observation record")
            response_id = _as_positive_int(observation.get("id"))
            response_uuid = _normalise_uuid(observation.get("uuid"))
            if response_id != self._observation_id or not response_uuid:
                raise ValueError("v2 observation identity response did not match the requested observation")
            result = ObservationUUIDResolution(
                self._request_id,
                self._observation_id,
                response_uuid,
            )
        except Exception as exc:
            result = ObservationUUIDResolution(
                self._request_id,
                self._observation_id,
                diagnostic=_safe_exception_diagnostic(exc),
                exception=exc,
            )
        self.signals.done.emit(result)


class IdentifyActionManager(QObject):
    """Application-scoped, paused-by-default durable Identify action manager."""

    action_changed = Signal(object)
    summary_changed = Signal(object)
    paused = Signal(str)
    running_changed = Signal(bool)
    authentication_context_changed = Signal()
    verification_completed = Signal(int, object)
    observation_uuid_resolved = Signal(object)
    action_confirmed = Signal(int, str)
    observation_refresh_requested = Signal(int, str)

    def __init__(
        self,
        client: INatClient,
        db: CacheDB,
        auth_provider: Callable[[], object],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._client = client
        self._db = db
        self._auth_provider = auth_provider
        self._running = False
        self._paused = True
        self._shutting_down = False
        # Runtime-only approval for unsafe dispatch.  Durable journal rows
        # always restart paused: no ID is restored into this set after a crash.
        self._dispatch_authorized_action_ids: set[int] = set()
        self._identity_cache: tuple[str, AccountIdentity] | None = None
        self._live_action_signals: set[_ActionSignals] = set()
        self._live_uuid_signals: set[_UUIDSignals] = set()
        self._uuid_request_number = 0
        self._active_action_id: int | None = None

        self._migrated_manual_retry_count = self._db.migrate_legacy_manual_retry_queued_actions()
        recovered = self._db.recover_identify_submitting_actions()
        if self._migrated_manual_retry_count:
            log.info(
                "Migrated %s legacy Identify manual retry row(s) into linked retries",
                self._migrated_manual_retry_count,
            )
        if recovered:
            log.info("Recovered %s interrupted Identify write(s) as ambiguous", recovered)
        self._emit_summary()

    # ------------------------------------------------------------------
    # Public backend API for the later UI phase.  The required future action
    # entry sequence is: validate input, resolve the observation UUID, commit
    # a journal row, optionally advance the UI after that commit, then call
    # request_dispatch() only for the newly authorized local action ID.
    # ------------------------------------------------------------------

    def queue_identification(
        self,
        *,
        account_login: str,
        observation_id: int,
        observation_uuid: str,
        taxon_id: int,
        body: str = "",
        disagreement: bool | None = None,
    ) -> IdentifyEnqueueResult:
        payload: dict[str, Any] = {"taxon_id": int(taxon_id), "body": body}
        # Only journal the disagreement flag when explicitly requested so a
        # normal ID keeps its existing deduplication identity and payload shape.
        if disagreement is not None:
            payload["disagreement"] = bool(disagreement)
        action = self._new_action(
            account_login=account_login,
            observation_id=observation_id,
            observation_uuid=observation_uuid,
            action_type="identification",
            payload=payload,
            desired_state=None,
        )
        result = self._db.enqueue_identification_or_comment(action)
        self._emit_enqueue_changes(result)
        return result

    def find_existing_identification(
        self,
        *,
        account_login: str,
        observation_id: int,
        taxon_id: int,
    ) -> "ExistingIdentification | None":
        """Classify the most recent journaled identification matching these inputs.

        Unlike enqueue coalescing (which only spans unresolved states), this scans
        every state — including terminal ones — so a caller can decide how to
        proceed instead of blindly reusing or duplicating an action. The returned
        ``disposition`` reflects what the existing action means:

        * ``reusable`` — still unresolved (queued/submitting/etc.); safe to reuse.
        * ``completed`` — confirmed; the identification was already made.
        * ``needs_recovery`` — outcome unknown; the Identify subsystem must
          reconcile it before anything else happens.
        * ``needs_retry`` — failed/cancelled terminal; there is no live
          identification, so the caller must route the user to Identify's retry
          rather than silently enqueueing a duplicate.

        Matches the default plain-identification shape (empty body, no explicit
        disagreement).
        """
        dedup_key = self._dedup_key(
            account_login,
            int(observation_id),
            "identification",
            {"taxon_id": int(taxon_id), "body": ""},
            None,
        )
        row = self._db.find_identify_action_by_dedup(dedup_key)
        if row is None:
            return None
        state = str(row.get("state") or "")
        action_id = int(row["local_action_id"])
        if bool(row.get("outcome_unknown")):
            disposition = "needs_recovery"
        elif state in UNFINISHED:
            disposition = "reusable"
        elif state == "confirmed":
            disposition = "completed"
        else:
            # failed_terminal, cancelled, tracking_cancelled, or a legacy state:
            # no actionable identification remains.
            disposition = "needs_retry"
        return ExistingIdentification(action_id=action_id, state=state, disposition=disposition)

    def queue_comment(
        self,
        *,
        account_login: str,
        observation_id: int,
        observation_uuid: str,
        body: str,
    ) -> IdentifyEnqueueResult:
        action = self._new_action(
            account_login=account_login,
            observation_id=observation_id,
            observation_uuid=observation_uuid,
            action_type="comment",
            payload={"body": body},
            desired_state=None,
        )
        result = self._db.enqueue_identification_or_comment(action)
        self._emit_enqueue_changes(result)
        return result

    def queue_desired_state(
        self,
        *,
        account_login: str,
        observation_id: int,
        observation_uuid: str,
        action_type: str,
        desired_state: bool,
    ) -> IdentifyEnqueueResult:
        if action_type not in {"reviewed", "favorite"}:
            raise ValueError("Desired-state actions are limited to reviewed and favorite")
        action = self._new_action(
            account_login=account_login,
            observation_id=observation_id,
            observation_uuid=observation_uuid,
            action_type=action_type,
            payload={},
            desired_state=desired_state,
        )
        result = self._db.enqueue_desired_state_action(action)
        self._emit_enqueue_changes(result)
        return result

    def queue_quality_metric(
        self,
        *,
        account_login: str,
        observation_id: int,
        observation_uuid: str,
        metric: str,
        vote: str,
    ) -> IdentifyEnqueueResult:
        """Journal one explicit Data Quality Assessment metric vote.

        This is the authenticated user's vote on the metric (for ``wild``:
        Captive/Cultivated is ``disagree``, Vote Wild is ``agree``, and
        ``remove`` clears the vote) -- never an observation-field update.
        ``desired_state`` stays ``None``; the tri-state operation lives
        entirely in the bounded ``{"metric": ..., "vote": ...}`` payload.
        """
        if metric != "wild":
            raise ValueError("Only the 'wild' quality metric is supported in this gate")
        if vote not in {"agree", "disagree", "remove"}:
            raise ValueError("Quality metric vote must be agree, disagree, or remove")
        action = self._new_action(
            account_login=account_login,
            observation_id=observation_id,
            observation_uuid=observation_uuid,
            action_type="quality_metric",
            payload={"metric": metric, "vote": vote},
            desired_state=None,
        )
        result = self._db.enqueue_quality_metric_action(action)
        self._emit_enqueue_changes(result)
        return result

    def submit(
        self,
        *,
        account_login: str,
        observation_id: int,
        observation_uuid: str,
        action_type: str,
        payload: dict[str, Any],
        desired_state: bool | None = None,
    ) -> int:
        """Compatibility shim; it queues but never starts a write by itself."""
        if action_type == "identification":
            return self.queue_identification(
                account_login=account_login,
                observation_id=observation_id,
                observation_uuid=observation_uuid,
                taxon_id=int(payload["taxon_id"]),
                body=str(payload.get("body") or ""),
            ).action_id
        if action_type == "comment":
            return self.queue_comment(
                account_login=account_login,
                observation_id=observation_id,
                observation_uuid=observation_uuid,
                body=str(payload["body"]),
            ).action_id
        return self.queue_desired_state(
            account_login=account_login,
            observation_id=observation_id,
            observation_uuid=observation_uuid,
            action_type=action_type,
            desired_state=bool(desired_state),
        ).action_id

    def list_actions(self) -> list[dict[str, Any]]:
        return [dict(action) for action in self._db.get_identify_actions()]

    def actions_for_observation(self, observation_id: int) -> list[dict[str, Any]]:
        return [
            dict(action)
            for action in self._db.get_identify_actions_for_observation(int(observation_id))
        ]

    def action_summary(self) -> IdentifyActionSummary:
        counts: dict[str, int] = {}
        for action in self._db.get_identify_actions():
            state = str(action.get("state") or "")
            counts[state] = counts.get(state, 0) + 1
        return IdentifyActionSummary(
            queued=counts.get("queued", 0),
            submitting=counts.get("submitting", 0),
            submitted_unverified=counts.get("submitted_unverified", 0),
            ambiguous=counts.get("ambiguous", 0),
            failed_retryable=counts.get("failed_retryable", 0),
            failed_terminal=counts.get("failed_terminal", 0),
            confirmed=counts.get("confirmed", 0),
            cancelled=counts.get("cancelled", 0),
            tracking_cancelled=counts.get("tracking_cancelled", 0),
        )

    def summary(self) -> dict[str, int]:
        """Compatibility mapping for existing callers.

        New presentation code should prefer :meth:`action_summary`, whose
        derived counts distinguish unfinished work, attention, and history.
        """
        return self.action_summary().as_dict()

    def unfinished_summary(self) -> dict[str, int]:
        summary = self.action_summary()
        return {
            "queued": summary.queued,
            "submitting": summary.submitting,
            "submitted_unverified": summary.submitted_unverified,
            "ambiguous": summary.ambiguous,
            "failed_retryable": summary.failed_retryable,
        }

    def attention_summary(self) -> dict[str, int]:
        summary = self.action_summary()
        return {
            "submitted_unverified": summary.submitted_unverified,
            "ambiguous": summary.ambiguous,
            "failed_retryable": summary.failed_retryable,
            "failed_terminal": summary.failed_terminal,
        }

    def history_summary(self) -> dict[str, int]:
        summary = self.action_summary()
        return {
            "confirmed": summary.confirmed,
            "cancelled": summary.cancelled,
            "tracking_cancelled": summary.tracking_cancelled,
        }

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def active_action_id(self) -> int | None:
        return self._active_action_id

    @property
    def migrated_manual_retry_count(self) -> int:
        """Number of legacy dead-end rows converted during this startup."""
        return self._migrated_manual_retry_count

    def current_authentication(self) -> IdentifyAuthSnapshot:
        """Return current credential-free Identify authentication state."""
        token, login, _fingerprint = self._auth_context()
        return IdentifyAuthSnapshot(login=login, authenticated=bool(token and login))

    def can_dispatch_for_account(self, account_login: str) -> tuple[bool, str]:
        """Check whether the locally authenticated account owns a row."""
        required = str(account_login or "").strip()
        auth = self.current_authentication()
        if not required:
            return False, "This action has no account owner."
        if not auth.authenticated:
            return False, f"Authenticate as {required} before submitting this action."
        if auth.login.casefold() != required.casefold():
            return False, f"Authenticate as {required} before submitting this action."
        return True, ""

    def queue_summary(self) -> IdentifyQueueSummary:
        """Report queued rows eligible for this login separately from others."""
        auth = self.current_authentication()
        eligible = 0
        other_count = 0
        other_logins: list[str] = []
        seen: set[str] = set()
        queued = self._db.get_identify_actions(("queued",))
        for action in queued:
            account = str(action.get("account_login") or "").strip()
            if auth.authenticated and account.casefold() == auth.login.casefold():
                eligible += 1
                continue
            other_count += 1
            key = account.casefold()
            if account and key not in seen:
                seen.add(key)
                other_logins.append(account)
        return IdentifyQueueSummary(
            current_login=auth.login if auth.authenticated else "",
            eligible_for_current_account=eligible,
            queued_for_other_accounts=other_count,
            other_account_logins=tuple(other_logins),
        )

    def queued_action_account(self) -> str:
        """Return one account with queued work when there is no current match."""
        queue = self.queue_summary()
        if queue.eligible_for_current_account:
            return queue.current_login
        return queue.other_account_logins[0] if queue.other_account_logins else ""

    def can_resume_queued_actions(self) -> tuple[bool, str]:
        """Check whether the current login has a queued batch to authorize."""
        queue = self.queue_summary()
        if queue.eligible_for_current_account:
            return True, queue.current_login
        return False, queue.other_account_logins[0] if queue.other_account_logins else ""

    def can_request_dispatch(self, action_id: int) -> tuple[bool, str]:
        """Validate a selected row before granting its one-action permission."""
        if self._shutting_down:
            return False, "Identify action manager is shutting down."
        action = self._action(action_id)
        if action is None:
            return False, "The selected action no longer exists."
        if str(action.get("state") or "") != "queued":
            return False, "Only an ordinary queued action can be submitted."
        if not _journal_action_is_executable(action):
            return False, "The selected queued action is invalid and needs correction."
        return self.can_dispatch_for_account(str(action.get("account_login") or ""))

    def request_dispatch(self, action_id: int) -> bool:
        """Authorize and schedule only one selected queued action.

        Authorization is deliberately in-memory and is removed before the
        action leaves ``queued``.  It is never inherited by later enqueues.
        """
        allowed, _reason = self.can_request_dispatch(int(action_id))
        if not allowed:
            return False
        self._dispatch_authorized_action_ids.add(int(action_id))
        self._paused = False
        self._dispatch_next()
        return True

    def resume_queued_actions(self) -> bool:
        """Authorize exactly the current login's queued-row snapshot."""
        if self._shutting_down:
            return False
        token, login, _fingerprint = self._auth_context()
        if not token or not login:
            return False
        snapshot = tuple(
            int(action["local_action_id"])
            for action in self._db.get_identify_actions(("queued",))
            if str(action.get("account_login") or "").casefold() == login.casefold()
        )
        if not snapshot:
            return False
        self._dispatch_authorized_action_ids.update(snapshot)
        self._paused = False
        self._dispatch_next()
        return True

    def resume(self) -> bool:
        """Compatibility wrapper for the explicit queued-batch operation."""
        return self.resume_queued_actions()

    def pause(self, reason: str) -> None:
        self._dispatch_authorized_action_ids.clear()
        self._paused = True
        self.paused.emit(str(reason))

    def cancel(self, action_id: int) -> bool:
        action = self._action(action_id)
        if action is None:
            return False
        state = str(action["state"])
        if state in {"queued", "failed_retryable"}:
            next_state = "cancelled"
        elif state in {"ambiguous", "submitted_unverified"}:
            # The server operation may exist. This only stops local tracking.
            next_state = "tracking_cancelled"
        else:
            return False
        changed = self._db.transition_identify_action(int(action_id), (state,), next_state)
        if changed:
            self._dispatch_authorized_action_ids.discard(int(action_id))
            self._emit_action_changed(action_id)
            self._emit_summary()
        return changed

    def verify_again(self, action_id: int) -> bool:
        """Run account preflight and a verification GET, never an unsafe write."""
        if self._shutting_down or self._running:
            return False
        action = self._action(action_id)
        if action is None or action["state"] not in VERIFYABLE_STATES:
            return False
        token, login, fingerprint = self._auth_context()
        cached_identity = self._cached_identity(fingerprint, login, action["account_login"])
        self._start_action_worker(
            action,
            token,
            fingerprint,
            cached_identity,
            verify_only=True,
        )
        return True

    def retry_definite_failure(self, action_id: int) -> bool:
        """Explicitly make a known-not-applied action eligible for a later resume."""
        action = self._action(action_id)
        if action is None or int(action.get("outcome_unknown") or 0):
            return False
        # Terminal validation/resource failures need a corrected future action,
        # not an unchanged blind resubmission. Authentication and temporary
        # account-state failures are classified as failed_retryable instead.
        if action["state"] != "failed_retryable":
            return False
        changed = self._db.transition_identify_action(
            int(action_id),
            (str(action["state"]),),
            "queued",
            last_error_json="",
            last_operation_phase="",
        )
        if changed:
            self._dispatch_authorized_action_ids.discard(int(action_id))
            self._emit_action_changed(action_id)
            self._emit_summary()
        return changed

    def retry_anyway(self, action_id: int, duplicate_risk_confirmed: bool) -> int | None:
        """Create an acknowledged, linked retry without dispatching it.

        The source remains ambiguous so its potentially successful unsafe write
        is never overwritten or silently treated as failed.
        """
        if duplicate_risk_confirmed is not True:
            return None
        action = self._action(action_id)
        if action is None or action["state"] != "ambiguous":
            return None
        retry_id = self._db.create_manual_retry(int(action_id))
        if retry_id is None:
            return None
        # A new retry is a distinct unsafe attempt and never inherits the
        # source row's (or any batch's) runtime-only dispatch permission.
        self._dispatch_authorized_action_ids.discard(retry_id)
        self.pause(
            "Manual retry queued; explicitly submit that action or resume a later queued snapshot."
        )
        self._emit_action_changed(action_id)
        self._emit_action_changed(retry_id)
        self._emit_summary()
        return retry_id

    def resolve_observation_uuid(
        self,
        observation_id: int,
        observation_uuid: str = "",
    ) -> int:
        """Resolve an observation UUID off the UI thread and emit the result.

        The returned request number pairs with :attr:`observation_uuid_resolved`.
        A valid supplied UUID is queued without a read, so the caller receives
        its request ID before the matching signal can arrive.
        """
        numeric_id = _as_positive_int(observation_id)
        if not numeric_id:
            raise ValueError("Observation UUID resolution requires a positive numeric ID")
        self._uuid_request_number += 1
        request_number = self._uuid_request_number
        supplied_uuid = _normalise_uuid(observation_uuid)
        if observation_uuid and not supplied_uuid:
            raise ValueError("Observation UUID must be valid when supplied")
        if supplied_uuid:
            self._queue_immediate_uuid_resolution(
                ObservationUUIDResolution(request_number, numeric_id, supplied_uuid)
            )
            return request_number
        if self._shutting_down:
            self._queue_immediate_uuid_resolution(
                ObservationUUIDResolution(
                    request_number,
                    numeric_id,
                    diagnostic="manager_shutting_down",
                )
            )
            return request_number
        token, _login, _fingerprint = self._auth_context()
        worker = _UUIDResolutionWorker(self._client, request_number, numeric_id, token)
        signals = worker.signals
        self._live_uuid_signals.add(signals)
        signals.done.connect(
            lambda result, s=signals: self._uuid_resolution_finished(s, result)
        )
        QThreadPool.globalInstance().start(worker)
        return request_number

    def resolve_observation_uuid_async(
        self,
        observation_id: int,
        observation_uuid: str = "",
    ) -> int:
        return self.resolve_observation_uuid(observation_id, observation_uuid)

    def authentication_changed(self) -> None:
        """Forget account-scoped runtime state after login/token changes."""
        self._identity_cache = None
        # A changed account is never an implicit continuation of a former
        # account's selected action or batch-resume authorization.
        self._dispatch_authorized_action_ids.clear()
        self._paused = True
        self.paused.emit("Authentication changed; queued Identify actions remain paused.")
        self.authentication_context_changed.emit()

    def prepare_shutdown(self) -> None:
        """Stop dispatching without changing an active submitting journal row."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self._dispatch_authorized_action_ids.clear()
        self._paused = True
        self.paused.emit("Identify action manager is shutting down.")
        if self._running:
            self._running = False
            self._active_action_id = None
            self.running_changed.emit(False)

    # ------------------------------------------------------------------
    # Dispatch and terminal state handling
    # ------------------------------------------------------------------

    def _dispatch_next(self) -> None:
        if self._paused or self._running or self._shutting_down:
            return
        authorized = self._dispatch_authorized_action_ids
        if not authorized:
            self._finish_authorized_dispatch_batch()
            return
        queued = self._db.get_identify_actions(("queued",))
        queued_by_id = {int(action["local_action_id"]): action for action in queued}
        # Remove stale permissions before selection.  This catches rows that
        # were cancelled, reclassified, or deleted by another transition.
        authorized.intersection_update(queued_by_id)
        if not authorized:
            self._finish_authorized_dispatch_batch()
            return
        action = next(
            (candidate for candidate in queued if int(candidate["local_action_id"]) in authorized),
            None,
        )
        if action is None:
            self._finish_authorized_dispatch_batch()
            return
        action_id = int(action["local_action_id"])
        if not _journal_action_is_executable(action):
            self._dispatch_authorized_action_ids.discard(action_id)
            if self._db.transition_identify_action(
                action_id,
                ("queued",),
                "failed_terminal",
                last_operation_phase="account_preflight",
                last_error_json=_diagnostic_json(
                    ActionPhase.ACCOUNT_PREFLIGHT,
                    ActionOutcome.PREFLIGHT_FAILED,
                    diagnostic="invalid_journal_action",
                ),
            ):
                self._emit_action_changed(action_id)
                self._emit_summary()
            self._dispatch_next()
            return

        token, login, fingerprint = self._auth_context()
        if not token or not login or login.casefold() != str(action["account_login"]).casefold():
            self.pause(
                "Authenticate as the account that owns the authorized queued action before submitting."
            )
            return
        cached_identity = self._cached_identity(fingerprint, login, action["account_login"])
        if not self._db.transition_identify_action(
            action_id,
            ("queued",),
            "submitting",
            attempt_count=int(action.get("attempt_count") or 0) + 1,
            attempt_started_at=time.time(),
            last_operation_phase=ActionPhase.ACCOUNT_PREFLIGHT.value,
            last_error_json="",
        ):
            # The exact row changed after it was selected.  Do not retain a
            # permission that might otherwise apply to a stale journal view.
            self._dispatch_authorized_action_ids.discard(action_id)
            self._dispatch_next()
            return
        # Permission is consumed by the successful queued -> submitting CAS;
        # it cannot survive into a retry, cancellation, or future enqueue.
        self._dispatch_authorized_action_ids.discard(action_id)
        current = self._action(action_id)
        if current is None:
            return
        self._emit_action_changed(action_id)
        self._active_action_id = action_id
        self._start_action_worker(current, token, fingerprint, cached_identity, verify_only=False)

    def _finish_authorized_dispatch_batch(self) -> None:
        """Return to paused after a selected action or batch snapshot drains."""
        if self._paused:
            return
        self._paused = True
        self.paused.emit(
            "Identify action manager finished its authorized batch; "
            "newly queued actions require explicit submission or a later resume."
        )

    def _start_action_worker(
        self,
        action: dict[str, Any],
        token: str,
        credential_fingerprint: str,
        cached_identity: AccountIdentity | None,
        *,
        verify_only: bool,
    ) -> None:
        if self._shutting_down:
            return
        self._running = True
        self._active_action_id = int(action["local_action_id"])
        self.running_changed.emit(True)
        worker = _ActionWorker(
            self,
            dict(action),
            token,
            credential_fingerprint,
            cached_identity,
            verify_only=verify_only,
        )
        signals = worker.signals
        self._live_action_signals.add(signals)
        signals.done.connect(lambda result, s=signals: self._finished(s, result))
        QThreadPool.globalInstance().start(worker)

    def _finished(self, signals: _ActionSignals, result: WorkerResult) -> None:
        self._live_action_signals.discard(signals)
        if self._running:
            self._running = False
            self._active_action_id = None
            self.running_changed.emit(False)
        if self._shutting_down:
            # Do not turn an interrupted action into a local failure.  A row
            # still marked submitting is recovered as ambiguous at next start.
            return
        log.debug(
            "Identify action %s worker finished: phase=%s outcome=%s "
            "diagnostic=%s exception=%s write_started=%s response_received=%s",
            result.local_action_id,
            result.phase.value,
            result.outcome.value,
            result.diagnostic or "none",
            type(result.exception).__name__ if result.exception is not None else "none",
            result.write_started,
            result.write_response_received,
        )
        self._remember_identity(result)
        if result.verify_only:
            self._finish_verify_only(result)
        else:
            self._finish_write_operation(result)
        # A Verify Again operation never grants unsafe dispatch permission.
        # If the user explicitly authorized a row while that safe read was in
        # flight, it can now be scheduled behind the completed read.
        if not self._paused:
            self._dispatch_next()

    def _finish_write_operation(self, result: WorkerResult) -> None:
        action = self._action(result.local_action_id)
        if action is None:
            return
        action_id = result.local_action_id
        transition_values = {
            "last_operation_phase": result.phase.value,
            "last_error_json": _diagnostic_json(
                result.phase,
                result.outcome,
                result.exception,
                result.diagnostic,
                response_received=result.write_response_received,
                response_metadata=result.response_metadata,
            ),
        }

        if result.outcome is ActionOutcome.PREFLIGHT_FAILED:
            changed = self._db.transition_identify_action(
                action_id,
                ("submitting",),
                "failed_retryable",
                **transition_values,
            )
            if _is_auth_error(result.exception) or result.diagnostic == "account_mismatch":
                self._identity_cache = None
                self.pause("Authenticate again before retrying the queued Identify action.")
        elif result.outcome is ActionOutcome.WRITE_OUTCOME_UNKNOWN:
            changed = self._db.transition_identify_action(
                action_id,
                ("submitting",),
                "ambiguous",
                outcome_unknown=1,
                **transition_values,
            )
            self.pause("An unsafe Identify write may have reached iNaturalist; it will not be retried automatically.")
        elif result.outcome is ActionOutcome.WRITE_DEFINITELY_REJECTED:
            if result.diagnostic == "local_write_failure":
                # The request was never built, so no credential refresh or
                # server-side change can make the unchanged payload succeed.
                # Only a corrected future action can, exactly as for a 4xx
                # validation rejection.
                terminal = "failed_terminal"
            else:
                terminal = _definite_write_state(result.exception)
            changed = self._db.transition_identify_action(
                action_id,
                ("submitting",),
                terminal,
                outcome_unknown=0,
                **transition_values,
            )
            if _is_auth_error(result.exception):
                self._identity_cache = None
                self.pause("Authenticate again before retrying the rejected Identify action.")
        else:
            verification = result.verification or VerificationResult(
                VerificationStatus.READ_FAILED,
                "missing_verification_result",
            )
            values = self._verification_values(action, result, verification)
            if verification.confirmed:
                changed = self._confirm_action(
                    action,
                    ("submitting",),
                    confirmed_at=time.time(),
                    outcome_unknown=0,
                    **values,
                )
            else:
                changed = self._db.transition_identify_action(
                    action_id,
                    ("submitting",),
                    "submitted_unverified",
                    outcome_unknown=0,
                    **values,
                )
        if changed:
            self._emit_action_changed(action_id)
            self._emit_summary()

    def _finish_verify_only(self, result: WorkerResult) -> None:
        action_id = result.local_action_id
        action = self._action(action_id)
        if action is None or action["state"] not in VERIFYABLE_STATES:
            # The row was cancelled or confirmed while its safe read was in
            # flight.  There is nothing to journal, but a listener that asked
            # for this verification must still learn that it finished.
            self.verification_completed.emit(action_id, result)
            return
        if result.outcome is ActionOutcome.PREFLIGHT_FAILED:
            # Account preflight failed before a verification read began.  Keep
            # the previous verification result intact and journal the phase
            # that actually failed.
            values = {
                "last_operation_phase": result.phase.value,
                "last_error_json": _diagnostic_json(
                    result.phase,
                    result.outcome,
                    result.exception,
                    result.diagnostic,
                ),
            }
            changed = self._db.transition_identify_action(
                action_id,
                (str(action["state"]),),
                str(action["state"]),
                **values,
            )
            if _is_auth_error(result.exception) or result.diagnostic == "account_mismatch":
                self._identity_cache = None
                self.pause("Authenticate again before verifying the Identify action.")
        else:
            verification = result.verification or VerificationResult(
                VerificationStatus.READ_FAILED,
                "missing_verification_result",
            )
            values = self._verification_values(action, result, verification)
            if verification.confirmed:
                changed = self._confirm_action(
                    action,
                    (str(action["state"]),),
                    confirmed_at=time.time(),
                    outcome_unknown=0,
                    **values,
                )
            else:
                changed = self._db.transition_identify_action(
                    action_id,
                    (str(action["state"]),),
                    str(action["state"]),
                    **values,
                )
        if changed:
            self._emit_action_changed(action_id)
            self._emit_summary()
        self.verification_completed.emit(action_id, result)

    def _verification_values(
        self,
        action: dict[str, Any],
        result: WorkerResult,
        verification: VerificationResult,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "last_operation_phase": result.phase.value,
            "verification_status": verification.status.value,
            "verification_diagnostic": _verification_diagnostic_json(verification),
            "last_error_json": _diagnostic_json(
                ActionPhase.VERIFICATION_READ,
                result.outcome,
                result.exception,
                verification.diagnostic,
                response_received=result.write_response_received,
                response_metadata=result.response_metadata,
            ),
            "server_object_id": result.server_object_id or str(action.get("server_object_id") or ""),
            "write_response_id": result.server_object_id or str(action.get("write_response_id") or ""),
            "write_response_uuid": result.server_object_uuid or str(action.get("write_response_uuid") or ""),
        }
        if result.phase is ActionPhase.VERIFICATION_READ:
            values["verification_attempt_count"] = (
                int(action.get("verification_attempt_count") or 0) + 1
            )
            values["last_verification_at"] = time.time()
        return values

    # ------------------------------------------------------------------
    # Phased worker implementation (always runs in a QRunnable)
    # ------------------------------------------------------------------

    def _run_action_operation(
        self,
        action: dict[str, Any],
        token: str,
        credential_fingerprint: str,
        cached_identity: AccountIdentity | None,
        *,
        verify_only: bool,
    ) -> WorkerResult:
        action_id = int(action["local_action_id"])
        identity = cached_identity
        if identity is None:
            try:
                identity = _extract_account_identity(self._client.get_current_user_v2(token))
            except Exception as exc:
                log.debug(
                    "Identify action %s account preflight failed (%s: %s)",
                    action_id,
                    type(exc).__name__,
                    _safe_exception_diagnostic(exc),
                    exc_info=True,
                )
                return WorkerResult(
                    action_id,
                    ActionPhase.ACCOUNT_PREFLIGHT,
                    ActionOutcome.PREFLIGHT_FAILED,
                    exception=exc,
                    diagnostic=_safe_exception_diagnostic(exc),
                    credential_fingerprint=credential_fingerprint,
                    verify_only=verify_only,
                )
            log.debug(
                "Identify action %s account preflight resolved user_id=%s login=%s",
                action_id,
                identity.user_id,
                identity.login,
            )
        if identity.login.casefold() != str(action["account_login"]).casefold():
            return WorkerResult(
                action_id,
                ActionPhase.ACCOUNT_PREFLIGHT,
                ActionOutcome.PREFLIGHT_FAILED,
                exception=ValueError("Authenticated account does not own this action"),
                diagnostic="account_mismatch",
                identity=identity,
                credential_fingerprint=credential_fingerprint,
                verify_only=verify_only,
            )

        if verify_only:
            verification = self._verify(
                action,
                token,
                identity,
                str(action.get("write_response_id") or ""),
                str(action.get("write_response_uuid") or ""),
            )
            return WorkerResult(
                action_id,
                ActionPhase.VERIFICATION_READ,
                _verification_outcome(verification),
                verification=verification,
                diagnostic=verification.diagnostic,
                identity=identity,
                credential_fingerprint=credential_fingerprint,
                verify_only=True,
            )

        try:
            write = self._prepare_write(action, token)
        except Exception as exc:
            # Payload parsing and argument construction are entirely local.
            # They are the only failures that prove the write never began.
            return WorkerResult(
                action_id,
                ActionPhase.UNSAFE_WRITE,
                ActionOutcome.WRITE_DEFINITELY_REJECTED,
                exception=exc,
                diagnostic="local_write_failure",
                identity=identity,
                credential_fingerprint=credential_fingerprint,
            )

        try:
            response = write()
        except UnsafeWriteOutcomeUnknown as exc:
            return WorkerResult(
                action_id,
                ActionPhase.UNSAFE_WRITE,
                ActionOutcome.WRITE_OUTCOME_UNKNOWN,
                write_started=True,
                write_response_received=False,
                exception=exc,
                diagnostic=_safe_exception_diagnostic(exc),
                identity=identity,
                credential_fingerprint=credential_fingerprint,
            )
        except INatAPIError as exc:
            outcome = (
                ActionOutcome.WRITE_OUTCOME_UNKNOWN
                if exc.outcome_unknown
                else ActionOutcome.WRITE_DEFINITELY_REJECTED
            )
            return WorkerResult(
                action_id,
                ActionPhase.UNSAFE_WRITE,
                outcome,
                write_started=True,
                write_response_received=bool(exc.response_received),
                response_metadata=_error_response_metadata(exc),
                exception=exc,
                diagnostic=_safe_exception_diagnostic(exc),
                identity=identity,
                credential_fingerprint=credential_fingerprint,
            )
        except Exception as exc:
            # Once the client call begins, an unclassified exception may have
            # occurred after the server applied the write.  Preserve the
            # no-double-submit guarantee by treating it as ambiguous.
            return WorkerResult(
                action_id,
                ActionPhase.UNSAFE_WRITE,
                ActionOutcome.WRITE_OUTCOME_UNKNOWN,
                write_started=True,
                exception=exc,
                diagnostic=_safe_exception_diagnostic(exc),
                identity=identity,
                credential_fingerprint=credential_fingerprint,
            )

        response_metadata = _response_metadata(response)
        server_id, server_uuid = _response_identifiers(response)
        verification = self._verify(action, token, identity, server_id, server_uuid)
        return WorkerResult(
            action_id,
            ActionPhase.VERIFICATION_READ,
            _verification_outcome(verification),
            write_started=True,
            write_response_received=True,
            response_metadata=response_metadata,
            server_object_id=server_id,
            server_object_uuid=server_uuid,
            verification=verification,
            diagnostic=verification.diagnostic,
            identity=identity,
            credential_fingerprint=credential_fingerprint,
        )

    def _prepare_write(
        self,
        action: dict[str, Any],
        token: str,
    ) -> Callable[[], dict[str, Any]]:
        """Build an unsafe client call without starting network I/O."""
        payload = _action_payload(action)
        kind = str(action["action_type"])
        observation_uuid = str(action["observation_uuid"])
        if kind == "identification":
            taxon_id = int(payload["taxon_id"])
            body = str(payload.get("body") or "")
            raw_disagreement = payload.get("disagreement")
            disagreement = bool(raw_disagreement) if raw_disagreement is not None else None
            return lambda: self._client.create_identification_v2(
                token, observation_uuid, taxon_id, body, disagreement
            )
        if kind == "comment":
            body = str(payload["body"])
            return lambda: self._client.create_comment_v2(token, observation_uuid, body)
        if kind == "reviewed":
            desired_state = bool(action["desired_state"])
            return lambda: self._client.set_reviewed_v2(token, observation_uuid, desired_state)
        if kind == "favorite":
            desired_state = bool(action["desired_state"])
            return lambda: self._client.set_favorite_v2(token, observation_uuid, desired_state)
        if kind == "quality_metric":
            metric = str(payload["metric"])
            vote = str(payload["vote"])
            return lambda: self._client.set_quality_metric_vote_v2(
                token, observation_uuid, metric, vote
            )
        raise ValueError("Unsupported Identify action type")

    def _verify(
        self,
        action: dict[str, Any],
        token: str,
        identity: AccountIdentity,
        server_id: str = "",
        server_uuid: str = "",
    ) -> VerificationResult:
        try:
            raw = self._client.get_observation_v2(str(action["observation_uuid"]), token)
            observation = _first_v2_resource(raw)
            if not isinstance(observation, dict):
                return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "missing_observation")
            if not _observation_matches_action(observation, action):
                return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "observation_identity_missing_or_mismatched")
            kind = str(action["action_type"])
            if kind == "identification":
                return _verify_identification(action, observation, identity, server_id, server_uuid)
            if kind == "comment":
                return _verify_comment(action, observation, identity, server_id, server_uuid)
            if kind == "reviewed":
                return _verify_reviewed(action, observation, identity)
            if kind == "favorite":
                return _verify_favorite(action, observation, identity)
            if kind == "quality_metric":
                return _verify_quality_metric(action, observation, identity)
            return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "unknown_action_type")
        except Exception as exc:
            return VerificationResult(VerificationStatus.READ_FAILED, _safe_exception_diagnostic(exc))

    # ------------------------------------------------------------------
    # Safe cache, signal, and data helpers
    # ------------------------------------------------------------------

    def _new_action(self, **values: Any) -> dict[str, Any]:
        account = str(values["account_login"]).strip()
        observation_id = int(values["observation_id"])
        observation_uuid = _normalise_uuid(values["observation_uuid"])
        if not observation_uuid:
            raise ValueError("Identify actions require a valid non-empty observation UUID")
        action_type = str(values["action_type"])
        payload = values["payload"]
        desired_state = values["desired_state"]
        values["observation_uuid"] = observation_uuid
        return {
            **values,
            "deduplication_key": self._dedup_key(
                account, observation_id, action_type, payload, desired_state
            ),
        }

    @staticmethod
    def _dedup_key(
        account: str,
        observation_id: int,
        action_type: str,
        payload: dict[str, Any],
        desired_state: bool | None,
    ) -> str:
        return make_identify_deduplication_key(
            account,
            observation_id,
            action_type,
            payload,
            desired_state,
        )

    def _action(self, action_id: int) -> dict[str, Any] | None:
        action = self._db.get_identify_action(int(action_id))
        return dict(action) if action is not None else None

    def _emit_enqueue_changes(self, result: IdentifyEnqueueResult) -> None:
        for action_id in result.cancelled_action_ids:
            self._dispatch_authorized_action_ids.discard(action_id)
            self._emit_action_changed(action_id)
        self._emit_action_changed(result.action_id)
        self._emit_summary()

    def _emit_action_changed(self, action_id: int) -> None:
        action = self._action(action_id)
        if action is not None:
            self.action_changed.emit(dict(action))

    def _emit_summary(self) -> None:
        self.summary_changed.emit(dict(self.summary()))

    def _confirm_action(
        self,
        action: dict[str, Any],
        expected_states: tuple[str, ...],
        **values: Any,
    ) -> bool:
        """Confirm an action and revoke permissions for cancelled retries."""
        result = self._db.confirm_identify_action(
            int(action["local_action_id"]),
            expected_states,
            **values,
        )
        if not result.confirmed:
            return False
        for retry_id in result.cancelled_retry_action_ids:
            self._dispatch_authorized_action_ids.discard(retry_id)
            self._emit_action_changed(retry_id)
        self._emit_confirmed(action)
        return True

    def _emit_confirmed(self, action: dict[str, Any]) -> None:
        observation_id = int(action["observation_id"])
        observation_uuid = str(action["observation_uuid"])
        self.action_confirmed.emit(observation_id, observation_uuid)
        self.observation_refresh_requested.emit(observation_id, observation_uuid)

    def _auth_context(self) -> tuple[str, str, str]:
        auth = self._auth_provider()
        token = str(getattr(auth, "api_token", "") or "").strip()
        login = str(getattr(auth, "login", "") or "").strip()
        fingerprint = _credential_fingerprint(token, login)
        cached = self._identity_cache
        if cached is not None and cached[0] != fingerprint:
            self._identity_cache = None
        return token, login, fingerprint

    def _cached_identity(
        self,
        fingerprint: str,
        login: str,
        action_login: str,
    ) -> AccountIdentity | None:
        cached = self._identity_cache
        if cached is None or cached[0] != fingerprint:
            return None
        identity = cached[1]
        if (
            not login
            or login.casefold() != action_login.casefold()
            or identity.login.casefold() != action_login.casefold()
        ):
            return None
        return identity

    def _remember_identity(self, result: WorkerResult) -> None:
        if result.identity is None or not result.credential_fingerprint:
            return
        _token, current_login, current_fingerprint = self._auth_context()
        if (
            current_fingerprint == result.credential_fingerprint
            and current_login.casefold() == result.identity.login.casefold()
        ):
            self._identity_cache = (result.credential_fingerprint, result.identity)

    def _uuid_resolution_finished(
        self,
        signals: _UUIDSignals,
        result: ObservationUUIDResolution,
    ) -> None:
        try:
            self._live_uuid_signals.discard(signals)
            if self._shutting_down:
                self.observation_uuid_resolved.emit(
                    ObservationUUIDResolution(
                        result.request_id,
                        result.observation_id,
                        diagnostic="manager_shutting_down",
                        exception=result.exception,
                    )
                )
                return
            self.observation_uuid_resolved.emit(result)
        except RuntimeError:
            # The application may have deleted the manager before a worker's
            # queued result reaches the UI event loop.
            return

    def _queue_immediate_uuid_resolution(self, result: ObservationUUIDResolution) -> None:
        """Queue even supplied UUIDs so callers receive the ID before its signal."""
        QTimer.singleShot(
            0,
            lambda resolution=result: self._emit_immediate_uuid_resolution(resolution),
        )

    def _emit_immediate_uuid_resolution(self, result: ObservationUUIDResolution) -> None:
        try:
            if self._shutting_down:
                self.observation_uuid_resolved.emit(
                    ObservationUUIDResolution(
                        result.request_id,
                        result.observation_id,
                        diagnostic="manager_shutting_down",
                    )
                )
                return
            self.observation_uuid_resolved.emit(result)
        except RuntimeError:
            # The application may have deleted the manager before the queued
            # zero-delay result reaches the UI event loop.
            return


def _verify_identification(
    action: dict[str, Any],
    observation: dict[str, Any],
    identity: AccountIdentity,
    server_id: str,
    server_uuid: str,
) -> VerificationResult:
    records, diagnostic, present = _records(observation, "identifications")
    if records is None:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, diagnostic)
    if not present:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "missing_identifications")
    payload = _action_payload(action)
    target_taxon = _as_positive_int(payload.get("taxon_id"))
    target_body = str(payload.get("body") or "")
    if not target_taxon:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "invalid_payload")
    if server_id or server_uuid:
        matching = [
            record for record in records
            if _record_identifier_matches(record, server_id, server_uuid)
        ]
        if not matching:
            return VerificationResult(VerificationStatus.NOT_FOUND, "returned_identification_not_found")
        return _match_identification_record(matching[0], identity, target_taxon, target_body)

    attempt_started_at = _as_timestamp(action.get("attempt_started_at"))
    if attempt_started_at is None:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "missing_attempt_timestamp")
    saw_old_match = False
    unreadable = ""
    for record in records:
        candidate = _match_identification_record(record, identity, target_taxon, target_body)
        if candidate.status is VerificationStatus.INSUFFICIENT_FIELDS:
            # One unreadable sibling -- an identification whose author was
            # suspended, so the API sends `user: null` -- must not hide our own
            # record further down the array.  Remember it and keep looking.
            unreadable = unreadable or candidate.diagnostic
            continue
        if not candidate.confirmed:
            continue
        created_at = _as_timestamp(record.get("created_at"))
        if created_at is None:
            unreadable = unreadable or "missing_identification_created_at"
            continue
        if created_at >= attempt_started_at - _CLOCK_TOLERANCE_SECONDS:
            return candidate
        saw_old_match = True
    if unreadable:
        # Something in this array could not be read, so "not found" would be a
        # claim the response does not support.
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, unreadable)
    return VerificationResult(
        VerificationStatus.NOT_FOUND,
        "only_preexisting_matching_identification" if saw_old_match else "matching_identification_not_found",
    )


def _match_identification_record(
    record: dict[str, Any],
    identity: AccountIdentity,
    target_taxon: int,
    target_body: str,
) -> VerificationResult:
    user, diagnostic = _record_user(record)
    if user is None:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, diagnostic)
    if not _identity_matches(user, identity):
        return VerificationResult(VerificationStatus.MISMATCHED, "identification_user_mismatch")
    if "taxon_id" not in record or not isinstance(record.get("taxon"), dict):
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "missing_identification_taxon")
    taxon_id = _as_positive_int(record.get("taxon_id"))
    nested_taxon_id = _as_positive_int(record["taxon"].get("id"))
    if not taxon_id or not nested_taxon_id:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "invalid_identification_taxon")
    if taxon_id != nested_taxon_id or taxon_id != target_taxon:
        return VerificationResult(VerificationStatus.MISMATCHED, "identification_taxon_mismatch")
    if "body" not in record:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "missing_identification_body")
    if str(record.get("body") or "") != target_body:
        return VerificationResult(VerificationStatus.MISMATCHED, "identification_body_mismatch")
    if record.get("current") is not True:
        # ``current`` is a supersession flag, not evidence that this action's
        # write failed: iNaturalist clears it as soon as the same account posts
        # a finer identification.  The record's existence still proves the
        # write reached the server, and reporting it as missing would invite a
        # duplicate resubmission -- exactly what this journal exists to prevent.
        return VerificationResult(VerificationStatus.CONFIRMED, "identification_superseded")
    return VerificationResult(VerificationStatus.CONFIRMED)


def _verify_comment(
    action: dict[str, Any],
    observation: dict[str, Any],
    identity: AccountIdentity,
    server_id: str,
    server_uuid: str,
) -> VerificationResult:
    records, diagnostic, present = _records(observation, "comments")
    if records is None:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, diagnostic)
    if not present:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "missing_comments")
    target_body = str(_action_payload(action).get("body") or "")
    if server_id or server_uuid:
        matching = [
            record for record in records
            if _record_identifier_matches(record, server_id, server_uuid)
        ]
        if not matching:
            return VerificationResult(VerificationStatus.NOT_FOUND, "returned_comment_not_found")
        return _match_comment_record(matching[0], identity, target_body)

    attempt_started_at = _as_timestamp(action.get("attempt_started_at"))
    if attempt_started_at is None:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "missing_attempt_timestamp")
    saw_old_match = False
    unreadable = ""
    for record in records:
        candidate = _match_comment_record(record, identity, target_body)
        if candidate.status is VerificationStatus.INSUFFICIENT_FIELDS:
            # See _verify_identification: an unreadable sibling comment must not
            # hide our own comment further down the array.
            unreadable = unreadable or candidate.diagnostic
            continue
        if not candidate.confirmed:
            continue
        created_at = _as_timestamp(record.get("created_at"))
        if created_at is None:
            unreadable = unreadable or "missing_comment_created_at"
            continue
        if created_at >= attempt_started_at - _CLOCK_TOLERANCE_SECONDS:
            return candidate
        saw_old_match = True
    if unreadable:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, unreadable)
    return VerificationResult(
        VerificationStatus.NOT_FOUND,
        "only_preexisting_matching_comment" if saw_old_match else "matching_comment_not_found",
    )


def _match_comment_record(
    record: dict[str, Any],
    identity: AccountIdentity,
    target_body: str,
) -> VerificationResult:
    user, diagnostic = _record_user(record)
    if user is None:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, diagnostic)
    if not _identity_matches(user, identity):
        return VerificationResult(VerificationStatus.MISMATCHED, "comment_user_mismatch")
    if "body" not in record or "created_at" not in record or "hidden" not in record:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "missing_comment_fields")
    if str(record.get("body") or "") != target_body:
        return VerificationResult(VerificationStatus.MISMATCHED, "comment_body_mismatch")
    # A deleted comment simply stops appearing in the observation's `comments`
    # array; the v2 Comment schema has no `deleted_at`, so there is nothing to
    # test for here beyond moderator hiding.  Hiding happens *after* a
    # successful post, so -- like a superseded identification -- the record's
    # existence still proves this action reached iNaturalist.  Reporting it as
    # missing would invite the user to post the comment a second time.
    if record.get("hidden") is not False:
        return VerificationResult(VerificationStatus.CONFIRMED, "comment_hidden")
    return VerificationResult(VerificationStatus.CONFIRMED)


def _verify_reviewed(
    action: dict[str, Any],
    observation: dict[str, Any],
    identity: AccountIdentity,
) -> VerificationResult:
    desired = bool(action["desired_state"])
    raw = observation.get("reviewed_by")
    present = "reviewed_by" in observation and raw is not None
    if present and not isinstance(raw, list):
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "invalid_reviewed_by")
    reviewer_ids: set[int] = set()
    unreadable = ""
    for value in (raw if isinstance(raw, list) else []):
        user_id = _as_positive_int(value)
        if not user_id:
            unreadable = unreadable or "invalid_reviewed_by"
            continue
        reviewer_ids.add(user_id)
    if identity.user_id in reviewer_ids:
        # Presence is conclusive whatever else the array contains.
        if desired:
            return VerificationResult(VerificationStatus.CONFIRMED)
        return VerificationResult(VerificationStatus.MISMATCHED, "reviewed_state_mismatch")
    if unreadable:
        # An entry we could not read may be ours, so absence is not established.
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, unreadable)
    if not desired:
        # Removing the review is proven by absence, which an omitted array
        # satisfies just as well as an empty one.
        return VerificationResult(VerificationStatus.CONFIRMED)
    if not present:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "missing_reviewed_by")
    return VerificationResult(VerificationStatus.MISMATCHED, "reviewed_state_mismatch")


def _verify_favorite(
    action: dict[str, Any],
    observation: dict[str, Any],
    identity: AccountIdentity,
) -> VerificationResult:
    faves, diagnostic, present = _records(observation, "faves")
    if faves is None:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, diagnostic)
    desired = bool(action["desired_state"])
    favorite_user_ids: set[int] = set()
    unreadable = ""
    for fave in faves:
        user, user_diagnostic = _record_user(fave)
        if user is None:
            # Another user's unreadable fave says nothing about our own.
            unreadable = unreadable or user_diagnostic
            continue
        favorite_user_ids.add(user.user_id)
    if identity.user_id in favorite_user_ids:
        if desired:
            return VerificationResult(VerificationStatus.CONFIRMED)
        return VerificationResult(VerificationStatus.MISMATCHED, "favorite_state_mismatch")
    if unreadable:
        # One of the records we could not read may be ours.
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, unreadable)
    if not desired:
        return VerificationResult(VerificationStatus.CONFIRMED)
    if not present:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "missing_faves")
    return VerificationResult(VerificationStatus.MISMATCHED, "favorite_state_mismatch")


def _verify_quality_metric(
    action: dict[str, Any],
    observation: dict[str, Any],
    identity: AccountIdentity,
) -> VerificationResult:
    """Verify only the exact authenticated user's "wild" quality-metric vote.

    This never falls back to ``observation.captive``, the aggregate quality
    grade, or another user's vote: those cannot establish what the
    authenticated user's own Data Quality Assessment vote is.
    """
    payload = _action_payload(action)
    metric = str(payload.get("metric") or "")
    vote = str(payload.get("vote") or "")
    if metric not in {"wild"} or vote not in {"agree", "disagree", "remove"}:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "invalid_payload")
    records, diagnostic, present = _records(observation, "quality_metrics")
    if records is None:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, diagnostic)

    matching_agree_values: list[bool] = []
    unreadable = ""
    for record in records:
        # Narrow to this metric and this user BEFORE validating any field.  The
        # array carries every user's vote on every DQA metric, and only `id` is
        # a required property, so another user's `needs_id` vote must never be
        # allowed to decide the outcome of our own "wild" vote.
        record_metric = record.get("metric")
        if not isinstance(record_metric, str) or record_metric != metric:
            continue
        user, user_diagnostic = _record_user(record)
        if user is None:
            unreadable = unreadable or user_diagnostic
            continue
        if not _identity_matches(user, identity):
            continue
        agree_value = record.get("agree")
        if not isinstance(agree_value, bool):
            unreadable = unreadable or "invalid_quality_metric_agree"
            continue
        matching_agree_values.append(agree_value)

    if vote == "remove":
        if matching_agree_values:
            return VerificationResult(VerificationStatus.MISMATCHED, "quality_metric_vote_still_present")
        if unreadable:
            # A vote on this metric we could not attribute may be ours.
            return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, unreadable)
        # Removal is proven by absence, which an omitted array satisfies.
        return VerificationResult(VerificationStatus.CONFIRMED)

    target_agree = vote == "agree"
    if target_agree in matching_agree_values:
        return VerificationResult(VerificationStatus.CONFIRMED)
    if unreadable:
        return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, unreadable)
    if not matching_agree_values:
        if not present:
            return VerificationResult(VerificationStatus.INSUFFICIENT_FIELDS, "missing_quality_metrics")
        return VerificationResult(VerificationStatus.NOT_FOUND, "quality_metric_vote_not_found")
    return VerificationResult(VerificationStatus.MISMATCHED, "quality_metric_agree_mismatch")


def _records(
    observation: dict[str, Any],
    field_name: str,
) -> tuple[list[dict[str, Any]] | None, str, bool]:
    """Return ``(records, diagnostic, present)`` for one verification array.

    A key the response omitted entirely (or sent as null) is reported as
    ``present=False`` with an empty record list rather than as a failure.  The
    caller decides what that means for its own action: an omitted array can
    legitimately prove an object is *absent* (an unfaved observation, a removed
    vote) but can never prove one is *present*.
    """
    if field_name not in observation or observation.get(field_name) is None:
        return [], "", False
    value = observation.get(field_name)
    if not isinstance(value, list):
        return None, f"invalid_{field_name}", True
    if not all(isinstance(record, dict) for record in value):
        return None, f"invalid_{field_name}", True
    return list(value), "", True


def _record_user(record: dict[str, Any]) -> tuple[AccountIdentity | None, str]:
    user = record.get("user")
    if not isinstance(user, dict):
        return None, "missing_record_user"
    user_id = _as_positive_int(user.get("id"))
    flat_user_id = (
        _as_positive_int(record.get("user_id")) if "user_id" in record else None
    )
    user_uuid = _normalise_uuid(user.get("uuid"))
    login = str(user.get("login") or "").strip()
    if not user_id or not login:
        return None, "invalid_record_user"
    # Some serializers include the redundant flat user_id and some omit it.
    # When present it must still agree with the nested stable numeric ID.
    if "user_id" in record and (not flat_user_id or user_id != flat_user_id):
        return None, "invalid_record_user"
    return AccountIdentity(user_id=user_id, uuid=user_uuid, login=login), ""


def _identity_matches(actual: AccountIdentity, expected: AccountIdentity) -> bool:
    return (
        actual.user_id == expected.user_id
        and actual.login.casefold() == expected.login.casefold()
        and (
            not actual.uuid
            or not expected.uuid
            or actual.uuid == expected.uuid
        )
    )


def _observation_matches_action(observation: dict[str, Any], action: dict[str, Any]) -> bool:
    return (
        _as_positive_int(observation.get("id")) == int(action["observation_id"])
        and _normalise_uuid(observation.get("uuid")) == str(action["observation_uuid"])
    )


def _record_identifier_matches(record: dict[str, Any], server_id: str, server_uuid: str) -> bool:
    if server_id and str(record.get("id") or "") == server_id:
        return True
    return bool(server_uuid and _normalise_uuid(record.get("uuid")) == server_uuid)


def _first_v2_resource(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    results = raw.get("results")
    if isinstance(results, list):
        return results[0] if results and isinstance(results[0], dict) else None
    return raw


def _extract_account_identity(raw: Any) -> AccountIdentity:
    user = _first_v2_resource(raw)
    if not isinstance(user, dict):
        raise ValueError("v2 users/me response had no user record")
    user_id = _as_positive_int(user.get("id"))
    user_uuid = _normalise_uuid(user.get("uuid"))
    login = str(user.get("login") or "").strip()
    if not user_id or not login:
        raise ValueError("v2 users/me response was missing id or login")
    return AccountIdentity(user_id=user_id, uuid=user_uuid, login=login)


def _response_metadata(response: Any) -> dict[str, Any]:
    metadata = getattr(response, "metadata", None)
    if metadata is None:
        return {}
    return {
        "endpoint": str(getattr(metadata, "endpoint", "")),
        "method": str(getattr(metadata, "method", "")),
        "status": int(getattr(metadata, "status_code", 0) or 0),
    }


def _error_response_metadata(exc: INatAPIError) -> dict[str, Any]:
    if not exc.response_received:
        return {}
    result: dict[str, Any] = {"endpoint": exc.endpoint, "method": exc.method}
    if exc.status_code is not None:
        result["status"] = exc.status_code
    return result


def _response_identifiers(response: Any) -> tuple[str, str]:
    resource = _first_v2_resource(response)
    if not isinstance(resource, dict):
        return "", ""
    identifier = str(resource.get("id") or "")
    return identifier, _normalise_uuid(resource.get("uuid"))


def _action_payload(action: dict[str, Any]) -> dict[str, Any]:
    raw = action.get("payload_json")
    if isinstance(raw, str):
        payload = json.loads(raw)
    elif isinstance(raw, dict):
        payload = raw
    else:
        raise ValueError("Identify action has no JSON payload")
    if not isinstance(payload, dict):
        raise ValueError("Identify action payload is not an object")
    return payload


def _journal_action_is_executable(action: dict[str, Any]) -> bool:
    try:
        if not _as_positive_int(action.get("observation_id")):
            return False
        if not _normalise_uuid(action.get("observation_uuid")):
            return False
        kind = str(action.get("action_type") or "")
        payload = _action_payload(action)
        if kind == "identification":
            return bool(_as_positive_int(payload.get("taxon_id"))) and isinstance(
                payload.get("body", ""), str
            )
        if kind == "comment":
            return isinstance(payload.get("body"), str) and bool(payload["body"].strip())
        if kind == "quality_metric":
            # desired_state must remain unset: the tri-state operation lives
            # entirely in the payload, never in reviewed/favorite's boolean.
            if action.get("desired_state") is not None:
                return False
            return payload.get("metric") == "wild" and payload.get("vote") in {
                "agree",
                "disagree",
                "remove",
            }
        return kind in {"reviewed", "favorite"} and _coerce_desired_state(action.get("desired_state")) is not None
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def _verification_outcome(verification: VerificationResult) -> ActionOutcome:
    if verification.status is VerificationStatus.CONFIRMED:
        return ActionOutcome.CONFIRMED
    if verification.status is VerificationStatus.MISMATCHED:
        return ActionOutcome.VERIFICATION_MISMATCH
    return ActionOutcome.SUBMITTED_UNVERIFIED


def _definite_write_state(exc: BaseException | None) -> str:
    # These responses establish that the unchanged payload cannot be safely
    # retried.  401/403 are left retryable because refreshing credentials or
    # account state may resolve them without mutating the journal payload.
    if isinstance(exc, INatAPIError) and exc.status_code in {400, 404, 409, 422}:
        return "failed_terminal"
    return "failed_retryable"


def _is_auth_error(exc: BaseException | None) -> bool:
    return isinstance(exc, INatAPIError) and exc.status_code in {401, 403}


def _credential_fingerprint(token: str, login: str) -> str:
    if not token:
        return ""
    # Memory-only context comparison; no token or digest reaches SQLite/logging.
    return hashlib.sha256(f"{login.casefold()}\0{token}".encode("utf-8")).hexdigest()


def _normalise_uuid(value: Any) -> str:
    try:
        return str(UUID(str(value or "").strip()))
    except (AttributeError, TypeError, ValueError):
        return ""


def _as_positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _as_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _safe_exception_diagnostic(exc: BaseException) -> str:
    if isinstance(exc, INatAPIError):
        if exc.status_code == 401:
            return "authentication_expired_or_rejected"
        if exc.status_code == 403:
            return "account_access_or_permission_problem"
        if exc.status_code in {400, 422}:
            return "payload_validation_failed"
        if exc.status_code == 404:
            return "observation_or_resource_unavailable"
        if exc.status_code == 409:
            return "server_conflict_requires_review"
        if exc.status_code == 429 or (exc.status_code is not None and exc.status_code >= 500):
            return "temporary_server_or_rate_limit_problem"
        if exc.status_code is not None:
            return f"http_{exc.status_code}"
        if exc.outcome_unknown:
            return "unsafe_transport_interruption"
    return type(exc).__name__.lower()


def _bounded_diagnostic_value(value: Any, *, depth: int = 0) -> Any:
    """Keep future diagnostic additions short and JSON-serializable."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:160]
    if depth >= 2:
        return str(value)[:160]
    if isinstance(value, dict):
        return {
            str(key)[:64]: _bounded_diagnostic_value(item, depth=depth + 1)
            for key, item in list(value.items())[:8]
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_diagnostic_value(item, depth=depth + 1)
            for item in value[:8]
        ]
    return str(value)[:160]


def _bounded_diagnostic_json(value: dict[str, Any], *, limit: int) -> str:
    """Serialize whole diagnostic fields only, never a partial JSON string."""
    bounded: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        candidate = {
            **bounded,
            str(raw_key)[:64]: _bounded_diagnostic_value(raw_value),
        }
        if len(json.dumps(candidate, sort_keys=True, separators=(",", ":"))) <= limit:
            bounded = candidate
    return json.dumps(bounded, sort_keys=True, separators=(",", ":"))


def _diagnostic_json(
    phase: ActionPhase,
    outcome: ActionOutcome,
    exc: BaseException | None = None,
    diagnostic: str = "",
    *,
    response_received: bool = False,
    response_metadata: dict[str, Any] | None = None,
) -> str:
    value: dict[str, Any] = {
        "phase": phase.value,
        "outcome": outcome.value,
        "response_received": bool(response_received),
    }
    if diagnostic:
        value["diagnostic"] = diagnostic[:160]
    if exc is not None:
        value["exception_type"] = type(exc).__name__[:160]
        if isinstance(exc, INatAPIError):
            if exc.endpoint:
                value["endpoint"] = str(exc.endpoint)[:255]
            if exc.status_code is not None:
                value["status"] = exc.status_code
    if response_metadata:
        response: dict[str, Any] = {}
        if "endpoint" in response_metadata:
            response["endpoint"] = str(response_metadata["endpoint"])[:255]
        if "method" in response_metadata:
            response["method"] = str(response_metadata["method"])[:32]
        if "status" in response_metadata:
            response["status"] = str(response_metadata["status"])[:32]
        value["response"] = response
    return _bounded_diagnostic_json(value, limit=2000)


def _verification_diagnostic_json(verification: VerificationResult) -> str:
    return _bounded_diagnostic_json(
        {"status": verification.status.value, "diagnostic": verification.diagnostic[:160]},
        limit=800,
    )
