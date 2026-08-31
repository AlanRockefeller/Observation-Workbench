"""Authenticated durable DNA-link write state machine and recovery."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from observation_workbench.api.auth import AuthState
from observation_workbench.api.client import INatAPIError, INatClient

from .db import DNALinkingDB
from .discovery import contains_qualifying_dna, extract_observation_field_rows
from .types import DestinationState, FieldRow

CANONICAL_TEMPLATE = (
    "DNA barcode for this observation is in "
    "https://www.inaturalist.org/observations/{source_id}"
)
_URL_RE = re.compile(r"https?://(?:www\.)?inaturalist\.org/observations/\d+[^\s<>'\"]*", re.I)


@dataclass(frozen=True)
class LinkInspection:
    destination: DestinationState
    outcome: str
    current_value: str = ""
    contains_dna: bool = False
    diagnostic: str = ""
    captured_user_id: int = 0
    captured_login: str = ""
    captured_generation: int = 0


@dataclass(frozen=True)
class WriteResult:
    operation_id: int
    state: str
    message: str
    expected_absent: bool = False


def canonical_value(source_id: int) -> str:
    return CANONICAL_TEMPLATE.format(source_id=int(source_id))


def linked_observation_ids(value: object) -> set[int]:
    result: set[int] = set()
    for match in _URL_RE.finditer(str(value or "")):
        parsed = urlparse(match.group(0).rstrip(".,);]"))
        pieces = [piece for piece in parsed.path.split("/") if piece]
        if len(pieces) >= 2 and pieces[0].casefold() == "observations" and pieces[1].isdigit():
            result.add(int(pieces[1]))
    return result


def field_state_fingerprint(rows: tuple[FieldRow, ...]) -> str:
    document = sorted(
        ({"row_id": row.row_id, "value": row.value} for row in rows),
        key=lambda item: (item["row_id"], item["value"]),
    )
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class DNALinkingService:
    def __init__(
        self,
        client: INatClient,
        db: DNALinkingDB,
        auth_provider: Callable[[], AuthState],
        auth_generation_provider: Callable[[], int],
    ) -> None:
        self.client = client
        self.db = db
        self.auth_provider = auth_provider
        self.auth_generation_provider = auth_generation_provider

    def validated_account(self) -> tuple[int, str, int]:
        auth = self.auth_provider()
        if not auth.is_authenticated:
            raise RuntimeError("Authenticate to iNaturalist before using DNA linking.")
        generation = self.auth_generation_provider()
        result = self.client.get_current_user_v2(auth.api_token)
        account = _first_result(result)
        user_id = int(account.get("id") or 0)
        login = str(account.get("login") or "")
        if not user_id or login.casefold() != auth.login.casefold():
            raise RuntimeError("The authenticated iNaturalist account could not be revalidated.")
        return user_id, login, generation

    def inspect_destination(
        self, destination_id: int, source_id: int, field_id: int
    ) -> LinkInspection:
        if self.db.unresolved_for_destination(destination_id) is not None:
            raise RuntimeError(
                "This destination has an active or unresolved DNA-link write. Wait for "
                "it or use recovery verification before making another write."
            )
        user_id, login, generation = self.validated_account()
        auth = self.auth_provider()
        state = self._read_destination(destination_id, field_id, auth.api_token)
        rows = state.rows
        if any(source_id in linked_observation_ids(row.value) for row in rows):
            return LinkInspection(
                state, "already_linked", captured_user_id=user_id,
                captured_login=login, captured_generation=generation,
            )
        if not rows:
            return LinkInspection(
                state, "create", captured_user_id=user_id,
                captured_login=login, captured_generation=generation,
            )
        if len(rows) > 1:
            return LinkInspection(
                state, "blocked", diagnostic=(
                    "Duplicate DNA Barcode ITS field rows require manual cleanup before linking."
                ), captured_user_id=user_id, captured_login=login,
                captured_generation=generation,
            )
        row = rows[0]
        if not row.row_id:
            return LinkInspection(
                state, "blocked", diagnostic=(
                    "The existing DNA field row has no UUID; replacement is blocked."
                ), captured_user_id=user_id, captured_login=login,
                captured_generation=generation,
            )
        return LinkInspection(
            state, "conflict", current_value=row.value,
            contains_dna=contains_qualifying_dna(row.value),
            captured_user_id=user_id, captured_login=login,
            captured_generation=generation,
        )

    def apply_link(
        self, *, candidate_pk: int, source_id: int, destination_id: int,
        field_id: int, inspection: LinkInspection, replace: bool = False,
        is_cancelled: Callable[[], bool] = lambda: False,
    ) -> WriteResult:
        if inspection.outcome == "already_linked":
            self.db.append_review(candidate_pk, "already_linked")
            return WriteResult(0, "confirmed", "The destination already links to this source.")
        if inspection.outcome == "blocked":
            raise RuntimeError(inspection.diagnostic)
        if inspection.outcome == "conflict" and not replace:
            self.db.append_review(candidate_pk, "kept_existing")
            return WriteResult(0, "confirmed", "Existing value kept; no write was sent.")
        operation_type = "replace" if inspection.outcome == "conflict" else "create"
        expected = canonical_value(source_id)
        user_id, login, generation = self.validated_account()
        if inspection.captured_user_id and (
            inspection.captured_user_id != user_id
            or inspection.captured_login.casefold() != login.casefold()
            or inspection.captured_generation != generation
        ):
            raise RuntimeError(
                "Authentication changed after destination preflight; review Same organism again."
            )
        auth = self.auth_provider()
        operation_id = self.db.prepare_write(
            candidate_pk=candidate_pk, user_id=user_id, login=login,
            auth_generation=generation, destination_id=destination_id,
            destination_uuid=inspection.destination.observation_uuid,
            operation_type=operation_type, expected_value=expected,
            before_fingerprint=inspection.destination.fingerprint,
        )
        if is_cancelled():
            self.db.transition_write(operation_id, "cancelled", "Cancelled before submission")
            return WriteResult(operation_id, "cancelled", "Cancelled safely; no write was sent.")

        # Prepared work is safe to reject on an auth transition. Revalidate both
        # the captured account and complete remote field fingerprint immediately
        # before crossing the write boundary.
        if generation != self.auth_generation_provider():
            self.db.transition_write(operation_id, "cancelled", "Authentication changed before submission")
            return WriteResult(operation_id, "cancelled", "Authentication changed; no write was sent.")
        fresh_user_id, fresh_login, fresh_generation = self.validated_account()
        if (fresh_user_id, fresh_login.casefold(), fresh_generation) != (
            user_id, login.casefold(), generation
        ):
            self.db.transition_write(operation_id, "cancelled", "Authenticated account changed")
            return WriteResult(operation_id, "cancelled", "Authenticated account changed; no write was sent.")
        fresh = self._read_destination(destination_id, field_id, auth.api_token)
        if fresh.fingerprint != inspection.destination.fingerprint:
            self.db.transition_write(operation_id, "cancelled", "Destination DNA field changed before submission")
            return WriteResult(operation_id, "cancelled", "The destination field changed; review it again.")
        if generation != self.auth_generation_provider():
            self.db.transition_write(operation_id, "cancelled", "Authentication changed before submission")
            return WriteResult(operation_id, "cancelled", "Authentication changed; no write was sent.")
        if is_cancelled():
            self.db.transition_write(operation_id, "cancelled", "Cancelled before submission")
            return WriteResult(operation_id, "cancelled", "Cancelled safely; no write was sent.")

        self.db.transition_write(operation_id, "submitting")
        try:
            if operation_type == "create":
                self.client.create_observation_field_value_v2(
                    auth.api_token, fresh.observation_uuid, field_id, expected
                )
            else:
                row_id = fresh.rows[0].row_id
                self.client.update_observation_field_value_v2(
                    auth.api_token, row_id, fresh.observation_uuid, field_id, expected
                )
        except INatAPIError as exc:
            if not exc.outcome_unknown and exc.status_code in {400, 401, 403, 404, 409, 422}:
                self.db.transition_write(operation_id, "rejected", f"Definite HTTP rejection {exc.status_code}")
                return WriteResult(operation_id, "rejected", "iNaturalist rejected the write; it was not applied.")
            self.db.transition_write(operation_id, "uncertain", _safe_error(exc))
            # Even an ambiguous transport/5xx outcome gets one safe verification
            # read. It is never automatically resent.
            return self._verify_with_token(operation_id, field_id, auth.api_token)
        except Exception as exc:
            self.db.transition_write(operation_id, "uncertain", _safe_error(exc))
            return self._verify_with_token(operation_id, field_id, auth.api_token)

        self.db.transition_write(operation_id, "submitted_unverified")
        return self._verify_with_token(operation_id, field_id, auth.api_token)

    def verify_operation(self, operation_id: int, field_id: int) -> WriteResult:
        operation = self.db.operation(operation_id)
        auth = self.auth_provider()
        if not auth.is_authenticated:
            self.db.transition_write(operation_id, "uncertain", "Authentication unavailable for verification")
            return WriteResult(operation_id, "uncertain", "Write outcome requires manual verification.")
        try:
            account = _first_result(self.client.get_current_user_v2(auth.api_token))
            if (
                int(account.get("id") or 0) != int(operation["user_id"])
                or str(account.get("login") or "").casefold()
                != str(operation["login"]).casefold()
            ):
                self.db.transition_write(
                    operation_id, "uncertain", "Recovery authentication does not match captured account"
                )
                return WriteResult(
                    operation_id, "uncertain",
                    f"Authenticate as {operation['login']} before verifying this operation.",
                )
        except Exception as exc:
            self.db.transition_write(operation_id, "uncertain", _safe_error(exc))
            return WriteResult(operation_id, "uncertain", "Could not revalidate the captured account.")
        return self._verify_with_token(operation_id, field_id, auth.api_token)

    def _verify_with_token(
        self, operation_id: int, field_id: int, api_token: str
    ) -> WriteResult:
        operation = self.db.operation(operation_id)
        try:
            state = self._read_destination(
                int(operation["destination_id"]), field_id, api_token
            )
        except Exception as exc:
            self.db.transition_write(operation_id, "uncertain", _safe_error(exc))
            return WriteResult(operation_id, "uncertain", "Could not verify the remote field state.")
        expected = str(operation["expected_value"])
        if len(state.rows) == 1 and state.rows[0].value == expected:
            self.db.transition_write(operation_id, "confirmed", "Exact canonical value verified")
            event = "replaced" if operation["operation_type"] == "replace" else "created"
            self.db.append_review(int(operation["candidate_pk"]), event)
            return WriteResult(operation_id, "confirmed", "Exact canonical link verified on iNaturalist.")
        expected_absent = not state.rows
        self.db.transition_write(
            operation_id, "uncertain",
            "Expected value absent, divergent, duplicated, or unreadable",
        )
        return WriteResult(
            operation_id, "uncertain",
            "Remote state does not exactly match the canonical value; do not retry without recovery review.",
            expected_absent=expected_absent,
        )

    def retry_write_after_absence(
        self, operation_id: int, field_id: int, *, confirmed: bool
    ) -> WriteResult:
        """Explicit retry only after a verification proves expected state absent."""
        if not confirmed:
            raise RuntimeError("Retry Write requires explicit confirmation.")
        old = self.db.operation(operation_id)
        if old["state"] != "uncertain":
            raise RuntimeError("Only an uncertain operation can be retried.")
        user_id, login, _generation = self.validated_account()
        if (
            user_id != int(old["user_id"])
            or login.casefold() != str(old["login"]).casefold()
        ):
            raise RuntimeError(
                f"Authenticate as {old['login']} before retrying this operation."
            )
        auth = self.auth_provider()
        state = self._read_destination(int(old["destination_id"]), field_id, auth.api_token)
        expected = str(old["expected_value"])
        if any(row.value == expected for row in state.rows):
            return self.verify_operation(operation_id, field_id)
        if state.rows:
            raise RuntimeError(
                "Verification did not prove a safely empty destination; retry remains blocked."
            )
        # Resolve the old destination block explicitly, then create a new
        # journalled operation through the normal fresh-state boundary.
        self.db.transition_write(operation_id, "cancelled", "Explicit retry superseded absent outcome")
        inspection = LinkInspection(state, "create")
        return self.apply_link(
            candidate_pk=int(old["candidate_pk"]),
            source_id=_source_id_from_expected(expected),
            destination_id=int(old["destination_id"]), field_id=field_id,
            inspection=inspection,
        )

    def _read_destination(
        self, destination_id: int, field_id: int, api_token: str
    ) -> DestinationState:
        raw = self.client.get_reconciliation_detail(destination_id, api_token, deep=False)
        observation = _first_result(raw)
        if int(observation.get("id") or 0) != int(destination_id):
            raise RuntimeError("Destination observation could not be read.")
        uuid = str(observation.get("uuid") or "")
        if not uuid:
            raise RuntimeError("Destination observation UUID is missing.")
        rows = tuple(
            FieldRow(
                row_id=str(row.get("uuid") or ""),
                value=str(row.get("value") or ""),
            )
            for row in extract_observation_field_rows(observation, field_id)
        )
        return DestinationState(destination_id, uuid, rows, field_state_fingerprint(rows))


def _first_result(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    results = raw.get("results") or []
    if isinstance(results, list) and results and isinstance(results[0], Mapping):
        return results[0]
    if raw.get("id"):
        return raw
    raise RuntimeError("iNaturalist returned no matching record.")


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, INatAPIError) and exc.status_code:
        return f"{type(exc).__name__} HTTP {exc.status_code}"
    return type(exc).__name__


def _source_id_from_expected(expected: str) -> int:
    ids = linked_observation_ids(expected)
    if len(ids) != 1:
        raise RuntimeError("Stored canonical expectation is invalid.")
    return next(iter(ids))
