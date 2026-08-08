#!/usr/bin/env python3
"""Disposable, offline Gate 2B saga smoke harness.

This is not a repository test and never opens a network connection. It uses
in-memory fake remote accounts plus a temporary SQLite file, exercises the
real ConsolidationService/LinkRepairService/DB path, and removes the database
after each scenario. No credential is read, printed, or persisted.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observation_workbench.api.auth import AuthState  # noqa: E402
from observation_workbench.api.client import INatAPIError  # noqa: E402
from observation_workbench.reconciliation.actions import LinkRepairService  # noqa: E402
from observation_workbench.reconciliation.consolidation import (  # noqa: E402
    ConsolidationError,
    ConsolidationService,
    select_canonical,
)
from observation_workbench.reconciliation.db import (
    ReconciliationDB,
    _utc_now,
)  # noqa: E402
from observation_workbench.reconciliation.mo_client import (  # noqa: E402
    MOAPIError,
    ReconciliationCancelled,
)
from observation_workbench.reconciliation.types import (  # noqa: E402
    ConsolidationEvidencePath,
    RemoteSite,
)

PROFILE_ID = 1
INAT_USER_ID = 500
MO_USER_ID = 900
FIELD_ID = 77
EXTERNAL_SITE_ID = 88


def _inat(observation_id: int, *, voucher: str = "AR-1") -> dict:
    return {
        "id": observation_id,
        "uuid": f"inat-uuid-{observation_id}",
        "user": {"id": INAT_USER_ID, "login": "inat_user"},
        "taxon": {
            "id": 123,
            "name": "Amanita muscaria",
            "rank": "species",
            "iconic_taxon_name": "Fungi",
            "ancestry": "47170/123",
        },
        "observed_on": "2026-06-01",
        "updated_at": "2026-07-01T00:00:00+00:00",
        "place_guess": "Some Forest",
        "geojson": {"coordinates": [-122.1, 47.1]},
        "positional_accuracy": 10,
        "description": "reviewed iNaturalist note",
        "ofvs": [
            {
                "id": f"voucher-{observation_id}",
                "uuid": f"voucher-uuid-{observation_id}",
                "observation_field": {"id": 901, "name": "Voucher Number"},
                "value": voucher,
            }
        ],
        "observation_photos": [],
    }


def _mo(observation_id: int, *, voucher: str = "AR-1") -> dict:
    return {
        "id": observation_id,
        "owner_id": MO_USER_ID,
        "owner": {"id": MO_USER_ID, "login": "mo_user"},
        "consensus": {
            "id": 321,
            "text_name": "Amanita muscaria",
            "rank": "Species",
            "classification": {"kingdom": "Fungi"},
        },
        "date": "2026-06-01",
        "updated_at": "2026-07-01T00:00:00+00:00",
        "location": {"id": 5, "name": "Some Forest"},
        "latitude": 47.1,
        "longitude": -122.1,
        "gps_accuracy": 10,
        "notes": "reviewed Mushroom Observer note",
        "herbarium_records": [{"accession_number": voucher}],
        "collection_numbers": [],
        "images": [],
    }


class FakeINat:
    def __init__(self, observations: dict[int, dict]) -> None:
        self.observations = observations
        self.write_calls = 0
        self.lose_next_response = False
        self.after_write = None
        self._lock = threading.Lock()

    def get_reconciliation_detail(self, observation_id, api_token="", *, deep=True):
        with self._lock:
            value = self.observations.get(int(observation_id))
            return {"results": [deepcopy(value)] if value else []}

    def get_current_user_v2(self, api_token):
        return {
            "results": (
                [{"id": INAT_USER_ID, "login": "inat_user"}]
                if api_token == "offline-inat-token"
                else []
            )
        }

    def get_observation_fields_autocomplete(self, query):
        return {
            "results": (
                [
                    {
                        "id": FIELD_ID,
                        "name": "Mushroom Observer URL",
                        "datatype": "text",
                    }
                ]
                if query == "Mushroom Observer URL"
                else []
            )
        }

    def create_reconciliation_field_value_v2(
        self,
        api_token,
        observation_uuid,
        observation_field_id,
        value,
    ):
        with self._lock:
            observation = next(
                item
                for item in self.observations.values()
                if item["uuid"] == observation_uuid
            )
            self.write_calls += 1
            observation["ofvs"].append(
                {
                    "id": f"link-{self.write_calls}",
                    "uuid": f"link-uuid-{self.write_calls}",
                    "observation_field": {
                        "id": observation_field_id,
                        "name": "Mushroom Observer URL",
                    },
                    "value": value,
                }
            )
            hook = self.after_write
            lose = self.lose_next_response
            self.lose_next_response = False
        if hook:
            hook()
        if lose:
            raise INatAPIError(
                "simulated lost response",
                endpoint="/observation_field_values",
                request_phase="unsafe_write",
                method="POST",
            )
        return {"results": [{"uuid": f"link-uuid-{self.write_calls}"}]}


class FakeMO:
    def __init__(self, observations: dict[int, dict]) -> None:
        self.observations = observations
        self.links: list[dict] = []
        self.write_calls = 0
        self.lose_next_response = False
        self.fail_next_observation_read = False
        self.after_write = None
        self._lock = threading.Lock()

    def external_sites(self, cancelled):
        return {
            "results": [
                {
                    "id": EXTERNAL_SITE_ID,
                    "name": "iNaturalist",
                    "url": "https://www.inaturalist.org",
                }
            ]
        }

    def observation(self, observation_id, cancelled, *, detail="high"):
        with self._lock:
            if self.fail_next_observation_read:
                self.fail_next_observation_read = False
                raise RuntimeError("simulated verification outage")
            value = self.observations.get(int(observation_id))
            return {"results": [deepcopy(value)] if value else []}

    def images_for_observation(self, observation_id, cancelled):
        return {"results": []}

    def sequences(self, observation_ids, cancelled):
        raise AssertionError("Phase 2B must not call the unverified MO sequence reader")

    def external_links(self, observation_ids, cancelled):
        wanted = {int(value) for value in observation_ids}
        with self._lock:
            return {
                "results": [
                    deepcopy(row)
                    for row in self.links
                    if int(row["observation"]) in wanted
                ]
            }

    def names(self, ids, cancelled):
        return {"results": []}

    def authenticated_user_id(self, api_key, expected_user_id, cancelled):
        return expected_user_id if api_key == "offline-mo-key" else None

    def create_external_link(
        self,
        api_key,
        observation_id,
        external_site_id,
        url,
        cancelled,
    ):
        with self._lock:
            self.write_calls += 1
            self.links.append(
                {
                    "id": self.write_calls,
                    "observation": int(observation_id),
                    "external_site": int(external_site_id),
                    "url": url,
                }
            )
            hook = self.after_write
            lose = self.lose_next_response
            self.lose_next_response = False
            if lose:
                self.fail_next_observation_read = True
        if hook:
            hook()
        if lose:
            raise MOAPIError(
                "external_links",
                None,
                "simulated lost response",
                response_received=False,
                outcome_unknown=True,
                error_code="simulated_lost_response",
            )
        return {"results": [{"id": self.write_calls}]}


class Environment:
    def __init__(
        self,
        mo_ids: tuple[int, ...],
        inat_ids: tuple[int, ...],
    ) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="gate2b-")
        self.path = Path(self.temp.name) / "reconciliation.db"
        self.db = ReconciliationDB(self.path)
        now = _utc_now()
        self.db.connection().execute(
            "INSERT INTO sync_profiles(profile_id,inat_user_id,inat_login,"
            "mo_user_id,mo_login,created_at,last_used_at) VALUES(?,?,?,?,?,?,?)",
            (
                PROFILE_ID,
                INAT_USER_ID,
                "inat_user",
                MO_USER_ID,
                "mo_user",
                now,
                now,
            ),
        )
        self.db.save_field_binding(
            PROFILE_ID,
            "mo_url",
            FIELD_ID,
            "Mushroom Observer URL",
            "text",
            "verified",
        )
        self.inat = FakeINat({value: _inat(value) for value in inat_ids})
        self.mo = FakeMO({value: _mo(value) for value in mo_ids})
        self.auth = AuthState("offline-inat-token", "inat_user")
        self._build_services()

    def _build_services(self) -> None:
        self.links = LinkRepairService(
            self.db,
            self.inat,
            self.mo,
            lambda: self.auth,
            lambda _profile_id: "offline-mo-key",
            lambda: 0,
            lambda: 0,
        )
        self.service = ConsolidationService(
            self.db,
            self.inat,
            self.mo,
            lambda: self.auth,
            lambda _profile_id: "offline-mo-key",
            lambda: 0,
            lambda: 0,
            self.links,
        )

    def reopen(self) -> None:
        self.db.close_thread_connection()
        self.db = ReconciliationDB(self.path)
        self._build_services()

    def preview(
        self,
        mo_ids: tuple[int, ...],
        inat_ids: tuple[int, ...],
        canonical_mo: int | None,
        canonical_inat: int | None,
    ):
        preview = self.service.prepare_preview(
            PROFILE_ID,
            [
                *((RemoteSite.MO, value) for value in mo_ids),
                *((RemoteSite.INAT, value) for value in inat_ids),
            ],
            lambda: False,
        )
        return select_canonical(preview, canonical_mo, canonical_inat)

    def journal(self, preview):
        return self.db.journal_consolidation_attempt(preview)

    def run(self, group_id: int):
        return self.service.execute_group(
            PROFILE_ID,
            group_id,
            lambda: False,
            lambda _message: None,
        )

    def close(self) -> None:
        self.db.close_thread_connection()
        self.temp.cleanup()


def _scenario_shape(
    label: str,
    mo_ids: tuple[int, ...],
    inat_ids: tuple[int, ...],
    canonical_mo: int | None,
    canonical_inat: int | None,
) -> None:
    env = Environment(mo_ids, inat_ids)
    try:
        donor_mo_before = {
            value: deepcopy(env.mo.observations[value])
            for value in mo_ids
            if value != canonical_mo
        }
        donor_inat_before = {
            value: deepcopy(env.inat.observations[value])
            for value in inat_ids
            if value != canonical_inat
        }
        preview = env.preview(mo_ids, inat_ids, canonical_mo, canonical_inat)
        consolidation_id, _attempt_id, group_id = env.journal(preview)
        results = env.run(group_id)
        assert results and results[-1].state == "succeeded", results
        detail = env.db.consolidation_detail(PROFILE_ID, consolidation_id)
        assert detail and detail["state"] == "finalized"
        members = detail["members"]
        assert all(
            row["local_state"]
            == ("canonical" if row["role"] == "canonical" else "superseded")
            for row in members
        )
        assert all(
            row["admitted_from_attempt_member_id"] is not None
            and row["added_by_attempt_id"] == row["superseded_by_attempt_id"]
            for row in members
            if row["role"] == "donor"
        )
        expected_link_writes = int(
            canonical_mo is not None and canonical_inat is not None
        )
        assert env.mo.write_calls == expected_link_writes
        assert env.inat.write_calls == expected_link_writes
        assert donor_mo_before == {
            value: env.mo.observations[value] for value in donor_mo_before
        }
        assert donor_inat_before == {
            value: env.inat.observations[value] for value in donor_inat_before
        }
        assert (
            env.db.connection()
            .execute(
                "SELECT COUNT(*) FROM sync_actions WHERE action_type LIKE '%remove%' "
                "OR action_type LIKE '%delete%'"
            )
            .fetchone()[0]
            == 0
        )
        print(f"{label}: PASS")
    finally:
        env.close()


def scenario_cancel_and_no_canonical() -> None:
    env = Environment((10, 11), (20,))
    try:
        preview = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 11), (RemoteSite.INAT, 20)],
            lambda: False,
        )
        assert (
            env.db.connection()
            .execute("SELECT COUNT(*) FROM sync_consolidations")
            .fetchone()[0]
            == 0
        )
        try:
            select_canonical(preview, None, None)
            raise AssertionError("canonical selection unexpectedly succeeded")
        except ConsolidationError:
            pass
        try:
            env.db.journal_consolidation_attempt(preview)
            raise AssertionError("unselected preview unexpectedly journaled")
        except ValueError:
            pass
        print("cancel preview / no canonical: PASS")
    finally:
        env.close()


def scenario_stale_member(canonical: bool) -> None:
    env = Environment((10, 11), (20,))
    try:
        preview = env.preview((10, 11), (20,), 10, 20)
        _cid, attempt_id, group_id = env.journal(preview)
        target = 10 if canonical else 11
        env.mo.observations[target]["notes"] += " changed"
        results = env.run(group_id)
        assert results and results[-1].state == "failed"
        assert env.mo.write_calls == 0 and env.inat.write_calls == 0
        assert env.db.consolidation_attempt(PROFILE_ID, attempt_id)["state"] == "failed"
        print(("canonical" if canonical else "source") + " changed after preview: PASS")
    finally:
        env.close()


def scenario_evidence_edge_removed() -> None:
    env = Environment((10, 11), (20,))
    try:
        preview = env.preview((10, 11), (20,), 10, 20)
        _cid, attempt_id, group_id = env.journal(preview)
        # Exact-date/close-coordinate edges remain, but the reviewed voucher
        # edge for this donor disappears. The attempt must not silently switch
        # to the alternate route.
        env.mo.observations[11]["herbarium_records"] = []
        result = env.run(group_id)
        assert result[-1].state == "failed", result
        assert env.mo.write_calls == 0 and env.inat.write_calls == 0
        assert env.db.consolidation_attempt(PROFILE_ID, attempt_id)["state"] == "failed"
        assert (
            env.db.consolidation_membership_for_observation(PROFILE_ID, "mo", 11)
            is None
        )
        proposed = next(
            row
            for row in env.db.consolidation_attempt_members(PROFILE_ID, attempt_id)
            if row["observation_id"] == 11
        )
        assert proposed["local_state"] == "proposed"
        print("reviewed edge removed while alternate edge remains: PASS")
    finally:
        env.close()


def scenario_journal_rebuilds_graph_paths() -> None:
    env = Environment((10, 11), (20,))
    try:
        preview = env.preview((10, 11), (20,), 10, 20)
        canonical_edge = next(
            edge
            for edge in preview.evidence_edges
            if edge.evidence_strength == "strong"
            if {
                (edge.left_site, edge.left_observation_id),
                (edge.right_site, edge.right_observation_id),
            }
            == {(RemoteSite.MO, 10), (RemoteSite.INAT, 20)}
        )
        donor_edge = next(
            edge
            for edge in preview.evidence_edges
            if (RemoteSite.MO, 11)
            in {
                (edge.left_site, edge.left_observation_id),
                (edge.right_site, edge.right_observation_id),
            }
            and edge is not canonical_edge
        )
        malicious = replace(
            preview,
            evidence_edges=(
                canonical_edge,
                replace(
                    donor_edge,
                    evidence_strength="corroborating",
                    evidence_type="exact_date_close_coordinates",
                    reviewed_evidence_fingerprint="fabricated-corrob-proof",
                ),
            ),
            donor_evidence_paths=(
                ConsolidationEvidencePath(
                    RemoteSite.MO,
                    11,
                    ("fabricated display path",),
                    strong_anchor_step="fabricated nonempty anchor",
                ),
            ),
        )
        try:
            env.journal(malicious)
            raise AssertionError("journal trusted a fabricated display path")
        except ValueError as exc:
            assert "durable evidence graph is invalid" in str(exc).lower()
        for forged_edge, expected in (
            (
                replace(
                    canonical_edge,
                    evidence_type="made_up_proof",
                    evidence_strength="strong",
                ),
                "unsupported evidence type",
            ),
            (
                replace(
                    canonical_edge,
                    evidence_strength="corroborating",
                ),
                "must have strength",
            ),
        ):
            forged = replace(
                preview,
                evidence_edges=tuple(
                    forged_edge if edge is canonical_edge else edge
                    for edge in preview.evidence_edges
                ),
            )
            try:
                env.journal(forged)
                raise AssertionError("journal accepted forged evidence semantics")
            except ValueError as exc:
                assert expected in str(exc).lower()
        assert (
            env.db.connection()
            .execute("SELECT COUNT(*) FROM sync_consolidation_attempts")
            .fetchone()[0]
            == 0
        )
        assert (
            env.db.connection()
            .execute("SELECT COUNT(*) FROM sync_consolidations")
            .fetchone()[0]
            == 0
        )
        print(
            "journal reconstructs paths and rejects forged evidence "
            "types/strengths: PASS"
        )
    finally:
        env.close()


def scenario_pair_conflict() -> None:
    env = Environment((10, 11, 99), (20, 98))
    try:
        preview = env.preview((10, 11), (20,), 10, 20)
        _cid, attempt_id, group_id = env.journal(preview)
        now = _utc_now()
        env.db.connection().execute(
            "INSERT INTO sync_pairs(profile_id,mo_observation_id,inat_observation_id,"
            "link_state,score,classification,review_state,confirmed_by,created_at,updated_at) "
            "VALUES(?,10,98,'',0,'manual','confirmed','user',?,?)",
            (PROFILE_ID, now, now),
        )
        results = env.run(group_id)
        assert results and results[-1].state == "failed"
        assert env.mo.write_calls == 0 and env.inat.write_calls == 0
        assert env.db.consolidation_attempt(PROFILE_ID, attempt_id)["state"] == "failed"
        print("pair conflict after preview: PASS")
    finally:
        env.close()


def scenario_second_preflight_fails() -> None:
    env = Environment((10, 11), (20,))
    try:
        preview = env.preview((10, 11), (20,), 10, 20)
        _cid, attempt_id, group_id = env.journal(preview)
        env.mo.after_write = lambda: env.inat.observations[20].update(
            {"description": "changed between ordinals"}
        )
        results = env.run(group_id)
        assert results[-1].state == "failed"
        assert env.mo.write_calls == 1 and env.inat.write_calls == 0
        assert not any(
            row["action_type"] == "consolidation_finalize"
            for row in env.db.action_group_rows(PROFILE_ID, group_id)
        )
        assert env.db.consolidation_attempt(PROFILE_ID, attempt_id)["state"] == "failed"
        print("first link succeeds / second preflight fails / tail stops: PASS")
    finally:
        env.close()


def scenario_unknown_resume() -> None:
    env = Environment((10, 11), (20,))
    try:
        preview = env.preview((10, 11), (20,), 10, 20)
        _cid, _attempt_id, group_id = env.journal(preview)
        env.mo.lose_next_response = True
        first = env.run(group_id)
        assert first[-1].state == "outcome_unknown"
        assert env.mo.write_calls == 1 and env.inat.write_calls == 0
        resumed = env.run(group_id)
        assert resumed[-1].state == "succeeded", resumed
        assert env.mo.write_calls == 1, "resume uploaded the MO link twice"
        assert env.inat.write_calls == 1
        print("outcome_unknown verifies on resume without duplicate write: PASS")
    finally:
        env.close()


def scenario_restart_each_ordinal() -> None:
    env = Environment((10, 11), (20,))
    try:
        preview = env.preview((10, 11), (20,), 10, 20)
        _cid, _attempt_id, group_id = env.journal(preview)
        original = env.links.execute_journaled_action
        count = {"value": 0}

        def crash_after_first(*args, **kwargs):
            result = original(*args, **kwargs)
            count["value"] += 1
            if count["value"] == 1:
                raise SystemExit("simulated process stop after ordinal 1")
            return result

        env.links.execute_journaled_action = crash_after_first
        try:
            env.run(group_id)
        except SystemExit:
            pass
        env.reopen()

        def crash_before_finalize(*args, **kwargs):
            raise SystemExit("simulated process stop before finalize")

        env.service._execute_finalize = crash_before_finalize
        try:
            env.run(group_id)
        except SystemExit:
            pass
        env.reopen()
        final = env.run(group_id)
        assert final[-1].state == "succeeded", final
        assert env.mo.write_calls == 1 and env.inat.write_calls == 1
        print("restart between every dynamically minted saga ordinal: PASS")
    finally:
        env.close()


def scenario_concurrent_resume() -> None:
    env = Environment((10, 11), (20,))
    try:
        preview = env.preview((10, 11), (20,), 10, 20)
        _cid, _attempt_id, group_id = env.journal(preview)
        barrier = threading.Barrier(2)
        outcomes: list[list] = []
        failures: list[BaseException] = []
        outcome_lock = threading.Lock()

        def resume() -> None:
            try:
                barrier.wait(timeout=5)
                result = env.run(group_id)
                with outcome_lock:
                    outcomes.append(result)
            except BaseException as exc:
                with outcome_lock:
                    failures.append(exc)
            finally:
                env.db.close_thread_connection()

        threads = [threading.Thread(target=resume) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not any(thread.is_alive() for thread in threads)
        assert not failures, failures
        # One caller may observe a claimed pending/running row while the other
        # completes it. A final ordinary resume must be read-only remotely.
        env.run(group_id)
        assert env.mo.write_calls == 1
        assert env.inat.write_calls == 1
        assert (
            env.db.consolidation_ledger_for_group(PROFILE_ID, group_id)["state"]
            == "succeeded"
        )
        print("two concurrent resumes produce one write per canonical link: PASS")
    finally:
        env.close()


def scenario_finalize_rollback() -> None:
    env = Environment((10, 11), (20,))
    try:
        preview = env.preview((10, 11), (20,), 10, 20)
        consolidation_id, attempt_id, group_id = env.journal(preview)
        original_finalize = env.service._execute_finalize

        def stop_before_finalize(*args, **kwargs):
            raise SystemExit("simulated stop after finalization row mint")

        env.service._execute_finalize = stop_before_finalize
        try:
            env.run(group_id)
        except SystemExit:
            pass
        finalize = next(
            row
            for row in env.db.action_group_rows(PROFILE_ID, group_id)
            if row["action_type"] == "consolidation_finalize"
        )
        # Deliberately violate one required provenance invariant, then let the
        # real transaction attempt finalization.
        env.db.connection().execute(
            "UPDATE sync_actions SET pair_id=NULL WHERE profile_id=? AND action_id=?",
            (PROFILE_ID, int(finalize["action_id"])),
        )
        env.service._execute_finalize = original_finalize
        result = env.run(group_id)
        assert result[-1].state == "failed", result
        members = env.db.consolidation_detail(PROFILE_ID, consolidation_id)["members"]
        assert all(row["local_state"] == "active" for row in members)
        pair = (
            env.db.connection()
            .execute(
                "SELECT review_state FROM sync_pairs WHERE profile_id=? "
                "AND mo_observation_id=10 AND inat_observation_id=20",
                (PROFILE_ID,),
            )
            .fetchone()
        )
        assert pair and pair["review_state"] == "provisional"
        assert env.db.consolidation_attempt(PROFILE_ID, attempt_id)["state"] == "failed"
        print("local finalization invariant failure rolls back atomically: PASS")
    finally:
        env.close()


def scenario_immutable_superseding_attempt() -> None:
    env = Environment((10, 11, 12), (20,))
    try:
        first_preview = env.preview((10, 11), (20,), 10, 20)
        consolidation_id, first_attempt_id, first_group_id = env.journal(first_preview)
        first_attempt_before = dict(
            env.db.consolidation_attempt(PROFILE_ID, first_attempt_id)
        )
        env.mo.observations[11]["notes"] = "donor changed after first review"
        failed = env.run(first_group_id)
        assert failed[-1].state == "failed"
        env.mo.observations[10]["notes"] = "canonical legitimately changed"

        second_preview = env.preview((10, 11), (20,), 10, 20)
        same_id, second_attempt_id, second_group_id = env.journal(second_preview)
        assert same_id == consolidation_id
        assert second_attempt_id != first_attempt_id
        second_attempt = env.db.consolidation_attempt(PROFILE_ID, second_attempt_id)
        assert second_attempt["supersedes_attempt_id"] == first_attempt_id
        assert second_attempt["action_group_id"] != first_group_id
        first_attempt_after = env.db.consolidation_attempt(PROFILE_ID, first_attempt_id)
        for immutable_column in (
            "action_group_id",
            "correlation_marker",
            "canonical_mo_fingerprint",
            "canonical_inat_fingerprint",
            "donor_fingerprints",
            "canonical_pair_fingerprint",
            "approved_unsupported_gaps",
        ):
            assert (
                first_attempt_after[immutable_column]
                == first_attempt_before[immutable_column]
            )
        completed = env.run(second_group_id)
        assert completed[-1].state == "succeeded", completed
        assert (
            env.db.get_consolidation(PROFILE_ID, consolidation_id)[
                "current_finalized_attempt_id"
            ]
            == second_attempt_id
        )
        extension = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 12)],
            lambda: False,
        )
        assert extension.eligibility.eligible
        _, third_attempt_id, third_group_id = env.journal(extension)
        extended = env.run(third_group_id)
        assert extended[-1].state == "succeeded", extended
        assert (
            env.db.get_consolidation(PROFILE_ID, consolidation_id)[
                "current_finalized_attempt_id"
            ]
            == third_attempt_id
        )
        print(
            "failed attempt → changed canonical → successful retry baseline "
            "→ valid extension: PASS"
        )
    finally:
        env.close()


def scenario_initial_canonical_abandonment_boundary() -> None:
    env = Environment((10, 11, 12), (20,))
    try:
        first = env.preview((10, 11), (20,), 10, 20)
        abandoned_id, failed_attempt_id, failed_group_id = env.journal(first)
        env.mo.observations[11]["notes"] += " fail before any write"
        failed = env.run(failed_group_id)
        assert failed[-1].state == "failed"
        assert env.mo.write_calls == 0 and env.inat.write_calls == 0

        replacement = env.preview((10, 11, 12), (20,), 11, 20)
        replacement_id, replacement_attempt_id, replacement_group_id = env.journal(
            replacement
        )
        assert replacement_id != abandoned_id
        assert (
            env.db.get_consolidation(PROFILE_ID, abandoned_id)["state"] == "cancelled"
        )
        assert not env.db.list_consolidation_members(PROFILE_ID, abandoned_id)
        assert (
            env.db.consolidation_attempt(PROFILE_ID, failed_attempt_id)["state"]
            == "failed"
        )
        assert (
            env.db.consolidation_attempt(PROFILE_ID, replacement_attempt_id)[
                "supersedes_attempt_id"
            ]
            == failed_attempt_id
        )
        completed = env.run(replacement_group_id)
        assert completed[-1].state == "succeeded", completed
        assert (
            env.db.get_consolidation(PROFILE_ID, replacement_id)[
                "canonical_mo_observation_id"
            ]
            == 11
        )

        # Once any remote write starts, canonical choices stay locked.
        env2 = Environment((30, 31), (40,))
        try:
            initial = env2.preview((30, 31), (40,), 30, 40)
            locked_id, _locked_attempt, locked_group = env2.journal(initial)
            env2.mo.after_write = lambda: env2.mo.observations[31].update(
                notes="changed after first canonical write"
            )
            locked_result = env2.run(locked_group)
            assert locked_result[-1].state == "failed", locked_result
            changed = env2.preview((30, 31), (40,), 31, 40)
            try:
                env2.journal(changed)
                raise AssertionError(
                    "canonical choices changed after a remote write started"
                )
            except ValueError as exc:
                assert "canonical choices are locked" in str(exc).lower()
            assert env2.db.get_consolidation(PROFILE_ID, locked_id)["state"] in {
                "confirmed",
                "draft",
            }
        finally:
            env2.close()
        print("no-write canonical abandonment / post-write lock: PASS")
    finally:
        env.close()


def scenario_unsupported_and_filtering() -> None:
    env = Environment((10, 11), (20,))
    try:
        preview = env.preview((10, 11), (20,), 10, 20)
        assert preview.unsupported_items
        assert all(not item.enabled for item in preview.unsupported_items)
        consolidation_id, attempt_id, group_id = env.journal(preview)
        items = env.db.consolidation_items(PROFILE_ID, attempt_id)
        assert items and all(item["state"] == "disabled" for item in items)
        assert not any(
            row["action_type"]
            in {
                "inat_photo_attach",
                "mo_photo_attach",
                "inat_its_add",
                "mo_sequence_add",
            }
            for row in env.db.action_group_rows(PROFILE_ID, group_id)
        )
        assert not any(
            "photo" in str(row["action_type"])
            for row in env.db.action_group_rows(PROFILE_ID, group_id)
        ), "an unsupported photo action was minted"
        print(
            "photo outcome_unknown/resume scenarios: SAFE N/A "
            "(photo transfer disabled; no action can be minted)"
        )
        env.run(group_id)
        assert ("mo", 11) in env.db.superseded_member_keys(PROFILE_ID)
        assert all(
            item.key.observation_id != 11
            for item in env.db.inventory_records(PROFILE_ID, "mo")
        )
        history = env.db.consolidation_detail(PROFILE_ID, consolidation_id)
        assert any(
            member["observation_id"] == 11
            and member["local_state"] == "superseded"
            and member["superseded_by"] == "Superseded by MO 10 / iNat 20."
            and member["remote_url"].endswith("/obs/11")
            for member in history["members"]
        )
        print("unsupported items disabled / superseded filtering / history: PASS")
    finally:
        env.close()


def _finalize_initial(env: Environment):
    preview = env.preview((10, 11), (20,), 10, 20)
    consolidation_id, attempt_id, group_id = env.journal(preview)
    result = env.run(group_id)
    assert result[-1].state == "succeeded", result
    return consolidation_id, attempt_id


def scenario_extension_noop_and_history() -> None:
    env = Environment((10, 11, 12, 13), (20, 21))
    try:
        consolidation_id, first_attempt_id = _finalize_initial(env)
        first_attempt_before = dict(
            env.db.consolidation_attempt(PROFILE_ID, first_attempt_id)
        )
        first_evidence_before = [
            dict(row)
            for row in env.db.consolidation_evidence(PROFILE_ID, first_attempt_id)
        ]
        prior_donor_before = next(
            dict(row)
            for row in env.db.list_consolidation_members(PROFILE_ID, consolidation_id)
            if row["site"] == "mo" and row["observation_id"] == 11
        )
        writes_before = (env.mo.write_calls, env.inat.write_calls)
        extension = env.service.prepare_preview(
            PROFILE_ID,
            [
                (RemoteSite.MO, 10),
                (RemoteSite.MO, 12),
                (RemoteSite.INAT, 21),
            ],
            lambda: False,
        )
        assert extension.is_extension
        assert extension.consolidation_id == consolidation_id
        assert extension.canonical_mo_observation_id == 10
        assert extension.canonical_inat_observation_id == 20
        assert {m.observation_id for m in extension.donor_members} == {12, 21}
        assert len(extension.donor_evidence_paths) == 2
        same_id, second_attempt_id, group_id = env.journal(extension)
        assert same_id == consolidation_id
        result = env.run(group_id)
        assert result[-1].state == "succeeded", result
        assert (env.mo.write_calls, env.inat.write_calls) == writes_before
        members = env.db.list_consolidation_members(PROFILE_ID, consolidation_id)
        prior_donor_after = next(
            dict(row)
            for row in members
            if row["site"] == "mo" and row["observation_id"] == 11
        )
        assert prior_donor_after == prior_donor_before
        assert (
            dict(env.db.consolidation_attempt(PROFILE_ID, first_attempt_id))
            == first_attempt_before
        )
        assert [
            dict(row)
            for row in env.db.consolidation_evidence(PROFILE_ID, first_attempt_id)
        ] == first_evidence_before
        assert all(
            row["local_state"] == "superseded"
            and row["added_by_attempt_id"] == second_attempt_id
            and row["superseded_by_attempt_id"] == second_attempt_id
            for row in members
            if row["observation_id"] in {12, 21}
        )
        detail = env.db.consolidation_detail(PROFILE_ID, consolidation_id)
        assert len(detail["attempts"]) == 2
        assert all(attempt["evidence"] for attempt in detail["attempts"])
        env.mo.observations[10]["notes"] += " canonical drift"
        drifted = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 13)],
            lambda: False,
        )
        assert drifted.eligibility.eligible
        assert any(
            "mutable canonical content changed" in warning.lower()
            and "description" in warning.lower()
            for warning in drifted.warnings
        )
        _, third_attempt_id, third_group_id = env.journal(drifted)
        third_result = env.run(third_group_id)
        assert third_result[-1].state == "succeeded", third_result
        assert (
            env.db.get_consolidation(PROFILE_ID, consolidation_id)[
                "current_finalized_attempt_id"
            ]
            == third_attempt_id
        )
        print(
            "extension donors / canonical no-op / mutable canonical review / "
            "history preserved: PASS"
        )
    finally:
        env.close()


def scenario_extension_identity_drift_blocks() -> None:
    env = Environment((10, 11, 12), (20,))
    try:
        _finalize_initial(env)
        env.inat.observations[20]["uuid"] = "replacement-remote-uuid"
        preview = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 12)],
            lambda: False,
        )
        assert not preview.eligibility.eligible
        assert any(
            "stable remote identity" in reason.lower()
            for reason in preview.eligibility.blocking_reasons
        )
        try:
            env.journal(preview)
            raise AssertionError("stable canonical identity drift was journaled")
        except ValueError:
            pass
        print("stable canonical UUID/account identity drift blocks extension: PASS")
    finally:
        env.close()
    env = Environment((10, 11, 12), (20,))
    try:
        _finalize_initial(env)
        env.inat.observations[20]["user"]["id"] = 999999
        preview = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 12)],
            lambda: False,
        )
        assert not preview.eligibility.eligible
        assert any(
            "owned by" in reason.lower() or "owner/account" in reason.lower()
            for reason in preview.eligibility.blocking_reasons
        )
        print("changed numeric canonical owner identity blocks extension: PASS")
    finally:
        env.close()
    env = Environment((10, 11, 12), (20,))
    try:
        _finalize_initial(env)
        env.mo.observations[10]["herbarium_records"] = [
            {"accession_number": "REPLACED-ANCHOR"}
        ]
        preview = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 12)],
            lambda: False,
        )
        assert not preview.eligibility.eligible
        assert any(
            "strong specimen-identity anchor" in reason.lower()
            for reason in preview.eligibility.blocking_reasons
        )
        forged = replace(
            preview,
            eligibility=replace(
                preview.eligibility,
                eligible=True,
                blocking_reasons=(),
            ),
        )
        try:
            env.journal(forged)
            raise AssertionError("changed finalized identity anchor was journaled")
        except ValueError:
            pass
        print("finalized strong canonical identity-anchor drift blocks extension: PASS")
    finally:
        env.close()


def scenario_login_change_is_reviewable() -> None:
    env = Environment((10, 11, 12, 13), (20,))
    try:
        consolidation_id, _first_attempt_id = _finalize_initial(env)
        env.inat.observations[20]["user"]["login"] = "renamed_inat_user"
        preview = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 12)],
            lambda: False,
        )
        assert preview.eligibility.eligible
        renamed = next(
            member
            for member in preview.canonical_members
            if member.site is RemoteSite.INAT
        )
        assert renamed.account_id == INAT_USER_ID
        assert renamed.owner_login == "renamed_inat_user"
        _same, attempt_id, group_id = env.journal(preview)
        persisted = next(
            row
            for row in env.db.consolidation_attempt_members(PROFILE_ID, attempt_id)
            if row["site"] == "inat"
        )
        assert persisted["reviewed_owner_account_id"] == INAT_USER_ID
        assert persisted["reviewed_owner_login"] == "renamed_inat_user"
        result = env.run(group_id)
        assert result[-1].state == "succeeded", result
        assert (
            env.db.get_consolidation(PROFILE_ID, consolidation_id)[
                "current_finalized_attempt_id"
            ]
            == attempt_id
        )

        pinned = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 13)],
            lambda: False,
        )
        _same, _attempt_id, pinned_group = env.journal(pinned)
        env.inat.observations[20]["user"]["login"] = "changed_after_review"
        blocked = env.run(pinned_group)
        assert blocked[-1].state == "failed", blocked
        assert "login" in blocked[-1].message.lower()
        print(
            "numeric owner remains stable across login rename; active-attempt "
            "login snapshot is pinned: PASS"
        )
    finally:
        env.close()


def scenario_baseline_pointer_guards() -> None:
    env = Environment((10, 11, 12, 13, 14), (20, 21))
    try:
        first_consolidation, first_attempt = _finalize_initial(env)
        second = env.preview((12, 13), (21,), 12, 21)
        second_consolidation, second_attempt, second_group = env.journal(second)
        result = env.run(second_group)
        assert result[-1].state == "succeeded", result
        conn = env.db.connection()
        for statement, values in (
            (
                "UPDATE sync_consolidations "
                "SET current_finalized_attempt_id=? WHERE consolidation_id=?",
                (second_attempt, first_consolidation),
            ),
        ):
            try:
                conn.execute(statement, values)
                raise AssertionError(
                    "cross-consolidation baseline pointer was accepted"
                )
            except sqlite3.IntegrityError:
                pass

        now = _utc_now()
        conn.execute(
            "INSERT INTO sync_profiles(profile_id,inat_user_id,inat_login,"
            "mo_user_id,mo_login,created_at,last_used_at) "
            "VALUES(2,501,'other_inat',901,'other_mo',?,?)",
            (now, now),
        )
        other_consolidation = conn.execute(
            "INSERT INTO sync_consolidations(profile_id,"
            "canonical_mo_observation_id,state,created_at,updated_at) "
            "VALUES(2,9001,'draft',?,?)",
            (now, now),
        ).lastrowid
        try:
            conn.execute(
                "UPDATE sync_consolidations "
                "SET current_finalized_attempt_id=? WHERE consolidation_id=?",
                (first_attempt, other_consolidation),
            )
            raise AssertionError("cross-profile baseline pointer was accepted")
        except sqlite3.IntegrityError:
            pass
        other_group = conn.execute(
            "INSERT INTO sync_action_groups(profile_id,source_kind,"
            "source_fingerprint,mo_observation_id,previewed_at,confirmed_at,"
            "created_at,updated_at) VALUES(2,'creation','other-plan',9001,?,?,?,?)",
            (now, now, now, now),
        ).lastrowid
        try:
            conn.execute(
                "INSERT INTO sync_consolidation_attempts("
                "consolidation_id,profile_id,action_group_id,correlation_marker,"
                "base_finalized_attempt_id,created_at,updated_at) "
                "VALUES(?,2,?,'cross-profile-base',?,?,?)",
                (other_consolidation, other_group, first_attempt, now, now),
            )
            raise AssertionError("cross-profile attempt base was accepted")
        except sqlite3.IntegrityError:
            pass

        extension = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 14)],
            lambda: False,
        )
        _same, extension_attempt, extension_group = env.journal(extension)
        try:
            conn.execute(
                "UPDATE sync_consolidation_attempts "
                "SET base_finalized_attempt_id=? WHERE attempt_id=?",
                (second_attempt, extension_attempt),
            )
            raise AssertionError("cross-consolidation attempt base was accepted")
        except sqlite3.IntegrityError:
            pass

        conn.execute("DROP TRIGGER trg_consolidation_current_baseline_update")
        conn.execute("DROP TRIGGER trg_consolidation_attempt_base_update")
        conn.execute(
            "UPDATE sync_consolidations SET current_finalized_attempt_id=? "
            "WHERE consolidation_id=?",
            (second_attempt, first_consolidation),
        )
        conn.execute(
            "UPDATE sync_consolidation_attempts SET base_finalized_attempt_id=? "
            "WHERE attempt_id=?",
            (second_attempt, extension_attempt),
        )
        rejected = env.run(extension_group)
        assert rejected[-1].state == "failed", rejected
        assert (
            env.db.consolidation_membership_for_observation(PROFILE_ID, "mo", 14)
            is None
        )
        assert first_consolidation != second_consolidation
        print(
            "cross-consolidation/cross-profile baseline guards and independent "
            "finalization join validation: PASS"
        )
    finally:
        env.close()


def scenario_extension_missing_link_unknown_resume() -> None:
    env = Environment((10, 11, 12, 13), (20,))
    try:
        consolidation_id, _first_attempt_id = _finalize_initial(env)
        env.mo.links = [row for row in env.mo.links if int(row["observation"]) != 10]
        extension = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 12)],
            lambda: False,
        )
        _same_id, attempt_id, group_id = env.journal(extension)
        writes_before = (env.mo.write_calls, env.inat.write_calls)
        env.mo.lose_next_response = True
        first = env.run(group_id)
        assert first[-1].state == "outcome_unknown", first
        assert (env.mo.write_calls, env.inat.write_calls) == (
            writes_before[0] + 1,
            writes_before[1],
        )
        donor = env.db.consolidation_membership_for_observation(PROFILE_ID, "mo", 12)
        assert donor and donor["local_state"] == "proposed"
        try:
            env.service.prepare_preview(
                PROFILE_ID,
                [(RemoteSite.MO, 10), (RemoteSite.MO, 13)],
                lambda: False,
            )
            raise AssertionError("concurrent extension unexpectedly previewed")
        except ConsolidationError as exc:
            assert exc.code == "extension_attempt_unresolved"
            pass
        resumed = env.run(group_id)
        assert resumed[-1].state == "succeeded", resumed
        assert env.mo.write_calls == writes_before[0] + 1
        assert env.inat.write_calls == writes_before[1]
        assert (
            env.db.consolidation_attempt(PROFILE_ID, attempt_id)["state"] == "succeeded"
        )
        donor = next(
            row
            for row in env.db.list_consolidation_members(PROFILE_ID, consolidation_id)
            if row["observation_id"] == 12
        )
        assert donor["local_state"] == "superseded"
        print(
            "extension one-link repair / unknown blocks / verifier resumes once: PASS"
        )
    finally:
        env.close()


def scenario_canonical_link_noop_matrix() -> None:
    for label, mo_present, inat_present, expected_mo, expected_inat in (
        ("both canonical links already correct", True, True, 0, 0),
        ("only MO canonical link exists", True, False, 0, 1),
        ("only iNaturalist canonical link exists", False, True, 1, 0),
    ):
        env = Environment((10, 11), (20,))
        try:
            if mo_present:
                env.mo.links.append(
                    {
                        "id": 900,
                        "observation": 10,
                        "external_site": EXTERNAL_SITE_ID,
                        "url": "https://www.inaturalist.org/observations/20",
                    }
                )
            if inat_present:
                env.inat.observations[20]["ofvs"].append(
                    {
                        "id": "existing-link",
                        "uuid": "existing-link-uuid",
                        "observation_field": {
                            "id": FIELD_ID,
                            "name": "Mushroom Observer URL",
                        },
                        "value": "https://mushroomobserver.org/obs/10",
                    }
                )
            preview = env.preview((10, 11), (20,), 10, 20)
            _cid, _attempt_id, group_id = env.journal(preview)
            result = env.run(group_id)
            assert result[-1].state == "succeeded", result
            assert env.mo.write_calls == expected_mo
            assert env.inat.write_calls == expected_inat
            print(f"{label}: PASS")
        finally:
            env.close()

    env = Environment((10, 11), (20, 999))
    try:
        env.mo.links.append(
            {
                "id": 901,
                "observation": 11,
                "external_site": EXTERNAL_SITE_ID,
                "url": "https://www.inaturalist.org/observations/999",
            }
        )
        donor_links_before = deepcopy(env.mo.links)
        preview = env.preview((10, 11), (20,), 10, 20)
        _cid, _attempt_id, group_id = env.journal(preview)
        result = env.run(group_id)
        assert result[-1].state == "succeeded", result
        assert donor_links_before[0] in env.mo.links
        assert all(
            int(row["observation"]) != 11 or row == donor_links_before[0]
            for row in env.mo.links
        )
        print("donor stale link retained and donor endpoint untouched: PASS")
    finally:
        env.close()

    env = Environment((10, 11), (20, 999))
    try:
        env.mo.links.append(
            {
                "id": 902,
                "observation": 10,
                "external_site": EXTERNAL_SITE_ID,
                "url": "https://www.inaturalist.org/observations/999",
            }
        )
        preview = env.preview((10, 11), (20,), 10, 20)
        assert not preview.eligibility.eligible
        try:
            env.journal(preview)
            raise AssertionError("conflicting canonical link unexpectedly journaled")
        except ValueError:
            pass
        assert env.mo.write_calls == 0 and env.inat.write_calls == 0
        print("canonical link to different observation blocks without overwrite: PASS")
    finally:
        env.close()


def scenario_failed_extension_retry() -> None:
    env = Environment((10, 11, 12), (20,))
    try:
        consolidation_id, _ = _finalize_initial(env)
        extension = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 12)],
            lambda: False,
        )
        _same_id, failed_attempt_id, group_id = env.journal(extension)
        env.mo.observations[12]["notes"] += " changed after review"
        failed = env.run(group_id)
        assert failed[-1].state == "failed"
        assert (
            env.db.consolidation_membership_for_observation(PROFILE_ID, "mo", 12)
            is None
        )
        retry = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 12)],
            lambda: False,
        )
        _same_id, retry_attempt_id, retry_group_id = env.journal(retry)
        retry_attempt = env.db.consolidation_attempt(PROFILE_ID, retry_attempt_id)
        assert retry_attempt["supersedes_attempt_id"] == failed_attempt_id
        completed = env.run(retry_group_id)
        assert completed[-1].state == "succeeded", completed
        donor = next(
            row
            for row in env.db.list_consolidation_members(PROFILE_ID, consolidation_id)
            if row["observation_id"] == 12
        )
        assert donor["added_by_attempt_id"] == retry_attempt_id
        assert donor["superseded_by_attempt_id"] == retry_attempt_id
        assert donor["local_state"] == "superseded"
        print("failed extension releases proposal; successful retry admits once: PASS")
    finally:
        env.close()


def scenario_cancelled_extension_different_donor() -> None:
    env = Environment((10, 11, 12, 13), (20,))
    try:
        consolidation_id, baseline_attempt_id = _finalize_initial(env)
        cancelled_preview = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 12)],
            lambda: False,
        )
        _, cancelled_attempt_id, _ = env.journal(cancelled_preview)
        env.db.set_consolidation_attempt_state(
            PROFILE_ID, cancelled_attempt_id, "cancelled"
        )
        assert (
            env.db.consolidation_membership_for_observation(PROFILE_ID, "mo", 12)
            is None
        )
        assert (
            env.db.get_consolidation(PROFILE_ID, consolidation_id)[
                "current_finalized_attempt_id"
            ]
            == baseline_attempt_id
        )

        replacement = env.service.prepare_preview(
            PROFILE_ID,
            [(RemoteSite.MO, 10), (RemoteSite.MO, 13)],
            lambda: False,
        )
        _, replacement_attempt_id, replacement_group_id = env.journal(replacement)
        completed = env.run(replacement_group_id)
        assert completed[-1].state == "succeeded", completed
        assert (
            env.db.consolidation_membership_for_observation(PROFILE_ID, "mo", 12)
            is None
        )
        admitted = env.db.consolidation_membership_for_observation(PROFILE_ID, "mo", 13)
        assert admitted and admitted["local_state"] == "superseded"
        assert (
            env.db.get_consolidation(PROFILE_ID, consolidation_id)[
                "current_finalized_attempt_id"
            ]
            == replacement_attempt_id
        )
        print("cancelled extension releases donor and accepts a different set: PASS")
    finally:
        env.close()


def scenario_existing_consolidation_conflict() -> None:
    env = Environment((10, 11, 30, 31), (20, 40))
    try:
        _finalize_initial(env)
        second = env.preview((30, 31), (40,), 30, 40)
        _cid, _attempt_id, group_id = env.journal(second)
        assert env.run(group_id)[-1].state == "succeeded"
        writes_before = (env.mo.write_calls, env.inat.write_calls)
        try:
            env.service.prepare_preview(
                PROFILE_ID,
                [(RemoteSite.MO, 10), (RemoteSite.MO, 31)],
                lambda: False,
            )
            raise AssertionError("two stable consolidations were merged")
        except ConsolidationError as exc:
            assert exc.code == "existing_consolidation_conflict"
        assert (env.mo.write_calls, env.inat.write_calls) == writes_before
        print("members of two finalized consolidations block automatic merge: PASS")
    finally:
        env.close()


def scenario_read_auth_and_cancellation_guards() -> None:
    env = Environment((10, 11), (20,))
    try:
        env.auth = AuthState("offline-inat-token", "different_user")
        try:
            env.service.prepare_preview(
                PROFILE_ID,
                [(RemoteSite.MO, 10), (RemoteSite.MO, 11), (RemoteSite.INAT, 20)],
                lambda: False,
            )
            raise AssertionError("account mismatch unexpectedly previewed")
        except ConsolidationError as exc:
            assert exc.code == "inat_auth_mismatch"
        env.auth = AuthState("offline-inat-token", "inat_user")
        original_uuid = env.inat.observations[20].pop("uuid")
        try:
            env.service.prepare_preview(
                PROFILE_ID,
                [(RemoteSite.MO, 10), (RemoteSite.MO, 11), (RemoteSite.INAT, 20)],
                lambda: False,
            )
            raise AssertionError("malformed remote record unexpectedly previewed")
        except ConsolidationError as exc:
            assert exc.code == "remote_uuid_unavailable"
        env.inat.observations[20]["uuid"] = original_uuid
        try:
            env.service.prepare_preview(
                PROFILE_ID,
                [(RemoteSite.MO, 10), (RemoteSite.MO, 11), (RemoteSite.INAT, 20)],
                lambda: True,
            )
            raise AssertionError("cancelled preview unexpectedly completed")
        except ReconciliationCancelled:
            pass
        assert (
            env.db.connection()
            .execute("SELECT COUNT(*) FROM sync_consolidation_attempts")
            .fetchone()[0]
            == 0
        )

        preview = env.preview((10, 11), (20,), 10, 20)
        _cid, attempt_id, group_id = env.journal(preview)
        cancelled = {"value": False}
        env.mo.after_write = lambda: cancelled.update(value=True)
        env.service.execute_group(
            PROFILE_ID,
            group_id,
            lambda: cancelled["value"],
            lambda _message: None,
        )
        assert env.mo.write_calls == 1 and env.inat.write_calls == 0
        assert (
            env.db.consolidation_attempt(PROFILE_ID, attempt_id)["state"] == "cancelled"
        )
        assert (
            env.db.consolidation_membership_for_observation(PROFILE_ID, "mo", 11)
            is None
        )
        print(
            "auth/malformed reads/cancel-before-journal/cancel-between-ordinals: PASS"
        )
    finally:
        env.close()


def main() -> int:
    _scenario_shape(
        "one MO donor + MO canonical + iNaturalist counterpart",
        (10, 11),
        (20,),
        10,
        20,
    )
    _scenario_shape(
        "one iNaturalist donor + iNaturalist canonical + MO counterpart",
        (10,),
        (20, 21),
        10,
        20,
    )
    _scenario_shape(
        "duplicates on both sites",
        (10, 11),
        (20, 21),
        10,
        20,
    )
    # Mixed-width observation ids on one site. Every other scenario uses
    # uniform 2-digit ids, which sort identically whether the reviewed evidence
    # graph is ordered numerically or lexicographically -- so they could not
    # catch the journal/settle fingerprint sort divergence. Real iNat ids span
    # 7-9 digits and MO ids 5-6, so this shape is the common case in production.
    _scenario_shape(
        "mixed-width observation ids (evidence-graph fingerprint ordering)",
        (9, 1011),
        (20,),
        9,
        20,
    )
    _scenario_shape(
        "MO-only duplicate set",
        (10, 11),
        (),
        10,
        None,
    )
    _scenario_shape(
        "iNaturalist-only duplicate set",
        (),
        (20, 21),
        None,
        20,
    )
    scenario_cancel_and_no_canonical()
    scenario_stale_member(canonical=False)
    scenario_stale_member(canonical=True)
    scenario_evidence_edge_removed()
    scenario_journal_rebuilds_graph_paths()
    scenario_pair_conflict()
    scenario_second_preflight_fails()
    scenario_unknown_resume()
    scenario_restart_each_ordinal()
    scenario_concurrent_resume()
    scenario_finalize_rollback()
    scenario_immutable_superseding_attempt()
    scenario_initial_canonical_abandonment_boundary()
    scenario_unsupported_and_filtering()
    scenario_extension_noop_and_history()
    scenario_extension_identity_drift_blocks()
    scenario_login_change_is_reviewable()
    scenario_baseline_pointer_guards()
    scenario_extension_missing_link_unknown_resume()
    scenario_canonical_link_noop_matrix()
    scenario_failed_extension_retry()
    scenario_cancelled_extension_different_donor()
    scenario_existing_consolidation_conflict()
    scenario_read_auth_and_cancellation_guards()
    print("ALL OFFLINE GATE 2B SAGA SMOKE SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
