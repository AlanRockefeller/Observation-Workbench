"""Read-only presentation policy for durable Identify journal rows.

Qt widgets use this module rather than repeating journal-state checks or
decoding payload JSON.  It keeps identification and comment body text out of
ordinary list rows; that text is exposed only in a deliberate Details view.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

_ACTIVE_STATES = frozenset(
    {"queued", "submitting", "submitted_unverified", "failed_retryable", "ambiguous"}
)
_ATTENTION_STATES = frozenset(
    {"submitted_unverified", "failed_retryable", "ambiguous", "failed_terminal"}
)


@dataclass(frozen=True)
class IdentifyActionPresentation:
    local_action_id: int
    observation_id: int
    observation_uuid: str
    account_login: str
    action_type: str
    state: str
    desired_state: bool | None
    created_at: float | None
    updated_at: float | None
    confirmed_at: float | None
    attempt_count: int
    verification_attempt_count: int
    parent_action_id: int | None
    manual_retry_count: int
    outcome_unknown: bool
    last_operation_phase: str
    server_object_id: str
    server_object_uuid: str
    verification_status: str
    verification_diagnostic: str
    error_summary: str
    intended_summary: str
    payload_body: str
    taxon_id: int | None
    can_cancel: bool
    can_resume_or_submit: bool
    can_verify: bool
    can_retry_definite_failure: bool
    can_retry_anyway: bool
    can_open_observation: bool
    is_active: bool
    requires_attention: bool

    @property
    def created_text(self) -> str:
        return format_journal_time(self.created_at)

    @property
    def updated_text(self) -> str:
        return format_journal_time(self.updated_at)

    @property
    def retry_lineage_text(self) -> str:
        if self.parent_action_id is not None:
            return f"Retry of action #{self.parent_action_id}"
        if self.manual_retry_count:
            count = self.manual_retry_count
            suffix = "retry" if count == 1 else "retries"
            return f"Source of {count} manual {suffix}"
        return ""

    @property
    def cancel_label(self) -> str:
        if self.state in {"ambiguous", "submitted_unverified"}:
            return "Stop tracking locally"
        if self.state == "failed_retryable":
            return "Cancel failed action"
        if self.state == "queued":
            return "Cancel queued action"
        return "Cancel action"

    @property
    def state_text(self) -> str:
        if (
            self.state == "submitted_unverified"
            and self.verification_status == "mismatched"
        ):
            # The write went through but the server state now disagrees with
            # intent (e.g. a later identification superseded this one). That
            # is a louder, attention-worthy condition than "not yet verified"
            # even though the durable state is the same submitted_unverified.
            return "Submitted — verification mismatch"
        return {
            "queued": "Queued",
            "submitting": "Submitting",
            "submitted_unverified": "Submitted; awaiting verification",
            "failed_retryable": "Failed; retry available",
            "failed_terminal": "Failed; correction required",
            "ambiguous": "Ambiguous — review required",
            "confirmed": "Confirmed",
            "cancelled": "Cancelled",
            "tracking_cancelled": "Local tracking stopped",
            "manual_retry_queued": "Legacy retry pending migration",
        }.get(self.state, self.state.replace("_", " ").capitalize())

    @property
    def is_recently_confirmed(self) -> bool:
        return (
            self.state == "confirmed"
            and self.confirmed_at is not None
            and time.time() - self.confirmed_at < 600
        )


def present_identify_action(action: Mapping[str, Any]) -> IdentifyActionPresentation:
    """Safely derive one UI-ready, credential-free action presentation."""
    payload = _payload(action.get("payload_json"))
    action_type = _text(action.get("action_type"))
    state = _text(action.get("state"))
    body = _text(payload.get("body"))
    taxon_id = _positive_int(payload.get("taxon_id"))
    desired_raw = action.get("desired_state")
    desired_state = None if desired_raw is None else bool(desired_raw)
    observation_id = _non_negative_int(action.get("observation_id"))
    outcome_unknown = bool(action.get("outcome_unknown"))
    error_summary = _diagnostic_summary(action.get("last_error_json"))
    verification_diagnostic = _diagnostic_summary(action.get("verification_diagnostic"))

    return IdentifyActionPresentation(
        local_action_id=_non_negative_int(action.get("local_action_id")),
        observation_id=observation_id,
        observation_uuid=_text(action.get("observation_uuid")),
        account_login=_text(action.get("account_login")),
        action_type=action_type,
        state=state,
        desired_state=desired_state,
        created_at=_timestamp(action.get("created_at")),
        updated_at=_timestamp(action.get("updated_at")),
        confirmed_at=_timestamp(action.get("confirmed_at")),
        attempt_count=_non_negative_int(action.get("attempt_count")),
        verification_attempt_count=_non_negative_int(
            action.get("verification_attempt_count")
        ),
        parent_action_id=_positive_int(action.get("parent_action_id")),
        manual_retry_count=_non_negative_int(action.get("manual_retry_count")),
        outcome_unknown=outcome_unknown,
        last_operation_phase=_text(action.get("last_operation_phase")),
        server_object_id=_text(
            action.get("server_object_id") or action.get("write_response_id")
        ),
        server_object_uuid=_text(action.get("write_response_uuid")),
        verification_status=_text(action.get("verification_status")),
        verification_diagnostic=verification_diagnostic,
        error_summary=error_summary,
        intended_summary=_intended_summary(
            action_type, desired_state, taxon_id, payload
        ),
        payload_body=body,
        taxon_id=taxon_id,
        can_cancel=state
        in {"queued", "failed_retryable", "ambiguous", "submitted_unverified"},
        can_resume_or_submit=state == "queued",
        can_verify=state in {"submitted_unverified", "ambiguous"},
        can_retry_definite_failure=state == "failed_retryable" and not outcome_unknown,
        can_retry_anyway=state == "ambiguous",
        can_open_observation=observation_id > 0,
        is_active=state in _ACTIVE_STATES,
        requires_attention=state in _ATTENTION_STATES,
    )


def compact_observation_action_text(actions: list[IdentifyActionPresentation]) -> str:
    """Produce a compact, multi-action status line for one Identify window."""
    labels: list[str] = []
    for action in actions:
        if (
            not action.is_active
            and not action.requires_attention
            and not action.is_recently_confirmed
        ):
            continue
        prefix = {
            "identification": "Identification",
            "comment": "Comment",
            "reviewed": "Reviewed change",
            "favorite": "Favorite change",
            "quality_metric": "Captive/Cultivated vote",
        }.get(action.action_type, "Action")
        if action.state == "confirmed":
            labels.append(f"{prefix} confirmed")
        elif action.parent_action_id is not None:
            labels.append(
                f"{prefix} retry queued from #{action.parent_action_id}"
                if action.state == "queued"
                else f"{prefix} ({action.state_text.lower()})"
            )
        else:
            labels.append(f"{prefix} {action.state_text.lower()}")
    return " · ".join(labels)


def describe_cancelled_opposite_actions(
    action_label: str,
    cancelled_action_ids: tuple[int, ...],
) -> str:
    """Describe queued opposite-intent rows a desired-state enqueue cancelled.

    ``CacheDB.enqueue_desired_state_action`` only ever cancels rows that were
    still ``queued``, and it can report cancellations alongside either a
    freshly inserted row or an unresolved duplicate row.  This helper is used
    for both cases so the wording never implies a submitting, ambiguous, or
    submitted-unverified row was cancelled, and it never exposes payload data
    -- only a bare count.
    """
    if not cancelled_action_ids:
        return ""
    count = len(cancelled_action_ids)
    noun = "action" if count == 1 else "actions"
    return f"Cancelled {count} obsolete queued opposite {action_label} {noun}."


def describe_cancelled_conflicting_actions(
    action_label: str,
    cancelled_action_ids: tuple[int, ...],
) -> str:
    """Describe queued conflicting-operation rows a tri-state enqueue cancelled.

    Unlike Reviewed/Favorite's two-state opposite, a quality-metric action
    has three possible operations (agree/disagree/remove), so a cancelled
    row is never described as merely "opposite" -- it is "conflicting".
    Used for both inserted and duplicate outcomes.
    """
    if not cancelled_action_ids:
        return ""
    count = len(cancelled_action_ids)
    noun = "action" if count == 1 else "actions"
    return f"Cancelled {count} obsolete queued conflicting {action_label} {noun}."


def format_journal_time(value: float | None) -> str:
    if value is None:
        return "—"
    try:
        return (
            datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        )
    except (OverflowError, OSError, ValueError):
        return "—"


def _intended_summary(
    action_type: str,
    desired_state: bool | None,
    taxon_id: int | None,
    payload: Mapping[str, Any] | None = None,
) -> str:
    if action_type == "identification":
        return f"Identify as taxon #{taxon_id}" if taxon_id else "Identification"
    if action_type == "comment":
        return "Comment"
    if action_type == "reviewed":
        if desired_state is True:
            return "Mark reviewed"
        if desired_state is False:
            return "Remove reviewed mark"
        return "Change reviewed mark"
    if action_type == "favorite":
        if desired_state is True:
            return "Add favorite"
        if desired_state is False:
            return "Remove favorite"
        return "Change favorite"
    if action_type == "quality_metric":
        vote = str((payload or {}).get("vote") or "")
        return {
            "disagree": "Mark Captive/Cultivated",
            "agree": "Vote organism is wild",
            "remove": "Remove Wild/Captive vote",
        }.get(vote, "Captive/Cultivated vote")
    return action_type.replace("_", " ").capitalize() or "Unknown action"


def _payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _diagnostic_summary(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return _short_text(raw, 180)
    if not isinstance(value, dict):
        return _short_text(raw, 180)
    parts = [
        _text(value.get("diagnostic")),
        _text(value.get("status")),
        _text(value.get("outcome")),
        _text(value.get("exception_type")),
    ]
    return _short_text(" · ".join(part for part in parts if part), 180)


def _short_text(value: str, limit: int = 96) -> str:
    normalized = " ".join(value.split())
    return (
        normalized
        if len(normalized) <= limit
        else f"{normalized[: max(1, limit - 1)]}…"
    )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        return float(value)
    return None
