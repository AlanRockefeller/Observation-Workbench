#!/usr/bin/env python3
"""Gate 2C Phase 1 live probe — characterise iNaturalist deletion, READ ONLY.

This script settles the ``NEEDS-LIVE-PROOF`` rows in
``docs/gate_2c_capability_note.md`` for iNaturalist: what an authenticated read
actually returns after a deletion, how fast that becomes stable, what the
deleted feed does, and — the load-bearing question — whether a real deletion is
DISTINGUISHABLE from every look-alike (nonexistent record, someone else's
record, bad credential, timeout). If it is not distinguishable, no verifier can
ever return "deleted" and the capability stays disabled.

Why a separate script, and why it never deletes
-----------------------------------------------
``DeletionService`` refuses to delete until the verifier is proven, and the
verifier cannot be proven without observing a real deletion. Enabling execution
to test execution is the trap. This breaks the circularity from the other side:
YOU delete disposable observations out-of-band (the iNaturalist website), and
this script only WATCHES. Nothing in ``observation_workbench.reconciliation.deletion``
runs, ``execution_enabled`` stays False, and no capability flag is touched.

Read-only is structural, not a promise: ``_ProbeClient.get`` is the only
request method and hardcodes ``"GET"``. There is no code path in this file that
can issue DELETE, POST, PUT, or PATCH.

Kept outside ``observation_workbench`` for the same reason as the Gate 1E/2A proof
harnesses: characterising a destructive capability must not wire anything into
the real application first. Nothing here imports the app, reads QSettings, or
touches the user's saved token.

What the bundled schema already settles (do not re-test)
-------------------------------------------------------
``api-docs.json`` defines ``ResultsObservationsDeleted.results`` as an array of
INTEGER, and ``since`` as a required ``format: date``. The deleted feed
therefore returns numeric ids by day, and can never carry the UUID identity
Phase 2C pins on. That makes the AUTHENTICATED UUID READ the primary evidence
and the feed a secondary cross-check — which is how ``--summarise`` scores it.

Usage
-----
    export INAT_JWT='...'      # https://www.inaturalist.org/users/api_token

    # 0. confirm identity and that the token works
    python tools/gate_2c_live_probe.py whoami

    # 1. distinguishability battery (no deletion needed) — run this FIRST
    python tools/gate_2c_live_probe.py lookalikes --uuid <an-owned-obs-uuid> \
        --other-uuid <someone-elses-obs-uuid> --log probe.jsonl

    # 2. watch one disposable observation across its deletion
    python tools/gate_2c_live_probe.py watch --uuid <disposable-obs-uuid> \
        --log probe.jsonl

    # 3. characterise the deleted feed
    python tools/gate_2c_live_probe.py feed --days 7 --log probe.jsonl

    # 4. derive the verifier signature from everything logged
    python tools/gate_2c_live_probe.py summarise --log probe.jsonl

Use ONLY disposable observations you created for this purpose on your own
account. Never a real record: iNaturalist deletion is permanent.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import uuid as uuidlib
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import httpx
except ImportError:  # pragma: no cover - operator-facing message
    sys.exit("httpx is required: ./.venv/bin/python -m pip install httpx")


INAT_V2 = "https://api.inaturalist.org/v2"
USER_AGENT = "iNat-Study-Gate2C-LiveProbe/1.0"

# The same identity-only field sets the app uses, so what this probe observes
# is what PhotoSyncService/DeletionService would observe.
IDENTITY_FIELDS = "(id:!t,uuid:!t,user:(id:!t,login:!t))"
DEEP_FIELDS = (
    "(id:!t,uuid:!t,user:(id:!t,login:!t),observed_on:!t,place_guess:!t,"
    "description:!t,geoprivacy:!t,license_code:!t,taxon:(id:!t,name:!t),"
    "photos:(id:!t,uuid:!t,license_code:!t),"
    "observation_photos:(id:!t,uuid:!t,photo:(id:!t,uuid:!t)),"
    "ofvs:(uuid:!t,field_id:!t,name:!t,value:!t),"
    "comments:(uuid:!t,user:(id:!t,login:!t)),"
    "identifications:(uuid:!t,user:(id:!t,login:!t)),"
    "annotations:(uuid:!t,controlled_attribute_id:!t),"
    "sounds:(id:!t,uuid:!t),faves:(id:!t))"
)

# Sampling schedule after the operator confirms the DELETE returned. Dense at
# the start because audit item 5 flags post-return visibility timing as
# unproven — a verifier that reads too early may see a stale positive.
DEFAULT_SCHEDULE = (0, 1, 2, 5, 10, 30, 60, 120, 300)


# ---------------------------------------------------------------------------
# Observation record
# ---------------------------------------------------------------------------


@dataclass
class Probe:
    """One recorded read. This is the unit ``summarise`` reasons over."""

    run_id: str
    scenario: str  # watch / lookalikes / feed / whoami
    probe: str  # auth_uuid / unauth_uuid / auth_numeric / feed / ...
    subject: str  # the uuid or id this read was about
    elapsed_s: float  # seconds since the deletion marker (watch only)
    at: str  # wall clock, UTC
    status_code: Optional[int]
    outcome: str  # present / absent / denied / ambiguous / error
    total_results: Optional[int] = None
    returned_uuid: str = ""
    returned_id: Optional[int] = None
    transport_error: str = ""
    note: str = ""
    body_excerpt: str = ""

    def line(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


class Ambiguous(RuntimeError):
    """A read whose outcome could not be determined (timeout / transport loss)."""


# ---------------------------------------------------------------------------
# Read-only client
# ---------------------------------------------------------------------------


class _ProbeClient:
    """GET-only client.

    Auth convention mirrors the app: the bare JWT is the ``Authorization``
    header value — NOT ``Bearer <jwt>``, which iNaturalist treats as
    unauthenticated. That distinction matters here more than anywhere, because
    "unauthenticated" and "deleted" can look identical and conflating them is
    exactly the failure this probe exists to rule out.
    """

    def __init__(
        self, jwt: str, *, timeout: float = 30.0, verbose: bool = False
    ) -> None:
        self._jwt = (jwt or "").strip().strip('"').strip("'")
        if self._jwt.lower().startswith("bearer "):
            self._jwt = self._jwt[7:].strip()
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
        """~1 request/second, the documented recommended limit."""
        remaining = 1.0 - (time.monotonic() - self._last)
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.monotonic()

    def get(
        self,
        path: str,
        *,
        params: Optional[dict] = None,
        token: Optional[str] = None,
        authenticate: bool = True,
        timeout: Optional[float] = None,
    ) -> tuple[int, Any]:
        """The ONLY request method in this file, and it is hardcoded to GET.

        ``token`` overrides the real credential (used for the deliberately
        invalid-credential look-alike); ``authenticate=False`` sends no
        Authorization header at all.
        """
        self._wait()
        headers: dict[str, str] = {}
        if authenticate:
            headers["Authorization"] = token if token is not None else self._jwt
        if self._verbose:
            shown = (
                "anon"
                if not authenticate
                else ("override" if token is not None else "auth")
            )
            print(f"    → GET {path} [{shown}]")
        try:
            response = self._client.request(
                "GET",
                path,
                params=params,
                headers=headers,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise Ambiguous(f"GET {path}: {type(exc).__name__}") from exc
        self._last = time.monotonic()
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {"_raw": response.text[:400]}
        if self._verbose:
            print(f"    ← {response.status_code} {json.dumps(payload)[:240]}")
        return response.status_code, payload


# ---------------------------------------------------------------------------
# Probe primitives
# ---------------------------------------------------------------------------


def _classify(
    status: int, payload: Any
) -> tuple[str, Optional[int], str, Optional[int]]:
    """Map one response to (outcome, total_results, returned_uuid, returned_id).

    ``outcome`` is deliberately coarse and never guesses "absent" from a bare
    404: distinguishing genuine deletion from the look-alikes is the whole
    point of the ``lookalikes`` scenario, and this function must not
    pre-empt that judgement. It reports what came back; ``summarise`` decides
    what is provably distinguishable.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    total = payload.get("total_results") if isinstance(payload, dict) else None
    first_uuid, first_id = "", None
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            first_uuid = str(first.get("uuid") or "")
            raw_id = first.get("id")
            first_id = int(raw_id) if isinstance(raw_id, int) else None
        elif isinstance(first, int):
            first_id = first
    if status in (401, 403):
        return "denied", total, first_uuid, first_id
    if status == 404:
        return "absent_404", total, first_uuid, first_id
    if status >= 500:
        return "server_error", total, first_uuid, first_id
    if status != 200:
        return f"http_{status}", total, first_uuid, first_id
    if isinstance(results, list):
        return ("present" if results else "absent_empty"), total, first_uuid, first_id
    if isinstance(payload, dict) and payload.get("uuid"):
        return "present", total, str(payload.get("uuid") or ""), first_id
    return "absent_empty", total, first_uuid, first_id


