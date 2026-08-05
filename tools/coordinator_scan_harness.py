#!/usr/bin/env python3
"""Disposable offline proof for ReconciliationCoordinator's scan pipeline.

This is not a repository test. It never opens a network connection, never reads
a credential, and never touches the user's real reconciliation database or
QSettings -- every path is redirected into a temporary directory, and that
redirection is asserted before any coordinator is constructed.

Why it exists
-------------
Every other harness in ``tools/`` builds the reconciliation *services* directly
and skips the coordinator, so nothing exercised ``_scan_mo``/``_scan_inat``,
the request builders they call, or the ``_generation`` cancellation machinery.
Four live-reproducible defects survived in exactly that gap.

The remote fakes are therefore installed as ``httpx.MockTransport`` handlers,
*below* the request builders rather than in place of them. A wrong parameter
name or a wrongly formatted parameter value reaches the handler and is rejected
the way Mushroom Observer actually rejects it -- as a FATAL error carried in an
HTTP 200 body. Faking at the client-method level (``FakeMO.observations_page``)
would have hidden all four defects.

MO's parameter allow-lists and error conventions below were recorded from live
``?help=1`` probes on 2026-07-27.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading

SANDBOX = tempfile.TemporaryDirectory(prefix="coordinator-scan-")
# Must precede every Qt import: QStandardPaths.AppDataLocation is what
# ReconciliationDB() falls back to, and it is derived from XDG_DATA_HOME.
os.environ["XDG_DATA_HOME"] = str(Path(SANDBOX.name) / "data")
os.environ["XDG_CONFIG_HOME"] = str(Path(SANDBOX.name) / "config")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone  # noqa: E402

import httpx  # noqa: E402

from PySide6.QtCore import QCoreApplication, QEventLoop, QSettings, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from observation_workbench.api.auth import AuthState  # noqa: E402
from observation_workbench.api.client import INatClient  # noqa: E402
from observation_workbench.models import StudyPhoto  # noqa: E402
import observation_workbench.reconciliation.coordinator as coordinator_module  # noqa: E402
from observation_workbench.reconciliation.coordinator import (  # noqa: E402
    ReconciliationCoordinator, _mo_time_range, _total,
)
from observation_workbench.reconciliation.db import ReconciliationDB, _utc_now  # noqa: E402
from observation_workbench.reconciliation.mo_client import (  # noqa: E402
    MO_API_BASE, MOClient, ReconciliationCancelled,
)
from observation_workbench.storage.settings import AppSettings  # noqa: E402

PROFILE_ID = 1
INAT_USER_ID = 500
INAT_LOGIN = "inat_user"
MO_USER_ID = 900
MO_LOGIN = "mo_user"
MO_FIELD_ID = 77
INAT_SITE_ID = 2
MO_PAGE_SIZE = 1000


class HarnessFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessFailure(message)


# --------------------------------------------------------------------------
# Mushroom Observer fake
# --------------------------------------------------------------------------

# Recorded live from /api2/<endpoint>?help=1 on 2026-07-27, plus the parameters
# MO accepts globally. Anything outside these sets is an API2::UnusedParameters
# FATAL error -- which MO returns with HTTP 200, not an error status.
_MO_GLOBAL_PARAMS = {"format", "detail", "page", "api_key"}
_MO_ENDPOINT_PARAMS = {
    "observations": {
        "user", "id", "updated_at", "created_at", "date", "notes_has",
        "has_specimen", "has_images", "has_name", "gps_hidden", "north",
        "south", "east", "west",
    },
    "external_sites": set(),
    "external_links": {"id", "observation"},
    "names": {"id"},
    # NOTE: no "observation" -- this is the whole point of the sequences check.
    "sequences": {"id", "observer", "user", "name", "herbarium", "locus",
                  "obs_date", "accession_has", "archive", "notes_has"},
    "images": {"id", "observation"},
}

# MO's time parser: each side is 4/6/8/10/12/14 digits, optionally as a range.
_MO_TIME_POINT = re.compile(r"^\d{4}(?:\d{2}){0,5}$")


def _mo_fatal(code: str, details: str) -> dict:
    """MO reports fatal errors in an HTTP 200 body; mimic that exactly."""
    return {
        "version": 2.0,
        "errors": [{"code": code, "details": details, "fatal": "true"}],
        "run_time": 0.01,
    }


def _mo_valid_time_range(value: str) -> bool:
    parts = value.split("-")
    if len(parts) not in (1, 2):
        return False
    return all(_MO_TIME_POINT.match(part) for part in parts)


def _mo_observation(observation_id: int, *, updated_at: str) -> dict:
    return {
        "id": observation_id,
        "owner_id": MO_USER_ID,
        "owner": {"id": MO_USER_ID, "login": MO_LOGIN},
        "consensus": {
            "id": 321, "text_name": "Amanita muscaria", "rank": "Species",
            "classification": {"kingdom": "Fungi"},
        },
        "date": "2026-06-01",
        "updated_at": updated_at,
        "location": {"id": 5, "name": "Some Forest"},
        "latitude": 47.1,
        "longitude": -122.1,
        "gps_accuracy": 10,
        "notes": "offline note",
        "herbarium_records": [],
        "collection_numbers": [],
        "images": [],
    }


class MORemote:
    """In-memory MO account served through the real MOClient request path."""

    def __init__(self, observation_ids: tuple[int, ...]) -> None:
        self.observations = {
            value: _mo_observation(value, updated_at="2026-07-01T00:00:00.000Z")
            for value in observation_ids
        }
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.page_size = MO_PAGE_SIZE
        self._lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        params = {key: value for key, value in request.url.params.items()}
        with self._lock:
            self.requests.append((endpoint, dict(params)))
        return httpx.Response(200, json=self._payload(endpoint, params))

    def _payload(self, endpoint: str, params: dict[str, str]) -> dict:
        if params.get("help"):
            # The capability probe. The details blob must mention updated_at,
            # which is how discover_observation_capabilities decides.
            return {
                "version": 2.0,
                "errors": [{
                    "code": "API2::HelpMessage",
                    "details": "Usage: updated_at: time range; user: user list",
                }],
            }
        allowed = _MO_GLOBAL_PARAMS | _MO_ENDPOINT_PARAMS.get(endpoint, set())
        unexpected = sorted(set(params) - allowed - {"help"})
        if unexpected:
            return _mo_fatal(
                "API2::UnusedParameters",
                f"Unexpected parameters: {', '.join(unexpected)}",
            )
        if "updated_at" in params and not _mo_valid_time_range(params["updated_at"]):
            return _mo_fatal(
                "API2::BadParameterValue",
                f'Invalid time range, "{params["updated_at"]}", expect '
                '"YYYYMMDDHHMMSS-YYYYMMDDHHMMSS", … "YYYYMMDD", "YYYY".',
            )
        if endpoint == "observations":
            return self._observations(params)
        if endpoint == "external_sites":
            return self._results([
                {"id": 1, "name": "MyCoPortal"},
                {"id": INAT_SITE_ID, "name": "iNaturalist"},
            ])
        if endpoint == "external_links":
            wanted = _int_csv(params.get("observation", ""))
            return self._paginated([
                {
                    "id": 10_000 + value,
                    "url": f"https://www.inaturalist.org/observations/{value}",
                    "observation_id": value,
                    "external_site_id": INAT_SITE_ID,
                }
                for value in wanted if value in self.observations
            ], params)
        if endpoint == "names":
            return self._paginated([
                {"id": value, "classification": {"kingdom": "Fungi"}}
                for value in _int_csv(params.get("id", ""))
            ], params)
        if endpoint == "sequences":
            # Only 'observer' can select an account's sequences; 'user' would
            # select by sequence AUTHOR and miss third-party rows.
            if "observer" not in params:
                return self._paginated([], params)
            row: dict = {
                "id": 1,
                "observation_id": next(iter(sorted(self.observations)), 0),
            }
            # MO's low serializer strips real fields (proven for /images). A row
            # missing 'locus' is DROPPED by its._mo_composites, which reads as
            # "this observation has no ITS sequence" and makes the ITS gate
            # propose MO_SEQUENCE_ADD -- writing a duplicate sequence onto an
            # observation that already has one. Model the strip so a caller that
            # forgets detail=high fails here instead of on MO.
            if params.get("detail") == "high":
                row.update({
                    "locus": "ITS", "accession": "MK000001", "archive": "GenBank",
                    "bases": "ACGT" * 30, "notes": "",
                    "user": {"id": MO_USER_ID, "login": MO_LOGIN},
                    "user_id": MO_USER_ID,
                    "created_at": "2026-07-01 00:00:00",
                    "updated_at": "2026-07-01 00:00:00",
                })
            return self._paginated([row], params)
        return self._paginated([], params)

    def _observations(self, params: dict[str, str]) -> dict:
        if "id" in params:
            wanted = _int_csv(params["id"])
            return self._results([
                self.observations[value] for value in wanted
                if value in self.observations
            ])
        rows = [self.observations[key] for key in sorted(self.observations)]
        page = max(1, int(params.get("page") or 1))
        start = (page - 1) * self.page_size
        return self._results(rows[start:start + self.page_size], total=len(rows))

    def _paginated(self, rows: list[dict], params: dict[str, str]) -> dict:
        """Serve one page and report the GRAND total, exactly as MO does.

        Every non-/observations endpoint used to report number_of_records as
        the length of the page it just returned. That made
        `len(combined) >= total` true after page 1 for every query, so
        MOClient._paged's loop never requested page 2 in any scenario and its
        repeat-page/short-page termination logic was dead code here.
        """
        page = max(1, int(params.get("page") or 1))
        start = (page - 1) * self.page_size
        return self._results(rows[start:start + self.page_size], total=len(rows))

    def _results(self, rows: list[dict], total: int | None = None) -> dict:
        # MO reports the total as 'number_of_records' -- deliberately NOT
        # 'total_results'. A reader that only knows the iNaturalist spelling
        # silently sees a total of zero.
        return {
            "version": 2.0,
            "number_of_records": len(rows) if total is None else total,
            "number_of_pages": 1,
            "page_number": 1,
            "results": rows,
            "run_time": 0.01,
        }


def _parse_inat_timestamp(value: object) -> datetime | None:
    """Accept exactly what iNaturalist accepts for an ISO cursor."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # Naive and aware cursors must stay comparable: coordinator._overlap emits
    # a naive string whenever the stored cursor had no timezone.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _int_csv(value: str) -> list[int]:
    result = []
    for part in str(value).split(","):
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


