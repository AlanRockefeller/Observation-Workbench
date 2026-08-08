#!/usr/bin/env python3
"""Gate 2A-M0 live-proof harness — run this yourself, against disposable records.

This script settles the single biggest open unknown in the Phase 2A plan
(``docs/gate_2a_capability_note.md``): whether Mushroom Observer's observation
creation endpoint exists, what it requires, and — the load-bearing question —
whether it exposes ANY field where an opaque client-chosen correlation marker
can be embedded at create time and independently searched for afterward. That
finding has a HARD binary outcome per the approved plan: if no such field
exists, Mushroom Observer is disabled as a Phase 2A creation destination for
this phase. There is no fallback (a marker written into a sequence or
external-link sub-resource cannot recover a lost creation response, because
attaching either of those needs the very observation id that would have been
lost).

Kept outside ``observation_workbench`` for the same reason as ``gate_1e_proof_harness.py``:
proving creation behaviour must not wire a write path into the real
application before the capability is confirmed. Nothing here imports the app,
reads QSettings, or touches the user's saved token.

It performs REAL WRITES (creates real observations) on REAL accounts. Guard
rails, identical in spirit to the Gate 1E harness:

* Dry-run is the default. Writes require ``--run --i-own-these-records``.
* Nothing is discovered, enumerated, or looped over — every write this script
  performs is a single disposable observation it creates itself and (where
  possible) deletes again during cleanup.
* Credentials come from the environment (``INAT_JWT``, ``MO_API_KEY``) or a
  hidden prompt. Never logged, echoed, or written to disk.
* Every created iNaturalist observation is deleted via ``DELETE
  /observations/{uuid}`` during cleanup (v2 exposes this, unlike photos).
  Every created Mushroom Observer observation is deleted if MO's API supports
  it; if it does not, the run reports the id so you can delete it on the
  website.

Usage
-----
    export INAT_JWT='...'          # from https://www.inaturalist.org/users/api_token
    python tools/gate_2a_proof_harness.py --inat --taxon-name "Amanita muscaria"

    # add --run --i-own-these-records to actually create+delete a test observation

    export MO_API_KEY='...'
    python tools/gate_2a_proof_harness.py --mo --taxon-name "Amanita muscaria" \
        --run --i-own-these-records
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import httpx
except ImportError:  # pragma: no cover - operator-facing message
    sys.exit("httpx is required: ./.venv/bin/python -m pip install httpx")


INAT_V2 = "https://api.inaturalist.org/v2"
MO_API2 = "https://mushroomobserver.org/api2"
USER_AGENT = "iNat-Study-Gate2A-ProofHarness/1.0"

OBS_FIELDS = (
    "(id:!t,uuid:!t,user:(id:!t,login:!t),description:!t,species_guess:!t,"
    "observed_on:!t,place_guess:!t)"
)

# A marker this harness plants in the description/notes field of every
# observation it creates, so a "lost response" recovery search has something
# unambiguous to grep for. Real Phase 2A code would use a shorter opaque
# token; this harness uses a self-describing one so a human glancing at the
# created record understands immediately why it exists.
MARKER_PREFIX = "[inat-study-gate2a-proof:"


PASS, FAIL, UNKNOWN, SKIP = "PASS", "FAIL", "UNKNOWN", "SKIP"


@dataclass
class Result:
    check_id: str
    title: str
    status: str = SKIP
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    blocking: bool = True


@dataclass
class Ledger:
    """Everything this run created, so cleanup and orphan reporting are honest."""

    inat_observation_uuids: list[str] = field(default_factory=list)
    mo_observation_ids: list[int] = field(default_factory=list)


class Ambiguous(RuntimeError):
    """A write whose outcome could not be determined (timeout / transport loss)."""


# ---------------------------------------------------------------------------
# iNaturalist v2 proof client
# ---------------------------------------------------------------------------


class INatProof:
    """Mirrors the app's auth convention: the bare JWT is the ``Authorization``
    header value — NOT ``Bearer <jwt>``, which iNaturalist treats as
    unauthenticated."""

    def __init__(
        self, jwt: str, *, timeout: float = 60.0, verbose: bool = False
    ) -> None:
        self._jwt = _normalise_jwt(jwt)
        self._verbose = verbose
        self._last = 0.0
        self._client = httpx.Client(
            base_url=INAT_V2,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _wait(self) -> None:
        remaining = 1.0 - (time.monotonic() - self._last)
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
    ) -> tuple[int, Any]:
        self._wait()
        if self._verbose:
            print(f"    → {method} {path}")
        try:
            response = self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                data=data,
                files=files,
                headers={"Authorization": self._jwt},
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise Ambiguous(f"{method} {path}: {type(exc).__name__}") from exc
        self._last = time.monotonic()
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {"_raw": response.text[:400]}
        if self._verbose:
            print(f"    ← {response.status_code} {json.dumps(payload)[:300]}")
        return response.status_code, payload

    def whoami(self) -> tuple[int, Any]:
        return self._request("GET", "/users/me", params={"fields": "(id:!t,login:!t)"})

    def observation(self, obs_uuid: str) -> tuple[int, Any]:
        return self._request(
            "GET", f"/observations/{obs_uuid}", params={"fields": OBS_FIELDS}
        )

    def create_observation(
        self,
        *,
        client_uuid: str,
        species_guess: str,
        observed_on_string: str,
        place_guess: str,
        description: str,
    ) -> tuple[int, Any]:
        """POST /observations. Body wraps under ``observation`` per
        ``api-docs.json``'s ``ObservationsCreate`` schema (confirmed by direct
        inspection, not assumed) — a sibling ``fields`` selector, same
        pattern as every other v2 read/write in this app."""
        return self._request(
            "POST",
            "/observations",
            params={"fields": OBS_FIELDS},
            json_body={
                "observation": {
                    "uuid": client_uuid,
                    "species_guess": species_guess,
                    "observed_on_string": observed_on_string,
                    "place_guess": place_guess,
                    "description": description,
                    "geoprivacy": "obscured",
                }
            },
        )

    def delete_observation(self, obs_uuid: str) -> tuple[int, Any]:
        """DELETE /observations/{uuid} — v2 exposes this for the owner, unlike
        the undeletable bare-photo-upload finding from Gate 1E."""
        return self._request("DELETE", f"/observations/{obs_uuid}")


# ---------------------------------------------------------------------------
# Mushroom Observer API2 proof client
# ---------------------------------------------------------------------------


class MOProof:
    """MO spaces requests conservatively (>=5s, or the server's reported runtime)."""

    def __init__(
        self, api_key: str = "", *, timeout: float = 60.0, verbose: bool = False
    ) -> None:
        self._key = api_key.strip()
        self._verbose = verbose
        self._last_finished = 0.0
        self._last_runtime = 0.0
        self._client = httpx.Client(
            base_url=MO_API2,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )

    def close(self) -> None:
        self._client.close()

    def _wait(self) -> None:
        minimum = max(5.0, self._last_runtime)
        remaining = minimum - (time.monotonic() - self._last_finished)
        while remaining > 0:
            time.sleep(min(0.25, remaining))
            remaining = minimum - (time.monotonic() - self._last_finished)

    def _request(
        self, method: str, endpoint: str, payload: dict, *, files: Optional[dict] = None
    ) -> tuple[int, Any]:
        self._wait()
        if self._verbose:
            print(f"    → MO {method} /{endpoint} {sorted(payload)}")
        body = {"format": "json", **payload}
        try:
            if method == "GET":
                response = self._client.get(f"/{endpoint}", params=body)
            else:
                response = self._client.request(
                    method, f"/{endpoint}", data=body, files=files
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self._last_finished = time.monotonic()
            raise Ambiguous(f"MO {method} /{endpoint}: {type(exc).__name__}") from exc
        self._last_finished = time.monotonic()
        try:
            parsed = response.json() if response.content else {}
        except ValueError:
            parsed = {"_raw": response.text[:400]}
        if isinstance(parsed, dict):
            try:
                self._last_runtime = float(parsed.get("run_time") or 0.0)
            except (TypeError, ValueError):
                self._last_runtime = 0.0
        if self._verbose:
            print(f"    ← {response.status_code} {json.dumps(parsed)[:300]}")
        return response.status_code, parsed

    def help_observations(self, method: str = "POST") -> tuple[int, Any]:
        """Probe the parameter list. Method-specific, per the Gate 1E finding
        for /images: GET returns query filters, POST returns create params."""
        payload: dict[str, Any] = {"help": 1}
        if self._key:
            payload["api_key"] = self._key
        return self._request(method, "observations", payload)

    def create_observation(self, fields: dict) -> tuple[int, Any]:
        payload = {"api_key": self._key, **fields}
        return self._request("POST", "observations", payload)

    def get_observation(self, observation_id: int) -> tuple[int, Any]:
        return self._request(
            "GET", "observations", {"id": int(observation_id), "detail": "high"}
        )

    def search_observations_by_notes(
        self, *, user_id: Optional[int], marker: str
    ) -> tuple[int, Any]:
        """Probe whether MO's list endpoint can filter by account + free text,
        which is exactly the M4 fallback recovery mechanism this run must
        prove viable (or not) before Mushroom Observer may ship as a creation
        destination."""
        payload: dict[str, Any] = {
            "api_key": self._key,
            "notes_has": marker,
            "detail": "low",
        }
        if user_id:
            payload["user"] = int(user_id)
        return self._request("GET", "observations", payload)

    def delete_observation(self, observation_id: int) -> tuple[int, Any]:
        return self._request(
            "DELETE", "observations", {"api_key": self._key, "id": int(observation_id)}
        )

    def whoami(self) -> tuple[int, Any]:
        payload: dict[str, Any] = {"api_key": self._key, "detail": "high"}
        return self._request("GET", "users", payload)


# ---------------------------------------------------------------------------
# Checks — iNaturalist create-endpoint proof
# ---------------------------------------------------------------------------


def run_inat_checks(inat: INatProof, taxon_name: str, ledger: Ledger) -> list[Result]:
    results: list[Result] = []

    def record(res: Result) -> Result:
        results.append(res)
        _print_result(res)
        return res

    status, payload = inat.whoami()
    if status >= 400:
        record(
            Result(
                "inat.auth",
                "JWT authenticates against v2",
                FAIL,
                f"HTTP {status} from /users/me — JWT missing/expired/wrongly Bearer-prefixed.",
            )
        )
        return results
    me = (payload.get("results") or [{}])[0]
    record(
        Result(
            "inat.auth",
            "JWT authenticates against v2",
            PASS,
            f"authenticated as {me.get('login')!r}",
        )
    )

    marker = f"{MARKER_PREFIX}{uuidlib.uuid4()}]"
    client_uuid = str(uuidlib.uuid4())

    # --- 1. POST /observations creates a real observation ------------------
    try:
        status, payload = inat.create_observation(
            client_uuid=client_uuid,
            species_guess=taxon_name,
            observed_on_string=time.strftime("%Y-%m-%d"),
            place_guess="(gate 2a proof harness — disposable test record)",
            description=marker,
        )
    except Ambiguous as exc:
        record(
            Result(
                "inat.create",
                "POST /observations creates an observation",
                UNKNOWN,
                str(exc),
            )
        )
        return results
    created = _first(payload)
    obs_uuid = str(created.get("uuid") or "")
    obs_id = created.get("id")
    if status >= 400 or not obs_uuid:
        record(
            Result(
                "inat.create",
                "POST /observations creates an observation",
                FAIL,
                f"HTTP {status}; no uuid in response",
                {"response": payload},
            )
        )
        return results
    ledger.inat_observation_uuids.append(obs_uuid)
    record(
        Result(
            "inat.create",
            "POST /observations creates an observation",
            PASS,
            f"created observation id={obs_id} uuid={obs_uuid}",
            {"observation_id": obs_id, "uuid": obs_uuid},
        )
    )

    # --- 2. client-supplied uuid round-trips exactly (the M4 anchor) -------
    matches = obs_uuid == client_uuid
    record(
        Result(
            "inat.uuid_anchor",
            "Client-supplied uuid becomes the observation's own uuid",
            PASS if matches else FAIL,
            f"sent {client_uuid!r}, observation uuid is {obs_uuid!r}"
            + (
                ""
                if matches
                else " — MISMATCH: the create uuid is NOT usable as a recovery anchor"
            ),
            {"sent": client_uuid, "returned": obs_uuid},
        )
    )

    # --- 3. exact-uuid re-fetch recovers a "lost" create response -----------
    try:
        status, payload = inat.observation(obs_uuid)
        found = _first(payload)
        recovered = str(found.get("uuid") or "") == obs_uuid
        record(
            Result(
                "inat.uuid_lookup",
                "GET /observations/{uuid} recovers a lost create response",
                PASS if status < 400 and recovered else FAIL,
                f"HTTP {status}; exact-uuid lookup {'succeeded' if recovered else 'failed'} — "
                "this is the iNat-side verify_unknown mechanism for Phase 2A",
                {
                    "marker_in_description": marker
                    in str(found.get("description") or "")
                },
            )
        )
    except Ambiguous as exc:
        record(
            Result(
                "inat.uuid_lookup",
                "GET /observations/{uuid} recovers a lost create response",
                UNKNOWN,
                str(exc),
            )
        )

    # --- 4. double-POST with the same uuid: dedupe or duplicate? ------------
    try:
        status, payload = inat.create_observation(
            client_uuid=client_uuid,
            species_guess=taxon_name,
            observed_on_string=time.strftime("%Y-%m-%d"),
            place_guess="(gate 2a proof harness — disposable test record, retry)",
            description=marker,
        )
        dup = _first(payload)
        dup_uuid = str(dup.get("uuid") or "")
        dup_id = dup.get("id")
        if status < 400 and dup_uuid and dup_uuid != obs_uuid:
            ledger.inat_observation_uuids.append(dup_uuid)
        if status >= 400:
            verdict, detail = (
                PASS,
                f"HTTP {status} — re-POST with the same uuid was REJECTED (not silently "
                "duplicated); verify_unknown must treat this status as 'already exists', "
                "not as a fresh failure",
            )
        elif dup_uuid == obs_uuid:
            verdict, detail = (
                PASS,
                f"same uuid returned the same observation {dup_uuid} — de-duplicated",
            )
        else:
            verdict, detail = (
                FAIL,
                f"same client uuid created a SECOND observation (id {dup_id}, uuid {dup_uuid} "
                f"!= {obs_uuid}) — the uuid is NOT idempotent for POST /observations; Phase 2A "
                "must never retry a create on outcome_unknown without a stronger anchor",
            )
        record(
            Result(
                "inat.uuid_idempotency",
                "Same-uuid re-POST does not create a second observation",
                verdict,
                detail,
                {
                    "first_uuid": obs_uuid,
                    "second_uuid": dup_uuid,
                    "second_status": status,
                },
            )
        )
    except Ambiguous as exc:
        record(
            Result(
                "inat.uuid_idempotency",
                "Same-uuid re-POST does not create a second observation",
                UNKNOWN,
                str(exc),
            )
        )

    # --- 5. cleanup via DELETE /observations/{uuid} -------------------------
    try:
        status, _ = inat.delete_observation(obs_uuid)
        deleted_ok = status < 400
        try:
            check_status, check_payload = inat.observation(obs_uuid)
            gone = check_status >= 400 or not _first(check_payload)
        except Ambiguous:
            gone = None
        if deleted_ok and obs_uuid in ledger.inat_observation_uuids:
            ledger.inat_observation_uuids.remove(obs_uuid)
        record(
            Result(
                "inat.delete",
                "DELETE /observations/{uuid} removes a created observation",
                PASS if deleted_ok else FAIL,
                f"HTTP {status}"
                + (f"; re-read confirms gone: {gone}" if gone is not None else ""),
            )
        )
    except Ambiguous as exc:
        record(
            Result(
                "inat.delete",
                "DELETE /observations/{uuid} removes a created observation",
                UNKNOWN,
                str(exc),
            )
        )

    return results


# ---------------------------------------------------------------------------
# Targeted MO recovery — one explicitly-named id, never enumeration.
#
# Exists because a prior run's ``mo.create`` returned HTTP 200 with no
# recognisable id in the response body (the parser's key guesses did not
# match MO's actual create-response shape), leaving a real, untracked test
# observation on the account. This mode operates on exactly one id the
# operator names explicitly (found by hand on the website), consistent with
# the harness's "nothing is discovered or enumerated" guard rail — it is the
# single-record analogue of Phase 2A's own real recovery path (find by
# planted marker, never blind-retry).
# ---------------------------------------------------------------------------


def run_mo_recover_checks(
    mo: MOProof, observation_id: int, ledger: Ledger
) -> list[Result]:
    results: list[Result] = []

    def record(res: Result) -> Result:
        results.append(res)
        _print_result(res)
        return res

    status, payload = mo.get_observation(observation_id)
    record(
        Result(
            "mo.recover_read",
            f"GET /api2/observations?id={observation_id} reads the named record",
            PASS if status < 400 else FAIL,
            f"HTTP {status}; raw response: {json.dumps(payload)[:1500]}",
            {"response": payload},
        )
    )
    rows = _mo_results(payload)
    if not rows:
        record(
            Result(
                "mo.recover_parse",
                "Response contains a parseable observation row",
                FAIL,
                f"No row found under any of the expected keys — raw top-level keys: "
                f"{sorted(payload) if isinstance(payload, dict) else type(payload).__name__}. "
                "Fix _mo_results()'s key list once you see the actual shape above.",
            )
        )
        return results
    row = rows[0]
    ledger.mo_observation_ids.append(observation_id)
    notes = str(row.get("notes") or "")
    marker = ""
    if MARKER_PREFIX in notes:
        start = notes.index(MARKER_PREFIX)
        end = notes.index("]", start) + 1
        marker = notes[start:end]
    record(
        Result(
            "mo.recover_parse",
            "Response contains a parseable observation row",
            PASS,
            f"id={row.get('id')}, name={row.get('name')!r}, notes={notes!r}"
            + (
                f" — marker extracted: {marker!r}"
                if marker
                else " — NO planted marker found in notes"
            ),
            {"row": row},
        )
    )

    if marker:
        status, payload = mo.search_observations_by_notes(user_id=None, marker=marker)
        found = _mo_results(payload)
        matched = any(_int_or_none(r.get("id")) == observation_id for r in found)
        record(
            Result(
                "mo.marker_searchable",
                "HARD OUTCOME: a create-time marker is independently searchable",
                PASS if matched else FAIL,
                f"HTTP {status}; search for the planted marker "
                + (
                    f"found observation {observation_id} — this IS a viable M4 recovery path"
                    if matched
                    else f"did NOT return observation {observation_id} — NOT a viable recovery path"
                ),
                {"matched_ids": [r.get("id") for r in found]},
            )
        )

    status, payload = mo.delete_observation(observation_id)
    error_text = _mo_error_text(payload)
    ok = status < 400 and not error_text
    if ok:
        ledger.mo_observation_ids.remove(observation_id)
    record(
        Result(
            "mo.recover_delete",
            f"DELETE removes observation {observation_id} (hygiene cleanup)",
            PASS if ok else FAIL,
            f"HTTP {status}"
            + (f"; MO error: {error_text}" if error_text else "; deleted"),
        )
    )
    return results


# ---------------------------------------------------------------------------
# Checks — Mushroom Observer create-endpoint proof (the hard-outcome gate)
# ---------------------------------------------------------------------------


def run_mo_checks(mo: MOProof, taxon_name: str, ledger: Ledger) -> list[Result]:
    results: list[Result] = []

    def record(res: Result) -> Result:
        results.append(res)
        _print_result(res)
        return res

    # --- 1. discover the create parameter list ------------------------------
    status, payload = mo.help_observations("POST")
    params = _mo_help_params(payload)
    record(
        Result(
            "mo.help_create",
            "POST /api2/observations create parameter names",
            PASS if params else FAIL,
            f"HTTP {status}; create parameters: {sorted(params) or 'none parsed — endpoint may not exist'}",
            {"parameters": sorted(params)},
        )
    )
    if not params:
        record(
            Result(
                "mo.hard_outcome",
                "HARD OUTCOME: Mushroom Observer creation destination",
                FAIL,
                "No creatable-observation endpoint was discovered. Per the approved Phase 2A "
                "plan, this is a hard NO-GO: Mushroom Observer must NOT be built as a creation "
                "destination this phase. Do not proceed past this point for MO.",
            )
        )
        return results

    _, who_payload = mo.whoami()
    me = _mo_results(who_payload)
    user_id = _int_or_none(me[0].get("id")) if me else None

    # --- 2. does any field accept free text a marker can live in, and is it
    #        searchable afterward? This is the entire hard-outcome question. ---
    marker = f"{MARKER_PREFIX}{uuidlib.uuid4()}]"
    notes_field = "notes" if "notes" in params else None
    if not notes_field:
        record(
            Result(
                "mo.marker_field",
                "A free-text create field exists to carry an opaque correlation marker",
                FAIL,
                f"No 'notes'-like field found among create parameters: {sorted(params)}",
            )
        )
        record(
            Result(
                "mo.hard_outcome",
                "HARD OUTCOME: Mushroom Observer creation destination",
                FAIL,
                "No field suitable for an opaque correlation marker was found. Per the approved "
                "plan, Mushroom Observer must NOT be built as a creation destination this phase.",
            )
        )
        return results

    try:
        status, payload = mo.create_observation(
            {
                "date": time.strftime("%Y-%m-%d"),
                "name": taxon_name,
                notes_field: marker,
                "location": "(gate 2a proof harness — disposable test record)",
            }
        )
    except Ambiguous as exc:
        record(
            Result(
                "mo.create",
                "POST /api2/observations creates an observation",
                UNKNOWN,
                f"{exc} — re-enumerate before any retry",
            )
        )
        return results

    error_text = _mo_error_text(payload)
    rows = _mo_results(payload)
    returned_id = _int_or_none(rows[0].get("id")) if rows else None
    record(
        Result(
            "mo.create",
            "POST /api2/observations creates an observation",
            PASS if status < 400 and not error_text else FAIL,
            f"HTTP {status}; returned id={returned_id}"
            + (f"; MO error: {error_text}" if error_text else ""),
            {"response": payload},
        )
    )
    if returned_id:
        ledger.mo_observation_ids.append(returned_id)
    else:
        record(
            Result(
                "mo.hard_outcome",
                "HARD OUTCOME: Mushroom Observer creation destination",
                UNKNOWN,
                "Create did not return a usable id and this harness does not enumerate/search "
                "to recover one blindly (that would contradict 'nothing is discovered or "
                "enumerated' guard rail) — re-run with a narrower manual follow-up to confirm.",
            )
        )
        return results

    # --- 3. is the marker actually searchable? -------------------------------
    try:
        status, payload = mo.search_observations_by_notes(
            user_id=user_id, marker=marker
        )
        found = _mo_results(payload)
        matched = any(_int_or_none(row.get("id")) == returned_id for row in found)
        record(
            Result(
                "mo.marker_searchable",
                "HARD OUTCOME: a create-time marker is independently searchable",
                PASS if matched else FAIL,
                f"HTTP {status}; search for the planted marker "
                + (
                    f"found the created observation {returned_id} — this IS a viable M4 recovery path"
                    if matched
                    else f"did NOT return observation {returned_id} — this field/search combination "
                    "is NOT a viable recovery path; Mushroom Observer must not ship as a "
                    "creation destination this phase unless a different searchable mechanism is found"
                ),
                {"matched_ids": [row.get("id") for row in found]},
            )
        )
    except Ambiguous as exc:
        record(
            Result(
                "mo.marker_searchable",
                "HARD OUTCOME: a create-time marker is independently searchable",
                UNKNOWN,
                str(exc),
            )
        )

    # --- 4. double-create idempotency behaviour ------------------------------
    try:
        status, payload = mo.create_observation(
            {
                "date": time.strftime("%Y-%m-%d"),
                "name": taxon_name,
                notes_field: marker,
                "location": "(gate 2a proof harness — disposable test record, retry)",
            }
        )
        dup_rows = _mo_results(payload)
        dup_id = _int_or_none(dup_rows[0].get("id")) if dup_rows else None
        dup_error = _mo_error_text(payload)
        if dup_id and dup_id != returned_id:
            ledger.mo_observation_ids.append(dup_id)
        record(
            Result(
                "mo.create_idempotency",
                "Double-create with the same marker: dedupe or silent duplicate?",
                PASS if dup_error or dup_id is None else FAIL,
                (
                    f"HTTP {status}; second create was rejected/errored ({dup_error}) — no silent duplicate"
                    if dup_error or dup_id is None
                    else f"HTTP {status}; second create SILENTLY produced a new observation {dup_id} "
                    f"(!= {returned_id}) — MO does NOT dedupe creates; Phase 2A's MO recovery path "
                    "must always search-before-retry and never rely on MO rejecting a duplicate"
                ),
                {"first_id": returned_id, "second_id": dup_id},
                blocking=False,
            )
        )
    except Ambiguous as exc:
        record(
            Result(
                "mo.create_idempotency",
                "Double-create with the same marker: dedupe or silent duplicate?",
                UNKNOWN,
                str(exc),
                blocking=False,
            )
        )

    # --- 5. cleanup: can created MO observations be deleted at all? ---------
    for oid in list(ledger.mo_observation_ids):
        try:
            status, payload = mo.delete_observation(oid)
            del_error = _mo_error_text(payload)
            ok = status < 400 and not del_error
            if ok:
                ledger.mo_observation_ids.remove(oid)
            record(
                Result(
                    "mo.delete",
                    "DELETE removes a created MO observation (informational, for hygiene)",
                    PASS if ok else UNKNOWN,
                    f"HTTP {status}; observation {oid} "
                    + ("deleted" if ok else f"NOT confirmed deleted: {del_error}"),
                    blocking=False,
                )
            )
        except Ambiguous as exc:
            record(
                Result(
                    "mo.delete",
                    "DELETE removes a created MO observation (informational, for hygiene)",
                    UNKNOWN,
                    str(exc),
                    blocking=False,
                )
            )

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_jwt(token: str) -> str:
    value = (token or "").strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith("bearer "):
        value = value.split(None, 1)[1].strip()
    return value


def _first(payload: Any) -> dict:
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list) and results:
            return results[0] if isinstance(results[0], dict) else {}
        if isinstance(results, dict):
            return results
        return payload
    return {}


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mo_results(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    for key in ("results", "observations", "users"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _mo_help_params(payload: Any) -> set[str]:
    """MO raises API2::HelpMessage and puts a usage string in errors[].details:
    ``Usage: date: date; name: string; notes: string; ...`` — same convention
    already discovered for /api2/images in Gate 1E."""
    found: set[str] = set()
    for error in _mo_errors(payload):
        details = str(error.get("details") or "")
        if "Usage:" not in details:
            continue
        usage = details.split("Usage:", 1)[1]
        for chunk in usage.split(";"):
            name = chunk.split(":", 1)[0].strip()
            if name and " " not in name and name.replace("_", "").isalnum():
                found.add(name)
    return found


def _mo_errors(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors")
    return (
        [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []
    )


def _mo_error_text(payload: Any) -> str:
    """MO returns HTTP 200 with a fatal error in the body."""
    parts = []
    for error in _mo_errors(payload):
        code = str(error.get("code") or "")
        details = str(error.get("details") or "").split("\n")[0]
        if "HelpMessage" in code:
            continue
        parts.append(f"{code}: {details}" if code else details)
    return "; ".join(parts)[:300]


_ICON = {PASS: "PASS", FAIL: "FAIL", UNKNOWN: "????", SKIP: "SKIP"}


def _print_result(res: Result) -> None:
    tag = _ICON[res.status]
    scope = "" if res.blocking else "  (informational)"
    print(
        f"  [{tag}] {res.check_id}{scope}\n         {res.title}\n         {res.detail}"
    )


# ---------------------------------------------------------------------------
# Cleanup + reporting
# ---------------------------------------------------------------------------


def cleanup(inat: Optional[INatProof], mo: Optional[MOProof], ledger: Ledger) -> None:
    print("\n--- cleanup ---")
    if inat:
        for obs_uuid in list(ledger.inat_observation_uuids):
            try:
                status, _ = inat.delete_observation(obs_uuid)
                print(f"  deleted iNaturalist observation {obs_uuid} (HTTP {status})")
                ledger.inat_observation_uuids.remove(obs_uuid)
            except Ambiguous as exc:
                print(f"  !! delete of {obs_uuid} outcome UNKNOWN: {exc}")
    if mo:
        for oid in list(ledger.mo_observation_ids):
            try:
                status, payload = mo.delete_observation(oid)
                if _mo_error_text(payload):
                    print(
                        f"  !! MO delete of {oid} reported an error: {_mo_error_text(payload)}"
                    )
                else:
                    print(f"  deleted MO observation {oid} (HTTP {status})")
                    ledger.mo_observation_ids.remove(oid)
            except Ambiguous as exc:
                print(f"  !! MO delete of {oid} outcome UNKNOWN: {exc}")
    if ledger.mo_observation_ids:
        print(
            "\n  NOTE: these MO observations could not be confirmed deleted. Check/remove"
        )
        print("  them on the website:")
        for oid in ledger.mo_observation_ids:
            print(f"    https://mushroomobserver.org/{oid}")


def summarise(results: list[Result]) -> int:
    print("\n" + "=" * 72)
    print("GATE 2A-M0 LIVE PROOF SUMMARY")
    print("=" * 72)
    blocking = [r for r in results if r.blocking]
    info = [r for r in results if not r.blocking]

    for label, group in (("BLOCKING", blocking), ("INFORMATIONAL", info)):
        if not group:
            continue
        print(f"\n{label}:")
        for res in group:
            print(f"  {_ICON[res.status]:<5} {res.check_id:<26} {res.title}")

    failed = [r for r in blocking if r.status == FAIL]
    unknown = [r for r in blocking if r.status == UNKNOWN]
    print("\n" + "-" * 72)
    mo_hard_outcome = next(
        (r for r in results if r.check_id == "mo.hard_outcome"), None
    )
    if mo_hard_outcome:
        print(
            f"Mushroom Observer hard outcome: {mo_hard_outcome.status} — "
            + (
                "MO may proceed as a creation destination (M4)."
                if mo_hard_outcome.status == PASS
                else "MO is DISABLED as a creation destination for this phase."
            )
        )
    if failed:
        print(f"RESULT: NOT CLEARED — {len(failed)} blocking check(s) failed.")
        for res in failed:
            print(f"  - {res.check_id}: {res.detail}")
        return 1
    if unknown:
        print(
            f"RESULT: INCONCLUSIVE — {len(unknown)} blocking check(s) unknown. Re-run."
        )
        return 2
    if not blocking:
        print("RESULT: nothing ran. Supply --inat and/or --mo.")
        return 2
    print("RESULT: CLEARED — every blocking check passed.")
    print(
        "Update docs/gate_2a_capability_note.md with these outcomes before M3 (schema) begins."
    )
    return 0


def write_report(results: list[Result], path: Path) -> None:
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks": [
            {
                "id": r.check_id,
                "title": r.title,
                "status": r.status,
                "blocking": r.blocking,
                "detail": r.detail,
                "evidence": r.evidence,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nMachine-readable results written to {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gate 2A-M0 live-proof harness (performs REAL writes when --run is given).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--inat", action="store_true", help="Run the iNaturalist create-endpoint proofs"
    )
    parser.add_argument(
        "--mo",
        action="store_true",
        help="Run the Mushroom Observer create-endpoint proofs",
    )
    parser.add_argument(
        "--mo-recover-id",
        type=int,
        default=0,
        metavar="ID",
        help=(
            "Skip the MO create step; instead read, marker-check, and delete this ONE "
            "explicitly-named MO observation id (recovery from a prior run whose create "
            "response could not be parsed for an id)."
        ),
    )
    parser.add_argument(
        "--taxon-name",
        default="Amanita muscaria",
        help="A real, unambiguous taxon/species name to submit on the test observation",
    )
    parser.add_argument(
        "--run", action="store_true", help="Actually execute (default is dry-run)"
    )
    parser.add_argument(
        "--i-own-these-records",
        action="store_true",
        help="Required with --run. Asserts you are creating disposable test records under your own account.",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Leave created test observations in place",
    )
    parser.add_argument(
        "--json-out", type=Path, help="Write machine-readable results here"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Log every request/response"
    )
    return parser


def plan(args: argparse.Namespace) -> None:
    print("DRY RUN — nothing was sent. This run would:\n")
    if args.mo_recover_id:
        print(f"  Mushroom Observer (recovery mode, id={args.mo_recover_id}):")
        print(
            "    - GET the named observation and print its raw response (fixes id parsing)"
        )
        print("    - extract the planted marker from its notes field, if present")
        print("    - search for that marker and confirm it resolves back to this id")
        print("    - DELETE the observation (hygiene cleanup)")
        return
    if args.inat:
        print("  iNaturalist:")
        print(
            f"    - POST /observations creating a disposable test observation ({args.taxon_name!r})"
        )
        print(
            "    - confirm the client-supplied uuid becomes the observation's own uuid"
        )
        print("    - GET /observations/{uuid} to prove exact-uuid recovery works")
        print("    - re-POST with the SAME uuid to check for silent duplication")
        print("    - DELETE /observations/{uuid} to clean up")
    if args.mo:
        print("\n  Mushroom Observer:")
        print(
            "    - probe POST /api2/observations?help=1 for the create parameter list"
        )
        print(
            "    - if a free-text field exists: create a disposable observation with an"
        )
        print(
            "      opaque marker embedded, then search for that marker (the HARD outcome"
        )
        print(
            "      check — determines whether MO may be a Phase 2A creation destination)"
        )
        print("    - re-create with the same marker to check for silent duplication")
        print("    - attempt to delete the created test observation(s)")
    print("\nRe-run with --run --i-own-these-records to execute.")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.inat and not args.mo and not args.mo_recover_id:
        print(
            "Nothing to do: pass --inat and/or --mo (or --mo-recover-id).",
            file=sys.stderr,
        )
        return 2

    if not args.run:
        plan(args)
        return 0

    if not args.i_own_these_records:
        print(
            "Refusing to write. --run requires --i-own-these-records, asserting these are\n"
            "disposable test records created under your own account.",
            file=sys.stderr,
        )
        return 2

    ledger = Ledger()
    results: list[Result] = []
    inat: Optional[INatProof] = None
    mo: Optional[MOProof] = None

    try:
        if args.inat:
            jwt = os.environ.get("INAT_JWT", "") or getpass.getpass(
                "iNaturalist JWT (hidden): "
            )
            if not jwt.strip():
                print("No iNaturalist JWT supplied.", file=sys.stderr)
                return 2
            inat = INatProof(jwt, verbose=args.verbose)
            print("\n--- iNaturalist proofs ---")
            results += run_inat_checks(inat, args.taxon_name, ledger)

        if args.mo or args.mo_recover_id:
            key = os.environ.get("MO_API_KEY", "") or getpass.getpass(
                "Mushroom Observer API key (hidden): "
            )
            if not key.strip():
                print("No Mushroom Observer API key supplied.", file=sys.stderr)
                return 2
            mo = MOProof(key, verbose=args.verbose)
            print("\n--- Mushroom Observer proofs ---")
            if args.mo_recover_id:
                results += run_mo_recover_checks(mo, args.mo_recover_id, ledger)
            else:
                results += run_mo_checks(mo, args.taxon_name, ledger)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    finally:
        if not args.no_cleanup:
            cleanup(inat, mo, ledger)
        elif ledger.inat_observation_uuids or ledger.mo_observation_ids:
            print("\n--no-cleanup: left these in place:")
            print(f"  iNaturalist observation uuids: {ledger.inat_observation_uuids}")
            print(f"  MO observation ids: {ledger.mo_observation_ids}")
        if inat:
            inat.close()
        if mo:
            mo.close()

    if args.json_out:
        write_report(results, args.json_out)
    return summarise(results)


if __name__ == "__main__":
    raise SystemExit(main())