def _record(
    client: _ProbeClient,
    run_id: str,
    scenario: str,
    probe: str,
    subject: str,
    path: str,
    *,
    params: dict,
    elapsed: float = -1.0,
    note: str = "",
    token: Optional[str] = None,
    authenticate: bool = True,
    timeout: Optional[float] = None,
    store_excerpt: bool = True,
) -> Probe:
    at = datetime.now(timezone.utc).isoformat()
    try:
        status, payload = client.get(
            path,
            params=params,
            token=token,
            authenticate=authenticate,
            timeout=timeout,
        )
    except Ambiguous as exc:
        return Probe(
            run_id=run_id,
            scenario=scenario,
            probe=probe,
            subject=subject,
            elapsed_s=round(elapsed, 3),
            at=at,
            status_code=None,
            outcome="ambiguous",
            transport_error=str(exc),
            note=note,
        )
    outcome, total, ruuid, rid = _classify(status, payload)
    return Probe(
        run_id=run_id,
        scenario=scenario,
        probe=probe,
        subject=subject,
        elapsed_s=round(elapsed, 3),
        at=at,
        status_code=status,
        outcome=outcome,
        total_results=total,
        returned_uuid=ruuid,
        returned_id=rid,
        note=note,
        body_excerpt=json.dumps(payload, sort_keys=True)[:300] if store_excerpt else "",
    )