class OfflineMOClient(MOClient):
    """Real MOClient; only the socket and the 5s request spacing are replaced."""

    def __init__(self, remote: MORemote) -> None:
        super().__init__()
        self.remote = remote
        self._client.close()
        self._client = httpx.Client(
            base_url=MO_API_BASE,
            transport=httpx.MockTransport(remote.handler),
            follow_redirects=False,
        )

    def _wait(self, cancelled) -> None:
        if cancelled():
            raise ReconciliationCancelled("Reconciliation scan cancelled")


# --------------------------------------------------------------------------
# iNaturalist fake
# --------------------------------------------------------------------------

def _inat_observation(observation_id: int, mo_target: int | None) -> dict:
    ofvs = []
    if mo_target is not None:
        ofvs.append({
            "id": f"ofv-{observation_id}",
            "uuid": f"ofv-uuid-{observation_id}",
            "observation_field": {"id": MO_FIELD_ID, "name": "Mushroom Observer URL"},
            "value": f"https://mushroomobserver.org/{mo_target}",
        })
    return {
        "id": observation_id,
        "uuid": f"inat-uuid-{observation_id}",
        "user": {"id": INAT_USER_ID, "login": INAT_LOGIN},
        "taxon": {
            "id": 123, "name": "Amanita muscaria", "rank": "species",
            "iconic_taxon_name": "Fungi", "ancestry": "48460/47170/123",
        },
        "observed_on": "2026-06-01",
        "updated_at": "2026-07-01T00:00:00+00:00",
        "place_guess": "Some Forest",
        "geojson": {"coordinates": [-122.1, 47.1]},
        "positional_accuracy": 10,
        "description": "offline note",
        "ofvs": ofvs,
        "observation_photos": [],
    }


