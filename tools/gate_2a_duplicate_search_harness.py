#!/usr/bin/env python3
"""Offline smoke harness for ObservationCreationService._live_search_inat_destination
(section 1: account-complete duplicate discovery; section 5: fail-closed on
malformed results). No network calls — INatClient.get_creation_duplicate_search
is replaced with a scripted stub returning crafted payloads.

Run:
    ./.venv/bin/python tools/gate_2a_duplicate_search_harness.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observation_workbench.reconciliation.observation_creation import (  # noqa: E402
    ObservationCreationError,
    ObservationCreationService,
)
from observation_workbench.reconciliation.types import (
    InventoryObservation,
    RemoteRecordKey,
    RemoteSite,
)  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((name, PASS if condition else FAIL))
    print(
        f"[{PASS if condition else FAIL}] {name}"
        + (f" — {detail}" if detail and not condition else "")
    )


class _StubDB:
    def __init__(self, mo_binding_state: str = "verified") -> None:
        self._mo_binding_state = mo_binding_state

    def field_binding(self, profile_id: int, purpose: str):
        if purpose == "mo_url":
            if self._mo_binding_state == "missing":
                return None
            if self._mo_binding_state == "raises":
                raise RuntimeError("simulated db error")
            return {"field_id": 12345, "verification_state": self._mo_binding_state}
        return None  # its binding: optional, absent is fine


class _StubINatClient:
    """Scripts a sequence of (kwargs-independent) payloads per call, keyed
    by whether the call used a date window (d1/d2) or a cursor (id_above)."""

    def __init__(self, window_pages=None, scan_pages=None) -> None:
        self._window_pages = list(window_pages or [])
        self._scan_pages = list(scan_pages or [])
        self.calls: list[dict] = []

    def get_creation_duplicate_search(
        self, token, *, user_id, page=1, d1="", d2="", id_above=None
    ):
        self.calls.append({"page": page, "d1": d1, "d2": d2, "id_above": id_above})
        if id_above is not None:
            if not self._scan_pages:
                return {"results": []}
            return self._scan_pages.pop(0)
        if not self._window_pages:
            return {"results": []}
        return self._window_pages.pop(0)


def _service(inat_client, db) -> ObservationCreationService:
    return ObservationCreationService(
        db,
        inat_client,
        None,
        lambda: SimpleNamespace(is_authenticated=True, api_token="tok"),
        lambda _p: "",
        lambda: 1,
        lambda: 1,
        None,
        None,
    )


def _source_inventory(with_date: bool) -> InventoryObservation:
    return InventoryObservation(
        key=RemoteRecordKey(RemoteSite.MO, 42),
        account_id=1,
        owner_id=1,
        owner_login="tester",
        observed_on=date(2026, 1, 1) if with_date else None,
        taxon_id=1,
        taxon_name="Amanita sp.",
        taxon_rank="species",
        public_locality="",
        fungi_status="in_scope",
        updated_at=None,
    )


def _profile():
    return SimpleNamespace(profile_id=1, inat_user_id=555)


def test_clean_full_search_with_date() -> None:
    """Zero matches anywhere: date window empty, full-account scan empty.
    Must complete without raising (a clean, complete search)."""
    inat = _StubINatClient(window_pages=[{"results": []}], scan_pages=[{"results": []}])
    service = _service(inat, _StubDB())
    try:
        service._live_search_inat_destination(
            _profile(), _source_inventory(True), lambda: False
        )  # noqa: SLF001
        ok = True
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  unexpected exception: {exc}")
    check(
        "clean search (date window + full scan both empty) completes without raising",
        ok,
    )
    check(
        "full-account scan STILL RAN even though a date window search ran first",
        any(c["id_above"] is not None for c in inat.calls),
    )


def test_dateless_destination_candidate_found_by_full_scan() -> None:
    """The source HAS a date, but the real duplicate on the destination has
    NO date and therefore never appears in the date-window search -- only
    the unconditional full-account fallback scan can find it. This is the
    core section-1 completeness guarantee."""
    dateless_candidate = {
        "id": 999001,
        "observed_on": None,
        "updated_at": "2026-01-01T00:00:00Z",
        "taxon": {"id": 1, "name": "Amanita sp.", "ancestor_ids": [47170]},
        "user": {"id": 555},
        "place_guess": "",
        "observation_photos": [],
        # A "Mushroom Observer URL" reciprocal-link field value pointing
        # back at the source MO observation (id 42, matching _source_inventory
        # below) -- strong LINK-family evidence (65 points, far above the
        # 35-point "possible" threshold), the exact kind of evidence a
        # taxon/date hard filter would have hidden entirely.
        "ofvs": [{"field_id": 12345, "value": "https://mushroomobserver.org/42"}],
    }
    inat = _StubINatClient(
        window_pages=[{"results": []}],
        scan_pages=[{"results": [dateless_candidate]}],
    )
    service = _service(inat, _StubDB())
    try:
        service._live_search_inat_destination(
            _profile(), _source_inventory(True), lambda: False
        )  # noqa: SLF001
        check("dateless destination duplicate: NOT caught (BUG)", False)
    except ObservationCreationError as exc:
        check(
            "dateless destination duplicate IS caught by the full-account fallback scan",
            exc.code == "possible_match_found",
            f"got code={exc.code}",
        )


def test_malformed_page_shape_fails_closed() -> None:
    inat = _StubINatClient(window_pages=[{"results": "not-a-list"}])
    service = _service(inat, _StubDB())
    try:
        service._live_search_inat_destination(
            _profile(), _source_inventory(True), lambda: False
        )  # noqa: SLF001
        check("malformed 'results' shape fails closed", False)
    except ObservationCreationError as exc:
        check(
            "malformed 'results' shape fails closed",
            exc.code == "duplicate_search_unavailable",
            f"got {exc.code}",
        )


def test_non_dict_candidate_fails_closed() -> None:
    inat = _StubINatClient(window_pages=[{"results": [1, 2, 3]}])
    service = _service(inat, _StubDB())
    try:
        service._live_search_inat_destination(
            _profile(), _source_inventory(True), lambda: False
        )  # noqa: SLF001
        check("non-dict candidate result fails closed", False)
    except ObservationCreationError as exc:
        check(
            "non-dict candidate result fails closed",
            exc.code == "duplicate_search_unavailable",
            f"got {exc.code}",
        )


def test_missing_id_field_fails_closed() -> None:
    inat = _StubINatClient(window_pages=[{"results": [{"observed_on": "2026-01-01"}]}])
    service = _service(inat, _StubDB())
    try:
        service._live_search_inat_destination(
            _profile(), _source_inventory(True), lambda: False
        )  # noqa: SLF001
        check("candidate missing required id field fails closed", False)
    except ObservationCreationError as exc:
        check(
            "candidate missing required id field fails closed",
            exc.code == "duplicate_search_unavailable",
            f"got {exc.code}",
        )


def test_missing_mo_binding_blocks() -> None:
    inat = _StubINatClient()
    service = _service(inat, _StubDB(mo_binding_state="missing"))
    try:
        service._live_search_inat_destination(
            _profile(), _source_inventory(True), lambda: False
        )  # noqa: SLF001
        check("missing MO reciprocal-link binding blocks creation", False)
    except ObservationCreationError as exc:
        check(
            "missing MO reciprocal-link binding blocks creation",
            exc.code == "duplicate_search_unavailable",
            f"got {exc.code}",
        )


def test_unverified_mo_binding_blocks() -> None:
    inat = _StubINatClient()
    service = _service(inat, _StubDB(mo_binding_state="pending"))
    try:
        service._live_search_inat_destination(
            _profile(), _source_inventory(True), lambda: False
        )  # noqa: SLF001
        check("unverified MO reciprocal-link binding blocks creation", False)
    except ObservationCreationError as exc:
        check(
            "unverified MO reciprocal-link binding blocks creation",
            exc.code == "duplicate_search_unavailable",
            f"got {exc.code}",
        )


def test_binding_load_error_blocks() -> None:
    inat = _StubINatClient()
    service = _service(inat, _StubDB(mo_binding_state="raises"))
    try:
        service._live_search_inat_destination(
            _profile(), _source_inventory(True), lambda: False
        )  # noqa: SLF001
        check(
            "MO binding DB read error blocks creation (not silently 'no evidence')",
            False,
        )
    except ObservationCreationError as exc:
        check(
            "MO binding DB read error blocks creation (not silently 'no evidence')",
            exc.code == "duplicate_search_unavailable",
            f"got {exc.code}",
        )


def test_repeated_cursor_fails_closed() -> None:
    stuck_page = {
        "results": [
            {
                "id": 100,
                "observed_on": None,
                "updated_at": "x",
                "taxon": None,
                "user": {"id": 555},
                "place_guess": "",
                "ofvs": [],
                "observation_photos": [],
            }
        ]
        * 200
    }
    inat = _StubINatClient(
        window_pages=[{"results": []}], scan_pages=[stuck_page, stuck_page]
    )
    service = _service(inat, _StubDB())
    try:
        service._live_search_inat_destination(
            _profile(), _source_inventory(True), lambda: False
        )  # noqa: SLF001
        check("repeated/non-advancing pagination cursor fails closed", False)
    except ObservationCreationError as exc:
        check(
            "repeated/non-advancing pagination cursor fails closed",
            exc.code == "duplicate_search_unavailable",
            f"got {exc.code}",
        )


def test_dateless_source_goes_straight_to_full_scan() -> None:
    inat = _StubINatClient(scan_pages=[{"results": []}])
    service = _service(inat, _StubDB())
    try:
        service._live_search_inat_destination(
            _profile(), _source_inventory(False), lambda: False
        )  # noqa: SLF001
        ok = True
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  unexpected exception: {exc}")
    check("source with no observed date still completes via full-account scan", ok)
    check(
        "no date-window params sent for a dateless source",
        bool(inat.calls) and all(c["d1"] == "" for c in inat.calls),
    )


def main() -> int:
    test_clean_full_search_with_date()
    test_dateless_destination_candidate_found_by_full_scan()
    test_malformed_page_shape_fails_closed()
    test_non_dict_candidate_fails_closed()
    test_missing_id_field_fails_closed()
    test_missing_mo_binding_blocks()
    test_unverified_mo_binding_blocks()
    test_binding_load_error_blocks()
    test_repeated_cursor_fails_closed()
    test_dateless_source_goes_straight_to_full_scan()
    failed = [n for n, s in _results if s == FAIL]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