def probe_auth_uuid(client, run_id, scenario, obs_uuid, elapsed=-1.0, **kw) -> Probe:
    """The PRIMARY evidence: authenticated read of the exact stable identity."""
    return _record(
        client,
        run_id,
        scenario,
        "auth_uuid",
        obs_uuid,
        f"/observations/{obs_uuid}",
        params={"fields": IDENTITY_FIELDS},
        elapsed=elapsed,
        **kw,
    )


def probe_unauth_uuid(client, run_id, scenario, obs_uuid, elapsed=-1.0) -> Probe:
    """Never acceptable as proof (audit item 7) — recorded to demonstrate why."""
    return _record(
        client,
        run_id,
        scenario,
        "unauth_uuid",
        obs_uuid,
        f"/observations/{obs_uuid}",
        params={"fields": IDENTITY_FIELDS},
        elapsed=elapsed,
        authenticate=False,
        note="unauthenticated absence is never proof",
    )


def probe_auth_numeric(client, run_id, scenario, observation_id, elapsed=-1.0) -> Probe:
    """Numeric-id search, the weaker identity. A hit whose UUID differs is a
    DIFFERENT object (audit item 8) and must never satisfy the verifier."""
    return _record(
        client,
        run_id,
        scenario,
        "auth_numeric",
        str(observation_id),
        "/observations",
        params={"id": int(observation_id), "per_page": 1, "fields": IDENTITY_FIELDS},
        elapsed=elapsed,
    )


def probe_feed(
    client, run_id, scenario, since: str, elapsed=-1.0, *, fields: bool = True
) -> Probe:
    params: dict[str, Any] = {"since": since}
    if fields:
        # The app currently sends this. The schema says results are bare
        # integers, so it is probably inert — this records whether it is.
        params["fields"] = IDENTITY_FIELDS
    return _record(
        client,
        run_id,
        scenario,
        "feed" if fields else "feed_nofields",
        since,
        "/observations/deleted",
        params=params,
        elapsed=elapsed,
        note=f"since={since}",
    )


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------