class INatRemote:
    def __init__(self, links: dict[int, int]) -> None:
        self.observations = {
            value: _inat_observation(value, target)
            for value, target in links.items()
        }
        self.deleted_ids: list[int] = []
        self.deleted_reported_total: int | None = None
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.updated_since_seen: list[str] = []
        self._lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = {key: value for key, value in request.url.params.items()}
        with self._lock:
            self.requests.append((path, dict(params)))
        if path.endswith("/observation_fields/autocomplete"):
            return httpx.Response(200, json={"results": [{
                "id": MO_FIELD_ID,
                "name": "Mushroom Observer URL",
                "datatype": "text",
            }] if params.get("q") == "Mushroom Observer URL" else []})
        if path.endswith("/observations/deleted"):
            total = (
                len(self.deleted_ids) if self.deleted_reported_total is None
                else self.deleted_reported_total
            )
            return httpx.Response(200, json={
                "total_results": total, "page": 1,
                "per_page": len(self.deleted_ids), "results": self.deleted_ids,
            })
        if path.endswith("/observations"):
            return httpx.Response(200, json=self._observations(params))
        return httpx.Response(200, json={"total_results": 0, "results": []})

    def _observations(self, params: dict[str, str]) -> dict:
        if "id" in params:
            wanted = _int_csv(params["id"])
            rows = [
                self.observations[value] for value in wanted
                if value in self.observations
            ]
            return {"total_results": len(rows), "results": rows}
        rows = [self.observations[key] for key in sorted(self.observations)]
        above = int(params.get("id_above") or 0)
        rows = [row for row in rows if row["id"] > above]
        # updated_since, page and per_page must all be HONOURED, not ignored.
        # While this fake served every row on every request regardless of
        # them, the coordinator's incremental branch could never be exercised:
        # a full result set always tripped `len(items) < 200`, so `page += 1`
        # never ran, and a broken/misspelled updated_since filter was
        # indistinguishable from a working one.
        updated_since = params.get("updated_since") or ""
        if updated_since:
            self.updated_since_seen.append(updated_since)
            cutoff = _parse_inat_timestamp(updated_since)
            if cutoff is None:
                # iNaturalist rejects an unparseable updated_since rather than
                # silently serving everything; mimic that so a bad cursor
                # format is loud here instead of invisible.
                return {"error": "invalid updated_since", "total_results": 0, "results": []}
            rows = [
                row for row in rows
                if (_parse_inat_timestamp(row.get("updated_at")) or cutoff) >= cutoff
            ]
        total = len(rows)
        per_page = max(1, int(params.get("per_page") or 200))
        page = max(1, int(params.get("page") or 1))
        start = (page - 1) * per_page
        return {
            "total_results": total,
            "page": page,
            "per_page": per_page,
            "results": rows[start:start + per_page],
        }


