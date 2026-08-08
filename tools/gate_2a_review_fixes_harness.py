#!/usr/bin/env python3
"""Offline, no-network smoke harness for the z(17).diff review fixes, plus
the Round-7 follow-up review fixes.

Exercises ONLY local ReconciliationDB / ObservationCreationService logic
against a throwaway temp-file sqlite database — no HTTP calls, no live
writes to mushroomobserver.org or inaturalist.org. Safe to run any time.

Covers:
  A. settle_pair_finalize_success provenance checks (section 4): positive
     case + every documented negative case (wrong group/attempt/pair/obs
     ids, excluded pair, conflicting confirmed pair, wrong action
     type/state, missing attempt, attempt belonging to another profile,
     attempt already finalized/non-succeeded).
  B. _execute_population_items ambiguous-action preservation (section 2):
     a pair-validation failure must never rewrite an existing
     outcome_unknown/succeeded/running action as failed, but MUST fail a
     genuinely untouched pending item with no linked action.
  C. (Round-7) ReconciliationDB.fail_creation_item_preflight atomicity: a
     concurrent mint/claim/write-start interleaved between the caller's
     earlier "check" and this atomic "act" must never be downgraded to
     'failed' -- the method must refuse and report the real state instead.
     Also covers the positive (no action yet / pending, no write started)
     cases, which must still fail atomically as before.
  D. (Round-7) _execute_population_items cancellation checkpoints around the
     full-image download: cancelling before any mint/download durably
     records the item as a failed (cancelled) preflight result and stops
     the tail without ever touching the download itself.
  E. (Round-7) settle_pair_finalize_success now also requires
     sync_created_observations.pair_id to equal the pair being finalized --
     a mismatch must refuse (raise, rolling back) rather than promote.

Run:
    ./.venv/bin/python tools/gate_2a_review_fixes_harness.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observation_workbench.reconciliation.db import ReconciliationDB  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((name, PASS if condition else FAIL, detail))
    print(f"[{PASS if condition else FAIL}] {name}" + (f" — {detail}" if detail and not condition else ""))


_temp_db_paths: list[Path] = []


def _fresh_db() -> tuple[ReconciliationDB, Path]:
    fd, tmp_name = tempfile.mkstemp(suffix=".gate2a-review.sqlite3")
    os.close(fd)
    path = Path(tmp_name)
    _temp_db_paths.append(path)
    return ReconciliationDB(str(path)), path


def _seed_source_record(db: ReconciliationDB, profile_id: int, mo_id: int) -> None:
    now = "2026-01-01T00:00:00Z"
    db.connection().execute(
        "INSERT INTO sync_records(profile_id,site,remote_observation_id,account_id,owner_id,"
        "owner_login,observed_on,taxon_id,taxon_name,taxon_rank,public_locality,fungi_status,"
        "remote_updated_at,content_fingerprint,is_deleted,scope_state,unpaired_state,last_seen_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
        (profile_id, "mo", mo_id, 42, 42, "tester", "2025-01-01", None, "Amanita sp.", "species",
         "", "in_scope", now, "fp", "in_scope", "confirmed_missing_on_inat", now),
    )
    db.connection().commit()


def _journal(db: ReconciliationDB, profile_id: int, mo_id: int) -> tuple[int, int, int]:
    from observation_workbench.reconciliation.normalization import public_fingerprint
    source_fingerprint = public_fingerprint(
        "record", "mo", mo_id, "confirmed_missing_on_inat", "2026-01-01T00:00:00Z",
    )
    return db.journal_observation_creation_actions(
        profile_id, source_site="mo", source_observation_id=mo_id, destination_site="inat",
        source_fingerprint=source_fingerprint, correlation_marker=f"marker-{mo_id}",
        marker_location="client_uuid_field", approved_field_gaps=[], item_specs=[],
    )


def _setup_finalizable(
    db: ReconciliationDB, *, inat_user_id: int = 9001, mo_user_id: int = 9002,
    mo_id: int = 111111, inat_id: int = 222222,
) -> dict:
    """Builds one fully-settled provisional pair + running pair_finalize
    action + eligible ('succeeded') creation attempt — the exact state
    settle_pair_finalize_success expects to promote."""
    profile = db.save_profile(inat_user_id, "inat_tester", mo_user_id, "mo_tester")
    _seed_source_record(db, profile.profile_id, mo_id)
    group_id, create_action_id, attempt_id = _journal(db, profile.profile_id, mo_id)
    pair_id = db.settle_creation_write_success(
        profile.profile_id, create_action_id, attempt_id,
        db.creation_ledger_for_group(profile.profile_id, group_id)["creation_id"], group_id,
        destination_site="inat", destination_id=inat_id, destination_uuid="uuid-1",
        source_record_id=mo_id,
    )
    finalize_action_id = db.mint_creation_followup_action(
        profile.profile_id, group_id, 3, "pair_finalize",
        pair_id=pair_id, mo_observation_id=mo_id, inat_observation_id=inat_id, site="inat",
    )
    assert db.claim_action(profile.profile_id, finalize_action_id, "verification")
    return {
        "db": db, "profile_id": profile.profile_id, "group_id": group_id, "pair_id": pair_id,
        "mo_id": mo_id, "inat_id": inat_id, "finalize_action_id": finalize_action_id,
        "attempt_id": attempt_id,
    }


def test_finalize_success() -> None:
    db, _path = _fresh_db()
    st = _setup_finalizable(db)
    ok = db.settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"], st["group_id"], st["pair_id"],
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
    )
    check("finalize: correct provenance succeeds", ok is True)
    pair = db.pair_detail(st["profile_id"], st["pair_id"])
    check("finalize: pair promoted to confirmed", pair is not None and str(pair["review_state"]) == "confirmed")
    action = db.action(st["profile_id"], st["finalize_action_id"])
    check("finalize: action marked succeeded", action is not None and str(action["state"]) == "succeeded")
    attempt_row = db.connection().execute(
        "SELECT state FROM sync_creation_attempts WHERE attempt_id=?", (st["attempt_id"],),
    ).fetchone()
    check("finalize: attempt remains succeeded", str(attempt_row["state"]) == "succeeded")
    # Re-running against the now-non-'running' action must refuse cleanly.
    ok2 = db.settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"], st["group_id"], st["pair_id"],
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
    )
    check("finalize: already-finalized action id refuses (double-finalize)", ok2 is False)


def test_finalize_negatives() -> None:
    def fresh_state():
        db, _path = _fresh_db()
        return _setup_finalizable(db)

    st = fresh_state()
    ok = st["db"].settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"], st["group_id"] + 999, st["pair_id"],
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
    )
    check("finalize negative: wrong group refuses", ok is False)
    pair = st["db"].pair_detail(st["profile_id"], st["pair_id"])
    check("finalize negative: wrong group leaves pair untouched", str(pair["review_state"]) == "provisional")

    st = fresh_state()
    ok = st["db"].settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"], st["group_id"], st["pair_id"] + 999,
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
    )
    check("finalize negative: wrong pair id refuses", ok is False)

    st = fresh_state()
    ok = st["db"].settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"], st["group_id"], st["pair_id"],
        mo_observation_id=st["mo_id"] + 1, inat_observation_id=st["inat_id"],
    )
    check("finalize negative: wrong mo observation id refuses", ok is False)

    st = fresh_state()
    ok = st["db"].settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"], st["group_id"], st["pair_id"],
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"] + 1,
    )
    check("finalize negative: wrong inat observation id refuses", ok is False)

    st = fresh_state()
    ok = st["db"].settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"] + 999, st["group_id"], st["pair_id"],
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
    )
    check("finalize negative: missing action id refuses", ok is False)

    # Wrong action type: mint a decoy row and target it instead of the real
    # pair_finalize row.
    st = fresh_state()
    decoy_id = st["db"].mint_creation_followup_action(
        st["profile_id"], st["group_id"], 4, "inat_ofv_add",
        pair_id=st["pair_id"], mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"], site="inat",
    )
    ok = st["db"].settle_pair_finalize_success(
        st["profile_id"], decoy_id, st["group_id"], st["pair_id"],
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
    )
    check("finalize negative: wrong action_type refuses", ok is False)

    # Wrong action state: finalize action never claimed (still 'pending').
    st = fresh_state()
    st["db"].connection().execute(
        "UPDATE sync_actions SET state='pending' WHERE action_id=?", (st["finalize_action_id"],),
    )
    st["db"].connection().commit()
    ok = st["db"].settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"], st["group_id"], st["pair_id"],
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
    )
    check("finalize negative: wrong action state ('pending') refuses", ok is False)

    # Excluded pair.
    st = fresh_state()
    st["db"].connection().execute(
        "INSERT INTO sync_pair_exclusions(profile_id,mo_observation_id,inat_observation_id,reason,created_at) "
        "VALUES(?,?,?,?,?)",
        (st["profile_id"], st["mo_id"], st["inat_id"], "test", "2026-01-01T00:00:00Z"),
    )
    st["db"].connection().commit()
    ok = st["db"].settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"], st["group_id"], st["pair_id"],
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
    )
    check("finalize negative: excluded pair refuses", ok is False)

    # Conflicting confirmed pair (a DIFFERENT inat id already confirmed
    # against this mo id).
    st = fresh_state()
    now = "2026-01-01T00:00:00Z"
    st["db"].connection().execute(
        "INSERT INTO sync_pairs(profile_id,mo_observation_id,inat_observation_id,link_state,score,"
        "classification,review_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (st["profile_id"], st["mo_id"], st["inat_id"] + 5000, "", 0, "manual", "confirmed", now, now),
    )
    st["db"].connection().commit()
    ok = st["db"].settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"], st["group_id"], st["pair_id"],
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
    )
    check("finalize negative: conflicting confirmed pair refuses", ok is False)

    # Attempt/action/group/pair all real and internally consistent, but
    # belonging to a DIFFERENT profile than the one passed in -- the
    # profile-scoped action lookup alone must already refuse this cleanly
    # (every table here is profile-scoped by FK, so a genuinely
    # cross-profile attempt row can never attach to another profile's
    # action group; this proves the boundary is enforced from the caller's
    # side too).
    db, _path = _fresh_db()
    st_a = _setup_finalizable(db, inat_user_id=9101, mo_user_id=9102, mo_id=711111, inat_id=811111)
    st_b = _setup_finalizable(db, inat_user_id=9111, mo_user_id=9112, mo_id=911111, inat_id=911112)
    ok = db.settle_pair_finalize_success(
        st_a["profile_id"], st_b["finalize_action_id"], st_b["group_id"], st_b["pair_id"],
        mo_observation_id=st_b["mo_id"], inat_observation_id=st_b["inat_id"],
    )
    check("finalize negative: cross-profile action id refuses", ok is False)

    # Attempt not in an eligible ('succeeded') state.
    st = fresh_state()
    st["db"].connection().execute(
        "UPDATE sync_creation_attempts SET state='outcome_unknown' WHERE attempt_id=?", (st["attempt_id"],),
    )
    st["db"].connection().commit()
    ok = st["db"].settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"], st["group_id"], st["pair_id"],
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
    )
    check("finalize negative: attempt not in eligible state refuses", ok is False)

    # Missing attempt entirely (deleted after settlement, which
    # RESTRICT normally prevents via the group FK -- simulate by deleting the
    # attempt row's link, i.e. no attempt row for this action_group_id at all).
    st = fresh_state()
    st["db"].connection().execute("DELETE FROM sync_creation_attempts WHERE attempt_id=?", (st["attempt_id"],))
    st["db"].connection().commit()
    ok = st["db"].settle_pair_finalize_success(
        st["profile_id"], st["finalize_action_id"], st["group_id"], st["pair_id"],
        mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
    )
    check("finalize negative: no attempt row for group refuses", ok is False)


def _has_exclusion_table(db: ReconciliationDB) -> bool:
    row = db.connection().execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sync_pair_exclusions'"
    ).fetchone()
    return row is not None


def test_ambiguous_action_preservation() -> None:
    """Section 2: a pair-validation failure inside _execute_population_items
    must never rewrite an existing outcome_unknown/succeeded/running action
    as failed. Only a genuinely untouched item (no linked action, or a
    'pending' action that never started a write) may be closed out."""
    from observation_workbench.reconciliation.observation_creation import ObservationCreationService

    db, _path = _fresh_db()
    profile = db.save_profile(9201, "inat_amb", 9202, "mo_amb")
    mo_id = 300001
    _seed_source_record(db, profile.profile_id, mo_id)
    group_id, create_action_id, attempt_id = _journal(db, profile.profile_id, mo_id)
    creation_id = db.creation_ledger_for_group(profile.profile_id, group_id)["creation_id"]
    inat_id = 400001
    pair_id = db.settle_creation_write_success(
        profile.profile_id, create_action_id, attempt_id, creation_id, group_id,
        destination_site="inat", destination_id=inat_id, destination_uuid="uuid-2", source_record_id=mo_id,
    )
    # Pair stays 'provisional' (never finalized) -- this alone makes pair_ok
    # False inside _execute_population_items, exactly the trigger condition
    # for the bug under review.

    # Item A: mint a real action and force it into 'outcome_unknown'.
    item_a = db.connection().execute(
        "INSERT INTO sync_creation_items(attempt_id,action_id,item_type,source_item_identity,"
        "reviewed_metadata_fingerprint,reviewed_byte_fingerprint,state,created_at,updated_at) "
        "VALUES(?,NULL,'photo','photoA','fp','bytefp','pending',?,?)",
        (attempt_id, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ).lastrowid
    db.connection().commit()
    action_a = db.mint_creation_item_action(
        profile.profile_id, group_id, item_a, 4, "inat_photo_attach",
        pair_id=pair_id, mo_observation_id=mo_id, inat_observation_id=inat_id, site="inat",
    )
    db.finish_action(profile.profile_id, action_a, "outcome_unknown", phase="unsafe_write", error_code="lost_response")

    service = ObservationCreationService(
        db, None, None, lambda: None, lambda _p: "", lambda: 1, lambda: 1, None, None,
    )
    create_row = db.action_group_rows(profile.profile_id, group_id)[0]
    results = service._execute_population_items(  # noqa: SLF001 - direct unit access, deliberate
        profile.profile_id, group_id, create_row, lambda: False, lambda _m: None,
    )
    action_a_after = db.action(profile.profile_id, action_a)
    check(
        "ambiguous preservation: outcome_unknown action untouched by pair-drift failure",
        action_a_after is not None and str(action_a_after["state"]) == "outcome_unknown",
        f"got state={action_a_after['state'] if action_a_after else None}",
    )
    check(
        "ambiguous preservation: result reports outcome_unknown, not failed",
        bool(results) and results[0].state == "outcome_unknown",
        f"got results={[(r.action_id, r.state) for r in results]}",
    )

    # --- Second scenario: item is genuinely untouched (no action minted at
    # all) -- this one SHOULD be safely closed out as a durable local
    # preflight failure by the same pair-drift check. -----------------------
    db2, _path2 = _fresh_db()
    profile2 = db2.save_profile(9301, "inat_safe", 9302, "mo_safe")
    mo_id2 = 500001
    _seed_source_record(db2, profile2.profile_id, mo_id2)
    group_id2, create_action_id2, attempt_id2 = _journal(db2, profile2.profile_id, mo_id2)
    creation_id2 = db2.creation_ledger_for_group(profile2.profile_id, group_id2)["creation_id"]
    inat_id2 = 600001
    pair_id2 = db2.settle_creation_write_success(
        profile2.profile_id, create_action_id2, attempt_id2, creation_id2, group_id2,
        destination_site="inat", destination_id=inat_id2, destination_uuid="uuid-3", source_record_id=mo_id2,
    )
    item_b = db2.connection().execute(
        "INSERT INTO sync_creation_items(attempt_id,action_id,item_type,source_item_identity,"
        "reviewed_metadata_fingerprint,reviewed_byte_fingerprint,state,created_at,updated_at) "
        "VALUES(?,NULL,'photo','photoB','fp','bytefp','pending',?,?)",
        (attempt_id2, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ).lastrowid
    db2.connection().commit()
    service2 = ObservationCreationService(
        db2, None, None, lambda: None, lambda _p: "", lambda: 1, lambda: 1, None, None,
    )
    create_row2 = db2.action_group_rows(profile2.profile_id, group_id2)[0]
    results2 = service2._execute_population_items(  # noqa: SLF001
        profile2.profile_id, group_id2, create_row2, lambda: False, lambda _m: None,
    )
    item_b_after = [i for i in db2.creation_items(profile2.profile_id, attempt_id2) if i["creation_item_id"] == item_b][0]
    check(
        "ambiguous preservation: untouched pending item IS safely closed out",
        str(item_b_after["state"]) == "failed",
        f"got state={item_b_after['state']}",
    )
    check(
        "ambiguous preservation: untouched item got a real durable action row",
        bool(results2) and results2[0].action_id != 0 and results2[0].state == "failed",
        f"got results={[(r.action_id, r.state) for r in results2]}",
    )


def _setup_confirmed_population(mo_id: int, inat_id: int, seed: int) -> dict:
    """A finalized (confirmed) pair with an eligible attempt -- the state
    ``_execute_population_items`` needs to actually reach its item-population
    loop (pair_ok True) instead of the pair-drift branch."""
    from observation_workbench.reconciliation.normalization import public_fingerprint

    db, _path = _fresh_db()
    profile = db.save_profile(9500 + seed, f"inat_cancel{seed}", 9600 + seed, f"mo_cancel{seed}")
    _seed_source_record(db, profile.profile_id, mo_id)
    group_id, create_action_id, attempt_id = _journal(db, profile.profile_id, mo_id)
    creation_id = db.creation_ledger_for_group(profile.profile_id, group_id)["creation_id"]
    pair_id = db.settle_creation_write_success(
        profile.profile_id, create_action_id, attempt_id, creation_id, group_id,
        destination_site="inat", destination_id=inat_id, destination_uuid=f"uuid-cancel{seed}",
        source_record_id=mo_id,
    )
    now = "2026-01-01T00:00:00Z"
    db.connection().execute(
        "UPDATE sync_pairs SET review_state='confirmed',ever_confirmed=1,updated_at=? "
        "WHERE profile_id=? AND pair_id=?",
        (now, profile.profile_id, pair_id),
    )
    db.connection().commit()
    photo_id = f"photo-cancel{seed}"
    license_label = "CC-BY"
    holder = "tester"
    metadata_fp = public_fingerprint(photo_id, license_label, holder)
    item_id = db.connection().execute(
        "INSERT INTO sync_creation_items(attempt_id,action_id,item_type,source_item_identity,"
        "reviewed_metadata_fingerprint,reviewed_byte_fingerprint,state,created_at,updated_at) "
        "VALUES(?,NULL,'photo',?,?,'pinnedbytefp','pending',?,?)",
        (attempt_id, photo_id, metadata_fp, now, now),
    ).lastrowid
    db.connection().commit()
    return {
        "db": db, "profile": profile, "profile_id": profile.profile_id, "group_id": group_id,
        "pair_id": pair_id, "attempt_id": attempt_id, "item_id": item_id, "mo_id": mo_id,
        "inat_id": inat_id, "photo_id": photo_id, "license_label": license_label, "holder": holder,
    }


class _StubPhotoService:
    """Duck-typed ``PhotoSyncService`` stand-in exposing only the two
    private methods ``_execute_population_items`` reuses
    (``_refresh``/``_download``) -- never touches the network."""

    def __init__(self, source, download_bytes: bytes = b"stub-image-bytes") -> None:
        self._source = source
        self._download_bytes = download_bytes
        self.refresh_calls = 0
        self.download_calls = 0

    def _refresh(self, profile, pair, cancelled):  # noqa: ANN001
        self.refresh_calls += 1
        return SimpleNamespace(
            source_photos=[self._source], inat_record_fingerprint="ifp", mo_record_fingerprint="mfp",
        )

    def _download(self, source):  # noqa: ANN001
        self.download_calls += 1
        return self._download_bytes


def test_preflight_atomicity_positive_cases() -> None:
    """ReconciliationDB.fail_creation_item_preflight, the happy paths: no
    action minted yet, and an action already minted but still 'pending' with
    no write started -- both MUST still fail atomically exactly as the old
    check-then-act sequence did."""
    st = _setup_confirmed_population(720001, 820001, 10)
    db = st["db"]

    outcome = db.fail_creation_item_preflight(
        st["profile_id"], st["group_id"], st["item_id"], "inat_photo_attach",
        reason="no action minted yet", next_ordinal=4,
        pair_id=st["pair_id"], mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"], site="inat",
    )
    check("preflight positive: no-existing-action case mints and fails atomically", outcome["downgraded"] is True and outcome["state"] == "failed", f"got {outcome}")
    action = db.action(st["profile_id"], outcome["action_id"])
    check("preflight positive: the newly-minted action itself is 'failed'", action is not None and str(action["state"]) == "failed")
    item_after = [i for i in db.creation_items(st["profile_id"], st["attempt_id"]) if i["creation_item_id"] == st["item_id"]][0]
    check("preflight positive: the creation item itself is 'failed'", str(item_after["state"]) == "failed")

    # Second item: action already minted, still pending, no write started.
    st2 = _setup_confirmed_population(720002, 820002, 11)
    db2 = st2["db"]
    pending_action_id = db2.mint_creation_item_action(
        st2["profile_id"], st2["group_id"], st2["item_id"], 4, "inat_photo_attach",
        pair_id=st2["pair_id"], mo_observation_id=st2["mo_id"], inat_observation_id=st2["inat_id"], site="inat",
    )
    outcome2 = db2.fail_creation_item_preflight(
        st2["profile_id"], st2["group_id"], st2["item_id"], "inat_photo_attach",
        reason="pending with no write started", next_ordinal=5,
        pair_id=st2["pair_id"], mo_observation_id=st2["mo_id"], inat_observation_id=st2["inat_id"], site="inat",
    )
    check(
        "preflight positive: existing-pending-no-write case fails atomically too",
        outcome2["downgraded"] is True and outcome2["state"] == "failed" and outcome2["action_id"] == pending_action_id,
        f"got {outcome2}",
    )


def test_preflight_atomicity_race_never_downgrades() -> None:
    """Section 2 (safety-critical, Round-7): simulate the exact interleaving
    the review flagged -- an action a concurrent worker already
    claimed/started (or whose result is unknown) BETWEEN some earlier
    caller-side 'check' and this atomic 'act' must never be silently
    overwritten as 'failed'. Seed the race state directly, then call the
    atomic method and prove it refuses to downgrade and reports the truth."""
    st = _setup_confirmed_population(730001, 830001, 20)
    db = st["db"]

    # A concurrent worker "wins": mints, claims, and marks the write started
    # -- entirely before fail_creation_item_preflight is ever invoked. Any
    # earlier check this test's caller might have made (e.g. "item.action_id
    # is None") is now stale.
    concurrent_action_id = db.mint_creation_item_action(
        st["profile_id"], st["group_id"], st["item_id"], 4, "inat_photo_attach",
        pair_id=st["pair_id"], mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"], site="inat",
    )
    assert db.claim_action(st["profile_id"], concurrent_action_id, "unsafe_write")
    db.mark_action_write_started(st["profile_id"], concurrent_action_id)

    outcome = db.fail_creation_item_preflight(
        st["profile_id"], st["group_id"], st["item_id"], "inat_photo_attach",
        reason="simulated preflight failure racing a concurrent in-flight write", next_ordinal=5,
        pair_id=st["pair_id"], mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"], site="inat",
    )
    check("preflight race: refuses to downgrade a running/write-started action", outcome["downgraded"] is False, f"got {outcome}")
    check("preflight race: reports the REAL state (running), not 'failed'", outcome["state"] == "running", f"got {outcome}")
    action_after = db.action(st["profile_id"], concurrent_action_id)
    check(
        "preflight race: the action row itself was never modified to 'failed'",
        action_after is not None and str(action_after["state"]) == "running",
        f"got state={action_after['state'] if action_after else None}",
    )

    # Same action later settles as outcome_unknown (write sent, result lost)
    # -- must ALSO never be downgraded to 'failed'.
    db.finish_action(st["profile_id"], concurrent_action_id, "outcome_unknown", phase="unsafe_write", error_code="lost_response")
    outcome2 = db.fail_creation_item_preflight(
        st["profile_id"], st["group_id"], st["item_id"], "inat_photo_attach",
        reason="second simulated preflight failure racing outcome_unknown", next_ordinal=6,
        pair_id=st["pair_id"], mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"], site="inat",
    )
    check(
        "preflight race: outcome_unknown is likewise never downgraded",
        outcome2["downgraded"] is False and outcome2["state"] == "outcome_unknown",
        f"got {outcome2}",
    )
    action_after2 = db.action(st["profile_id"], concurrent_action_id)
    check(
        "preflight race: outcome_unknown action row still untouched",
        action_after2 is not None and str(action_after2["state"]) == "outcome_unknown",
    )


def test_population_loop_cancellation_before_download() -> None:
    """Section 3 (Round-7): cancellation checked immediately before starting
    the full-image download in ``_execute_population_items``. Cancelling
    before any mint/download must durably record the item as a failed
    (cancelled) preflight result, stop the tail at exactly this item, and
    never even invoke the download itself."""
    from observation_workbench.reconciliation.observation_creation import ObservationCreationService

    st = _setup_confirmed_population(740001, 840001, 30)
    db = st["db"]
    source = SimpleNamespace(
        photo_id=st["photo_id"], source_url="stub://cancel-before-download",
        license_label=st["license_label"], copyright_holder=st["holder"],
    )
    stub = _StubPhotoService(source)
    service = ObservationCreationService(
        db, None, None, lambda: None, lambda _p: "", lambda: 1, lambda: 1, stub, None,
    )
    create_row = db.action_group_rows(st["profile_id"], st["group_id"])[0]
    results = service._execute_population_items(  # noqa: SLF001 - direct unit access, deliberate
        st["profile_id"], st["group_id"], create_row, lambda: True, lambda _m: None,
    )
    check("cancel-before-download: tail stops at exactly one item", len(results) == 1, f"got {len(results)} results")
    check(
        "cancel-before-download: reported as a durable failed/cancelled preflight result",
        bool(results) and results[0].state == "failed",
        f"got results={[(r.action_id, r.state) for r in results]}",
    )
    check(
        "cancel-before-download: result message names cancellation",
        bool(results) and "cancel" in results[0].message.lower(),
        f"got message={results[0].message if results else None}",
    )
    check("cancel-before-download: the full-image download was never attempted", stub.download_calls == 0)
    item_after = [i for i in db.creation_items(st["profile_id"], st["attempt_id"]) if i["creation_item_id"] == st["item_id"]][0]
    check("cancel-before-download: item durably recorded 'failed'", str(item_after["state"]) == "failed")
    check("cancel-before-download: item got a real linked action (not a bare -1/0 pseudo id)", item_after.get("action_id") is not None)
    if item_after.get("action_id") is not None:
        action_after = db.action(st["profile_id"], int(item_after["action_id"]))
        check(
            "cancel-before-download: no action left orphaned as ambiguous (running/outcome_unknown)",
            action_after is not None and str(action_after["state"]) not in ("running", "outcome_unknown"),
            f"got state={action_after['state'] if action_after else None}",
        )


def test_finalize_pair_id_provenance_mismatch() -> None:
    """Section 4 (Round-7): settle_pair_finalize_success must also require
    sync_created_observations.pair_id to equal the pair actually being
    finalized -- defense in depth beyond the existing source/destination id
    checks. A mismatch must refuse (raise, causing rollback), never promote."""
    db, _path = _fresh_db()
    st = _setup_finalizable(db)
    creation_id = db.creation_ledger_for_group(st["profile_id"], st["group_id"])["creation_id"]
    # The decoy pair_id must reference a REAL sync_pairs row (the column has
    # a live FK to it) -- an unrelated, unconnected pair for a different
    # mo/inat id is exactly the kind of accidental-mismatch this defense is
    # meant to catch.
    now = "2026-01-01T00:00:00Z"
    decoy_pair_id = db.connection().execute(
        "INSERT INTO sync_pairs(profile_id,mo_observation_id,inat_observation_id,link_state,score,"
        "classification,review_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (st["profile_id"], st["mo_id"] + 12345, st["inat_id"] + 12345, "", 0, "manual", "provisional", now, now),
    ).lastrowid
    db.connection().execute(
        "UPDATE sync_created_observations SET pair_id=? WHERE creation_id=?",
        (decoy_pair_id, creation_id),
    )
    db.connection().commit()
    raised = False
    try:
        db.settle_pair_finalize_success(
            st["profile_id"], st["finalize_action_id"], st["group_id"], st["pair_id"],
            mo_observation_id=st["mo_id"], inat_observation_id=st["inat_id"],
        )
    except RuntimeError:
        raised = True
    check("finalize negative: sync_created_observations.pair_id mismatch refuses (raises)", raised)
    pair = db.pair_detail(st["profile_id"], st["pair_id"])
    check(
        "finalize negative: pair_id mismatch leaves the pair untouched (rolled back, still provisional)",
        pair is not None and str(pair["review_state"]) == "provisional",
    )
    action = db.action(st["profile_id"], st["finalize_action_id"])
    check(
        "finalize negative: pair_id mismatch leaves the finalize action untouched (still running)",
        action is not None and str(action["state"]) == "running",
    )


def main() -> int:
    try:
        test_finalize_success()
        test_finalize_negatives()
        test_ambiguous_action_preservation()
        test_preflight_atomicity_positive_cases()
        test_preflight_atomicity_race_never_downgrades()
        test_population_loop_cancellation_before_download()
        test_finalize_pair_id_provenance_mismatch()
    finally:
        for path in _temp_db_paths:
            path.unlink(missing_ok=True)
    failed = [r for r in _results if r[1] == FAIL]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed.")
    if failed:
        print("FAILURES:")
        for name, _state, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