class Log:
    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self.records: list[Probe] = []

    def add(self, probe: Probe) -> Probe:
        self.records.append(probe)
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(probe.line() + "\n")
        marker = f"{probe.elapsed_s:>7.1f}s" if probe.elapsed_s >= 0 else "      —"
        status = probe.status_code if probe.status_code is not None else "---"
        extra = ""
        if probe.returned_uuid:
            extra = f" uuid={probe.returned_uuid[:8]}…"
        elif probe.returned_id is not None:
            extra = f" id={probe.returned_id}"
        if probe.transport_error:
            extra = f" {probe.transport_error}"
        print(
            f"  {marker}  {probe.probe:<14} {str(status):>4}  {probe.outcome:<14}{extra}"
        )
        return probe


def load_log(path: Path) -> list[Probe]:
    records: list[Probe] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(Probe(**json.loads(line)))
        except (ValueError, TypeError) as exc:
            print(f"  skipping unreadable log line: {exc}", file=sys.stderr)
    return records


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_whoami(client: _ProbeClient, log: Log, run_id: str) -> int:
    print("\n--- whoami ---")
    probe = log.add(
        _record(
            client,
            run_id,
            "whoami",
            "whoami",
            "me",
            "/users/me",
            params={"fields": "(id:!t,login:!t)"},
        )
    )
    if probe.outcome != "present":
        print(
            "\nThe token did not authenticate. Everything else in this probe "
            "depends on it, so stop here."
        )
        return 1
    print("\nAuthenticated. Only delete observations owned by this account.")
    return 0


def scenario_baseline(
    client: _ProbeClient, log: Log, run_id: str, obs_uuid: str
) -> int:
    """Deep pre-deletion snapshot, so a later cascade check has a reference."""
    print("\n--- baseline (deep read, before any deletion) ---")
    # DEEP_FIELDS pulls in other users' logins/comments and place_guess;
    # don't persist that into the evidence log.
    probe = log.add(
        _record(
            client,
            run_id,
            "baseline",
            "auth_uuid_deep",
            obs_uuid,
            f"/observations/{obs_uuid}",
            params={"fields": DEEP_FIELDS},
            note="pre-deletion content inventory",
            store_excerpt=False,
        )
    )
    if probe.outcome != "present":
        print("\nThe observation is not readable, so there is nothing to watch.")
        return 1
    return 0


def scenario_watch(
    client: _ProbeClient,
    log: Log,
    run_id: str,
    obs_uuid: str,
    observation_id: Optional[int],
    schedule: tuple[int, ...],
    assume_deleted: bool,
) -> int:
    """Sample the transition across an out-of-band deletion.

    The operator performs the deletion on the website. This never deletes.
    """
    if scenario_baseline(client, log, run_id, obs_uuid) != 0:
        return 1
    # Resolve the numeric id from the UUID so the weaker-identity probe has a
    # subject, and so a later numeric hit can be compared against this UUID.
    if observation_id is None:
        try:
            _status, payload = client.get(
                f"/observations/{obs_uuid}",
                params={"fields": IDENTITY_FIELDS},
            )
        except Ambiguous:
            payload = None
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list) and results and isinstance(results[0], dict):
                raw = results[0].get("id")
                observation_id = int(raw) if isinstance(raw, int) else None
            elif isinstance(payload.get("id"), int):
                observation_id = int(payload["id"])
    print(f"\nWatching uuid={obs_uuid} id={observation_id}")

    if not assume_deleted:
        print(
            "\n" + "=" * 72 + "\n"
            "NOW DELETE THIS OBSERVATION YOURSELF, on the iNaturalist website:\n"
            f"    https://www.inaturalist.org/observations/{observation_id or obs_uuid}\n"
            "\nThis script will not delete it. Deletion is PERMANENT — be certain\n"
            "this is a disposable observation you created for this test.\n"
            "\nPress Enter the INSTANT the site confirms the deletion (the t=0\n"
            "sample is the one that shows whether a verifier can read too early).\n"
            + "=" * 72
        )
        try:
            input("\n  [Enter] once deleted, or Ctrl-C to abort: ")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted; nothing was deleted by this script.")
            return 130

    started = time.monotonic()
    today = date.today().isoformat()
    print("\n--- post-deletion samples ---")
    for offset in schedule:
        wait = offset - (time.monotonic() - started)
        if wait > 0:
            time.sleep(wait)
        elapsed = time.monotonic() - started
        log.add(probe_auth_uuid(client, run_id, "watch", obs_uuid, elapsed))
        if observation_id is not None:
            elapsed = time.monotonic() - started
            log.add(
                probe_auth_numeric(client, run_id, "watch", observation_id, elapsed)
            )
        elapsed = time.monotonic() - started
        log.add(probe_unauth_uuid(client, run_id, "watch", obs_uuid, elapsed))
        elapsed = time.monotonic() - started
        log.add(probe_feed(client, run_id, "watch", today, elapsed))
    print("\nWatch complete. Run `summarise` once you have logged a few of these.")
    return 0