class OfflineINatClient(INatClient):
    def __init__(self, remote: INatRemote) -> None:
        super().__init__()
        self.remote = remote
        self._client.close()
        self._client = httpx.Client(
            base_url="https://api.inaturalist.org/v1",
            transport=httpx.MockTransport(remote.handler),
            follow_redirects=True,
        )
        self._rate._min_interval = 0.0


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

class Environment:
    def __init__(
        self, *, mo_ids: tuple[int, ...] = (10, 11),
        inat_links: dict[int, int] | None = None,
    ) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="coordinator-scan-db-")
        self.path = Path(self.temp.name) / "reconciliation.db"
        self.mo_remote = MORemote(mo_ids)
        self.inat_remote = INatRemote(
            inat_links if inat_links is not None else {20: 10, 21: 11}
        )
        self.inat = OfflineINatClient(self.inat_remote)
        self.auth = AuthState("offline-inat-token", INAT_LOGIN)
        self.settings = AppSettings()

        # ReconciliationCoordinator constructs ReconciliationDB() with no path,
        # which resolves to the user's real AppDataLocation. Pin it to the
        # sandbox and prove it landed there before anything is written.
        original_db = coordinator_module.ReconciliationDB
        path = self.path
        coordinator_module.ReconciliationDB = lambda: original_db(path)
        try:
            self.coordinator = ReconciliationCoordinator(
                self.inat, lambda: self.auth, self.settings,
            )
        finally:
            coordinator_module.ReconciliationDB = original_db
        check(
            Path(self.coordinator.db.path) == self.path,
            "the coordinator database escaped the sandbox",
        )
        self.coordinator.mo_client.close()
        self.coordinator.mo_client = OfflineMOClient(self.mo_remote)
        self.db = self.coordinator.db
        self._insert_profile()

    def _insert_profile(self) -> None:
        now = _utc_now()
        self.db.connection().execute(
            "INSERT INTO sync_profiles(profile_id,inat_user_id,inat_login,"
            "mo_user_id,mo_login,created_at,last_used_at) VALUES(?,?,?,?,?,?,?)",
            (PROFILE_ID, INAT_USER_ID, INAT_LOGIN, MO_USER_ID, MO_LOGIN, now, now),
        )
        self.db.save_field_binding(
            PROFILE_ID, "mo_url", MO_FIELD_ID, "Mushroom Observer URL",
            "text", "verified",
        )

    def scan(self, *, force_full: bool = False, timeout_ms: int = 30_000) -> str:
        """Run one scan to completion, returning 'ok:<msg>' or 'fail:<msg>'."""
        loop = QEventLoop()
        outcome: list[str] = []

        def finished(message: str) -> None:
            outcome.append(f"ok:{message}")
            loop.quit()

        def failed(message: str) -> None:
            outcome.append(f"fail:{message}")
            loop.quit()

        self.coordinator.scan_finished.connect(finished)
        self.coordinator.scan_failed.connect(failed)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        try:
            self.coordinator.scan(PROFILE_ID, force_full=force_full)
            loop.exec()
        finally:
            timer.stop()
            self.coordinator.scan_finished.disconnect(finished)
            self.coordinator.scan_failed.disconnect(failed)
        check(bool(outcome), "the scan neither finished nor failed before timeout")
        return outcome[0]

    def mo_requests(self, endpoint: str) -> list[dict[str, str]]:
        return [
            params for name, params in self.mo_remote.requests
            if name == endpoint and not params.get("help")
        ]

    def close(self) -> None:
        self.coordinator.shutdown()
        self.temp.cleanup()


