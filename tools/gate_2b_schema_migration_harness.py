"""Disposable smoke harness for the Gate 2B/2C consolidation schema (v17).

Not a repository test (CLAUDE.md forbids adding/running tests). Builds a
fresh on-disk DB (runs the full v1->v17 migration chain), seeds minimal rows
into sync_profiles/sync_pairs/sync_action_groups/sync_actions (including one
'consolidation_finalize' row, proving the CHECK extension), then inserts one
row into each new consolidation table and asserts:

  * PRAGMA integrity_check / foreign_key_check are clean
  * the partial unique indexes actually reject a second draft consolidation
    with the same canonical_mo_observation_id
  * sync_consolidation_members' global (profile,site,observation) UNIQUE
    rejects a second membership row for the same observation
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from observation_workbench.reconciliation import db as db_module  # noqa: E402
from observation_workbench.reconciliation.db import ReconciliationDB, _utc_now  # noqa: E402

DB_PATH = Path("/tmp/gate2b_schema_migration_harness.db")
V12_PATH = Path("/tmp/gate2b_populated_v12_harness.db")
MALFORMED_ACCOUNT_PATH = Path("/tmp/gate2b_malformed_account_v13_harness.db")
MALFORMED_CURRENT_PATH = Path("/tmp/gate2b_malformed_current_v14_harness.db")
MALFORMED_BASE_PATH = Path("/tmp/gate2b_malformed_base_v14_harness.db")
V8_SNAPSHOT_PATH = Path("/tmp/gate2b_populated_v8_snapshot_harness.db")


def _fresh(path: Path) -> sqlite3.Connection:
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_range(conn: sqlite3.Connection, first: int, last: int) -> None:
    for version in range(first, last + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            getattr(db_module, f"_migration_v{version}")(conn)
            conn.execute(f"PRAGMA user_version={version}")
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def _validate_populated_v8_snapshot_upgrade() -> None:
    """Regression: v9 must not cascade sync_action_snapshot_rows away.

    The other validators in this file either run v1..v10 against an EMPTY
    database or start at v12, and the one snapshot row they do create is
    inserted at v11 with foreign_keys=OFF. So none of them ever had a
    sync_action_snapshot_rows row present while _migration_v9 ran -- which is
    exactly the state in which the original rebuild order let the DROP of the
    temporary sync_action_groups_v8 parent CASCADE every reviewed link-repair
    snapshot away and leave the live table pointing at a table that no longer
    existed. Seed a real v8 journal (Gate 1B/1D era shipped SCHEMA_VERSION 8)
    and prove the rows survive to v17 with a usable parent reference.
    """
    conn = _fresh(V8_SNAPSHOT_PATH)
    _migrate_range(conn, 1, 8)
    now = _utc_now()
    conn.execute(
        "INSERT INTO sync_profiles(profile_id,inat_user_id,inat_login,mo_user_id,"
        "mo_login,created_at,last_used_at) VALUES(1,1,'inatuser',2,'mouser',?,?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO sync_pairs(pair_id,profile_id,mo_observation_id,"
        "inat_observation_id,classification,created_at,updated_at) "
        "VALUES(1,1,100,200,'strong',?,?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO sync_action_groups(action_group_id,profile_id,source_kind,"
        "pair_id,issue_id,source_fingerprint,mo_observation_id,inat_observation_id,"
        "previewed_at,confirmed_at,created_at,updated_at) "
        "VALUES(1,1,'pair',1,NULL,'group-fp',100,200,?,?,?,?)",
        (now, now, now, now),
    )
    conn.execute(
        "INSERT INTO sync_actions(action_id,profile_id,action_group_id,ordinal,"
        "action_type,site,state,last_phase,pair_id,mo_observation_id,"
        "inat_observation_id,inat_observation_uuid,preview_inat_record_fingerprint,"
        "preview_mo_record_fingerprint,preview_inat_links_fingerprint,"
        "preview_mo_links_fingerprint,deduplication_key,created_at,confirmed_at,"
        "updated_at) VALUES(1,1,1,0,'inat_ofv_add','inat','succeeded','verification',"
        "1,100,200,'inat-uuid-200','','','','','v8-link-add',?,?,?)",
        (now, now, now),
    )
    for snapshot_id, site, observation_id, row_id in (
        (1, "inat", 200, "ofv-1"), (2, "mo", 100, "mo-link-1"),
    ):
        conn.execute(
            "INSERT INTO sync_action_snapshot_rows(snapshot_row_id,profile_id,"
            "action_group_id,site,observation_id,remote_row_id,remote_row_uuid,"
            "binding_id,normalized_target_id,parse_state,row_fingerprint) "
            "VALUES(?,1,1,?,?,?,?,7,?,'valid',?)",
            (
                snapshot_id, site, observation_id, row_id, f"{row_id}-uuid",
                200 if site == "mo" else 100, f"{row_id}-fp",
            ),
        )
    conn.close()

    db = ReconciliationDB(path=V8_SNAPSHOT_PATH)
    upgraded = db.connection()
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 17
    rows = upgraded.execute(
        "SELECT * FROM sync_action_snapshot_rows ORDER BY snapshot_row_id"
    ).fetchall()
    assert len(rows) == 2, (
        f"v9 destroyed reviewed link snapshots: {len(rows)} of 2 survived"
    )
    assert [str(r["row_fingerprint"]) for r in rows] == ["ofv-1-fp", "mo-link-1-fp"]
    assert [int(r["action_group_id"]) for r in rows] == [1, 1]
    # The FK must point at the live parent, not a temporary rebuild name: a
    # dangling reference still allows SELECT but fails every later INSERT.
    upgraded.execute(
        "INSERT INTO sync_action_snapshot_rows(profile_id,action_group_id,site,"
        "observation_id,remote_row_id,remote_row_uuid,binding_id,"
        "normalized_target_id,parse_state,row_fingerprint) "
        "VALUES(1,1,'inat',200,'ofv-2','ofv-2-uuid',7,100,'valid','ofv-2-fp')"
    )
    assert upgraded.execute(
        "SELECT COUNT(*) FROM sync_action_snapshot_rows"
    ).fetchone()[0] == 3
    fk = upgraded.execute("PRAGMA foreign_key_check").fetchall()
    assert not fk, f"post-upgrade foreign_key_check failed: {fk}"
    db.close_thread_connection()
    print("populated v8 link snapshots survived v9→v17 and remain writable: PASS")


def _build_seeded_v10() -> None:
    """Create real v10 tables and seed Gate 1B–2A journal provenance."""
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_range(conn, 1, 10)

    now = _utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO sync_profiles(profile_id,inat_user_id,inat_login,mo_user_id,"
            "mo_login,created_at,last_used_at) VALUES(1,1,'inatuser',2,'mouser',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO sync_pairs(pair_id,profile_id,mo_observation_id,"
            "inat_observation_id,link_state,score,classification,review_state,"
            "confirmed_by,created_at,updated_at) "
            "VALUES(1,1,100,200,'link_confirmed',100,'exact','confirmed','user',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO sync_action_groups(action_group_id,profile_id,source_kind,pair_id,"
            "issue_id,source_fingerprint,mo_observation_id,inat_observation_id,"
            "previewed_at,confirmed_at,created_at,updated_at) "
            "VALUES(1,1,'pair',1,NULL,'gate1e-fp',100,200,?,?,?,?)",
            (now, now, now, now),
        )
        conn.execute(
            "INSERT INTO sync_actions(action_id,profile_id,action_group_id,ordinal,"
            "action_type,site,state,pair_id,mo_observation_id,inat_observation_id,"
            "inat_observation_uuid,preview_inat_record_fingerprint,"
            "preview_mo_record_fingerprint,preview_inat_links_fingerprint,"
            "preview_mo_links_fingerprint,deduplication_key,source_site,"
            "source_record_id,source_photo_id,reviewed_byte_fingerprint,"
            "write_started_at,outcome_unknown,created_at,confirmed_at,updated_at) "
            "VALUES(1,1,1,1,'inat_photo_attach','inat','outcome_unknown',1,100,200,"
            "'inat-uuid-200','','','','','legacy-photo-action','mo',100,'photo-1',"
            "'bytes-fp',?,1,?,?,?)",
            (now, now, now, now),
        )
        conn.execute(
            "INSERT INTO sync_photo_transfers(transfer_id,profile_id,pair_id,action_id,"
            "source_site,source_photo_id,destination_site,destination_observation_id,"
            "byte_fingerprint,state,created_at,updated_at) "
            "VALUES(1,1,1,1,'mo','photo-1','inat',200,'bytes-fp','outcome_unknown',?,?)",
            (now, now),
        )

        conn.execute(
            "INSERT INTO sync_action_groups(action_group_id,profile_id,source_kind,pair_id,"
            "issue_id,source_fingerprint,mo_observation_id,inat_observation_id,"
            "previewed_at,confirmed_at,created_at,updated_at) "
            "VALUES(2,1,'creation',NULL,NULL,'gate2a-fp',101,NULL,?,?,?,?)",
            (now, now, now, now),
        )
        conn.execute(
            "INSERT INTO sync_actions(action_id,profile_id,action_group_id,ordinal,"
            "action_type,site,state,mo_observation_id,inat_observation_id,"
            "inat_observation_uuid,preview_inat_record_fingerprint,"
            "preview_mo_record_fingerprint,preview_inat_links_fingerprint,"
            "preview_mo_links_fingerprint,deduplication_key,write_started_at,"
            "outcome_unknown,created_at,confirmed_at,updated_at) "
            "VALUES(2,1,2,1,'inat_observation_create','inat','outcome_unknown',101,NULL,"
            "'','','','','','legacy-create-action',?,1,?,?,?)",
            (now, now, now, now),
        )
        conn.execute(
            "INSERT INTO sync_actions(action_id,profile_id,action_group_id,ordinal,"
            "action_type,site,state,mo_observation_id,inat_observation_id,"
            "inat_observation_uuid,preview_inat_record_fingerprint,"
            "preview_mo_record_fingerprint,preview_inat_links_fingerprint,"
            "preview_mo_links_fingerprint,deduplication_key,source_site,"
            "source_record_id,source_photo_id,reviewed_byte_fingerprint,"
            "write_started_at,outcome_unknown,created_at,confirmed_at,updated_at) "
            "VALUES(3,1,2,2,'inat_photo_attach','inat','outcome_unknown',101,201,"
            "'inat-uuid-201','','','','','legacy-creation-photo','mo',101,'photo-2',"
            "'creation-bytes-fp',?,1,?,?,?)",
            (now, now, now, now),
        )
        conn.execute(
            "INSERT INTO sync_created_observations(creation_id,profile_id,pair_id,"
            "source_site,source_observation_id,destination_site,"
            "destination_observation_id,destination_observation_uuid,created_at,updated_at) "
            "VALUES(1,1,NULL,'mo',101,'inat',NULL,NULL,?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO sync_creation_attempts(attempt_id,creation_id,profile_id,"
            "action_group_id,destination_site,correlation_marker,marker_location,"
            "approved_field_gaps,source_fingerprint,reviewed_payload_fingerprint,"
            "state,created_at,updated_at) "
            "VALUES(1,1,1,2,'inat','seeded-marker','client_uuid_field','[]',"
            "'gate2a-fp','payload-fp','outcome_unknown',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO sync_creation_items(creation_item_id,attempt_id,action_id,"
            "item_type,source_item_identity,reviewed_metadata_fingerprint,"
            "reviewed_byte_fingerprint,state,created_at,updated_at) "
            "VALUES(1,1,3,'photo','photo-2','photo-meta','creation-bytes-fp',"
            "'outcome_unknown',?,?)",
            (now, now),
        )
    except Exception:
        conn.rollback()
        conn.close()
        raise
    else:
        conn.commit()
        conn.close()


def _validate_populated_v12_upgrade() -> None:
    for path in (V12_PATH, Path(str(V12_PATH) + "-wal"), Path(str(V12_PATH) + "-shm")):
        path.unlink(missing_ok=True)
    conn = sqlite3.connect(V12_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_range(conn, 1, 11)
    now = _utc_now()
    conn.execute(
        "INSERT INTO sync_profiles(profile_id,inat_user_id,inat_login,mo_user_id,"
        "mo_login,created_at,last_used_at) VALUES(1,1,'inatuser',2,'mouser',?,?)",
        (now, now),
    )
    states = (
        (1, "finalized", "succeeded"),
        (2, "draft", "pending"),
        (3, "draft", "outcome_unknown"),
        (4, "draft", "failed"),
        (5, "draft", "cancelled"),
    )
    for identity, consolidation_state, attempt_state in states:
        canonical_id = 1000 + identity * 10
        donor_id = canonical_id + 1
        canonical_inat_id = 2000 if identity == 1 else None
        donor_inat_id = 2001 if identity == 1 else None
        group_id = identity
        conn.execute(
            "INSERT INTO sync_action_groups(action_group_id,profile_id,source_kind,"
            "source_fingerprint,mo_observation_id,inat_observation_id,previewed_at,"
            "confirmed_at,created_at,updated_at) "
            "VALUES(?,1,'creation',?,?,?,?,?,?,?)",
            (
                group_id, f"pair-fp-{identity}", canonical_id,
                canonical_inat_id, now, now, now, now,
            ),
        )
        conn.execute(
            "INSERT INTO sync_consolidations(consolidation_id,profile_id,"
            "canonical_mo_observation_id,canonical_inat_observation_id,state,"
            "created_at,updated_at) VALUES(?,1,?,?,?,?,?)",
            (
                identity, canonical_id, canonical_inat_id,
                consolidation_state, now, now,
            ),
        )
        member_specs = [
            (identity * 10, canonical_id, "canonical",
             "canonical" if consolidation_state == "finalized" else "active", "mo"),
            (identity * 10 + 1, donor_id, "donor",
             "superseded" if consolidation_state == "finalized" else "active", "mo"),
        ]
        if canonical_inat_id is not None and donor_inat_id is not None:
            member_specs.extend((
                (identity * 10 + 2, canonical_inat_id, "canonical", "canonical", "inat"),
                (identity * 10 + 3, donor_inat_id, "donor", "superseded", "inat"),
            ))
        for member_id, observation_id, role, local_state, site in member_specs:
            conn.execute(
                "INSERT INTO sync_consolidation_members("
                "consolidation_member_id,consolidation_id,profile_id,site,"
                "observation_id,role,reviewed_record_fingerprint,"
                "preflight_record_fingerprint,local_state,created_at,updated_at"
                ") VALUES(?,?,1,?,?,?,?,?,?,?,?)",
                (
                    member_id, identity, site, observation_id, role,
                    f"member-fp-{observation_id}", f"preflight-{observation_id}",
                    local_state, now, now,
                ),
            )
        donor_specs = [("mo", donor_id)]
        if donor_inat_id is not None:
            donor_specs.append(("inat", donor_inat_id))
        donors = json.dumps([
            {
                "site": site, "observation_id": observation_id,
                "fingerprint": f"member-fp-{observation_id}",
            }
            for site, observation_id in donor_specs
        ], sort_keys=True, separators=(",", ":"))
        donor_preflight = json.dumps([
            {
                "site": site, "observation_id": observation_id,
                "fingerprint": f"preflight-{observation_id}",
            }
            for site, observation_id in donor_specs
        ], sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO sync_consolidation_attempts("
            "attempt_id,consolidation_id,profile_id,action_group_id,"
            "correlation_marker,canonical_mo_fingerprint,"
            "canonical_inat_fingerprint,canonical_mo_preflight_fingerprint,"
            "canonical_inat_preflight_fingerprint,donor_fingerprints,"
            "donor_preflight_fingerprints,canonical_pair_fingerprint,"
            "destination_mo_account,destination_inat_account,state,"
            "created_at,updated_at) VALUES(?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity, identity, group_id, f"marker-{identity}",
                f"member-fp-{canonical_id}",
                (
                    f"member-fp-{canonical_inat_id}"
                    if canonical_inat_id is not None else ""
                ),
                f"preflight-{canonical_id}",
                (
                    f"preflight-{canonical_inat_id}"
                    if canonical_inat_id is not None else ""
                ),
                donors, donor_preflight, f"pair-fp-{identity}",
                "2:mouser", "1:inatuser", attempt_state,
                now, now,
            ),
        )
    conn.execute(
        "INSERT INTO sync_actions(profile_id,action_group_id,ordinal,action_type,"
        "site,state,mo_observation_id,inat_observation_id,inat_observation_uuid,"
        "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
        "preview_inat_links_fingerprint,preview_mo_links_fingerprint,"
        "deduplication_key,outcome_unknown,created_at,confirmed_at,updated_at) "
        "VALUES(1,1,1,'consolidation_finalize','mo','succeeded',1010,2000,"
        "'inat-2000','','','','','v11-finalized-consolidation',0,?,?,?)",
        (now, now, now),
    )
    conn.execute(
        "INSERT INTO sync_actions(profile_id,action_group_id,ordinal,action_type,"
        "site,state,mo_observation_id,inat_observation_id,inat_observation_uuid,"
        "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
        "preview_inat_links_fingerprint,preview_mo_links_fingerprint,"
        "deduplication_key,outcome_unknown,created_at,confirmed_at,updated_at) "
        "VALUES(1,3,1,'mo_external_link_add','mo','outcome_unknown',1030,2030,"
        "'inat-2030','','','','','v11-unknown-link',1,?,?,?)",
        (now, now, now),
    )
    # v11's stale temporary-parent FK prevents this historical row under FK
    # enforcement. Seed it with enforcement off to prove v12's child-table
    # rebuild preserves the row and restores a valid parent reference.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO sync_action_snapshot_rows("
        "profile_id,action_group_id,site,observation_id,remote_row_id,"
        "parse_state,row_fingerprint) VALUES(1,1,'mo',1010,'legacy-link',"
        "'valid','legacy-link-fingerprint')"
    )
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        db_module._migration_v12(conn)
        conn.execute("PRAGMA user_version=12")
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "sync_consolidations", "sync_consolidation_members",
            "sync_consolidation_attempts", "sync_actions",
            "sync_creation_attempts", "sync_photo_transfers",
            "sync_action_snapshot_rows",
        )
    }
    conn.close()

    db = ReconciliationDB(V12_PATH)
    upgraded = db.connection()
    after = {
        table: upgraded.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert after["sync_consolidations"] == before["sync_consolidations"]
    assert after["sync_consolidation_attempts"] == before["sync_consolidation_attempts"]
    assert after["sync_actions"] == before["sync_actions"]
    assert after["sync_consolidation_members"] == before["sync_consolidation_members"] - 4
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 17
    assert upgraded.execute(
        "SELECT state FROM sync_actions WHERE deduplication_key='v11-unknown-link'"
    ).fetchone()["state"] == "outcome_unknown"
    assert upgraded.execute(
        "SELECT COUNT(*) FROM sync_consolidation_attempt_members"
    ).fetchone()[0] == 12
    assert upgraded.execute(
        "SELECT COUNT(*) FROM sync_unresolved_consolidation_proposals"
    ).fetchone()[0] == 2
    finalized_donors = upgraded.execute(
        "SELECT added_by_attempt_id,superseded_by_attempt_id,superseded_at "
        "FROM sync_consolidation_members WHERE consolidation_id=1 AND role='donor'"
    ).fetchall()
    assert len(finalized_donors) == 2
    assert all(row["added_by_attempt_id"] == 1 for row in finalized_donors)
    assert all(row["superseded_by_attempt_id"] == 1 for row in finalized_donors)
    assert all(row["superseded_at"] for row in finalized_donors)
    assert upgraded.execute(
        "SELECT COUNT(*) FROM sync_consolidation_members "
        "WHERE role='donor' AND admitted_from_attempt_member_id IS NOT NULL "
        "AND added_by_attempt_id=superseded_by_attempt_id"
    ).fetchone()[0] == 2
    assert upgraded.execute(
        "SELECT current_finalized_attempt_id FROM sync_consolidations "
        "WHERE consolidation_id=1"
    ).fetchone()[0] == 1
    assert upgraded.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not upgraded.execute("PRAGMA foreign_key_check").fetchall()
    assert upgraded.execute(
        "SELECT COUNT(*) FROM sync_consolidation_members "
        "WHERE consolidation_id IN (4,5) AND role='donor'"
    ).fetchone()[0] == 0
    db.close_thread_connection()
    print(
        "populated finalized/pending/outcome-unknown/failed/cancelled "
        "v12 data survived v12→v17"
    )


def _validate_malformed_legacy_account_rejected() -> None:
    for path in (
        MALFORMED_ACCOUNT_PATH,
        Path(str(MALFORMED_ACCOUNT_PATH) + "-wal"),
        Path(str(MALFORMED_ACCOUNT_PATH) + "-shm"),
    ):
        path.unlink(missing_ok=True)
    conn = sqlite3.connect(MALFORMED_ACCOUNT_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate_range(conn, 1, 13)
    now = _utc_now()
    conn.execute(
        "INSERT INTO sync_profiles(profile_id,inat_user_id,inat_login,"
        "mo_user_id,mo_login,created_at,last_used_at) "
        "VALUES(1,1,'inatuser',2,'mouser',?,?)",
        (now, now),
    )
    group_id = conn.execute(
        "INSERT INTO sync_action_groups(profile_id,source_kind,"
        "source_fingerprint,mo_observation_id,previewed_at,confirmed_at,"
        "created_at,updated_at) VALUES(1,'creation','legacy',100,?,?,?,?)",
        (now, now, now, now),
    ).lastrowid
    consolidation_id = conn.execute(
        "INSERT INTO sync_consolidations(profile_id,"
        "canonical_mo_observation_id,state,created_at,updated_at) "
        "VALUES(1,100,'draft',?,?)",
        (now, now),
    ).lastrowid
    member_id = conn.execute(
        "INSERT INTO sync_consolidation_members(consolidation_id,profile_id,"
        "site,observation_id,role,remote_uuid,reviewed_record_fingerprint,"
        "preflight_record_fingerprint,local_state,created_at,updated_at) "
        "VALUES(?,1,'mo',100,'canonical','mo-uuid','full','preflight',"
        "'active',?,?)",
        (consolidation_id, now, now),
    ).lastrowid
    attempt_id = conn.execute(
        "INSERT INTO sync_consolidation_attempts(consolidation_id,profile_id,"
        "action_group_id,correlation_marker,state,created_at,updated_at) "
        "VALUES(?,1,?,'legacy-malformed','failed',?,?)",
        (consolidation_id, group_id, now, now),
    ).lastrowid
    conn.execute(
        "INSERT INTO sync_consolidation_attempt_members(attempt_id,"
        "stable_member_id,site,observation_id,remote_uuid,participation_role,"
        "proposal_state,reviewed_record_fingerprint,"
        "preflight_record_fingerprint,reviewed_account_identity,created_at) "
        "VALUES(?,?,'mo',100,'mo-uuid','canonical_context',"
        "'canonical_context','full','preflight','not-a-numeric-owner',?)",
        (attempt_id, member_id, now),
    )
    conn.close()
    try:
        ReconciliationDB(MALFORMED_ACCOUNT_PATH)
        raise AssertionError("malformed legacy account identity was migrated")
    except RuntimeError as exc:
        assert "normalize a legacy account identity" in str(exc)
    print("malformed legacy account identity fails v14→v15 migration: PASS")


def _validate_malformed_v14_baselines_rejected() -> None:
    for target, corrupt_current in (
        (MALFORMED_CURRENT_PATH, True),
        (MALFORMED_BASE_PATH, False),
    ):
        for path in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
            path.unlink(missing_ok=True)
        conn = sqlite3.connect(target, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        _migrate_range(conn, 1, 14)
        now = _utc_now()
        for profile_id in (1, 2):
            conn.execute(
                "INSERT INTO sync_profiles(profile_id,inat_user_id,inat_login,"
                "mo_user_id,mo_login,created_at,last_used_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    profile_id, profile_id * 100 + 1, f"inat{profile_id}",
                    profile_id * 100 + 2, f"mo{profile_id}", now, now,
                ),
            )
            group_id = conn.execute(
                "INSERT INTO sync_action_groups(profile_id,source_kind,"
                "source_fingerprint,mo_observation_id,previewed_at,confirmed_at,"
                "created_at,updated_at) VALUES(?,'creation',?,?,?, ?,?,?)",
                (
                    profile_id, f"source-{profile_id}", profile_id * 1000,
                    now, now, now, now,
                ),
            ).lastrowid
            consolidation_id = conn.execute(
                "INSERT INTO sync_consolidations(profile_id,"
                "canonical_mo_observation_id,state,created_at,updated_at) "
                "VALUES(?,?,'finalized',?,?)",
                (profile_id, profile_id * 1000, now, now),
            ).lastrowid
            attempt_id = conn.execute(
                "INSERT INTO sync_consolidation_attempts(consolidation_id,"
                "profile_id,action_group_id,correlation_marker,state,"
                "created_at,updated_at) VALUES(?,?,?,?,'succeeded',?,?)",
                (
                    consolidation_id, profile_id, group_id,
                    f"attempt-{profile_id}", now, now,
                ),
            ).lastrowid
            conn.execute(
                "INSERT INTO sync_actions(profile_id,action_group_id,ordinal,"
                "action_type,site,state,mo_observation_id,deduplication_key,"
                "inat_observation_uuid,preview_inat_record_fingerprint,"
                "preview_mo_record_fingerprint,preview_inat_links_fingerprint,"
                "preview_mo_links_fingerprint,outcome_unknown,created_at,"
                "confirmed_at,updated_at) "
                "VALUES(?,?,1,'consolidation_finalize','mo','succeeded',?,?,"
                "'','','','','',0,?,?,?)",
                (
                    profile_id, group_id, profile_id * 1000,
                    f"finalize-{profile_id}", now, now, now,
                ),
            )
            if profile_id == 1:
                first_consolidation, first_attempt = consolidation_id, attempt_id
            else:
                second_attempt = attempt_id
        if corrupt_current:
            conn.execute(
                "UPDATE sync_consolidations SET current_finalized_attempt_id=? "
                "WHERE consolidation_id=?",
                (second_attempt, first_consolidation),
            )
            expected = "current finalized-attempt pointer"
        else:
            conn.execute(
                "UPDATE sync_consolidations SET current_finalized_attempt_id=? "
                "WHERE consolidation_id=?",
                (first_attempt, first_consolidation),
            )
            group_id = conn.execute(
                "INSERT INTO sync_action_groups(profile_id,source_kind,"
                "source_fingerprint,mo_observation_id,previewed_at,confirmed_at,"
                "created_at,updated_at) VALUES(1,'creation','child',1000,?,?,?,?)",
                (now, now, now, now),
            ).lastrowid
            conn.execute(
                "INSERT INTO sync_consolidation_attempts(consolidation_id,"
                "profile_id,action_group_id,correlation_marker,state,"
                "base_finalized_attempt_id,created_at,updated_at) "
                "VALUES(?,1,?,'bad-base','failed',?,?,?)",
                (first_consolidation, group_id, second_attempt, now, now),
            )
            expected = "baseline chain"
        conn.close()
        try:
            ReconciliationDB(target)
            raise AssertionError("malformed v14 baseline pointer was migrated")
        except RuntimeError as exc:
            assert expected in str(exc)
    print("cross-profile/cross-consolidation v14 baselines fail v15 migration: PASS")


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()

    _build_seeded_v10()
    db = ReconciliationDB(path=DB_PATH)
    conn = db.connection()
    now = _utc_now()

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 17, "migration did not reach v17"
    assert conn.execute(
        "SELECT state FROM sync_actions WHERE action_id=1"
    ).fetchone()["state"] == "outcome_unknown"
    assert conn.execute(
        "SELECT state FROM sync_photo_transfers WHERE transfer_id=1"
    ).fetchone()["state"] == "outcome_unknown"
    assert conn.execute(
        "SELECT state FROM sync_creation_attempts WHERE attempt_id=1"
    ).fetchone()["state"] == "outcome_unknown"
    assert conn.execute(
        "SELECT reviewed_byte_fingerprint FROM sync_creation_items "
        "WHERE creation_item_id=1"
    ).fetchone()["reviewed_byte_fingerprint"] == "creation-bytes-fp"
    print("seeded Gate 1B–2A journals and unknown outcomes survived v10→v17")

    with db.transaction() as tx:
        profile_id = tx.execute("SELECT profile_id FROM sync_profiles").fetchone()["profile_id"]

        pair_id = tx.execute("SELECT pair_id FROM sync_pairs").fetchone()["pair_id"]

        tx.execute(
            "INSERT INTO sync_action_groups(profile_id,source_kind,pair_id,issue_id,source_fingerprint,"
            "mo_observation_id,inat_observation_id,previewed_at,confirmed_at,created_at,updated_at) "
            "VALUES (?,'pair',?,NULL,'fp',100,200,?,?,?,?)",
            (profile_id, pair_id, now, now, now, now),
        )
        action_group_id = tx.execute(
            "SELECT action_group_id FROM sync_action_groups "
            "ORDER BY action_group_id DESC LIMIT 1"
        ).fetchone()["action_group_id"]

        # Proves the action_type CHECK extension accepts 'consolidation_finalize'.
        tx.execute(
            "INSERT INTO sync_actions(profile_id,action_group_id,ordinal,action_type,site,state,"
            "pair_id,mo_observation_id,inat_observation_id,inat_observation_uuid,"
            "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
            "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
            "created_at,confirmed_at,updated_at) VALUES "
            "(?,?,0,'consolidation_finalize','mo','pending',?,100,200,'',"
            "'','','','','dedup-1',?,?,?)",
            (profile_id, action_group_id, pair_id, now, now, now),
        )
        action_id = tx.execute(
            "SELECT action_id FROM sync_actions ORDER BY action_id DESC LIMIT 1"
        ).fetchone()["action_id"]

        tx.execute(
            "INSERT INTO sync_consolidations(profile_id,canonical_mo_observation_id,"
            "canonical_inat_observation_id,canonical_pair_id,state,created_at,updated_at) "
            "VALUES (?,100,200,?,'draft',?,?)",
            (profile_id, pair_id, now, now),
        )
        consolidation_id = tx.execute(
            "SELECT consolidation_id FROM sync_consolidations"
        ).fetchone()["consolidation_id"]

        tx.execute(
            "INSERT INTO sync_consolidation_members(consolidation_id,profile_id,site,observation_id,"
            "role,remote_uuid,reviewed_record_fingerprint,local_state,created_at,updated_at) "
            "VALUES (?,?,'mo',100,'canonical','','fp','canonical',?,?)",
            (consolidation_id, profile_id, now, now),
        )
        tx.execute(
            "INSERT INTO sync_consolidation_attempts(consolidation_id,profile_id,action_group_id,"
            "correlation_marker,canonical_mo_fingerprint,canonical_inat_fingerprint,donor_fingerprints,"
            "canonical_pair_fingerprint,approved_unsupported_gaps,destination_mo_account,"
            "destination_inat_account,state,created_at,updated_at) VALUES "
            "(?,?,?,'marker-1','','','[]','','[]','mouser','inatuser','pending',?,?)",
            (consolidation_id, profile_id, action_group_id, now, now),
        )
        attempt_id = tx.execute(
            "SELECT attempt_id FROM sync_consolidation_attempts"
        ).fetchone()["attempt_id"]
        canonical_member_id = tx.execute(
            "SELECT consolidation_member_id FROM sync_consolidation_members "
            "WHERE consolidation_id=? AND role='canonical'",
            (consolidation_id,),
        ).fetchone()["consolidation_member_id"]
        tx.execute(
            "INSERT INTO sync_consolidation_attempt_members("
            "attempt_id,stable_member_id,site,observation_id,participation_role,"
            "proposal_state,reviewed_record_fingerprint,"
            "preflight_record_fingerprint,reviewed_account_identity,created_at"
            ") VALUES(?,?,'mo',100,'canonical_context','canonical_context',"
            "'fp','preflight-fp','2:mouser',?)",
            (attempt_id, canonical_member_id, now),
        )
        tx.execute(
            "INSERT INTO sync_consolidation_attempt_members("
            "attempt_id,stable_member_id,site,observation_id,participation_role,"
            "proposal_state,reviewed_record_fingerprint,"
            "preflight_record_fingerprint,reviewed_account_identity,created_at"
            ") VALUES(?,NULL,'mo',101,'new_donor','proposed',"
            "'fp','preflight-fp','2:mouser',?)",
            (attempt_id, now),
        )
        member_rows = tx.execute(
            "SELECT attempt_member_id FROM sync_consolidation_attempt_members "
            "WHERE attempt_id=? ORDER BY attempt_member_id",
            (attempt_id,),
        ).fetchall()
        left_id, right_id = sorted(
            int(member["attempt_member_id"]) for member in member_rows
        )
        tx.execute(
            "INSERT INTO sync_consolidation_evidence("
            "attempt_id,left_attempt_member_id,right_attempt_member_id,evidence_type,"
            "evidence_strength,reviewed_evidence_fingerprint,display_summary,created_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                attempt_id, left_id, right_id, "exact_voucher",
                "strong", "edge-fingerprint", "Exact normalized voucher identity.", now,
            ),
        )

        tx.execute(
            "INSERT INTO sync_consolidation_items(attempt_id,source_site,source_observation_id,"
            "destination_site,destination_observation_id,item_type,source_item_identity,"
            "reviewed_metadata_fingerprint,reviewed_byte_fingerprint,action_id,state,disabled_reason,"
            "created_at,updated_at) VALUES "
            "(?,'mo',101,'mo',100,'photo','photo-1','fp','',?,'disabled','needs_capability_proof',?,?)",
            (attempt_id, action_id, now, now),
        )

    ic = conn.execute("PRAGMA integrity_check").fetchall()
    assert [r[0] for r in ic] == ["ok"], f"integrity_check failed: {ic}"
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert fk == [], f"foreign_key_check failed: {fk}"
    snapshot_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='sync_action_snapshot_rows'"
    ).fetchone()["sql"]
    assert "sync_action_groups_v8" not in snapshot_sql
    assert conn.execute(
        "SELECT COUNT(*) FROM sync_consolidation_evidence"
    ).fetchone()[0] == 1
    print("integrity_check: ok, foreign_key_check: clean")

    # Partial unique index: a second draft/confirmed consolidation claiming
    # the same canonical MO observation must be rejected.
    try:
        with db.transaction() as tx:
            tx.execute(
                "INSERT INTO sync_consolidations(profile_id,canonical_mo_observation_id,"
                "canonical_inat_observation_id,state,created_at,updated_at) "
                "VALUES (?,100,999,'draft',?,?)",
                (profile_id, now, now),
            )
        raise AssertionError("expected IntegrityError for duplicate canonical_mo_observation_id")
    except sqlite3.IntegrityError as exc:
        print(f"uq_consolidation_canonical_mo correctly rejected duplicate: {exc}")

    # Global membership uniqueness: same (profile,site,observation) cannot
    # join a second consolidation.
    try:
        with db.transaction() as tx:
            tx.execute(
                "INSERT INTO sync_consolidations(profile_id,canonical_mo_observation_id,"
                "canonical_inat_observation_id,state,created_at,updated_at) "
                "VALUES (?,555,556,'draft',?,?)",
                (profile_id, now, now),
            )
            other_consolidation_id = tx.execute(
                "SELECT consolidation_id FROM sync_consolidations WHERE canonical_mo_observation_id=555"
            ).fetchone()["consolidation_id"]
            tx.execute(
                "INSERT INTO sync_consolidation_members(consolidation_id,profile_id,site,observation_id,"
                "role,remote_uuid,reviewed_record_fingerprint,local_state,created_at,updated_at) "
                "VALUES (?,?,'mo',100,'donor','','fp','active',?,?)",
                (other_consolidation_id, profile_id, now, now),
            )
        raise AssertionError("expected IntegrityError for duplicate member observation")
    except sqlite3.IntegrityError as exc:
        print(f"sync_consolidation_members UNIQUE(profile,site,observation) correctly rejected duplicate: {exc}")

    ic2 = conn.execute("PRAGMA integrity_check").fetchall()
    assert [r[0] for r in ic2] == ["ok"], f"post-rollback integrity_check failed: {ic2}"
    print("post-rollback integrity_check: ok")
    _validate_populated_v8_snapshot_upgrade()
    _validate_populated_v12_upgrade()
    _validate_malformed_legacy_account_rejected()
    _validate_malformed_v14_baselines_rejected()
    print("ALL GATE 2B-M1 SCHEMA SMOKE CHECKS PASSED")


if __name__ == "__main__":
    main()