def scenario_lookalikes(
    client: _ProbeClient,
    log: Log,
    run_id: str,
    owned_uuid: str,
    other_uuid: str,
) -> int:
    """The decisive experiment: is a deletion distinguishable from everything
    that merely LOOKS like one?

    Run this BEFORE deleting anything. If a nonexistent record, another user's
    record, a bad credential, or a timeout produce the same signature a real
    deletion will, then no verifier can honestly return "deleted" and the
    capability stays disabled — no amount of deletion testing changes that.
    """
    print("\n--- look-alikes (no deletion required) ---")
    nonexistent = str(uuidlib.uuid4())

    log.add(
        probe_auth_uuid(
            client,
            run_id,
            "lookalike",
            owned_uuid,
            note="control: an existing observation you own MUST read as present",
        )
    )
    log.add(
        probe_auth_uuid(
            client,
            run_id,
            "lookalike",
            nonexistent,
            note="never existed — must be distinguishable from deleted",
        )
    )
    log.add(
        _record(
            client,
            run_id,
            "lookalike",
            "auth_uuid",
            "not-a-uuid",
            "/observations/not-a-uuid",
            params={"fields": IDENTITY_FIELDS},
            note="malformed identity",
        )
    )
    log.add(
        _record(
            client,
            run_id,
            "lookalike",
            "auth_uuid_badtoken",
            owned_uuid,
            f"/observations/{owned_uuid}",
            params={"fields": IDENTITY_FIELDS},
            token="not-a-valid-jwt",
            note="bad credential — must NOT look like deleted",
        )
    )
    log.add(probe_unauth_uuid(client, run_id, "lookalike", owned_uuid))
    if other_uuid:
        log.add(
            probe_auth_uuid(
                client,
                run_id,
                "lookalike",
                other_uuid,
                note="another user's record — must NOT look like deleted",
            )
        )
    else:
        print("  (no --other-uuid given; the someone-else's-record case is UNTESTED)")
    log.add(
        _record(
            client,
            run_id,
            "lookalike",
            "auth_uuid_timeout",
            owned_uuid,
            f"/observations/{owned_uuid}",
            params={"fields": IDENTITY_FIELDS},
            timeout=0.001,
            note="forced timeout — must classify ambiguous, never absent",
        )
    )
    return 0