def pump(milliseconds: int = 50) -> None:
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

def full_scan_completes() -> None:
    """Finding 1: 'limit' on /observations is a FATAL MO error at HTTP 200.

    With the parameter present, MO returns API2::UnusedParameters and the whole
    scan aborts; before the fatal-error check existed it instead reported an
    empty MO inventory, which is indistinguishable from a genuinely empty
    account.
    """
    env = Environment()
    try:
        result = env.scan(force_full=True)
        check(result.startswith("ok:"), f"full scan failed: {result}")
        pages = env.mo_requests("observations")
        check(bool(pages), "no MO observation page was requested")
        for params in pages:
            check(
                "limit" not in params,
                "MO /observations was sent 'limit', which MO rejects as fatal",
            )
        records = env.db.inventory_records(PROFILE_ID, "mo")
        check(
            {item.key.observation_id for item in records} == {10, 11},
            f"MO inventory did not persist: {[i.key.observation_id for i in records]}",
        )
        inat_records = env.db.inventory_records(PROFILE_ID, "inat")
        check(
            {item.key.observation_id for item in inat_records} == {20, 21},
            "iNaturalist inventory did not persist",
        )
    finally:
        env.close()
    print("  ok  full scan completes and never sends 'limit' to MO")


def incremental_scan_sends_mo_time_range() -> None:
    """Finding 2: MO's updated_at is a YYYYMMDDHHMMSS range, never ISO 8601."""
    env = Environment()
    try:
        check(env.scan(force_full=True).startswith("ok:"), "seed scan failed")
        before = len(env.mo_remote.requests)
        result = env.scan()
        check(result.startswith("ok:"), f"incremental scan failed: {result}")
        filtered = [
            params for endpoint, params in env.mo_remote.requests[before:]
            if endpoint == "observations" and "updated_at" in params
        ]
        check(bool(filtered), "the incremental scan sent no updated_at filter")
        for params in filtered:
            value = params["updated_at"]
            check(
                _mo_valid_time_range(value),
                f"updated_at={value!r} is not an MO time range",
            )
            check(
                "-" in value,
                f"updated_at={value!r} is a single point in time, not a lower bound",
            )
    finally:
        env.close()
    print("  ok  incremental scan sends a well-formed MO updated_at range")


def mo_time_range_helper_is_well_formed() -> None:
    value = _mo_time_range("2026-07-20T07:00:00+00:00")
    start, _, end = value.partition("-")
    check(start == "20260720070000", f"unexpected range start {start!r}")
    check(_mo_valid_time_range(value), f"{value!r} is not an MO time range")
    check(end > start, "the range end must follow its start")
    naive = _mo_time_range("2026-07-20T07:00:00")
    check(naive.startswith("20260720070000"), f"naive cursor mishandled: {naive}")
    print("  ok  _mo_time_range renders MO's documented range format")


def mo_totals_are_read() -> None:
    """Finding 4: MO reports 'number_of_records', not 'total_results'."""
    check(_total({"number_of_records": 6900}) == 6900, "MO total was not read")
    check(_total({"total_results": 12}) == 12, "iNaturalist total was not read")
    check(_total({"total": 7}) == 7, "the 'total' fallback is unreachable")
    check(_total({}) == 0, "an absent total must read as 0")
    check(_total({"number_of_records": "x"}) == 0, "a junk total must read as 0")
    print("  ok  _total reads both MO and iNaturalist spellings")


def truncated_deleted_feed_holds_cursor() -> None:
    """Finding 5: a short deleted feed must not advance the cursor."""
    env = Environment()
    try:
        check(env.scan(force_full=True).startswith("ok:"), "seed scan failed")
        baseline = env.db.cursor(PROFILE_ID, "inat_deleted")
        check(bool(baseline), "a complete deleted feed should advance the cursor")

        env.inat_remote.deleted_ids = [20]
        env.inat_remote.deleted_reported_total = 3  # two ids are unreachable
        check(env.scan(force_full=True).startswith("ok:"), "second scan failed")
        after = env.db.cursor(PROFILE_ID, "inat_deleted")
        check(
            after == baseline,
            "the deleted cursor advanced past a truncated feed; unseen "
            "deletions would be lost permanently",
        )
        issues = {
            str(row["issue_type"]) for row in env.db.connection().execute(
                "SELECT issue_type FROM sync_issues WHERE profile_id=? "
                "AND state='open'",
                (PROFILE_ID,),
            ).fetchall()
        }
        check(
            "deleted_feed_incomplete" in issues,
            f"the truncated read was not surfaced: {sorted(issues)}",
        )
    finally:
        env.close()
    print("  ok  a truncated deleted feed holds the cursor and raises an issue")


def dropped_action_result_releases_the_action_lock() -> None:
    """Finding 3: a generation bump must not latch _action_running forever."""
    env = Environment()
    try:
        coordinator = env.coordinator
        release = threading.Event()
        started = threading.Event()

        def blocked(_progress):
            started.set()
            release.wait(10.0)
            return "ignored"

        coordinator._action_running = True
        coordinator._start_worker(
            coordinator.action_pool, "harness_action", coordinator.generation,
            blocked,
            lambda *_args: None,
            lambda *_args: None,
        )
        check(started.wait(5.0), "the action worker never started")

        # Exactly what ReconciliationSetupDialog's "Resolve exact accounts"
        # does to a coordinator shared by another reconciliation window.
        coordinator.cancel()
        release.set()
        for _ in range(40):
            pump(25)
            if not coordinator._action_running:
                break
        check(
            not coordinator._action_running,
            "_action_running stayed latched after its result was dropped; "
            "every later action would be refused until restart",
        )
    finally:
        env.close()
    print("  ok  a dropped action result releases the action lock")