def scenario_feed(client: _ProbeClient, log: Log, run_id: str, days: int) -> int:
    """Characterise the deleted feed: granularity, retention, `fields` effect."""
    print("\n--- deleted feed ---")
    today = date.today()
    for back in (0, 1, min(7, days), days):
        since = (today - timedelta(days=back)).isoformat()
        log.add(probe_feed(client, run_id, "feed", since))
    log.add(probe_feed(client, run_id, "feed", today.isoformat(), fields=False))
    # `since` is documented as format: date. Record what a timestamp does.
    log.add(
        _record(
            client,
            run_id,
            "feed",
            "feed_timestamp",
            datetime.now(timezone.utc).isoformat(),
            "/observations/deleted",
            params={"since": datetime.now(timezone.utc).isoformat()},
            note="timestamp instead of date — schema says format: date",
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Summary — derive the verifier signature
# ---------------------------------------------------------------------------


def _signature(probe: Probe) -> str:
    return f"{probe.status_code}/{probe.outcome}"


def scenario_summarise(records: list[Probe]) -> int:
    if not records:
        print("No probe records in the log.")
        return 1
    print(f"\n{len(records)} probe records\n")

    look = [r for r in records if r.scenario == "lookalike"]
    watch_all = [r for r in records if r.scenario == "watch"]
    # A log file can accumulate several `watch` invocations (each a separate
    # process, each with its own run_id) against different observations. Mix
    # their elapsed-time samples together and the stability/timing analysis
    # below becomes meaningless — so only the most recent watch run is used
    # for that. Look-alike signatures are structural (status/outcome only,
    # not tied to a particular observation or run), so they are intentionally
    # left aggregated across all runs.
    watch_run_id = max((r.run_id for r in watch_all), default=None)
    watch = [r for r in watch_all if r.run_id == watch_run_id]

    print("=" * 72)
    print("DISTINGUISHABILITY  (can a verifier ever say 'deleted'?)")
    print("=" * 72)
    if not look:
        print("  no `lookalikes` run in this log — run it before trusting anything")
    else:
        for probe in look:
            print(f"  {probe.probe:<22} {_signature(probe):<22} {probe.note}")

    deleted_sig: set[str] = set()
    if watch:
        settled = [r for r in watch if r.probe == "auth_uuid" and r.elapsed_s >= 30]
        deleted_sig = {_signature(r) for r in settled}
        print(
            "\n  authenticated-UUID signature after deletion (t>=30s): "
            + (", ".join(sorted(deleted_sig)) or "none recorded")
        )

    if look and deleted_sig:
        collisions = []
        for probe in look:
            if probe.probe.startswith("auth_uuid") and _signature(probe) in deleted_sig:
                if "control" in probe.note:
                    continue
                collisions.append((probe.probe, _signature(probe), probe.note))
        print()
        if collisions:
            print("  *** COLLISION: these look-alikes are INDISTINGUISHABLE from a")
            print("      deletion. verify_exact_absence must NOT return 'deleted'")
            print("      on this signature; the capability stays disabled. ***")
            for name, sig, note in collisions:
                print(f"        {name:<22} {sig:<22} {note}")
        else:
            print("  No look-alike collides with the post-deletion signature.")
            print("  A verifier keyed on that exact signature is defensible.")

    print("\n" + "=" * 72)
    print("TIMING  (how early can the verifier read?)")
    print("=" * 72)
    if not watch:
        print("  no `watch` run in this log")
    else:
        by_probe: dict[str, list[Probe]] = {}
        for probe in watch:
            by_probe.setdefault(probe.probe, []).append(probe)
        for name, probes in sorted(by_probe.items()):
            probes.sort(key=lambda p: p.elapsed_s)
            trail = "  ".join(f"{p.elapsed_s:.0f}s:{_signature(p)}" for p in probes)
            print(f"  {name:<14} {trail}")
        auth = sorted(
            (p for p in watch if p.probe == "auth_uuid"), key=lambda p: p.elapsed_s
        )
        if auth:
            final = _signature(auth[-1])
            stable_from: Optional[float] = None
            for index, probe in enumerate(auth):
                if all(_signature(p) == final for p in auth[index:]):
                    stable_from = probe.elapsed_s
                    break
            # elapsed_s is a wall-clock float, so treat anything within a
            # small tolerance of zero as zero rather than requiring an exact
            # match — the schedule's first sample is rarely at t=0.000 sharp.
            STABLE_TOLERANCE_S = 0.05
            if stable_from is not None and stable_from > STABLE_TOLERANCE_S:
                print(
                    f"\n  *** The signature only became stable at t={stable_from:.0f}s."
                )
                print("      A verifier reading earlier than that can see a stale")
                print(
                    "      positive and wrongly report 'present'. Build in the delay. ***"
                )
            elif stable_from is not None:
                print("\n  Stable from t=0: no post-return visibility lag observed.")

    print("\n" + "=" * 72)
    print("DELETED FEED  (secondary cross-check only)")
    print("=" * 72)
    feed = [r for r in records if r.probe.startswith("feed")]
    if not feed:
        print("  no feed probes in this log")
    else:
        for probe in feed:
            shape = (
                "int-ids"
                if probe.returned_id is not None and not probe.returned_uuid
                else "?"
            )
            if probe.returned_uuid:
                shape = "CARRIES UUID (unexpected — re-read the schema)"
            print(
                f"  {probe.probe:<14} {probe.subject:<28} {_signature(probe):<20} "
                f"total={probe.total_results} {shape}"
            )
        print("\n  Reminder: the schema types results as integers, so the feed")
        print("  cannot establish UUID identity. Treat it as corroboration.")

    print("\n" + "=" * 72)
    print("NEXT")
    print("=" * 72)
    print("  Fill in docs/gate_2c_capability_note.md from the evidence above,")
    print("  then write DisabledProductionDeleteDispatch.verify_exact_absence to")
    print("  return 'deleted' ONLY on the exact settled signature, 'present' only")
    print("  on a positive read whose uuid matches, and 'ambiguous' otherwise —")
    print("  including every look-alike and every transport failure.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gate 2C Phase 1 live probe — READ ONLY, never deletes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--log", type=Path, help="append JSONL probe records here")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="scenario", required=True)

    sub.add_parser("whoami", help="confirm the token and show the account")

    p_watch = sub.add_parser("watch", help="sample one observation across its deletion")
    p_watch.add_argument("--uuid", required=True, help="disposable observation UUID")
    p_watch.add_argument(
        "--id", type=int, default=None, help="its numeric id (auto-resolved)"
    )
    p_watch.add_argument(
        "--schedule",
        default=",".join(str(v) for v in DEFAULT_SCHEDULE),
        help="comma-separated seconds after deletion to sample",
    )
    p_watch.add_argument(
        "--assume-deleted",
        action="store_true",
        help="skip the prompt; the observation is already deleted",
    )

    p_look = sub.add_parser(
        "lookalikes", help="distinguishability battery (no deletion)"
    )
    p_look.add_argument("--uuid", required=True, help="an observation you own and KEEP")
    p_look.add_argument(
        "--other-uuid", default="", help="an observation you do NOT own"
    )

    p_feed = sub.add_parser("feed", help="characterise the deleted feed")
    p_feed.add_argument("--days", type=int, default=30, help="furthest `since` to try")

    sub.add_parser("summarise", help="derive the verifier signature from --log")

    args = parser.parse_args()

    if args.scenario == "summarise":
        if not args.log or not args.log.exists():
            print("summarise needs an existing --log file.", file=sys.stderr)
            return 2
        return scenario_summarise(load_log(args.log))

    jwt = os.environ.get("INAT_JWT", "") or getpass.getpass(
        "iNaturalist JWT (hidden): "
    )
    if not jwt.strip():
        print("No iNaturalist JWT supplied.", file=sys.stderr)
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = Log(args.log)
    client = _ProbeClient(jwt, verbose=args.verbose)
    try:
        if scenario_whoami(client, log, run_id) != 0:
            return 1
        if args.scenario == "whoami":
            return 0
        if args.scenario == "watch":
            schedule = tuple(
                int(part) for part in str(args.schedule).split(",") if part.strip()
            )
            return scenario_watch(
                client,
                log,
                run_id,
                args.uuid,
                args.id,
                schedule,
                args.assume_deleted,
            )
        if args.scenario == "lookalikes":
            return scenario_lookalikes(client, log, run_id, args.uuid, args.other_uuid)
        if args.scenario == "feed":
            return scenario_feed(client, log, run_id, args.days)
    except KeyboardInterrupt:
        print("\nInterrupted; nothing was deleted by this script.", file=sys.stderr)
        return 130
    finally:
        client.close()
        if args.log:
            print(f"\nProbe records appended to {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