def sequences_select_by_observer() -> None:
    """Finding 8: /api2/sequences has no 'observation' parameter."""
    remote = MORemote((10, 11))
    client = OfflineMOClient(remote)
    try:
        payload = client.sequences(MO_USER_ID, (10,), lambda: False)
        sent = [params for endpoint, params in remote.requests if endpoint == "sequences"]
        check(bool(sent), "no sequences request was issued")
        for params in sent:
            check(
                "observation" not in params,
                "sequences was filtered by 'observation', which MO rejects as fatal",
            )
            check(
                params.get("observer") == str(MO_USER_ID),
                "sequences must select by observer (the observation's OWNER), "
                "so third-party sequence rows are still returned",
            )
        for params in sent:
            # The composite parser needs locus/bases/accession/archive/user.
            # MO's low serializer strips them, and a stripped row is dropped
            # silently -- which reads as "no ITS sequence here" and makes the
            # ITS gate propose a DUPLICATE MO_SEQUENCE_ADD.
            check(
                params.get("detail") == "high",
                "sequences must be read at detail=high; a low-detail row loses "
                "locus/bases/accession and reads as 'no sequence'",
            )
        rows = payload.get("results") or []
        check(bool(rows), "the sequence row was dropped before reaching the caller")
        check(
            all(row.get("locus") for row in rows),
            "sequence rows arrived without 'locus'; its._mo_composites drops those",
        )
        check(
            all(int(row["observation_id"]) == 10 for row in rows),
            "sequences were not narrowed to the requested observation",
        )
    finally:
        client.close()
    print("  ok  sequences select by observer and narrow client-side")


def mo_paging_follows_every_page() -> None:
    """MOClient._paged must follow page 2+ when MO reports a real grand total.

    While the fake reported number_of_records as the length of the page it had
    just returned, `len(combined) >= total` was true after page 1 for every
    query and this loop never ran past its first iteration.
    """
    # external_links goes through _batched, which caps each query at 100 ids,
    # so MO's real 1000-row page is never full. Shrink the page instead.
    ids = tuple(range(1, 101))
    remote = MORemote(ids)
    remote.page_size = 40  # 100 rows -> 3 pages
    client = OfflineMOClient(remote)
    try:
        payload = client.external_links(ids, lambda: False)
        rows = payload.get("results") or []
        check(
            len(rows) == len(ids),
            f"paged read returned {len(rows)} of {len(ids)} rows -- pages were dropped",
        )
        check(
            len({int(row["observation_id"]) for row in rows}) == len(ids),
            "paged read returned duplicate rows instead of advancing the page",
        )
        pages = [
            int(params.get("page") or 1)
            for endpoint, params in remote.requests if endpoint == "external_links"
        ]
        check(max(pages) >= 2, "the paging loop never requested a second page")
    finally:
        client.close()
    print("  ok  MO paging follows every page of a multi-page result")


def inat_incremental_scan_pages_through_a_delta() -> None:
    """The updated_since branch must page, and must send a cursor iNat accepts.

    The fake used to ignore page/per_page/updated_since entirely, so a full
    result set always tripped `len(items) < 200` and `page += 1` never ran.
    """
    links = {value: value + 10_000 for value in range(20, 20 + 450)}
    env = Environment(mo_ids=(), inat_links=links)
    try:
        check(env.scan(force_full=True).startswith("ok:"), "the baseline scan failed")
        # Every record must post-date the cursor the baseline just stored, or
        # the delta is legitimately empty and proves nothing about paging.
        for row in env.inat_remote.observations.values():
            row["updated_at"] = "2099-01-01T00:00:00+00:00"
        env.inat_remote.requests.clear()
        env.inat_remote.updated_since_seen.clear()
        outcome = env.scan()
        check(outcome.startswith("ok:"), f"the incremental scan failed: {outcome}")
        check(
            bool(env.inat_remote.updated_since_seen),
            "the incremental scan never sent updated_since",
        )
        for value in env.inat_remote.updated_since_seen:
            check(
                _parse_inat_timestamp(value) is not None,
                f"updated_since={value!r} is not a timestamp iNaturalist accepts",
            )
        pages = [
            int(params.get("page") or 1)
            for path, params in env.inat_remote.requests
            if path.endswith("/observations") and "id" not in params
        ]
        check(
            bool(pages) and max(pages) >= 2,
            "450 changed records fit in one 200-row page -- the delta never paged",
        )
    finally:
        env.close()
    print("  ok  the incremental iNat scan pages through a multi-page delta")


def non_substitutable_photo_url_is_not_called_original() -> None:
    """Finding 6: an unsubstitutable URL must not be labelled 'original'."""
    from observation_workbench.services.prefetcher import (
        _SIZE_ORDER, _higher_quality_candidates, _same_or_lower_quality_candidates,
    )

    normal = StudyPhoto(1, "https://static.inaturalist.org/photos/1/square.jpg")
    sizes = [size for size, _url in normal.candidate_size_urls()]
    check(sizes[0] == "original", f"a normal photo lost its ladder: {sizes}")
    check("square" not in sizes, f"a normal photo gained a square rung: {sizes}")

    legacy = StudyPhoto(2, "https://farm1.staticflickr.com/1/2_3_m.jpg")
    candidates = legacy.candidate_size_urls()
    check(len(candidates) == 1, f"expected one candidate, got {candidates}")
    size, url = candidates[0]
    check(
        size == "square",
        f"an unsubstitutable url was labelled {size!r}; the prefetcher would "
        "cache a thumbnail as the original and never upgrade it",
    )
    check(url == legacy.url_square, "the fallback url changed")

    # The label must be a KNOWN rank, or the prefetcher's helpers fall back to
    # 'return every candidate' and redownload the photo on every navigation.
    check("square" in _SIZE_ORDER, "'square' must be a ranked size")
    check(
        _higher_quality_candidates(tuple(candidates), "square") == (),
        "a square-only photo must have nothing to upgrade to",
    )
    check(
        _same_or_lower_quality_candidates(tuple(candidates), "square")
        == tuple(candidates),
        "a square-only photo must still be re-decodable from disk",
    )
    print("  ok  an unsubstitutable photo url is ranked square, not original")


def sandbox_is_isolated() -> None:
    settings_path = QSettings().fileName()
    check(
        str(settings_path).startswith(SANDBOX.name),
        f"QSettings escaped the sandbox: {settings_path}",
    )
    data_root = os.environ["XDG_DATA_HOME"]
    check(data_root.startswith(SANDBOX.name), "XDG_DATA_HOME escaped the sandbox")
    print(f"  ok  settings and data confined to {SANDBOX.name}")


SCENARIOS = (
    sandbox_is_isolated,
    mo_time_range_helper_is_well_formed,
    mo_totals_are_read,
    non_substitutable_photo_url_is_not_called_original,
    sequences_select_by_observer,
    mo_paging_follows_every_page,
    full_scan_completes,
    incremental_scan_sends_mo_time_range,
    inat_incremental_scan_pages_through_a_delta,
    truncated_deleted_feed_holds_cursor,
    dropped_action_result_releases_the_action_lock,
)


def main() -> int:
    QCoreApplication.setOrganizationName("iNatStudyHarness")
    QCoreApplication.setApplicationName("CoordinatorScanHarness")
    app = QApplication.instance() or QApplication(sys.argv)
    failures = 0
    print("ReconciliationCoordinator scan harness (offline, no network)")
    for scenario in SCENARIOS:
        try:
            scenario()
        except HarnessFailure as exc:
            failures += 1
            print(f"  FAIL  {scenario.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - harness surface
            failures += 1
            print(f"  ERROR {scenario.__name__}: {type(exc).__name__}: {exc}")
    del app
    print("all scenarios passed" if not failures else f"{failures} scenario(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
