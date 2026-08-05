"""Profile-scoped SQLite persistence for read-only remote reconciliation."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from PySide6.QtCore import QStandardPaths

from .types import (
    AuthoritativeLinkRow, CoordinateActionOption, CoordinateComparisonPreview,
    ConsolidationEvidenceEdge, ConsolidationPreview,
    InventoryObservation, ITSActionOption, ITSActionType,
    ITSComparisonPreview, LinkRepairOption, LinkRepairPreview, ObservationPair,
    PhotoActionOption, PhotoComparisonPreview, ReconciliationPlan,
    ReconciliationProfile, RemoteSite,
)
from .consolidation_graph import (
    canonical_strong_anchor_signatures,
    validate_consolidation_graph,
)
from .consolidation_identity import (
    canonical_identity_fingerprint,
    canonical_stable_identity_fingerprint,
    parse_legacy_account_identity,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 18
REPAIRABLE_LINK_ISSUE_TYPES = frozenset({"one_way_link", "malformed_link"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReconciliationDB:
    """A separate database whose connections are confined to calling threads."""

    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
            path = root / "reconciliation.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._migration_lock = threading.Lock()
        self.migrate()

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "connection", None)
        if conn is None:
            conn = self._new_connection()
            self._local.connection = conn
        return conn

    def close_thread_connection(self) -> None:
        conn = getattr(self._local, "connection", None)
        if conn is not None:
            conn.close()
            del self._local.connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    def migrate(self) -> None:
        with self._migration_lock:
            conn = self.connection()
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"Reconciliation database version {version} is newer than supported")
            if version == 0:
                with self.transaction() as tx:
                    _migration_v1(tx)
                    tx.execute("PRAGMA user_version=1")
                version = 1
            if version == 1:
                with self.transaction() as tx:
                    _migration_v2(tx)
                    tx.execute("PRAGMA user_version=2")
                version = 2
            if version == 2:
                with self.transaction() as tx:
                    _migration_v3(tx)
                    tx.execute("PRAGMA user_version=3")
                version = 3
            if version == 3:
                with self.transaction() as tx:
                    _migration_v4(tx)
                    tx.execute("PRAGMA user_version=4")
                version = 4
            if version == 4:
                with self.transaction() as tx:
                    _migration_v5(tx)
                    tx.execute("PRAGMA user_version=5")
                version = 5
            if version == 5:
                with self.transaction() as tx:
                    _migration_v6(tx)
                    tx.execute("PRAGMA user_version=6")
                version = 6
            if version == 6:
                with self.transaction() as tx:
                    _migration_v7(tx)
                    tx.execute("PRAGMA user_version=7")
                version = 7
            if version == 7:
                with self.transaction() as tx:
                    _migration_v8(tx)
                    tx.execute("PRAGMA user_version=8")
                version = 8
            if version == 8:
                with self.transaction() as tx:
                    _migration_v9(tx)
                    tx.execute("PRAGMA user_version=9")
                version = 9
            if version == 9:
                with self.transaction() as tx:
                    _migration_v10(tx)
                    tx.execute("PRAGMA user_version=10")
                version = 10
            if version == 10:
                with self.transaction() as tx:
                    _migration_v11(tx)
                    tx.execute("PRAGMA user_version=11")
                version = 11
            if version == 11:
                with self.transaction() as tx:
                    _migration_v12(tx)
                    tx.execute("PRAGMA user_version=12")
                version = 12
            if version == 12:
                with self.transaction() as tx:
                    _migration_v13(tx)
                    tx.execute("PRAGMA user_version=13")
                version = 13
            if version == 13:
                with self.transaction() as tx:
                    _migration_v14(tx)
                    tx.execute("PRAGMA user_version=14")
                version = 14
            if version == 14:
                with self.transaction() as tx:
                    _migration_v15(tx)
                    tx.execute("PRAGMA user_version=15")
                version = 15
            if version == 15:
                with self.transaction() as tx:
                    _migration_v16(tx)
                    tx.execute("PRAGMA user_version=16")
                version = 16
            if version == 16:
                with self.transaction() as tx:
                    _migration_v17(tx)
                    tx.execute("PRAGMA user_version=17")
                version = 17
            if version == 17:
                with self.transaction() as tx:
                    _migration_v18(tx)
                    tx.execute("PRAGMA user_version=18")
                version = 18
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"Incomplete reconciliation database migration: {version}")

    # Profiles ---------------------------------------------------------

    def profiles(self) -> list[ReconciliationProfile]:
        rows = self.connection().execute(
            "SELECT * FROM sync_profiles ORDER BY last_used_at DESC, profile_id DESC"
        ).fetchall()
        return [ReconciliationProfile(**dict(row)) for row in rows]

    def save_profile(
        self, inat_user_id: int, inat_login: str, mo_user_id: int, mo_login: str
    ) -> ReconciliationProfile:
        now = _utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT profile_id FROM sync_profiles WHERE inat_user_id=? AND mo_user_id=?",
                (int(inat_user_id), int(mo_user_id)),
            ).fetchone()
            if row:
                profile_id = int(row[0])
                conn.execute(
                    "UPDATE sync_profiles SET inat_login=?, mo_login=?, last_used_at=? WHERE profile_id=?",
                    (inat_login.strip(), mo_login.strip(), now, profile_id),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO sync_profiles(inat_user_id,inat_login,mo_user_id,mo_login,created_at,last_used_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (int(inat_user_id), inat_login.strip(), int(mo_user_id), mo_login.strip(), now, now),
                )
                profile_id = int(cursor.lastrowid)
        return self.profile(profile_id)

    def profile(self, profile_id: int) -> ReconciliationProfile:
        row = self.connection().execute(
            "SELECT * FROM sync_profiles WHERE profile_id=?", (int(profile_id),)
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown reconciliation profile {profile_id}")
        return ReconciliationProfile(**dict(row))

    def touch_profile(self, profile_id: int) -> None:
        self.connection().execute(
            "UPDATE sync_profiles SET last_used_at=? WHERE profile_id=?", (_utc_now(), int(profile_id))
        )

    # Bindings / cursors / runs ---------------------------------------

    def save_field_binding(
        self, profile_id: int, purpose: str, field_id: int, name: str,
        datatype: str, verification_state: str, *, is_override: bool = False,
    ) -> None:
        self.connection().execute(
            "INSERT INTO sync_profile_field_bindings(profile_id,purpose,field_id,exact_name,datatype,"
            "verification_state,is_override,verified_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(profile_id,purpose) DO UPDATE SET field_id=excluded.field_id,"
            "exact_name=excluded.exact_name,datatype=excluded.datatype,"
            "verification_state=excluded.verification_state,is_override=excluded.is_override,"
            "verified_at=excluded.verified_at",
            (profile_id, purpose, field_id, name, datatype, verification_state, int(is_override), _utc_now()),
        )

    def field_binding(self, profile_id: int, purpose: str) -> Optional[sqlite3.Row]:
        return self.connection().execute(
            "SELECT * FROM sync_profile_field_bindings WHERE profile_id=? AND purpose=?",
            (profile_id, purpose),
        ).fetchone()

    def mark_field_binding_invalid(self, profile_id: int, purpose: str) -> None:
        self.connection().execute(
            "UPDATE sync_profile_field_bindings SET verification_state='invalid',verified_at=? "
            "WHERE profile_id=? AND purpose=?",
            (_utc_now(), profile_id, purpose),
        )

    def start_run(self, profile_id: int, mode: str, scan_started_at: str) -> int:
        cur = self.connection().execute(
            "INSERT INTO sync_runs(profile_id,mode,outcome,scan_started_at,started_at) VALUES(?,?,?,?,?)",
            (profile_id, mode, "running", scan_started_at, _utc_now()),
        )
        return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, outcome: str, *, error: str = "", capabilities: str = "",
    ) -> None:
        self.connection().execute(
            "UPDATE sync_runs SET outcome=?,finished_at=?,error_summary=?,capabilities=? WHERE run_id=?",
            (outcome, _utc_now(), error[:1000], capabilities[:2000], run_id),
        )

    def latest_run(self, profile_id: int) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT run_id,mode,outcome,started_at,finished_at,error_summary "
            "FROM sync_runs WHERE profile_id=? ORDER BY run_id DESC LIMIT 1",
            (profile_id,),
        ).fetchone()
        return dict(row) if row else None

    def latest_successful_run(self, profile_id: int) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT run_id,mode,outcome,started_at,finished_at,error_summary "
            "FROM sync_runs WHERE profile_id=? AND outcome='success' "
            "ORDER BY run_id DESC LIMIT 1",
            (profile_id,),
        ).fetchone()
        return dict(row) if row else None

    def cursor(self, profile_id: int, stream: str) -> str:
        row = self.connection().execute(
            "SELECT successful_scan_started_at FROM sync_cursors WHERE profile_id=? AND stream=?",
            (profile_id, stream),
        ).fetchone()
        return str(row[0]) if row else ""

    def advance_cursor(self, profile_id: int, stream: str, scan_started_at: str) -> None:
        self.connection().execute(
            "INSERT INTO sync_cursors(profile_id,stream,successful_scan_started_at,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(profile_id,stream) DO UPDATE SET successful_scan_started_at=excluded.successful_scan_started_at,"
            "updated_at=excluded.updated_at",
            (profile_id, stream, scan_started_at, _utc_now()),
        )

    def complete_successful_scan(
        self, profile_id: int, run_id: int, scan_started_at: str,
        streams: Iterable[str], capabilities: str,
    ) -> None:
        """Atomically advance every successful cursor and close the run."""
        now = _utc_now()
        with self.transaction() as conn:
            for stream in streams:
                conn.execute(
                    "INSERT INTO sync_cursors(profile_id,stream,successful_scan_started_at,updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(profile_id,stream) DO UPDATE SET "
                    "successful_scan_started_at=excluded.successful_scan_started_at,updated_at=excluded.updated_at",
                    (profile_id, stream, scan_started_at, now),
                )
            conn.execute(
                "UPDATE sync_runs SET outcome='success',finished_at=?,capabilities=?,error_summary='' "
                "WHERE profile_id=? AND run_id=?",
                (now, capabilities[:2000], profile_id, run_id),
            )
            conn.execute(
                "UPDATE sync_profiles SET last_used_at=? WHERE profile_id=?",
                (now, profile_id),
            )

    # Inventory --------------------------------------------------------

    def upsert_records(self, profile_id: int, observations: Iterable[InventoryObservation]) -> None:
        with self.transaction() as conn:
            self._upsert_records_tx(conn, profile_id, observations)

    def _upsert_records_tx(
        self, conn: sqlite3.Connection, profile_id: int,
        observations: Iterable[InventoryObservation],
    ) -> None:
        now = _utc_now()
        for item in observations:
            site = item.key.site.value
            observation_id = item.key.observation_id
            existing = conn.execute(
                "SELECT content_fingerprint FROM sync_records WHERE profile_id=? AND site=? "
                "AND remote_observation_id=?",
                (profile_id, site, observation_id),
            ).fetchone()
            changed_at = now if not existing or existing[0] != item.content_fingerprint else None
            conn.execute(
                "INSERT INTO sync_records(profile_id,site,remote_observation_id,account_id,owner_id,owner_login,"
                "observed_on,taxon_id,taxon_name,taxon_rank,public_locality,fungi_status,remote_updated_at,"
                "content_fingerprint,is_deleted,scope_state,unpaired_state,last_seen_at,link_malformed,changed_at,"
                "availability_state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(profile_id,site,remote_observation_id) DO UPDATE SET "
                "account_id=excluded.account_id,owner_id=excluded.owner_id,owner_login=excluded.owner_login,"
                "observed_on=excluded.observed_on,taxon_id=excluded.taxon_id,taxon_name=excluded.taxon_name,"
                "taxon_rank=excluded.taxon_rank,public_locality=excluded.public_locality,"
                "fungi_status=excluded.fungi_status,remote_updated_at=excluded.remote_updated_at,"
                "content_fingerprint=excluded.content_fingerprint,is_deleted=excluded.is_deleted,"
                "scope_state=excluded.scope_state,last_seen_at=excluded.last_seen_at,"
                "availability_state=excluded.availability_state,"
                "link_malformed=excluded.link_malformed,"
                "changed_at=COALESCE(excluded.changed_at,sync_records.changed_at)",
                (
                    profile_id, site, observation_id, item.account_id, item.owner_id,
                    item.owner_login, item.observed_on.isoformat() if item.observed_on else None,
                    item.taxon_id, item.taxon_name, item.taxon_rank, item.public_locality,
                    item.fungi_status, item.updated_at.isoformat() if item.updated_at else None,
                    item.content_fingerprint, int(item.deleted), item.scope_state,
                    "unpaired_no_candidate", now, int(item.link_malformed), changed_at,
                    item.availability_state,
                ),
            )
            conn.execute(
                "DELETE FROM sync_links WHERE profile_id=? AND source_site=? AND source_observation_id=?",
                (profile_id, site, observation_id),
            )
            links = item.authoritative_links or tuple(
                AuthoritativeLinkRow(
                    f"{site}:{observation_id}:{index}", None,
                    RemoteSite.MO if site == "inat" else RemoteSite.INAT,
                    target_id, "valid", item.content_fingerprint,
                )
                for index, target_id in enumerate(item.authoritative_targets)
            )
            for link in links:
                conn.execute(
                    "INSERT INTO sync_links(profile_id,link_row_id,source_site,source_observation_id,"
                    "target_site,target_observation_id,direction,link_state,external_site_id,parse_state,fingerprint) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (profile_id, link.row_id, site, observation_id, link.target_site.value,
                     link.target_observation_id, f"{site}_to_{link.target_site.value}",
                     link.parse_state, link.external_site_id, link.parse_state, link.fingerprint),
                )
            if item.inventory_identifiers is not None:
                conn.execute(
                    "DELETE FROM sync_identifiers WHERE profile_id=? AND site=? AND observation_id=? "
                    "AND evidence_tier=1",
                    (profile_id, site, observation_id),
                )
                for kind, value in set(item.inventory_identifiers):
                    if value:
                        conn.execute(
                            "INSERT INTO sync_identifiers(profile_id,site,observation_id,identifier_type,"
                            "normalized_value,evidence_tier) VALUES(?,?,?,?,?,1)",
                            (profile_id, site, observation_id, kind, value),
                        )
            if item.inventory_sequence_hashes is not None:
                conn.execute(
                    "DELETE FROM sync_sequence_hashes WHERE profile_id=? AND site=? AND observation_id=? "
                    "AND evidence_tier=2",
                    (profile_id, site, observation_id),
                )
                for digest in set(item.inventory_sequence_hashes):
                    if digest:
                        conn.execute(
                            "INSERT INTO sync_sequence_hashes(profile_id,site,observation_id,sequence_hash,evidence_tier) "
                            "VALUES(?,?,?,?,2)",
                            (profile_id, site, observation_id, digest),
                        )
            incoming_media = {(media.photo_id, media.rendition) for media in item.media}
            existing_media = conn.execute(
                "SELECT photo_id,rendition FROM sync_media_hashes WHERE profile_id=? AND site=? AND observation_id=?",
                (profile_id, site, observation_id),
            ).fetchall()
            for row in existing_media:
                if (str(row[0]), str(row[1])) not in incoming_media:
                    conn.execute(
                        "DELETE FROM sync_media_hashes WHERE profile_id=? AND site=? AND observation_id=? "
                        "AND photo_id=? AND rendition=?",
                        (profile_id, site, observation_id, row[0], row[1]),
                    )
            for media in item.media:
                conn.execute(
                    "INSERT OR IGNORE INTO sync_media_hashes(profile_id,site,observation_id,photo_id,rendition,"
                    "source_fingerprint,exact_pixel_hash,perceptual_hash,source_site,source_photo_id) "
                    "VALUES(?,?,?,?,?,'','','',?,?)",
                    (profile_id, site, observation_id, media.photo_id, media.rendition,
                     media.source_site.value if media.source_site else None, media.source_photo_id),
                )
                conn.execute(
                    "UPDATE sync_media_hashes SET source_site=?,source_photo_id=? WHERE profile_id=? AND site=? "
                    "AND observation_id=? AND photo_id=? AND rendition=?",
                    (media.source_site.value if media.source_site else None, media.source_photo_id,
                     profile_id, site, observation_id, media.photo_id, media.rendition),
                )

    def mark_deleted(self, profile_id: int, site: str, observation_ids: Iterable[int]) -> None:
        ids = [int(value) for value in observation_ids]
        with self.transaction() as conn:
            for observation_id in ids:
                conn.execute(
                    "UPDATE sync_records SET is_deleted=1,scope_state='out_of_scope',availability_state='deleted' "
                    "WHERE profile_id=? AND site=? AND remote_observation_id=?",
                    (profile_id, site, observation_id),
                )
            self._invalidate_pairs_tx(conn, profile_id)

    def invalidate_affected_pairs(self, profile_id: int) -> None:
        """Reopen confirmations whose ownership, scope, or reciprocal links changed."""
        with self.transaction() as conn:
            self._invalidate_confirmations_tx(conn, profile_id)

    def _invalidate_confirmations_tx(self, conn: sqlite3.Connection, profile_id: int) -> None:
        self._invalidate_pairs_tx(conn, profile_id)
        conn.execute(
            "UPDATE sync_pairs SET review_state='candidate',confirmed_by='',updated_at=? "
            "WHERE profile_id=? AND review_state='confirmed' AND ("
                "EXISTS (SELECT 1 FROM sync_records r WHERE r.profile_id=sync_pairs.profile_id AND r.site='mo' "
                "AND r.remote_observation_id=sync_pairs.mo_observation_id AND r.owner_id IS NOT r.account_id) OR "
                "EXISTS (SELECT 1 FROM sync_records r WHERE r.profile_id=sync_pairs.profile_id AND r.site='inat' "
                "AND r.remote_observation_id=sync_pairs.inat_observation_id AND r.owner_id IS NOT r.account_id) OR "
                "EXISTS (SELECT 1 FROM sync_links l WHERE l.profile_id=sync_pairs.profile_id AND l.source_site='mo' "
                "AND l.source_observation_id=sync_pairs.mo_observation_id AND (l.target_site!='inat' "
                "OR l.target_observation_id!=sync_pairs.inat_observation_id)) OR "
                "EXISTS (SELECT 1 FROM sync_links l WHERE l.profile_id=sync_pairs.profile_id AND l.source_site='inat' "
                "AND l.source_observation_id=sync_pairs.inat_observation_id AND (l.target_site!='mo' "
                "OR l.target_observation_id!=sync_pairs.mo_observation_id)) OR "
                "(confirmed_by='reciprocal_link' AND NOT ("
                "EXISTS (SELECT 1 FROM sync_links l WHERE l.profile_id=sync_pairs.profile_id AND l.source_site='mo' "
                "AND l.source_observation_id=sync_pairs.mo_observation_id AND l.target_site='inat' "
                "AND l.target_observation_id=sync_pairs.inat_observation_id) AND "
                "EXISTS (SELECT 1 FROM sync_links l WHERE l.profile_id=sync_pairs.profile_id AND l.source_site='inat' "
                "AND l.source_observation_id=sync_pairs.inat_observation_id AND l.target_site='mo' "
                "AND l.target_observation_id=sync_pairs.mo_observation_id))))",
            (_utc_now(), profile_id),
        )

    def store_sequence_hashes(
        self, profile_id: int, site: str, observation_id: int, hashes: Iterable[str],
        *, evidence_tier: int = 2,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM sync_sequence_hashes WHERE profile_id=? AND site=? AND observation_id=? "
                "AND evidence_tier=?",
                (profile_id, site, observation_id, evidence_tier),
            )
            for digest in set(hashes):
                if digest:
                    conn.execute(
                        "INSERT INTO sync_sequence_hashes(profile_id,site,observation_id,sequence_hash,evidence_tier) "
                        "VALUES(?,?,?,?,?)",
                        (profile_id, site, observation_id, digest, evidence_tier),
                    )

    def store_identifiers(
        self, profile_id: int, site: str, observation_id: int,
        identifiers: Iterable[tuple[str, str]], *, evidence_tier: int = 2,
    ) -> None:
        values = {(str(kind), str(value)) for kind, value in identifiers if value}
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM sync_identifiers WHERE profile_id=? AND site=? AND observation_id=? "
                "AND evidence_tier=?",
                (profile_id, site, observation_id, evidence_tier),
            )
            for kind, value in values:
                conn.execute(
                    "INSERT INTO sync_identifiers(profile_id,site,observation_id,identifier_type,normalized_value,"
                    "evidence_tier) VALUES(?,?,?,?,?,?)",
                    (profile_id, site, observation_id, kind, value, evidence_tier),
                )

    def refresh_its_evidence(
        self, profile_id: int, pair_id: int, *, mo_observation_id: int,
        inat_observation_id: int, mo_hashes: Iterable[str], inat_hashes: Iterable[str],
        mo_accessions: Iterable[tuple[str, str]], inat_accessions: Iterable[tuple[str, str]],
    ) -> None:
        """Persist only safe ITS derivatives and recalculate barcode support.

        Accessions are archive-qualified ``(archive, accession)`` pairs. Exact
        accession evidence requires both the archive namespace and the accession
        to match, so a GenBank value never matches a same-string ENA/UNITE value.
        """
        from .matching import score_evidence
        from .types import EvidenceFamily, EvidenceTier, MatchEvidence

        hashes = {
            "mo": {value for value in mo_hashes if value},
            "inat": {value for value in inat_hashes if value},
        }
        accessions = {
            "mo": {(a, v) for a, v in mo_accessions if a and v},
            "inat": {(a, v) for a, v in inat_accessions if a and v},
        }
        ids = {"mo": int(mo_observation_id), "inat": int(inat_observation_id)}
        with self.transaction() as conn:
            for site in ("mo", "inat"):
                conn.execute(
                    "DELETE FROM sync_sequence_hashes WHERE profile_id=? AND site=? AND observation_id=? "
                    "AND evidence_tier=3", (profile_id, site, ids[site]),
                )
                for digest in hashes[site]:
                    conn.execute(
                        "INSERT INTO sync_sequence_hashes(profile_id,site,observation_id,sequence_hash,evidence_tier) "
                        "VALUES(?,?,?,?,3)", (profile_id, site, ids[site], digest),
                    )
                conn.execute(
                    "DELETE FROM sync_identifiers WHERE profile_id=? AND site=? AND observation_id=? "
                    "AND identifier_type='accession' AND evidence_tier=3",
                    (profile_id, site, ids[site]),
                )
                for archive, accession in accessions[site]:
                    # Persist the archive-qualified identity so exact-accession
                    # evidence cannot cross namespaces.
                    conn.execute(
                        "INSERT INTO sync_identifiers(profile_id,site,observation_id,identifier_type,"
                        "normalized_value,evidence_tier) VALUES(?,?,?,'accession',?,3)",
                        (profile_id, site, ids[site], f"{archive}\x1f{accession}"),
                    )
            conn.execute(
                "DELETE FROM sync_evidence WHERE profile_id=? AND pair_id=? "
                "AND evidence_type IN ('exact_accession','sequence_equivalence')",
                (profile_id, pair_id),
            )
            additions: list[MatchEvidence] = []
            if hashes["mo"].intersection(hashes["inat"]):
                additions.append(MatchEvidence(
                    "sequence_equivalence", EvidenceFamily.BARCODE, 35,
                    "Normalized sequences are equal, including reverse-complement equivalence.",
                    EvidenceTier.DEEP,
                ))
            if accessions["mo"].intersection(accessions["inat"]):
                additions.append(MatchEvidence(
                    "exact_accession", EvidenceFamily.BARCODE, 35,
                    "Exact normalized accession matches.", EvidenceTier.DEEP,
                ))
            for item in additions:
                conn.execute(
                    "INSERT INTO sync_evidence(profile_id,pair_id,evidence_type,family,score,tier,explanation) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (profile_id, pair_id, item.evidence_type, item.family.value,
                     item.score, int(item.tier), item.explanation),
                )
            current = [MatchEvidence(
                str(row["evidence_type"]), EvidenceFamily(str(row["family"])),
                int(row["score"]), str(row["explanation"]), EvidenceTier(int(row["tier"])),
            ) for row in conn.execute(
                "SELECT evidence_type,family,score,tier,explanation FROM sync_evidence "
                "WHERE profile_id=? AND pair_id=?", (profile_id, pair_id),
            ).fetchall()]
            score = score_evidence(current)
            conn.execute(
                "UPDATE sync_pairs SET score=?,classification=?,updated_at=? "
                "WHERE profile_id=? AND pair_id=?",
                (score.total, score.classification, _utc_now(), profile_id, pair_id),
            )

    def store_media_hash(
        self, profile_id: int, site: str, photo_id: str, rendition: str,
        source_fingerprint: str, exact_pixel_hash: str, perceptual_hash: str,
    ) -> None:
        self.connection().execute(
            "UPDATE sync_media_hashes SET source_fingerprint=?,exact_pixel_hash=?,perceptual_hash=? "
            "WHERE profile_id=? AND site=? AND photo_id=? AND rendition=?",
            (source_fingerprint, exact_pixel_hash, perceptual_hash,
             profile_id, site, photo_id, rendition),
        )

    def media_hash_pairs(
        self, profile_id: int, site: str, photo_id: str, rendition: str,
    ) -> list[tuple[int, int, bool]]:
        source_rows = self.connection().execute(
            "SELECT observation_id,exact_pixel_hash,perceptual_hash FROM sync_media_hashes "
            "WHERE profile_id=? AND site=? AND photo_id=? AND rendition=?",
            (profile_id, site, photo_id, rendition),
        ).fetchall()
        opposite = "mo" if site == "inat" else "inat"
        pairs: set[tuple[int, int, bool]] = set()
        for source in source_rows:
            if not source["exact_pixel_hash"] and not source["perceptual_hash"]:
                continue
            matches = self.connection().execute(
                "SELECT observation_id,exact_pixel_hash,perceptual_hash FROM sync_media_hashes "
                "WHERE profile_id=? AND site=? AND ((exact_pixel_hash!='' AND exact_pixel_hash=?) OR "
                "(perceptual_hash!='' AND perceptual_hash=?))",
                (profile_id, opposite, source["exact_pixel_hash"], source["perceptual_hash"]),
            ).fetchall()
            for match in matches:
                exact = bool(source["exact_pixel_hash"] and source["exact_pixel_hash"] == match["exact_pixel_hash"])
                mo_id = int(source["observation_id"] if site == "mo" else match["observation_id"])
                inat_id = int(source["observation_id"] if site == "inat" else match["observation_id"])
                pairs.add((mo_id, inat_id, exact))
        return sorted(pairs)

    # Pairs, evidence and review --------------------------------------

    def replace_candidate(self, profile_id: int, pair: ObservationPair) -> int:
        with self.transaction() as conn:
            return self._replace_candidate_tx(conn, profile_id, pair)

    def _replace_candidate_tx(
        self, conn: sqlite3.Connection, profile_id: int, pair: ObservationPair,
    ) -> int:
        existing = conn.execute(
            "SELECT pair_id,review_state,confirmed_by FROM sync_pairs WHERE profile_id=? AND mo_observation_id=? "
            "AND inat_observation_id=?",
            (profile_id, pair.mo_observation_id, pair.inat_observation_id),
        ).fetchone()
        if existing:
            pair_id = int(existing[0])
            review_state = str(existing[1])
            confirmed_by = str(existing[2])
            if (review_state == "confirmed" and confirmed_by == "reciprocal_link"
                    and pair.state != "link_confirmed"):
                review_state = "candidate"
                confirmed_by = ""
            conn.execute(
                "UPDATE sync_pairs SET link_state=?,score=?,classification=?,review_state=?,"
                "confirmed_by=?,updated_at=? WHERE pair_id=?",
                (pair.state, pair.score, _classification(pair.score), review_state,
                 confirmed_by, _utc_now(), pair_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO sync_pairs(profile_id,mo_observation_id,inat_observation_id,link_state,score,"
                "classification,review_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (profile_id, pair.mo_observation_id, pair.inat_observation_id, pair.state,
                 pair.score, _classification(pair.score), "candidate", _utc_now(), _utc_now()),
            )
            pair_id = int(cur.lastrowid)
        conn.execute("DELETE FROM sync_evidence WHERE profile_id=? AND pair_id=?", (profile_id, pair_id))
        for evidence in pair.evidence:
            conn.execute(
                "INSERT INTO sync_evidence(profile_id,pair_id,evidence_type,family,score,tier,explanation) "
                "VALUES(?,?,?,?,?,?,?)",
                (profile_id, pair_id, evidence.evidence_type, evidence.family.value, evidence.score,
                 int(evidence.tier), evidence.explanation),
            )
        return pair_id

    def prune_candidates(
        self, profile_id: int, active_pairs: Iterable[tuple[int, int]],
    ) -> None:
        active = set(active_pairs)
        rows = self.connection().execute(
            "SELECT pair_id,mo_observation_id,inat_observation_id FROM sync_pairs "
            "WHERE profile_id=? AND review_state='candidate' AND ever_reviewed=0 AND ever_confirmed=0",
            (profile_id,),
        ).fetchall()
        stale = [int(row["pair_id"]) for row in rows
                 if (int(row["mo_observation_id"]), int(row["inat_observation_id"])) not in active]
        if not stale:
            return
        with self.transaction() as conn:
            for pair_id in stale:
                conn.execute(
                    "DELETE FROM sync_pairs WHERE profile_id=? AND pair_id=?",
                    (profile_id, pair_id),
                )

    def set_pair_review(self, profile_id: int, pair_id: int, state: str) -> None:
        if state not in {"candidate", "confirmed", "rejected"}:
            raise ValueError("Invalid pair review state")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT mo_observation_id,inat_observation_id FROM sync_pairs WHERE profile_id=? AND pair_id=?",
                (profile_id, pair_id),
            ).fetchone()
            if not row:
                return
            if state in {"confirmed", "candidate"}:
                conn.execute(
                    "DELETE FROM sync_pair_exclusions WHERE profile_id=? AND mo_observation_id=? "
                    "AND inat_observation_id=?",
                    (profile_id, row[0], row[1]),
                )
                self._clear_rejected_unpaired_state_tx(conn, profile_id, row[0], row[1])
            conn.execute(
                "UPDATE sync_pairs SET review_state=?,confirmed_by=?,ever_reviewed=1,"
                "ever_confirmed=CASE WHEN ?='confirmed' THEN 1 ELSE ever_confirmed END,"
                "historical_confirmed_by=CASE WHEN ?='confirmed' THEN 'user' ELSE historical_confirmed_by END,"
                "updated_at=? WHERE profile_id=? AND pair_id=?",
                (state, "user" if state == "confirmed" else "", state, state,
                 _utc_now(), profile_id, pair_id),
            )
            if state == "rejected":
                fingerprint = self._pair_source_fingerprint_tx(conn, profile_id, row[0], row[1])
                conn.execute(
                    "INSERT OR REPLACE INTO sync_pair_exclusions(profile_id,mo_observation_id,"
                    "inat_observation_id,reason,created_at,source_fingerprint) VALUES(?,?,?,?,?,?)",
                    (profile_id, row[0], row[1], "user_rejected", _utc_now(), fingerprint),
                )
                conn.execute(
                    "UPDATE sync_records SET unpaired_state='unpaired_with_rejected_candidates' "
                    "WHERE profile_id=? AND ((site='mo' AND remote_observation_id=?) OR "
                    "(site='inat' AND remote_observation_id=?))",
                    (profile_id, row[0], row[1]),
                )

    def _clear_rejected_unpaired_state_tx(
        self, conn: sqlite3.Connection, profile_id: int, mo_id: int, inat_id: int,
    ) -> None:
        """Undo the record-level half of a rejection once its exclusion is gone.

        Rejecting a candidate writes TWO things: the exclusion row, and
        ``unpaired_state='unpaired_with_rejected_candidates'`` on both records.
        Deleting only the exclusion leaves the Unpaired list reporting a
        rejection that no longer exists, so every caller that removes an
        exclusion must call this.

        A record can be one side of several candidate pairs, so the state is
        only wound back when NO rejection remains against it. ``user_excluded``
        exclusions are deliberately not counted: they are a different decision
        and never set this state. The ``unpaired_state=`` guard keeps
        ``confirmed_missing_on_*`` — set by a separate operator decision —
        from being overwritten.
        """
        conn.execute(
            "UPDATE sync_records SET unpaired_state='unpaired_no_candidate' "
            "WHERE profile_id=? AND unpaired_state='unpaired_with_rejected_candidates' AND ("
            "  (site='mo' AND remote_observation_id=? AND NOT EXISTS ("
            "     SELECT 1 FROM sync_pair_exclusions x WHERE x.profile_id=? "
            "     AND x.mo_observation_id=? AND x.reason='user_rejected'))"
            "  OR (site='inat' AND remote_observation_id=? AND NOT EXISTS ("
            "     SELECT 1 FROM sync_pair_exclusions x WHERE x.profile_id=? "
            "     AND x.inat_observation_id=? AND x.reason='user_rejected')))",
            (profile_id, mo_id, profile_id, mo_id, inat_id, profile_id, inat_id),
        )

    def auto_confirm_pair(self, profile_id: int, pair_id: int) -> bool:
        """Confirm only a fully validated reciprocal pair without overriding review."""
        with self.transaction() as conn:
            return self._auto_confirm_pair_tx(conn, profile_id, pair_id)

    def _auto_confirm_pair_tx(
        self, conn: sqlite3.Connection, profile_id: int, pair_id: int,
    ) -> bool:
        row = conn.execute(
            "SELECT mo_observation_id,inat_observation_id,link_state,review_state,confirmed_by FROM sync_pairs "
            "WHERE profile_id=? AND pair_id=?", (profile_id, pair_id),
        ).fetchone()
        if not row or row["link_state"] != "link_confirmed" or row["review_state"] == "rejected":
            return False
        if row["review_state"] == "confirmed":
            return True
        excluded = conn.execute(
            "SELECT 1 FROM sync_pair_exclusions WHERE profile_id=? AND mo_observation_id=? AND inat_observation_id=?",
            (profile_id, row["mo_observation_id"], row["inat_observation_id"]),
        ).fetchone()
        if excluded:
            return False
        try:
            conn.execute(
                "UPDATE sync_pairs SET review_state='confirmed',confirmed_by='reciprocal_link',"
                "ever_confirmed=1,historical_confirmed_by='reciprocal_link',updated_at=? "
                "WHERE profile_id=? AND pair_id=?",
                (_utc_now(), profile_id, pair_id),
            )
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE sync_pairs SET link_state='ambiguous_link',review_state='candidate',confirmed_by='',updated_at=? "
                "WHERE profile_id=? AND pair_id=?",
                (_utc_now(), profile_id, pair_id),
            )
            return False
        return True

    def apply_reconciliation_scan(
        self,
        profile_id: int,
        run_id: int,
        scan_started_at: str,
        records: Iterable[InventoryObservation],
        deleted: dict[str, Iterable[int]],
        plan: ReconciliationPlan,
        streams: Iterable[str],
        capabilities: str,
        *,
        resolved_issue_types: Iterable[str] = (),
        field_bindings: Sequence[tuple[str, int, str, str, bool]] = (),
        invalid_bindings: Iterable[str] = (),
        metadata_identifiers: Sequence[tuple[str, int, Sequence[tuple[str, str]]]] = (),
        metadata_sequences: Sequence[tuple[str, int, Sequence[str]]] = (),
        invalidate_deep_records: Sequence[tuple[str, int]] = (),
        disabled_link_sources: Sequence[str] = (),
    ) -> None:
        """Persist a worker-computed scan result and advance cursors atomically."""
        now = _utc_now()
        with self.transaction() as conn:
            issue_types = tuple(sorted(set(resolved_issue_types)))
            if issue_types:
                placeholders = ",".join("?" for _ in issue_types)
                conn.execute(
                    f"UPDATE sync_issues SET state='resolved',updated_at=? WHERE profile_id=? "
                    f"AND state='open' AND issue_type IN ({placeholders})",
                    (now, profile_id, *issue_types),
                )
            self._upsert_records_tx(conn, profile_id, records)
            for site, observation_id in invalidate_deep_records:
                conn.execute(
                    "DELETE FROM sync_identifiers WHERE profile_id=? AND site=? AND observation_id=? "
                    "AND evidence_tier=3",
                    (profile_id, site, observation_id),
                )
                conn.execute(
                    "DELETE FROM sync_sequence_hashes WHERE profile_id=? AND site=? AND observation_id=? "
                    "AND evidence_tier=3",
                    (profile_id, site, observation_id),
                )
                pair_column = "mo_observation_id" if site == "mo" else "inat_observation_id"
                conn.execute(
                    "DELETE FROM sync_evidence WHERE profile_id=? AND tier>=3 AND pair_id IN "
                    f"(SELECT pair_id FROM sync_pairs WHERE profile_id=? AND {pair_column}=?)",
                    (profile_id, profile_id, observation_id),
                )
            for site, observation_id, identifiers in metadata_identifiers:
                conn.execute(
                    "DELETE FROM sync_identifiers WHERE profile_id=? AND site=? AND observation_id=? "
                    "AND evidence_tier=2",
                    (profile_id, site, observation_id),
                )
                for kind, value in set(identifiers):
                    if value:
                        conn.execute(
                            "INSERT INTO sync_identifiers(profile_id,site,observation_id,identifier_type,"
                            "normalized_value,evidence_tier) VALUES(?,?,?,?,?,2)",
                            (profile_id, site, observation_id, kind, value),
                        )
            for site, observation_id, hashes in metadata_sequences:
                conn.execute(
                    "DELETE FROM sync_sequence_hashes WHERE profile_id=? AND site=? AND observation_id=? "
                    "AND evidence_tier=2",
                    (profile_id, site, observation_id),
                )
                for digest in set(hashes):
                    if digest:
                        conn.execute(
                            "INSERT INTO sync_sequence_hashes(profile_id,site,observation_id,sequence_hash,"
                            "evidence_tier) VALUES(?,?,?,?,2)",
                            (profile_id, site, observation_id, digest),
                        )
            for site, observation_ids in deleted.items():
                for observation_id in observation_ids:
                    conn.execute(
                        "UPDATE sync_records SET is_deleted=1,scope_state='out_of_scope',"
                        "availability_state='deleted',changed_at=? "
                        "WHERE profile_id=? AND site=? AND remote_observation_id=?",
                        (now, profile_id, site, int(observation_id)),
                    )
            self._invalidate_confirmations_tx(conn, profile_id)
            if disabled_link_sources:
                conn.execute(
                    "UPDATE sync_pairs SET review_state='candidate',confirmed_by='',updated_at=? "
                    "WHERE profile_id=? AND review_state='confirmed' AND confirmed_by='reciprocal_link'",
                    (now, profile_id),
                )
            for purpose, field_id, exact_name, datatype, override in field_bindings:
                conn.execute(
                    "INSERT INTO sync_profile_field_bindings(profile_id,purpose,field_id,exact_name,datatype,"
                    "verification_state,is_override,verified_at) VALUES(?,?,?,?,?,'verified',?,?) "
                    "ON CONFLICT(profile_id,purpose) DO UPDATE SET field_id=excluded.field_id,"
                    "exact_name=excluded.exact_name,datatype=excluded.datatype,verification_state='verified',"
                    "is_override=excluded.is_override,verified_at=excluded.verified_at",
                    (profile_id, purpose, field_id, exact_name, datatype, int(override), now),
                )
            for purpose in invalid_bindings:
                conn.execute(
                    "UPDATE sync_profile_field_bindings SET verification_state='invalid',verified_at=? "
                    "WHERE profile_id=? AND purpose=?",
                    (now, profile_id, purpose),
                )
            active = set(plan.active_pairs)
            stale = conn.execute(
                "SELECT pair_id,mo_observation_id,inat_observation_id FROM sync_pairs "
                "WHERE profile_id=? AND review_state='candidate' AND ever_reviewed=0 AND ever_confirmed=0",
                (profile_id,),
            ).fetchall()
            for row in stale:
                if (int(row["mo_observation_id"]), int(row["inat_observation_id"])) not in active:
                    conn.execute(
                        "DELETE FROM sync_pairs WHERE profile_id=? AND pair_id=?",
                        (profile_id, int(row["pair_id"])),
                    )
            pair_ids: dict[tuple[int, int], int] = {}
            for pair in plan.pairs:
                key = (pair.mo_observation_id, pair.inat_observation_id)
                pair_ids[key] = self._replace_candidate_tx(conn, profile_id, pair)
            auto_keys = set(plan.auto_confirm_pairs)
            for key, pair_id in pair_ids.items():
                if key in auto_keys and not self._auto_confirm_pair_tx(conn, profile_id, pair_id):
                    mo_id, inat_id = key
                    self._upsert_issue_tx(
                        conn, profile_id, "link_one_to_one_conflict", "warning",
                        f"One-to-one pair conflict: MO {mo_id} ↔ iNat {inat_id}",
                        "A local exclusion, rejection, or existing confirmed pair prevented automatic confirmation.",
                        _local_fingerprint(mo_id, inat_id, "link_confirmed"),
                        (("mo", mo_id), ("inat", inat_id)),
                    )
            for issue in plan.issues:
                self._upsert_issue_tx(
                    conn, profile_id, issue.issue_type, issue.severity, issue.title,
                    issue.detail, issue.fingerprint,
                    tuple((record.site.value, record.observation_id) for record in issue.records),
                )
            for stream in streams:
                conn.execute(
                    "INSERT INTO sync_cursors(profile_id,stream,successful_scan_started_at,updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(profile_id,stream) DO UPDATE SET "
                    "successful_scan_started_at=excluded.successful_scan_started_at,updated_at=excluded.updated_at",
                    (profile_id, stream, scan_started_at, now),
                )
            conn.execute(
                "UPDATE sync_runs SET outcome='success',finished_at=?,capabilities=?,error_summary='' "
                "WHERE profile_id=? AND run_id=?",
                (now, capabilities[:2000], profile_id, run_id),
            )
            conn.execute(
                "UPDATE sync_profiles SET last_used_at=? WHERE profile_id=?",
                (now, profile_id),
            )

    def upsert_issue(
        self, profile_id: int, issue_type: str, severity: str, title: str,
        detail: str, fingerprint: str, records: Sequence[tuple[str, int]] = (),
    ) -> int:
        with self.transaction() as conn:
            return self._upsert_issue_tx(
                conn, profile_id, issue_type, severity, title, detail, fingerprint, records
            )

    def set_pair_excluded(self, profile_id: int, pair_id: int, excluded: bool) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT mo_observation_id,inat_observation_id FROM sync_pairs WHERE profile_id=? AND pair_id=?",
                (profile_id, pair_id),
            ).fetchone()
            if not row:
                return
            if excluded:
                fingerprint = self._pair_source_fingerprint_tx(conn, profile_id, row[0], row[1])
                conn.execute(
                    "INSERT OR REPLACE INTO sync_pair_exclusions(profile_id,mo_observation_id,inat_observation_id,"
                    "reason,created_at,source_fingerprint) VALUES(?,?,?,?,?,?)",
                    (profile_id, row[0], row[1], "user_excluded", _utc_now(), fingerprint),
                )
                conn.execute(
                    "UPDATE sync_pairs SET review_state='candidate',confirmed_by='',ever_reviewed=1,updated_at=? "
                    "WHERE profile_id=? AND pair_id=?",
                    (_utc_now(), profile_id, pair_id),
                )
            else:
                conn.execute(
                    "DELETE FROM sync_pair_exclusions WHERE profile_id=? AND mo_observation_id=? "
                    "AND inat_observation_id=?",
                    (profile_id, row[0], row[1]),
                )
                conn.execute(
                    "UPDATE sync_pairs SET review_state='candidate',confirmed_by='',updated_at=? "
                    "WHERE profile_id=? AND pair_id=? AND review_state='rejected'",
                    (_utc_now(), profile_id, pair_id),
                )
                self._clear_rejected_unpaired_state_tx(conn, profile_id, row[0], row[1])

    def _pair_source_fingerprint_tx(
        self, conn: sqlite3.Connection, profile_id: int, mo_id: int, inat_id: int,
    ) -> str:
        rows = conn.execute(
            "SELECT site,content_fingerprint FROM sync_records WHERE profile_id=? AND "
            "((site='mo' AND remote_observation_id=?) OR (site='inat' AND remote_observation_id=?)) "
            "ORDER BY site",
            (profile_id, mo_id, inat_id),
        ).fetchall()
        import hashlib
        return hashlib.sha256(
            "\x1f".join(str(row["content_fingerprint"]) for row in rows).encode("utf-8")
        ).hexdigest()

    def pair_is_excluded(self, profile_id: int, pair_id: int) -> bool:
        row = self.connection().execute(
            "SELECT 1 FROM sync_pair_exclusions e JOIN sync_pairs p ON p.profile_id=e.profile_id "
            "AND p.mo_observation_id=e.mo_observation_id AND p.inat_observation_id=e.inat_observation_id "
            "WHERE p.profile_id=? AND p.pair_id=?",
            (profile_id, pair_id),
        ).fetchone()
        return row is not None

    def set_confirmed_missing(self, profile_id: int, site: str, observation_id: int, missing: bool) -> None:
        if site not in {"mo", "inat"}:
            raise ValueError("Invalid site")
        state = ("confirmed_missing_on_inat" if site == "mo" else "confirmed_missing_on_mo") if missing else "unpaired_no_candidate"
        self.connection().execute(
            "UPDATE sync_records SET unpaired_state=? WHERE profile_id=? AND site=? AND remote_observation_id=?",
            (state, profile_id, site, observation_id),
        )

    def set_issue_state(self, profile_id: int, issue_id: int, state: str) -> None:
        if state not in {"open", "ignored", "resolved"}:
            raise ValueError("Invalid issue state")
        self.connection().execute(
            "UPDATE sync_issues SET state=?,updated_at=? WHERE profile_id=? AND issue_id=?",
            (state, _utc_now(), profile_id, issue_id),
        )

    def resolve_open_issues(self, profile_id: int, issue_types: Iterable[str]) -> None:
        values = tuple(sorted(set(issue_types)))
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        self.connection().execute(
            f"UPDATE sync_issues SET state='resolved',updated_at=? WHERE profile_id=? "
            f"AND state='open' AND issue_type IN ({placeholders})",
            (_utc_now(), profile_id, *values),
        )

    # Gate 1B link-repair review and durable action journal ---------------

    def review_link_issue(
        self, profile_id: int, issue_id: int, intent: str,
        mo_observation_id: Optional[int], inat_observation_id: Optional[int],
    ) -> None:
        if intent not in {"reciprocal", "remove_only"}:
            raise ValueError("Invalid link-repair review intent")
        row = self.connection().execute(
            "SELECT fingerprint,issue_type,state FROM sync_issues WHERE profile_id=? AND issue_id=?",
            (profile_id, issue_id),
        ).fetchone()
        if (
            not row or str(row["state"]) != "open"
            or str(row["issue_type"]) not in REPAIRABLE_LINK_ISSUE_TYPES
        ):
            raise ValueError("Only current link issues can be reviewed for repair")
        mo_id = int(mo_observation_id or 0)
        inat_id = int(inat_observation_id or 0)
        if mo_id <= 0 or inat_id <= 0:
            raise ValueError("A link-issue review requires both exact observation IDs")
        issue_records = self.connection().execute(
            "SELECT site,observation_id FROM sync_issue_records WHERE profile_id=? AND issue_id=?",
            (profile_id, issue_id),
        ).fetchall()
        if not issue_records:
            raise ValueError("The issue is not tied to a remote observation")
        by_site: dict[str, set[int]] = {}
        for record in issue_records:
            by_site.setdefault(str(record["site"]), set()).add(int(record["observation_id"]))
        if len(by_site.get("mo", set())) > 1 or len(by_site.get("inat", set())) > 1:
            raise ValueError("The issue does not identify a unique record on each represented site")
        if by_site.get("mo") and mo_id not in by_site["mo"]:
            raise ValueError("The reviewed MO observation does not match the issue record")
        if by_site.get("inat") and inat_id not in by_site["inat"]:
            raise ValueError("The reviewed iNaturalist observation does not match the issue record")
        reviewed_at = _utc_now()
        self.connection().execute(
            "INSERT INTO sync_link_issue_reviews(profile_id,issue_id,review_intent,"
            "mo_observation_id,inat_observation_id,issue_fingerprint,review_state,reviewed_at) "
            "VALUES(?,?,?,?,?,?,'approved',?) ON CONFLICT(profile_id,issue_id) DO UPDATE SET "
            "review_intent=excluded.review_intent,mo_observation_id=excluded.mo_observation_id,"
            "inat_observation_id=excluded.inat_observation_id,issue_fingerprint=excluded.issue_fingerprint,"
            "review_state='approved',reviewed_at=excluded.reviewed_at",
            (profile_id, issue_id, intent, mo_id, inat_id,
             str(row["fingerprint"]), reviewed_at),
        )

    def revoke_link_issue_review(self, profile_id: int, issue_id: int) -> None:
        self.connection().execute(
            "UPDATE sync_link_issue_reviews SET review_state='revoked' WHERE profile_id=? AND issue_id=?",
            (profile_id, issue_id),
        )

    def link_issue_review(self, profile_id: int, issue_id: int) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT r.* FROM sync_link_issue_reviews r JOIN sync_issues i "
            "ON i.profile_id=r.profile_id AND i.issue_id=r.issue_id "
            "WHERE r.profile_id=? AND r.issue_id=? AND r.review_state='approved' "
            "AND r.issue_fingerprint=i.fingerprint AND i.state='open' "
            "AND i.issue_type IN ('one_way_link','malformed_link')",
            (profile_id, issue_id),
        ).fetchone()
        return dict(row) if row else None

    def journal_link_actions(
        self, preview: LinkRepairPreview, options: Sequence[LinkRepairOption],
    ) -> tuple[int, tuple[int, ...]]:
        """Atomically persist an explicitly confirmed preview selection."""
        if not options:
            raise ValueError("Select at least one link repair action")
        if any(not option.enabled for option in options):
            raise ValueError("A disabled link repair action cannot be journaled")
        now = _utc_now()
        with self.transaction() as conn:
            if preview.pair_id is not None:
                pair = conn.execute(
                    "SELECT review_state FROM sync_pairs WHERE profile_id=? AND pair_id=?",
                    (preview.profile_id, preview.pair_id),
                ).fetchone()
                if not pair or str(pair["review_state"]) != "confirmed":
                    raise ValueError("The source pair is no longer confirmed")
            if preview.issue_id is not None:
                review = conn.execute(
                    "SELECT r.*,i.issue_type,i.state AS issue_state FROM sync_link_issue_reviews r JOIN sync_issues i "
                    "ON i.profile_id=r.profile_id AND i.issue_id=r.issue_id "
                    "WHERE r.profile_id=? AND r.issue_id=? AND r.review_state='approved' "
                    "AND r.issue_fingerprint=i.fingerprint AND i.state='open'",
                    (preview.profile_id, preview.issue_id),
                ).fetchone()
                if not review:
                    raise ValueError("The source link issue review is no longer current")
                if str(review["issue_type"]) not in REPAIRABLE_LINK_ISSUE_TYPES:
                    raise ValueError("This issue type cannot authorize remote writes")
                if (
                    int(review["mo_observation_id"] or 0) != preview.mo_observation_id
                    or int(review["inat_observation_id"] or 0) != preview.inat_observation_id
                    or _review_fingerprint(review) != preview.source_fingerprint
                ):
                    raise ValueError("The exact issue review identity changed after preview")
            cursor = conn.execute(
                "INSERT INTO sync_action_groups(profile_id,source_kind,pair_id,issue_id,source_fingerprint,"
                "mo_observation_id,inat_observation_id,previewed_at,confirmed_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (preview.profile_id, preview.source_kind, preview.pair_id, preview.issue_id,
                 preview.source_fingerprint, preview.mo_observation_id, preview.inat_observation_id,
                 now, now, now, now),
            )
            group_id = int(cursor.lastrowid)
            action_ids: list[int] = []
            for ordinal, option in enumerate(options, 1):
                deduplication_key = _local_fingerprint(
                    preview.profile_id, option.action_type.value, option.site.value,
                    option.mo_observation_id, option.inat_observation_id,
                    option.remote_row_id, option.remote_row_uuid,
                    option.current_target_id, option.desired_target_id,
                    preview.inat_links_fingerprint, preview.mo_links_fingerprint,
                )
                duplicate = conn.execute(
                    "SELECT action_id FROM sync_actions WHERE profile_id=? AND deduplication_key=? "
                    "AND state IN ('pending','running','outcome_unknown') ORDER BY action_id LIMIT 1",
                    (preview.profile_id, deduplication_key),
                ).fetchone()
                if duplicate:
                    raise ValueError(f"Equivalent unresolved action {int(duplicate[0])} already exists")
                action_cursor = conn.execute(
                    "INSERT INTO sync_actions(profile_id,action_group_id,ordinal,action_type,site,state,last_phase,"
                    "pair_id,issue_id,mo_observation_id,inat_observation_id,inat_observation_uuid,"
                    "binding_id,remote_row_id,remote_row_uuid,current_target_id,desired_target_id,destructive,"
                    "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
                    "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
                    "created_at,confirmed_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (preview.profile_id, group_id, ordinal, option.action_type.value, option.site.value,
                     "pending", "preview", preview.pair_id, preview.issue_id, option.mo_observation_id,
                     option.inat_observation_id, preview.inat_observation_uuid,
                     option.binding_id, option.remote_row_id, option.remote_row_uuid,
                     option.current_target_id, option.desired_target_id, int(option.destructive),
                     preview.inat_record_fingerprint, preview.mo_record_fingerprint,
                     preview.inat_links_fingerprint, preview.mo_links_fingerprint,
                     deduplication_key, now, now, now),
                )
                action_ids.append(int(action_cursor.lastrowid))
            for row in (*preview.inat_rows, *preview.mo_rows):
                conn.execute(
                    "INSERT INTO sync_action_snapshot_rows(profile_id,action_group_id,site,observation_id,"
                    "remote_row_id,remote_row_uuid,binding_id,normalized_target_id,parse_state,row_fingerprint) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (preview.profile_id, group_id, row.site.value, row.observation_id,
                     row.row_id, row.row_uuid, row.binding_id, row.target_observation_id,
                     row.parse_state, row.row_fingerprint),
                )
        return group_id, tuple(action_ids)

    def journal_its_actions(
        self, preview: ITSComparisonPreview, options: Sequence[ITSActionOption],
    ) -> tuple[int, tuple[int, ...]]:
        """Persist selected Gate 1C actions without persisting any sequence body."""
        if not options:
            raise ValueError("Select at least one ITS synchronization action")
        if len(options) != 1:
            raise ValueError("Gate 1C requires one individually reviewed write per action group")
        if any(not option.enabled for option in options):
            raise ValueError("A disabled ITS action cannot be journaled")
        option = options[0]
        if (
            option.destination_site is RemoteSite.INAT
            and option.action_type is not ITSActionType.INAT_ITS_REMOVE
            and not option.destination_binding_id
        ):
            raise ValueError("An exact verified iNaturalist destination field is required")
        now = _utc_now()
        with self.transaction() as conn:
            pair = conn.execute(
                "SELECT pair_id,updated_at,review_state,link_state,confirmed_by FROM sync_pairs "
                "WHERE profile_id=? AND pair_id=?",
                (preview.profile_id, preview.pair_id),
            ).fetchone()
            if not pair or str(pair["review_state"]) != "confirmed":
                raise ValueError("The source pair is no longer confirmed")
            # Compare the current pair fingerprint with the same ``public_fingerprint``
            # used by the preview and by ``_require_current_source`` at execution time.
            # This avoids journaling a predictably-failed action, and — unlike the prior
            # ``_local_fingerprint`` recompute — does not diverge on NULL/zero fields.
            from .normalization import public_fingerprint as _public_fingerprint
            current_source = _public_fingerprint(
                "pair", pair["pair_id"], pair["updated_at"], pair["review_state"],
                pair["link_state"], pair["confirmed_by"],
            )
            if current_source != preview.source_fingerprint:
                raise ValueError("The confirmed pair changed after ITS preview")
            cursor = conn.execute(
                "INSERT INTO sync_action_groups(profile_id,source_kind,pair_id,issue_id,source_fingerprint,"
                "mo_observation_id,inat_observation_id,previewed_at,confirmed_at,created_at,updated_at) "
                "VALUES(?, 'pair', ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (preview.profile_id, preview.pair_id, preview.source_fingerprint,
                 preview.mo_observation_id, preview.inat_observation_id,
                 now, now, now, now),
            )
            group_id = int(cursor.lastrowid)
            action_ids: list[int] = []
            for ordinal, option in enumerate(options, 1):
                deduplication_key = _local_fingerprint(
                    preview.profile_id, preview.pair_id, option.action_type.value,
                    option.destination_site.value, option.source_site.value,
                    option.source_remote_id, option.destination_remote_id,
                    option.sequence_fingerprint, option.archive, option.normalized_accession,
                    option.source_metadata_fingerprint,
                    option.destination_preflight_fingerprint,
                )
                duplicate = conn.execute(
                    "SELECT action_id FROM sync_actions WHERE profile_id=? AND deduplication_key=? "
                    "AND state IN ('pending','running','outcome_unknown') LIMIT 1",
                    (preview.profile_id, deduplication_key),
                ).fetchone()
                if duplicate:
                    raise ValueError(f"Equivalent unresolved action {int(duplicate[0])} already exists")
                action_cursor = conn.execute(
                    "INSERT INTO sync_actions(profile_id,action_group_id,ordinal,action_type,site,state,last_phase,"
                    "pair_id,issue_id,mo_observation_id,inat_observation_id,inat_observation_uuid,binding_id,"
                    "remote_row_id,remote_row_uuid,current_target_id,desired_target_id,destructive,"
                    "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
                    "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
                    "source_site,source_record_id,source_sequence_remote_id,sequence_fingerprint,"
                    "normalized_accession,normalized_archive,source_metadata_fingerprint,"
                    "destination_preflight_fingerprint,evidence_type,created_at,confirmed_at,updated_at) "
                    "VALUES(:profile_id,:group_id,:ordinal,:action_type,:site,'pending','preview',"
                    ":pair_id,NULL,:mo_id,:inat_id,:inat_uuid,:binding_id,:remote_id,:remote_uuid,"
                    "NULL,NULL,:destructive,:inat_record_fp,:mo_record_fp,:specimen_fp,'',:dedupe,"
                    ":source_site,:source_record_id,"
                    ":source_remote_id,:sequence_fingerprint,:normalized_accession,:normalized_archive,"
                    ":source_metadata_fingerprint,:destination_fingerprint,:evidence_type,:now,:now,:now)",
                    {
                        "profile_id": preview.profile_id, "group_id": group_id,
                        "ordinal": ordinal, "action_type": option.action_type.value,
                        "site": option.destination_site.value, "pair_id": preview.pair_id,
                        "mo_id": preview.mo_observation_id,
                        "inat_id": preview.inat_observation_id,
                        "inat_uuid": preview.inat_observation_uuid,
                        # Observation-level record fingerprints and the specimen
                        # validation fingerprint are journaled (never empty) so
                        # execution can stop if either changed after preview.
                        "inat_record_fp": preview.inat_record_fingerprint,
                        "mo_record_fp": preview.mo_record_fingerprint,
                        "specimen_fp": preview.specimen_state_fingerprint,
                        "binding_id": (
                            option.destination_binding_id
                            if option.destination_site is RemoteSite.INAT else None
                        ),
                        "remote_id": option.destination_remote_id,
                        "remote_uuid": option.destination_remote_uuid,
                        "destructive": int(option.destructive), "dedupe": deduplication_key,
                        "source_site": option.source_site.value,
                        "source_record_id": option.source_record_id,
                        "source_remote_id": option.source_remote_id,
                        "sequence_fingerprint": option.sequence_fingerprint,
                        "normalized_accession": option.normalized_accession,
                        "normalized_archive": option.archive,
                        "source_metadata_fingerprint": option.source_metadata_fingerprint,
                        "destination_fingerprint": option.destination_preflight_fingerprint,
                        "evidence_type": (
                            "invalid_value_removal"
                            if option.action_type.value.endswith("_remove")
                            else "sequence" if option.sequence_fingerprint else "accession"
                        ),
                        "now": now,
                    },
                )
                action_ids.append(int(action_cursor.lastrowid))
        return group_id, tuple(action_ids)

    def journal_coordinate_actions(
        self, preview: CoordinateComparisonPreview, options: Sequence[CoordinateActionOption],
    ) -> tuple[int, tuple[int, ...]]:
        """Persist one reviewed Gate 1D coordinate copy.

        Only privacy-safe fields are stored: IDs, action type, privacy states,
        and the two observation record/version fingerprints. No raw
        latitude/longitude, exact distance, or coordinate-derived hash is ever
        written here — an unkeyed hash of a low-entropy point is enumerable, so
        coordinate freshness is anchored to the sites' own ``updated_at`` version
        fingerprints instead.
        """
        if not options:
            raise ValueError("Select one coordinate synchronization action")
        if len(options) != 1:
            raise ValueError("Gate 1D requires one individually reviewed coordinate write per group")
        option = options[0]
        if not option.enabled:
            raise ValueError("A disabled coordinate action cannot be journaled")
        if option.destination_site is not RemoteSite.INAT:
            raise ValueError("Coordinate writes target only iNaturalist")
        now = _utc_now()
        with self.transaction() as conn:
            pair = conn.execute(
                "SELECT pair_id,updated_at,review_state,link_state,confirmed_by FROM sync_pairs "
                "WHERE profile_id=? AND pair_id=?",
                (preview.profile_id, preview.pair_id),
            ).fetchone()
            if not pair or str(pair["review_state"]) != "confirmed":
                raise ValueError("The source pair is no longer confirmed")
            from .normalization import public_fingerprint as _public_fingerprint
            current_source = _public_fingerprint(
                "pair", pair["pair_id"], pair["updated_at"], pair["review_state"],
                pair["link_state"], pair["confirmed_by"],
            )
            if current_source != preview.source_fingerprint:
                raise ValueError("The confirmed pair changed after coordinate preview")
            cursor = conn.execute(
                "INSERT INTO sync_action_groups(profile_id,source_kind,pair_id,issue_id,source_fingerprint,"
                "mo_observation_id,inat_observation_id,previewed_at,confirmed_at,created_at,updated_at) "
                "VALUES(?, 'pair', ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (preview.profile_id, preview.pair_id, preview.source_fingerprint,
                 preview.mo_observation_id, preview.inat_observation_id,
                 now, now, now, now),
            )
            group_id = int(cursor.lastrowid)
            # Dedup on the pair, direction, and MO record version. If the source
            # observation changes (new updated_at → new record fingerprint), a
            # fresh copy is legitimately re-proposable; two identical actions
            # against the same source version collapse.
            deduplication_key = _local_fingerprint(
                preview.profile_id, preview.pair_id, option.action_type.value,
                option.source_site.value, option.source_record_id,
                option.destination_record_id, option.proposed_privacy_state,
                preview.mo_record_fingerprint, preview.inat_record_fingerprint,
            )
            duplicate = conn.execute(
                "SELECT action_id FROM sync_actions WHERE profile_id=? AND deduplication_key=? "
                "AND state IN ('pending','running','outcome_unknown') LIMIT 1",
                (preview.profile_id, deduplication_key),
            ).fetchone()
            if duplicate:
                raise ValueError(f"Equivalent unresolved action {int(duplicate[0])} already exists")
            action_cursor = conn.execute(
                "INSERT INTO sync_actions(profile_id,action_group_id,ordinal,action_type,site,state,last_phase,"
                "pair_id,issue_id,mo_observation_id,inat_observation_id,inat_observation_uuid,binding_id,"
                "remote_row_id,remote_row_uuid,current_target_id,desired_target_id,destructive,"
                "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
                "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
                "source_site,source_record_id,evidence_type,"
                "source_privacy_state,proposed_privacy_state,"
                "created_at,confirmed_at,updated_at) "
                "VALUES(:profile_id,:group_id,1,:action_type,'inat','pending','preview',"
                ":pair_id,NULL,:mo_id,:inat_id,:inat_uuid,NULL,'','',NULL,NULL,:destructive,"
                ":inat_record_fp,:mo_record_fp,'','',:dedupe,"
                "'mo',:source_record_id,'coordinate',"
                ":source_privacy,:proposed_privacy,:now,:now,:now)",
                {
                    "profile_id": preview.profile_id, "group_id": group_id,
                    "action_type": option.action_type.value,
                    "pair_id": preview.pair_id,
                    "mo_id": preview.mo_observation_id,
                    "inat_id": preview.inat_observation_id,
                    "inat_uuid": preview.inat_observation_uuid,
                    "inat_record_fp": preview.inat_record_fingerprint,
                    "mo_record_fp": preview.mo_record_fingerprint,
                    "destructive": int(option.destructive), "dedupe": deduplication_key,
                    "source_record_id": option.source_record_id,
                    "source_privacy": option.source_privacy_state,
                    "proposed_privacy": option.proposed_privacy_state,
                    "now": now,
                },
            )
            action_id = int(action_cursor.lastrowid)
        return group_id, (action_id,)

    # Gate 1E photo transfer -------------------------------------------

    def journal_photo_actions(
        self, preview: PhotoComparisonPreview, options: Sequence[PhotoActionOption],
        *, planned_observation_photo_uuid: str,
    ) -> tuple[int, tuple[int, ...]]:
        """Persist exactly one reviewed Gate 1E photo transfer.

        ``planned_observation_photo_uuid`` is generated by the caller and stored
        **before** the request is sent. That ordering is what makes a lost
        response recoverable: the destination re-read returns this uuid, whereas
        a bare uploaded photo's own uuid is never readable back.

        Only IDs, one-way content digests, and license/holder labels are stored.
        No photo bytes and no signed URL are written here.
        """
        if not options:
            raise ValueError("Select one photo transfer action")
        if len(options) != 1:
            raise ValueError("Gate 1E requires one individually reviewed photo transfer per group")
        option = options[0]
        if not option.enabled:
            raise ValueError("A disabled photo action cannot be journaled")
        if option.destination_site is not RemoteSite.INAT:
            raise ValueError("Gate 1E photo transfer targets only iNaturalist")
        if not planned_observation_photo_uuid.strip():
            raise ValueError("A planned observation_photo uuid is required before any photo write")
        now = _utc_now()
        with self.transaction() as conn:
            pair = conn.execute(
                "SELECT pair_id,updated_at,review_state,link_state,confirmed_by FROM sync_pairs "
                "WHERE profile_id=? AND pair_id=?",
                (preview.profile_id, preview.pair_id),
            ).fetchone()
            if not pair or str(pair["review_state"]) != "confirmed":
                raise ValueError("The source pair is no longer confirmed")
            from .normalization import public_fingerprint as _public_fingerprint
            current_source = _public_fingerprint(
                "pair", pair["pair_id"], pair["updated_at"], pair["review_state"],
                pair["link_state"], pair["confirmed_by"],
            )
            if current_source != preview.source_fingerprint:
                raise ValueError("The confirmed pair changed after photo preview")
            # One durable transfer identity per (source photo -> destination
            # observation), enforced by a table-level UNIQUE. An unresolved or
            # succeeded row blocks a second attempt outright; a definitively
            # failed one is RESET and reused, because the unique constraint would
            # otherwise make a legitimate retry impossible.
            existing = conn.execute(
                "SELECT transfer_id,state FROM sync_photo_transfers WHERE profile_id=? AND source_site=? "
                "AND source_photo_id=? AND destination_site='inat' AND destination_observation_id=?",
                (preview.profile_id, option.source_site.value, option.source_photo_id,
                 preview.inat_observation_id),
            ).fetchone()
            if existing and str(existing["state"]) != "failed":
                raise ValueError(
                    f"Photo {option.source_photo_id} already has transfer record "
                    f"{int(existing['transfer_id'])} in state '{existing['state']}' for this "
                    "destination observation"
                )
            cursor = conn.execute(
                "INSERT INTO sync_action_groups(profile_id,source_kind,pair_id,issue_id,source_fingerprint,"
                "mo_observation_id,inat_observation_id,previewed_at,confirmed_at,created_at,updated_at) "
                "VALUES(?, 'pair', ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (preview.profile_id, preview.pair_id, preview.source_fingerprint,
                 preview.mo_observation_id, preview.inat_observation_id, now, now, now, now),
            )
            group_id = int(cursor.lastrowid)
            # Keyed on the specific photo and both record versions, so a changed
            # source observation legitimately re-proposes while an identical
            # re-confirmation collapses.
            deduplication_key = _local_fingerprint(
                preview.profile_id, preview.pair_id, option.action_type.value,
                option.source_site.value, option.source_photo_id,
                preview.inat_observation_id,
                preview.mo_record_fingerprint, preview.inat_record_fingerprint,
            )
            duplicate = conn.execute(
                "SELECT action_id FROM sync_actions WHERE profile_id=? AND deduplication_key=? "
                "AND state IN ('pending','running','outcome_unknown') LIMIT 1",
                (preview.profile_id, deduplication_key),
            ).fetchone()
            if duplicate:
                raise ValueError(f"Equivalent unresolved action {int(duplicate[0])} already exists")
            action_cursor = conn.execute(
                "INSERT INTO sync_actions(profile_id,action_group_id,ordinal,action_type,site,state,last_phase,"
                "pair_id,issue_id,mo_observation_id,inat_observation_id,inat_observation_uuid,binding_id,"
                "remote_row_id,remote_row_uuid,current_target_id,desired_target_id,destructive,"
                "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
                "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
                "source_site,source_record_id,evidence_type,source_photo_id,"
                "planned_observation_photo_uuid,reviewed_byte_fingerprint,created_at,confirmed_at,updated_at) "
                "VALUES(:profile_id,:group_id,1,:action_type,'inat','pending','preview',"
                ":pair_id,NULL,:mo_id,:inat_id,:inat_uuid,NULL,'','',NULL,NULL,0,"
                ":inat_record_fp,:mo_record_fp,'','',:dedupe,"
                ":source_site,:source_record_id,'photo',:source_photo_id,"
                ":planned_uuid,:reviewed_fp,:now,:now,:now)",
                {
                    "profile_id": preview.profile_id, "group_id": group_id,
                    "action_type": option.action_type.value,
                    "pair_id": preview.pair_id,
                    "mo_id": preview.mo_observation_id,
                    "inat_id": preview.inat_observation_id,
                    "inat_uuid": preview.inat_observation_uuid,
                    "inat_record_fp": preview.inat_record_fingerprint,
                    "mo_record_fp": preview.mo_record_fingerprint,
                    "dedupe": deduplication_key,
                    "source_site": option.source_site.value,
                    "source_record_id": option.source_record_id,
                    "source_photo_id": option.source_photo_id,
                    "planned_uuid": planned_observation_photo_uuid,
                    "reviewed_fp": option.byte_fingerprint,
                    "now": now,
                },
            )
            action_id = int(action_cursor.lastrowid)
            if existing:
                # Reuse the failed row so the UNIQUE constraint cannot block a
                # retry, clearing every result field from the previous attempt.
                conn.execute(
                    "UPDATE sync_photo_transfers SET pair_id=?,action_id=?,"
                    "destination_observation_photo_uuid=?,destination_photo_id='',"
                    "byte_fingerprint=?,md5='',destination_license_code='',"
                    "source_license_label=?,source_copyright_holder=?,state='pending',updated_at=? "
                    "WHERE transfer_id=?",
                    (preview.pair_id, action_id, planned_observation_photo_uuid,
                     option.byte_fingerprint, option.source_license_label,
                     option.source_copyright_holder, now, int(existing["transfer_id"])),
                )
            else:
                conn.execute(
                    "INSERT INTO sync_photo_transfers(profile_id,pair_id,action_id,source_site,source_photo_id,"
                    "destination_site,destination_observation_id,destination_observation_photo_uuid,"
                    "byte_fingerprint,source_license_label,source_copyright_holder,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'inat',?,?,?,?,?, 'pending',?,?)",
                    (preview.profile_id, preview.pair_id, action_id, option.source_site.value,
                     option.source_photo_id, preview.inat_observation_id,
                     planned_observation_photo_uuid, option.byte_fingerprint,
                     option.source_license_label, option.source_copyright_holder, now, now),
                )
        return group_id, (action_id,)

    def transferred_source_photo_ids(
        self, profile_id: int, source_site: RemoteSite, destination_observation_id: int,
        *, destination_site: RemoteSite = RemoteSite.INAT,
    ) -> set[str]:
        """Source photo IDs already sent to this destination observation.

        This is the first and most authoritative duplicate signal: it records
        what *this application* has previously transferred.

        ``destination_site`` is an explicit parameter rather than a hardcoded
        'inat' because an observation id is only unique WITHIN a site. Every
        journaled transfer is iNat-destined today (``mo_photo_attach`` has no
        working write path), but if this filter kept assuming iNat once MO
        transfers land, it would return an empty set for every MO destination
        — indistinguishable from "nothing has been sent yet", which is exactly
        the answer that re-uploads a duplicate photo.
        """
        rows = self.connection().execute(
            "SELECT source_photo_id FROM sync_photo_transfers WHERE profile_id=? AND source_site=? "
            "AND destination_site=? AND destination_observation_id=? "
            "AND state IN ('succeeded','pending','outcome_unknown')",
            (profile_id, source_site.value, destination_site.value,
             int(destination_observation_id)),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def transferred_photo_digests(
        self, profile_id: int, *, destination_observation_id: Optional[int] = None,
        destination_site: RemoteSite = RemoteSite.INAT,
        exclude_action_id: Optional[int] = None,
    ) -> dict[str, str]:
        """Content digest -> a human label for photos we have transferred.

        The second duplicate signal from the report: it catches the same image
        arriving under a different source id, which the id-keyed ledger cannot.

        Scope matters. Passing ``destination_observation_id`` restricts this to
        one destination, which is what may legitimately *block* a transfer: the
        same image can properly belong to two different confirmed specimen
        pairs, so a profile-wide match is advisory, not disqualifying. That
        narrowing is qualified by ``destination_site`` because an observation
        id only identifies a record within one site — without it an MO
        observation whose id happened to equal an iNat one would pull in the
        other site's digests and block a legitimate transfer. The profile-wide
        advisory form (no ``destination_observation_id``) stays deliberately
        cross-site.
        """
        sql = (
            "SELECT byte_fingerprint,source_site,source_photo_id,"
            "destination_observation_id,destination_site "
            "FROM sync_photo_transfers WHERE profile_id=? AND byte_fingerprint<>'' "
            "AND state IN ('succeeded','pending','outcome_unknown')"
        )
        params: list[Any] = [profile_id]
        if destination_observation_id is not None:
            sql += " AND destination_site=? AND destination_observation_id=?"
            params.append(destination_site.value)
            params.append(int(destination_observation_id))
        if exclude_action_id is not None:
            # An in-flight transfer journals its own reviewed digest before the
            # write, so the pre-write duplicate check must not match on itself.
            sql += " AND (action_id IS NULL OR action_id<>?)"
            params.append(int(exclude_action_id))
        rows = self.connection().execute(sql, tuple(params)).fetchall()
        labels = {"inat": "iNaturalist", "mo": "Mushroom Observer"}
        return {
            str(row[0]): (
                f"{row[1]} photo {row[2]} -> "
                f"{labels.get(str(row[4]), str(row[4]))} observation {row[3]}"
            )
            for row in rows
        }

    def finish_photo_transfer(
        self, profile_id: int, action_id: int, state: str, *,
        destination_photo_id: Optional[str] = None,
        byte_fingerprint: Optional[str] = None,
        md5: Optional[str] = None,
        destination_license_code: Optional[str] = None,
    ) -> None:
        """Record the settled outcome of one photo transfer.

        Every result field is optional and **omitting one preserves its current
        value**. Recovery paths (verifying an unknown outcome, or finding the
        photo already attached) legitimately know the destination id without
        re-deriving the content digest, and blanking that digest would destroy
        the duplicate evidence future transfers depend on.
        """
        if state not in {"succeeded", "failed", "outcome_unknown", "pending"}:
            raise ValueError(f"Unsupported photo transfer state: {state}")
        assignments = ["state=?", "updated_at=?"]
        params: list[Any] = [state, _utc_now()]
        for column, value in (
            ("destination_photo_id", destination_photo_id),
            ("byte_fingerprint", byte_fingerprint),
            ("md5", md5),
            ("destination_license_code", destination_license_code),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                params.append(value)
        params.extend([profile_id, action_id])
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE sync_photo_transfers SET {','.join(assignments)} "
                "WHERE profile_id=? AND action_id=?",
                tuple(params),
            )

    # Gate 2A: missing-observation creation ------------------------------
    #
    # A creation saga's per-item rows (photos, identifiers, sequences,
    # reciprocal links) and its final ``pair_finalize`` row cannot be
    # journaled up front alongside ordinal 0: every existing action_type
    # other than the three Gate 2A types requires BOTH mo_observation_id and
    # inat_observation_id (the compound CHECK added in _migration_v9), and
    # the destination id genuinely does not exist until ordinal 0's write
    # succeeds. So only ordinal 0 (the create) is journaled by
    # ``journal_observation_creation_actions``; the REST of the saga is
    # minted lazily by the service's ``execute_group`` once the destination
    # id is known, via ``mint_creation_followup_rows``. ``sync_creation_items``
    # durably records which items the user approved (independent of
    # ``sync_actions``) so a restart before ordinal 0 succeeds never loses
    # the approved item set (saga-architecture-rules rule 5).

    def journal_observation_creation_actions(
        self, profile_id: int, *,
        source_site: str, source_observation_id: int, destination_site: str,
        source_fingerprint: str, correlation_marker: str, marker_location: str,
        approved_field_gaps: Sequence[str], item_specs: Sequence[dict[str, Any]],
        reviewed_destination_taxon_id: Optional[int] = None,
        reviewed_destination_taxon_name: str = "",
        reviewed_source_taxon_name: str = "",
        reviewed_source_taxon_rank: str = "",
        resolution_mode: str = "",
        taxon_resolution_fingerprint: str = "",
        reviewed_payload_fingerprint: str = "",
    ) -> tuple[int, int, int]:
        """Journal ordinal 0 (the create) plus a new, IMMUTABLE creation
        attempt under the stable source->destination creation identity.

        Returns ``(group_id, create_action_id, attempt_id)`` — the third
        element used to be a mutable "ledger" (``creation_id``); it is now
        the id of the freshly minted, never-overwritten attempt row (section
        7). ``item_specs`` is a list of
        ``{"item_type", "source_identity", "metadata_fingerprint"}`` dicts —
        these become ``sync_creation_items`` rows scoped to THIS attempt,
        with no ``sync_actions`` row yet (``action_id`` NULL, ``state=
        'pending'``).

        Retrying a prior attempt NEVER reuses, repoints, or deletes it: the
        identity row (``sync_created_observations``) is looked up but never
        mutated here, and a brand-new ``sync_creation_attempts`` row is
        always inserted, linked to the prior terminal attempt via
        ``supersedes_attempt_id``. An attempt that is still ``pending`` or
        ``outcome_unknown`` blocks a new attempt outright — an unknown
        outcome must be resolved by recovery, never bypassed by retrying.
        """
        if source_site not in {"mo", "inat"} or destination_site not in {"mo", "inat"}:
            raise ValueError("Invalid site")
        if destination_site == "mo":
            # Section 4: only MO->iNaturalist creation is proven. Journaling
            # is the last local gate before a create write is even queued, so
            # this is fail-closed independent of the service-layer guard in
            # ObservationCreationService.prepare_preview and independent of
            # whatever the UI already blocks.
            raise ValueError(
                "Creating a missing observation on Mushroom Observer is not supported yet."
            )
        if source_site == destination_site:
            raise ValueError("Source and destination sites must differ")
        now = _utc_now()
        with self.transaction() as conn:
            record = conn.execute(
                "SELECT unpaired_state,COALESCE(remote_updated_at,last_seen_at) AS ts FROM sync_records "
                "WHERE profile_id=? AND site=? AND remote_observation_id=?",
                (profile_id, source_site, source_observation_id),
            ).fetchone()
            expected_state = (
                "confirmed_missing_on_mo" if destination_site == "mo" else "confirmed_missing_on_inat"
            )
            if not record or str(record["unpaired_state"]) != expected_state:
                raise ValueError(
                    "The source record is not (or is no longer) confirmed missing on the "
                    "destination site — an unpaired record is not automatically a missing record"
                )
            from .normalization import public_fingerprint as _public_fingerprint
            current_source = _public_fingerprint(
                "record", source_site, source_observation_id, record["unpaired_state"], record["ts"],
            )
            if current_source != source_fingerprint:
                raise ValueError("The source record changed after the creation preview")
            existing_identity = conn.execute(
                "SELECT creation_id,destination_observation_id FROM sync_created_observations "
                "WHERE profile_id=? AND source_site=? AND source_observation_id=? AND destination_site=?",
                (profile_id, source_site, source_observation_id, destination_site),
            ).fetchone()
            if existing_identity and existing_identity["destination_observation_id"] is not None:
                raise ValueError(
                    f"A destination observation already exists for this record "
                    f"(creation {int(existing_identity['creation_id'])})"
                )
            supersedes_attempt_id: Optional[int] = None
            if existing_identity:
                latest_attempt = conn.execute(
                    "SELECT attempt_id,state FROM sync_creation_attempts WHERE profile_id=? AND creation_id=? "
                    "ORDER BY attempt_id DESC LIMIT 1",
                    (profile_id, int(existing_identity["creation_id"])),
                ).fetchone()
                if latest_attempt and str(latest_attempt["state"]) in ("pending", "outcome_unknown"):
                    raise ValueError(
                        f"Attempt {int(latest_attempt['attempt_id'])} for this record is still "
                        f"{latest_attempt['state']} — resolve it (verify, or wait for it to reach a "
                        f"terminal state) before starting a new attempt. An outcome_unknown attempt is "
                        f"never automatically superseded."
                    )
                if latest_attempt and str(latest_attempt["state"]) in ("failed", "cancelled"):
                    supersedes_attempt_id = int(latest_attempt["attempt_id"])
            action_type = "mo_observation_create" if destination_site == "mo" else "inat_observation_create"
            deduplication_key = _local_fingerprint(
                profile_id, action_type, source_site, source_observation_id, destination_site,
            )
            duplicate = conn.execute(
                "SELECT action_id FROM sync_actions WHERE profile_id=? AND deduplication_key=? "
                "AND state IN ('pending','running','outcome_unknown') LIMIT 1",
                (profile_id, deduplication_key),
            ).fetchone()
            if duplicate:
                raise ValueError(f"Equivalent unresolved action {int(duplicate[0])} already exists")
            cursor = conn.execute(
                "INSERT INTO sync_action_groups(profile_id,source_kind,pair_id,issue_id,source_fingerprint,"
                "mo_observation_id,inat_observation_id,previewed_at,confirmed_at,created_at,updated_at) "
                "VALUES(?,'creation',NULL,NULL,?,NULL,NULL,?,?,?,?)",
                (profile_id, source_fingerprint, now, now, now, now),
            )
            group_id = int(cursor.lastrowid)
            source_mo_id = source_observation_id if source_site == "mo" else None
            source_inat_id = source_observation_id if source_site == "inat" else None
            action_cursor = conn.execute(
                "INSERT INTO sync_actions(profile_id,action_group_id,ordinal,action_type,site,state,"
                "last_phase,pair_id,issue_id,mo_observation_id,inat_observation_id,inat_observation_uuid,"
                "destructive,preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
                "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
                "source_site,source_record_id,created_at,confirmed_at,updated_at) "
                "VALUES(?,?,0,?,?,'pending','preview',NULL,NULL,?,?,'',1,'','','','' ,?,?,?,?,?,?)",
                (profile_id, group_id, action_type, destination_site,
                 source_mo_id, source_inat_id, deduplication_key,
                 source_site, source_observation_id, now, now, now),
            )
            create_action_id = int(action_cursor.lastrowid)
            # The IDENTITY row is looked up but NEVER updated here (section
            # 7) — created once, and touched again only by
            # finish_observation_creation once a destination id is actually
            # known.
            if existing_identity:
                creation_id = int(existing_identity["creation_id"])
            else:
                identity_cursor = conn.execute(
                    "INSERT INTO sync_created_observations(profile_id,pair_id,source_site,"
                    "source_observation_id,destination_site,destination_observation_id,"
                    "destination_observation_uuid,created_at,updated_at) "
                    "VALUES(?,NULL,?,?,?,NULL,NULL,?,?)",
                    (profile_id, source_site, source_observation_id, destination_site, now, now),
                )
                creation_id = int(identity_cursor.lastrowid)
            # A brand-new, immutable attempt row every time — never reused,
            # never overwritten, always linked to its predecessor (if any)
            # via supersedes_attempt_id rather than replacing it.
            attempt_cursor = conn.execute(
                "INSERT INTO sync_creation_attempts(creation_id,profile_id,action_group_id,"
                "destination_site,correlation_marker,marker_location,approved_field_gaps,"
                "source_fingerprint,reviewed_payload_fingerprint,"
                "reviewed_destination_taxon_id,reviewed_destination_taxon_name,"
                "reviewed_source_taxon_name,reviewed_source_taxon_rank,resolution_mode,"
                "taxon_resolution_fingerprint,state,supersedes_attempt_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (creation_id, profile_id, group_id, destination_site, correlation_marker,
                 marker_location, json.dumps(list(approved_field_gaps)), source_fingerprint,
                 reviewed_payload_fingerprint,
                 reviewed_destination_taxon_id, reviewed_destination_taxon_name,
                 reviewed_source_taxon_name, reviewed_source_taxon_rank, resolution_mode,
                 taxon_resolution_fingerprint, supersedes_attempt_id, now, now),
            )
            attempt_id = int(attempt_cursor.lastrowid)
            for spec in item_specs:
                conn.execute(
                    "INSERT INTO sync_creation_items(attempt_id,action_id,item_type,source_item_identity,"
                    "reviewed_metadata_fingerprint,reviewed_byte_fingerprint,state,created_at,updated_at) "
                    "VALUES(?,NULL,?,?,?,?,'pending',?,?)",
                    (attempt_id, spec["item_type"], spec["source_identity"],
                     spec.get("metadata_fingerprint", ""), spec.get("reviewed_byte_fingerprint", ""), now, now),
                )
        return group_id, create_action_id, attempt_id

    def creation_identity(self, profile_id: int, creation_id: int) -> Optional[dict[str, Any]]:
        """The stable source->destination creation IDENTITY row only (no
        attempt data) — e.g. for checking the eventual real-world outcome
        (``destination_observation_id``) independent of any one attempt."""
        row = self.connection().execute(
            "SELECT * FROM sync_created_observations WHERE profile_id=? AND creation_id=?",
            (profile_id, creation_id),
        ).fetchone()
        return dict(row) if row else None

    def creation_attempts_for_identity(self, profile_id: int, creation_id: int) -> list[dict[str, Any]]:
        """Every attempt ever made for this identity, oldest first — the full
        auditable history (section 7): none are ever deleted or overwritten."""
        return [dict(row) for row in self.connection().execute(
            "SELECT * FROM sync_creation_attempts WHERE profile_id=? AND creation_id=? ORDER BY attempt_id",
            (profile_id, creation_id),
        ).fetchall()]

    def creation_ledger_for_group(self, profile_id: int, group_id: int) -> Optional[dict[str, Any]]:
        """The merged identity+attempt view ``observation_creation.py`` reads
        as "the ledger" for a given action group — the identity's stable
        fields (source/destination, the eventual real-world destination id)
        joined with THIS action group's specific, immutable attempt (its
        correlation marker, approved gaps, and reviewed taxon pin)."""
        row = self.connection().execute(
            "SELECT co.creation_id AS creation_id, co.profile_id AS profile_id, co.pair_id AS pair_id, "
            "co.source_site AS source_site, co.source_observation_id AS source_observation_id, "
            "co.destination_site AS destination_site, "
            "co.destination_observation_id AS destination_observation_id, "
            "co.destination_observation_uuid AS destination_observation_uuid, "
            "ca.attempt_id AS attempt_id, ca.action_group_id AS action_group_id, "
            "ca.correlation_marker AS correlation_marker, ca.marker_location AS marker_location, "
            "ca.approved_field_gaps AS approved_field_gaps, ca.source_fingerprint AS source_fingerprint, "
            "ca.reviewed_payload_fingerprint AS reviewed_payload_fingerprint, "
            "ca.reviewed_destination_taxon_id AS reviewed_destination_taxon_id, "
            "ca.reviewed_destination_taxon_name AS reviewed_destination_taxon_name, "
            "ca.reviewed_source_taxon_name AS reviewed_source_taxon_name, "
            "ca.reviewed_source_taxon_rank AS reviewed_source_taxon_rank, "
            "ca.resolution_mode AS resolution_mode, "
            "ca.taxon_resolution_fingerprint AS taxon_resolution_fingerprint, "
            "ca.state AS attempt_state, ca.supersedes_attempt_id AS supersedes_attempt_id, "
            "ca.created_at AS created_at, ca.updated_at AS updated_at "
            "FROM sync_creation_attempts ca "
            "JOIN sync_created_observations co ON co.creation_id = ca.creation_id "
            "WHERE ca.profile_id=? AND ca.action_group_id=?",
            (profile_id, group_id),
        ).fetchone()
        return dict(row) if row else None

    def finish_creation_attempt(self, profile_id: int, attempt_id: int, state: str) -> None:
        if state not in {"pending", "succeeded", "failed", "cancelled", "outcome_unknown", "superseded"}:
            raise ValueError(f"Unsupported creation attempt state: {state}")
        self.connection().execute(
            "UPDATE sync_creation_attempts SET state=?,updated_at=? WHERE profile_id=? AND attempt_id=?",
            (state, _utc_now(), profile_id, attempt_id),
        )

    def settle_creation_write_success(
        self, profile_id: int, action_id: int, attempt_id: int, creation_id: int, action_group_id: int,
        *, destination_site: str, destination_id: int, destination_uuid: str, source_record_id: int,
        verification_state: str = "",
    ) -> int:
        """Atomically settle a successful creation write: the create action,
        the attempt, the identity's destination id, both mo/inat id columns
        on every row in the saga's action group AND the group itself, and the
        provisional pair — all in one transaction.

        Used by BOTH the normal ``_execute_create`` success path and
        ``verify_unknown``'s outcome_unknown-recovery success path. Before
        this existed, ``verify_unknown`` only updated the create action's
        terminal state and the identity's destination id — it never filled
        ``mo_observation_id``/``inat_observation_id`` on the action row (or
        the group), never created the provisional pair, and never recorded
        the pair id on the identity. The very next ``execute_group()`` call
        immediately does ``int(create_row["mo_observation_id"])``, which
        crashed on the still-NULL column after a recovery instead of
        resuming the saga. Returns the (created-or-existing) provisional
        pair id.
        """
        now = _utc_now()
        mo_id = destination_id if destination_site == "mo" else source_record_id
        inat_id = destination_id if destination_site == "inat" else source_record_id
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sync_actions SET state='succeeded',last_phase='verification',last_error_code='',"
                "last_http_status=NULL,verification_state=?,"
                "verified_at=CASE WHEN ?!='' THEN ? ELSE verified_at END,"
                "server_row_id=?,server_row_uuid=?,outcome_unknown=0,finished_at=?,updated_at=? "
                "WHERE profile_id=? AND action_id=?",
                (verification_state, verification_state, now, str(destination_id), destination_uuid,
                 now, now, profile_id, action_id),
            )
            conn.execute(
                "UPDATE sync_creation_attempts SET state='succeeded',updated_at=? "
                "WHERE profile_id=? AND attempt_id=?",
                (now, profile_id, attempt_id),
            )
            identity_row = conn.execute(
                "SELECT destination_observation_id FROM sync_created_observations "
                "WHERE profile_id=? AND creation_id=?",
                (profile_id, creation_id),
            ).fetchone()
            existing_destination_id = identity_row["destination_observation_id"] if identity_row else None
            if existing_destination_id is not None and int(existing_destination_id) != int(destination_id):
                # Round-3 smaller issue: an unconditional overwrite here would let
                # a second, unrelated settlement silently repoint an identity's
                # destination observation. The identity's destination id is set
                # exactly once (by the first successful settlement) and must
                # never change out from under it.
                raise ValueError(
                    f"Creation identity {creation_id} already has destination_observation_id="
                    f"{existing_destination_id}; refusing to overwrite it with {destination_id}."
                )
            conn.execute(
                "UPDATE sync_created_observations SET destination_observation_id=?,"
                "destination_observation_uuid=?,updated_at=? WHERE profile_id=? AND creation_id=?",
                (destination_id, destination_uuid, now, profile_id, creation_id),
            )
            conn.execute(
                "UPDATE sync_actions SET mo_observation_id=?,inat_observation_id=? "
                "WHERE profile_id=? AND action_group_id=?",
                (mo_id, inat_id, profile_id, action_group_id),
            )
            # _mark_action_groups_stale_if_written reads these two columns
            # unconditionally once ANY row in the group has write_started_at
            # set, so leaving them NULL crashes that bookkeeping the moment a
            # later ordinal (e.g. the reciprocal link) starts its own write.
            conn.execute(
                "UPDATE sync_action_groups SET mo_observation_id=?,inat_observation_id=? "
                "WHERE profile_id=? AND action_group_id=?",
                (mo_id, inat_id, profile_id, action_group_id),
            )
            existing_pair = conn.execute(
                "SELECT pair_id FROM sync_pairs WHERE profile_id=? AND mo_observation_id=? "
                "AND inat_observation_id=?",
                (profile_id, mo_id, inat_id),
            ).fetchone()
            if existing_pair:
                pair_id = int(existing_pair["pair_id"])
            else:
                cursor = conn.execute(
                    "INSERT INTO sync_pairs(profile_id,mo_observation_id,inat_observation_id,link_state,"
                    "score,classification,review_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (profile_id, mo_id, inat_id, "", 0, "gate_2a_creation", "provisional", now, now),
                )
                pair_id = int(cursor.lastrowid)
            conn.execute(
                "UPDATE sync_created_observations SET pair_id=?,updated_at=? "
                "WHERE profile_id=? AND creation_id=?",
                (pair_id, now, profile_id, creation_id),
            )
            return pair_id

    def creation_items(self, profile_id: int, attempt_id: int) -> list[dict[str, Any]]:
        """Items belong to one specific, immutable attempt (section 7) —
        never the identity as a whole, so an old attempt's reviewed item plan
        can never be seen or touched by a newer attempt.

        Round-3 finding 6: joined through ``sync_creation_attempts`` and
        filtered on ``profile_id`` — an ``attempt_id`` alone is an
        auto-increment integer with no profile scoping of its own, so
        without this join a caller that (by bug, not malice) passed the
        wrong profile's attempt id would silently read another profile's
        reviewed item plan.
        """
        return [dict(row) for row in self.connection().execute(
            "SELECT ci.* FROM sync_creation_items ci "
            "JOIN sync_creation_attempts ca ON ca.attempt_id = ci.attempt_id "
            "WHERE ca.profile_id=? AND ci.attempt_id=? ORDER BY ci.creation_item_id",
            (profile_id, attempt_id),
        ).fetchall()]

    def creation_item_for_action(self, profile_id: int, action_id: int) -> Optional[dict[str, Any]]:
        """Whether ``action_id`` is a population-item action (linked from
        ``sync_creation_items``) as opposed to a reciprocal-link bootstrap or
        ``pair_finalize`` row — used by the creation-saga link exemption
        (section 8) to require a CONFIRMED pair specifically for population,
        while the two bootstrap link-add rows may still run against a
        provisional one."""
        row = self.connection().execute(
            "SELECT ci.* FROM sync_creation_items ci "
            "JOIN sync_creation_attempts ca ON ca.attempt_id = ci.attempt_id "
            "WHERE ca.profile_id=? AND ci.action_id=?",
            (profile_id, action_id),
        ).fetchone()
        return dict(row) if row else None

    def finish_observation_creation(
        self, profile_id: int, creation_id: int, *,
        destination_observation_id: Optional[int] = None,
        destination_observation_uuid: Optional[str] = None,
        pair_id: Optional[int] = None,
    ) -> None:
        """Every field is optional and omitting one preserves its current
        value — mirrors ``finish_photo_transfer``'s contract exactly."""
        assignments = ["updated_at=?"]
        params: list[Any] = [_utc_now()]
        for column, value in (
            ("destination_observation_id", destination_observation_id),
            ("destination_observation_uuid", destination_observation_uuid),
            ("pair_id", pair_id),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                params.append(value)
        params.extend([profile_id, creation_id])
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE sync_created_observations SET {','.join(assignments)} "
                "WHERE profile_id=? AND creation_id=?",
                tuple(params),
            )

    def finish_creation_item(
        self, profile_id: int, creation_item_id: int, state: str, *,
        destination_remote_id: str = "",
    ) -> bool:
        """Settle one reviewed creation item, scoped to its owning profile.

        ``creation_item_id`` is a bare auto-increment integer with no profile
        ownership of its own — the same reason ``creation_items`` and
        ``_require_scoped_creation_item`` join through
        ``sync_creation_attempts`` before reading one. The WRITE side needs
        that scoping at least as much: without it a caller that (by bug, not
        malice) passed another profile's item id would silently settle that
        profile's reviewed item. Returns whether a row in this profile was
        actually updated.
        """
        if state not in {"pending", "succeeded", "failed", "outcome_unknown"}:
            raise ValueError(f"Unsupported creation item state: {state}")
        assignments = ["state=?", "updated_at=?"]
        params: list[Any] = [state, _utc_now()]
        if destination_remote_id:
            assignments.append("destination_remote_id=?")
            params.append(destination_remote_id)
        params.extend([creation_item_id, profile_id])
        cursor = self.connection().execute(
            f"UPDATE sync_creation_items SET {','.join(assignments)} "
            "WHERE creation_item_id=? AND attempt_id IN "
            "(SELECT attempt_id FROM sync_creation_attempts WHERE profile_id=?)",
            tuple(params),
        )
        return cursor.rowcount == 1

    def create_provisional_pair(
        self, profile_id: int, mo_observation_id: int, inat_observation_id: int,
    ) -> int:
        """Gate 2A: a pair that exists only so pair-based services (reciprocal
        link add, final specimen verification) have something to operate
        against before the saga finalizes. Deliberately excluded from every
        candidate/confirmed dashboard listing until ``promote_provisional_pair``
        runs — see the 'unpaired' category exclusion and every
        ``review_state=='confirmed'`` gate elsewhere in this file."""
        now = _utc_now()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT pair_id FROM sync_pairs WHERE profile_id=? AND mo_observation_id=? "
                "AND inat_observation_id=?",
                (profile_id, mo_observation_id, inat_observation_id),
            ).fetchone()
            if existing:
                return int(existing["pair_id"])
            cursor = conn.execute(
                "INSERT INTO sync_pairs(profile_id,mo_observation_id,inat_observation_id,link_state,score,"
                "classification,review_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (profile_id, mo_observation_id, inat_observation_id, "", 0,
                 "gate_2a_creation", "provisional", now, now),
            )
            return int(cursor.lastrowid)

    def promote_provisional_pair(self, profile_id: int, pair_id: int) -> bool:
        """Promote exactly once, only from 'provisional' — never from any other
        state, so this can never be used to bypass normal pair confirmation."""
        now = _utc_now()
        try:
            with self.transaction() as conn:
                cursor = conn.execute(
                    "UPDATE sync_pairs SET review_state='confirmed',confirmed_by='observation_creation',"
                    "ever_reviewed=1,ever_confirmed=1,historical_confirmed_by='observation_creation',"
                    "updated_at=? WHERE profile_id=? AND pair_id=? AND review_state='provisional'",
                    (now, profile_id, pair_id),
                )
                return cursor.rowcount == 1
        except sqlite3.IntegrityError:
            # Another confirmed pair already claims this mo/inat id (the
            # partial unique indexes uq_sync_confirmed_mo/uq_sync_confirmed_inat)
            # — leave the pair provisional and let the caller surface a repair.
            return False

    def settle_pair_finalize_success(
        self, profile_id: int, action_id: int, action_group_id: int, pair_id: int,
        mo_observation_id: int, inat_observation_id: int,
    ) -> bool:
        """Round-4 finding 4: promote the provisional pair AND mark the
        ``pair_finalize`` action succeeded in ONE transaction, rechecking
        the pair's identity/exclusion/conflict state immediately beforehand.

        Before this existed, ``_execute_pair_finalize`` called
        ``promote_provisional_pair`` and ``finish_action`` as two separate
        statements/transactions — a crash between them left the pair
        confirmed but the finalize action still 'running' (claimed,
        unfinished), with no clean way for a resume to tell whether
        finalize itself had actually completed. Returns ``False`` (nothing
        written) if the pair no longer matches the expected identity, is
        excluded, is not (still) provisional, or a conflicting confirmed
        pair already claims one of these records — the caller then reports
        a failure rather than assuming success.

        Round-5 finding 4: this is now the authoritative finalization
        boundary (not merely an internal helper the caller could trust to
        already be correct), so it validates its OWN complete contract
        rather than only the pair — the action must exist, belong to the
        expected group, be a ``pair_finalize`` row, currently ``running``
        (i.e. actually claimed, not pending/already-terminal), and
        reference the SAME pair/mo/inat ids passed in. Any mismatch refuses
        cleanly (returns ``False``, nothing written) exactly like a pair
        mismatch already did.
        """
        now = _utc_now()
        with self.transaction() as conn:
            action = conn.execute(
                "SELECT action_id,action_group_id,action_type,state,pair_id,"
                "mo_observation_id,inat_observation_id FROM sync_actions "
                "WHERE profile_id=? AND action_id=?",
                (profile_id, action_id),
            ).fetchone()
            if (
                not action
                or int(action["action_group_id"]) != action_group_id
                or str(action["action_type"]) != "pair_finalize"
                or str(action["state"]) != "running"
                or int(action["pair_id"] or -1) != pair_id
                or int(action["mo_observation_id"] or 0) != mo_observation_id
                or int(action["inat_observation_id"] or 0) != inat_observation_id
            ):
                return False
            pair = conn.execute(
                "SELECT pair_id,mo_observation_id,inat_observation_id,review_state FROM sync_pairs "
                "WHERE profile_id=? AND pair_id=?", (profile_id, pair_id),
            ).fetchone()
            if (
                not pair
                or int(pair["mo_observation_id"]) != mo_observation_id
                or int(pair["inat_observation_id"]) != inat_observation_id
                or str(pair["review_state"]) != "provisional"
            ):
                return False
            if self.pair_is_excluded(profile_id, pair_id):
                return False
            conflict = conn.execute(
                "SELECT 1 FROM sync_pairs WHERE profile_id=? AND review_state='confirmed' AND "
                "((mo_observation_id=? AND inat_observation_id!=?) OR "
                "(inat_observation_id=? AND mo_observation_id!=?)) LIMIT 1",
                (profile_id, mo_observation_id, inat_observation_id,
                 inat_observation_id, mo_observation_id),
            ).fetchone()
            if conflict:
                return False
            # Post-review fix (section 4): finalization must be tied to the
            # EXACT creation attempt that produced this pair, not merely to
            # an action/pair pointing at the right ids. Require exactly one
            # sync_creation_attempts row owning this action group, belonging
            # to the same profile, whose identity's source/destination ids
            # agree with the mo/inat ids being finalized, and whose state is
            # the one and only state a successfully-written attempt reaches
            # ('succeeded' -- set once by settle_creation_write_success and
            # never touched again before finalize). Anything else (no
            # attempt, more than one, wrong profile, mismatched ids, or an
            # attempt not eligible for finalization) refuses cleanly with
            # nothing written yet.
            attempt_rows = conn.execute(
                "SELECT ca.attempt_id AS attempt_id, ca.profile_id AS profile_id, "
                "ca.state AS attempt_state, co.source_site AS source_site, "
                "co.source_observation_id AS source_observation_id, "
                "co.destination_site AS destination_site, "
                "co.destination_observation_id AS destination_observation_id, "
                "co.pair_id AS creation_pair_id "
                "FROM sync_creation_attempts ca "
                "JOIN sync_created_observations co ON co.creation_id = ca.creation_id "
                "WHERE ca.profile_id=? AND ca.action_group_id=?",
                (profile_id, action_group_id),
            ).fetchall()
            if len(attempt_rows) != 1:
                return False
            attempt = attempt_rows[0]
            if int(attempt["profile_id"]) != profile_id:
                return False
            if str(attempt["attempt_state"]) != "succeeded":
                return False
            # Defense in depth (Round-7 review): sync_created_observations
            # carries its OWN pair_id, set once by finish_observation_creation
            # when the destination identity was linked back to this pair (see
            # the "UPDATE sync_created_observations SET pair_id=?" call
            # above). The source/destination id checks below make an
            # accidental mismatch here unlikely on their own, but the
            # authoritative finalization transaction should still validate
            # this direct provenance link explicitly rather than rely on that
            # being merely implied -- refuse to promote (raise, rolling back
            # anything already written in this transaction) if it disagrees
            # with the pair actually being finalized.
            if attempt["creation_pair_id"] is None or int(attempt["creation_pair_id"]) != pair_id:
                raise RuntimeError(
                    f"settle_pair_finalize_success: sync_created_observations.pair_id "
                    f"({attempt['creation_pair_id']!r}) for action group {action_group_id} does not "
                    f"match the pair being finalized ({pair_id}); refusing to promote."
                )
            source_site = str(attempt["source_site"])
            destination_site = str(attempt["destination_site"])
            source_id = attempt["source_observation_id"]
            destination_id = attempt["destination_observation_id"]
            if source_id is None or destination_id is None:
                return False
            expected_mo = source_id if source_site == "mo" else (destination_id if destination_site == "mo" else None)
            expected_inat = destination_id if destination_site == "inat" else (source_id if source_site == "inat" else None)
            if expected_mo is None or expected_inat is None:
                return False
            if int(expected_mo) != mo_observation_id or int(expected_inat) != inat_observation_id:
                return False
            cursor = conn.execute(
                "UPDATE sync_pairs SET review_state='confirmed',confirmed_by='observation_creation',"
                "ever_reviewed=1,ever_confirmed=1,historical_confirmed_by='observation_creation',"
                "updated_at=? WHERE profile_id=? AND pair_id=? AND review_state='provisional'",
                (now, profile_id, pair_id),
            )
            if cursor.rowcount != 1:
                # Nothing has been written yet in this transaction -- safe
                # to report failure without a rollback-worthy exception.
                return False
            # From this point on, the pair has ALREADY been promoted inside
            # this open transaction. Section 4: never return False (a
            # "nothing happened" signal to the caller) once that write has
            # occurred -- either every remaining step also succeeds, or an
            # exception is raised so the whole transaction (including the
            # pair promotion) rolls back. Returning False here previously
            # risked committing a promoted pair with the finalize action
            # left un-succeeded: a genuine partial finalization.
            action_cursor = conn.execute(
                "UPDATE sync_actions SET state='succeeded',last_phase='verification',last_error_code='',"
                "last_http_status=NULL,outcome_unknown=0,finished_at=?,updated_at=? "
                "WHERE profile_id=? AND action_id=? AND state='running'",
                (now, now, profile_id, action_id),
            )
            if action_cursor.rowcount != 1:
                raise RuntimeError(
                    f"settle_pair_finalize_success: pair {pair_id} was promoted but the finalize "
                    f"action {action_id} update affected {action_cursor.rowcount} rows (expected 1); "
                    "rolling back the entire finalization rather than committing a partial state."
                )
            attempt_cursor = conn.execute(
                "UPDATE sync_creation_attempts SET state='succeeded',updated_at=? "
                "WHERE profile_id=? AND attempt_id=? AND state='succeeded'",
                (now, profile_id, int(attempt["attempt_id"])),
            )
            if attempt_cursor.rowcount != 1:
                raise RuntimeError(
                    f"settle_pair_finalize_success: pair {pair_id} was promoted and the finalize "
                    f"action succeeded but the creation attempt {int(attempt['attempt_id'])} update "
                    f"affected {attempt_cursor.rowcount} rows (expected 1); rolling back the entire "
                    "finalization rather than committing a partial state."
                )
            return True

    def mint_creation_followup_action(
        self, profile_id: int, group_id: int, ordinal: int, action_type: str, *,
        pair_id: int, mo_observation_id: int, inat_observation_id: int,
        inat_observation_uuid: str = "", site: str,
        source_site: str = "", source_record_id: Optional[int] = None,
        remote_row_id: str = "", remote_row_uuid: str = "",
        binding_id: Optional[int] = None,
        current_target_id: Optional[int] = None, desired_target_id: Optional[int] = None,
        source_photo_id: str = "", planned_observation_photo_uuid: str = "",
        reviewed_byte_fingerprint: str = "",
        sequence_fingerprint: str = "", normalized_accession: str = "", normalized_archive: str = "",
        source_metadata_fingerprint: str = "", destination_preflight_fingerprint: str = "",
        evidence_type: str = "",
        preview_inat_record_fingerprint: str = "", preview_mo_record_fingerprint: str = "",
        preview_inat_links_fingerprint: str = "", preview_mo_links_fingerprint: str = "",
    ) -> int:
        """Mint one ordinal row AFTER the destination id is known.

        This is how every per-item row (photo/identifier/sequence attach),
        reciprocal-link row, and the final ``pair_finalize`` row enter the
        saga — never at initial journal time, because none of them can
        satisfy the mo/inat-id-required CHECK before ordinal 0 succeeds. By
        the time this runs, both ids are real, so the row looks exactly like
        any other action of its type and the existing per-type service can
        execute it unmodified.

        The four ``preview_*_fingerprint`` columns matter specifically for
        minted ``mo_external_link_add``/``inat_ofv_add`` rows: the REUSED,
        unmodified ``LinkRepairService`` preflight compares these stored
        "preview" fingerprints against a fresh live re-read
        (``_matches_preview``) and refuses to write if they differ or are
        blank — so the caller must capture a live fingerprint snapshot
        (e.g. via ``LinkRepairService._refresh_state``) immediately before
        minting a link row and pass it here, exactly as a normal Gate 1B
        preview would. Leaving these blank (the default) is only correct for
        row types whose service does not consult them (photo/identifier/
        sequence attach, ``pair_finalize``).
        """
        with self.transaction() as conn:
            return self._insert_creation_followup_action(
                conn, profile_id, group_id, ordinal, action_type,
                pair_id=pair_id, mo_observation_id=mo_observation_id, inat_observation_id=inat_observation_id,
                inat_observation_uuid=inat_observation_uuid, site=site,
                source_site=source_site, source_record_id=source_record_id,
                remote_row_id=remote_row_id, remote_row_uuid=remote_row_uuid,
                binding_id=binding_id, current_target_id=current_target_id, desired_target_id=desired_target_id,
                source_photo_id=source_photo_id, planned_observation_photo_uuid=planned_observation_photo_uuid,
                reviewed_byte_fingerprint=reviewed_byte_fingerprint,
                sequence_fingerprint=sequence_fingerprint, normalized_accession=normalized_accession,
                normalized_archive=normalized_archive, source_metadata_fingerprint=source_metadata_fingerprint,
                destination_preflight_fingerprint=destination_preflight_fingerprint, evidence_type=evidence_type,
                preview_inat_record_fingerprint=preview_inat_record_fingerprint,
                preview_mo_record_fingerprint=preview_mo_record_fingerprint,
                preview_inat_links_fingerprint=preview_inat_links_fingerprint,
                preview_mo_links_fingerprint=preview_mo_links_fingerprint,
            )

    def _insert_creation_followup_action(
        self, conn: sqlite3.Connection, profile_id: int, group_id: int, ordinal: int, action_type: str, *,
        pair_id: int, mo_observation_id: int, inat_observation_id: int,
        inat_observation_uuid: str = "", site: str,
        source_site: str = "", source_record_id: Optional[int] = None,
        remote_row_id: str = "", remote_row_uuid: str = "",
        binding_id: Optional[int] = None,
        current_target_id: Optional[int] = None, desired_target_id: Optional[int] = None,
        source_photo_id: str = "", planned_observation_photo_uuid: str = "",
        reviewed_byte_fingerprint: str = "",
        sequence_fingerprint: str = "", normalized_accession: str = "", normalized_archive: str = "",
        source_metadata_fingerprint: str = "", destination_preflight_fingerprint: str = "",
        evidence_type: str = "",
        preview_inat_record_fingerprint: str = "", preview_mo_record_fingerprint: str = "",
        preview_inat_links_fingerprint: str = "", preview_mo_links_fingerprint: str = "",
    ) -> int:
        """The raw INSERT shared by ``mint_creation_followup_action`` (its own
        transaction) and ``mint_creation_item_action`` (folded into that
        method's single item-link transaction). Takes an already-open ``conn``
        — never opens its own transaction — so callers control atomicity."""
        now = _utc_now()
        deduplication_key = _local_fingerprint(
            profile_id, group_id, ordinal, action_type, mo_observation_id, inat_observation_id,
            source_photo_id, remote_row_id, sequence_fingerprint,
        )
        cursor = conn.execute(
                "INSERT INTO sync_actions(profile_id,action_group_id,ordinal,action_type,site,state,"
                "last_phase,pair_id,issue_id,mo_observation_id,inat_observation_id,inat_observation_uuid,"
                "binding_id,remote_row_id,remote_row_uuid,current_target_id,desired_target_id,destructive,"
                "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
                "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
                "source_site,source_record_id,source_photo_id,planned_observation_photo_uuid,"
                "reviewed_byte_fingerprint,sequence_fingerprint,normalized_accession,normalized_archive,"
                "source_metadata_fingerprint,destination_preflight_fingerprint,evidence_type,"
                "created_at,confirmed_at,updated_at) "
                "VALUES(:profile_id,:group_id,:ordinal,:action_type,:site,'pending','preview',"
                ":pair_id,NULL,:mo_id,:inat_id,:inat_uuid,:binding_id,:remote_id,:remote_uuid,"
                ":current_target,:desired_target,0,"
                ":preview_inat_fp,:preview_mo_fp,:preview_inat_links_fp,:preview_mo_links_fp,:dedupe,"
                ":source_site,:source_record_id,:source_photo_id,:planned_uuid,:reviewed_fp,"
                ":sequence_fp,:normalized_accession,:normalized_archive,:source_metadata_fp,"
                ":destination_fp,:evidence_type,:now,:now,:now)",
                {
                    "profile_id": profile_id, "group_id": group_id, "ordinal": ordinal,
                    "action_type": action_type, "site": site, "pair_id": pair_id,
                    "mo_id": mo_observation_id, "inat_id": inat_observation_id,
                    "inat_uuid": inat_observation_uuid, "binding_id": binding_id,
                    "preview_inat_fp": preview_inat_record_fingerprint,
                    "preview_mo_fp": preview_mo_record_fingerprint,
                    "preview_inat_links_fp": preview_inat_links_fingerprint,
                    "preview_mo_links_fp": preview_mo_links_fingerprint,
                    "remote_id": remote_row_id, "remote_uuid": remote_row_uuid,
                    "current_target": current_target_id, "desired_target": desired_target_id,
                    # source_site's CHECK only allows NULL or 'inat'/'mo' — an
                    # empty string satisfies neither and violates the
                    # constraint, which reciprocal-link/pair_finalize rows
                    # (Gate 2A) hit because they legitimately have no
                    # source_site to record. Bind NULL when unset.
                    "dedupe": deduplication_key, "source_site": source_site or None,
                    "source_record_id": source_record_id, "source_photo_id": source_photo_id,
                    "planned_uuid": planned_observation_photo_uuid,
                    "reviewed_fp": reviewed_byte_fingerprint,
                    "sequence_fp": sequence_fingerprint,
                    "normalized_accession": normalized_accession,
                    "normalized_archive": normalized_archive,
                    "source_metadata_fp": source_metadata_fingerprint,
                    "destination_fp": destination_preflight_fingerprint,
                    "evidence_type": evidence_type, "now": now,
                },
        )
        return int(cursor.lastrowid)

    def mint_creation_item_action(
        self, profile_id: int, group_id: int, creation_item_id: int, ordinal: int, action_type: str, *,
        pair_id: int, mo_observation_id: int, inat_observation_id: int,
        inat_observation_uuid: str = "", site: str,
        source_site: str = "", source_record_id: Optional[int] = None,
        remote_row_id: str = "", remote_row_uuid: str = "",
        binding_id: Optional[int] = None,
        current_target_id: Optional[int] = None, desired_target_id: Optional[int] = None,
        source_photo_id: str = "", planned_observation_photo_uuid: str = "",
        reviewed_byte_fingerprint: str = "",
        sequence_fingerprint: str = "", normalized_accession: str = "", normalized_archive: str = "",
        source_metadata_fingerprint: str = "", destination_preflight_fingerprint: str = "",
        evidence_type: str = "",
        preview_inat_record_fingerprint: str = "", preview_mo_record_fingerprint: str = "",
        preview_inat_links_fingerprint: str = "", preview_mo_links_fingerprint: str = "",
    ) -> int:
        """Atomically mint a per-item follow-up action AND link it to its
        ``sync_creation_items`` row, in one transaction.

        Replaces the previous two-call sequence
        (``mint_creation_followup_action`` then ``mark_creation_item_action``)
        for item rows specifically: a crash between those two calls could
        leave a pending action with no item linkage, and a restart could then
        mint a second action for the same item. Here, the existing-action
        check, the INSERT, and the item-linkage UPDATE all happen under one
        ``BEGIN IMMEDIATE`` — there is no interval in which the action exists
        without its item linkage, and a repeated/concurrent resume for the
        same item always finds and returns the already-linked action instead
        of minting a duplicate.
        """
        with self.transaction() as conn:
            existing = self._require_scoped_creation_item(conn, profile_id, group_id, creation_item_id)
            if existing["action_id"] is not None:
                return int(existing["action_id"])
            action_id = self._insert_creation_followup_action(
                conn, profile_id, group_id, ordinal, action_type,
                pair_id=pair_id, mo_observation_id=mo_observation_id, inat_observation_id=inat_observation_id,
                inat_observation_uuid=inat_observation_uuid, site=site,
                source_site=source_site, source_record_id=source_record_id,
                remote_row_id=remote_row_id, remote_row_uuid=remote_row_uuid,
                binding_id=binding_id, current_target_id=current_target_id, desired_target_id=desired_target_id,
                source_photo_id=source_photo_id, planned_observation_photo_uuid=planned_observation_photo_uuid,
                reviewed_byte_fingerprint=reviewed_byte_fingerprint,
                sequence_fingerprint=sequence_fingerprint, normalized_accession=normalized_accession,
                normalized_archive=normalized_archive, source_metadata_fingerprint=source_metadata_fingerprint,
                destination_preflight_fingerprint=destination_preflight_fingerprint, evidence_type=evidence_type,
                preview_inat_record_fingerprint=preview_inat_record_fingerprint,
                preview_mo_record_fingerprint=preview_mo_record_fingerprint,
                preview_inat_links_fingerprint=preview_inat_links_fingerprint,
                preview_mo_links_fingerprint=preview_mo_links_fingerprint,
            )
            conn.execute(
                "UPDATE sync_creation_items SET action_id=?,updated_at=? WHERE creation_item_id=?",
                (action_id, _utc_now(), creation_item_id),
            )
            return action_id

    def fail_creation_item_preflight(
        self, profile_id: int, group_id: int, creation_item_id: int, action_type: str, *,
        reason: str, next_ordinal: int, pair_id: int, mo_observation_id: int, inat_observation_id: int,
        site: str = "inat",
    ) -> dict[str, Any]:
        """Atomically fail one creation item's PREFLIGHT check -- i.e. record
        that it will never be attempted -- without ever downgrading a write
        that might already be running or ambiguous.

        Round-7 review finding: the previous call sequence checked an
        item's existing action state, then called ``mint_creation_item_action``
        (which SILENTLY returns an already-existing action id rather than
        raising) followed by an unconditional ``finish_action(..., "failed")``
        -- two separate statements/transactions with a gap between them. A
        concurrent worker could claim/start/complete that same action in the
        gap, and the caller would then overwrite it as 'failed' regardless.
        Everything here happens in ONE ``BEGIN IMMEDIATE`` transaction:

          * No action yet linked to the item: mint one and finish it as
            'failed' right here, atomically -- there is no window in which
            any other caller could observe or claim it first.
          * An action IS already linked: transition it to 'failed' ONLY via
            a conditional ``UPDATE ... WHERE state='pending' AND
            write_started_at IS NULL``, requiring exactly one affected row.
            If zero rows are affected (the action is no longer pending, or
            is still 'pending' but already carries a write_started_at
            marker -- which should be unreachable but is treated exactly
            like an in-flight write regardless), nothing is downgraded: the
            CURRENT state is read back and returned instead, so the caller
            can route to its verify/outcome_unknown path rather than pretend
            the item failed.

          * No action, and the item is ALREADY terminally resolved: nothing
            is minted at all and the item's real state is reported with
            ``action_id`` 0 -- an already-settled item must never acquire a
            brand-new, never-executed action row.

        Returns ``{"action_id": int, "downgraded": bool, "state": str}``.
        ``downgraded`` is True only when this call itself transitioned the
        action (or a freshly-minted one) to 'failed'; False means an
        existing action was left exactly as it was found -- ``state``
        reports what it actually is now (never fabricated as 'failed') --
        and ``action_id`` is 0 when no action exists or was created.
        """
        now = _utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT ci.*, ca.action_group_id AS attempt_action_group_id "
                "FROM sync_creation_items ci "
                "JOIN sync_creation_attempts ca ON ca.attempt_id = ci.attempt_id "
                "WHERE ci.creation_item_id=? AND ca.profile_id=?",
                (creation_item_id, profile_id),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"creation_item_id {creation_item_id} does not exist or does not belong to "
                    f"profile {profile_id}"
                )
            if int(row["attempt_action_group_id"]) != group_id:
                raise ValueError(
                    f"creation_item_id {creation_item_id} belongs to action group "
                    f"{row['attempt_action_group_id']}, not {group_id}"
                )
            existing_action_id = row["action_id"]
            if existing_action_id is None:
                if str(row["state"]) != "pending":
                    # Same rule ``_require_scoped_creation_item`` enforces for
                    # every other minting path: an item with no linked action
                    # that is already terminally resolved must never acquire
                    # one. Minting here would attach a brand-new, never-executed
                    # action to a settled item and repoint its ``action_id`` at
                    # a row that was not part of any real attempt (reachable
                    # via the ``pair_id`` fallback in
                    # ``_populate_items._fail_item_durably``, which resolves an
                    # item with ``finish_creation_item`` and leaves action_id
                    # NULL). Report the item's REAL state with no action id,
                    # exactly like the already-claimed case below.
                    return {
                        "action_id": 0, "downgraded": False,
                        "state": str(row["state"]),
                    }
                action_id = self._insert_creation_followup_action(
                    conn, profile_id, group_id, next_ordinal, action_type,
                    pair_id=pair_id, mo_observation_id=mo_observation_id,
                    inat_observation_id=inat_observation_id, site=site,
                )
                conn.execute(
                    "UPDATE sync_creation_items SET action_id=?,updated_at=? WHERE creation_item_id=?",
                    (action_id, now, creation_item_id),
                )
                fail_cursor = conn.execute(
                    "UPDATE sync_actions SET state='failed',last_phase='preflight',last_error_code=?,"
                    "finished_at=?,updated_at=? WHERE profile_id=? AND action_id=? AND state='pending'",
                    (reason[:80], now, now, profile_id, action_id),
                )
                if fail_cursor.rowcount != 1:
                    # Unreachable in practice -- this action was just minted
                    # inside this same still-open transaction, so nothing
                    # else could have touched it yet -- but never silently
                    # commit a mint whose failure-transition didn't actually
                    # take; raise (rolling back the mint too) instead of
                    # returning a misleading "downgraded" result.
                    raise RuntimeError(
                        f"fail_creation_item_preflight: freshly-minted action {action_id} failed-update "
                        f"affected {fail_cursor.rowcount} rows (expected 1)"
                    )
                conn.execute(
                    "UPDATE sync_creation_items SET state='failed',updated_at=? WHERE creation_item_id=?",
                    (now, creation_item_id),
                )
                return {"action_id": action_id, "downgraded": True, "state": "failed"}

            action_id = int(existing_action_id)
            fail_cursor = conn.execute(
                "UPDATE sync_actions SET state='failed',last_phase='preflight',last_error_code=?,"
                "finished_at=?,updated_at=? WHERE profile_id=? AND action_id=? AND state='pending' "
                "AND write_started_at IS NULL",
                (reason[:80], now, now, profile_id, action_id),
            )
            if fail_cursor.rowcount == 1:
                conn.execute(
                    "UPDATE sync_creation_items SET state='failed',updated_at=? WHERE creation_item_id=?",
                    (now, creation_item_id),
                )
                return {"action_id": action_id, "downgraded": True, "state": "failed"}
            # The conditional update affected zero rows: another worker
            # already claimed/started/finished this action between the
            # caller's earlier check and this call. Never downgrade it --
            # read back and report its ACTUAL current state instead.
            current = conn.execute(
                "SELECT state FROM sync_actions WHERE profile_id=? AND action_id=?",
                (profile_id, action_id),
            ).fetchone()
            current_state = str(current["state"]) if current else "failed"
            return {"action_id": action_id, "downgraded": False, "state": current_state}

    def _require_scoped_creation_item(
        self, conn: sqlite3.Connection, profile_id: int, group_id: int, creation_item_id: int,
    ) -> sqlite3.Row:
        """Round-3 finding 6: joined through ``sync_creation_attempts`` and
        checked against BOTH ``profile_id`` and ``group_id`` — a bare
        ``creation_item_id`` is an auto-increment integer with no ownership
        of its own, so without this join a caller that (by bug) passed the
        wrong profile_id or the wrong group_id would silently mint an action
        for another profile's, or another attempt's, reviewed item. Also
        requires the item still be 'pending' (not already terminally
        resolved) and the attempt be the one ``creation_ledger_for_group``
        would resolve for this group (i.e. not a superseded prior attempt).
        """
        row = conn.execute(
            "SELECT ci.*, ca.action_group_id AS attempt_action_group_id "
            "FROM sync_creation_items ci "
            "JOIN sync_creation_attempts ca ON ca.attempt_id = ci.attempt_id "
            "WHERE ci.creation_item_id=? AND ca.profile_id=?",
            (creation_item_id, profile_id),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"creation_item_id {creation_item_id} does not exist or does not belong to profile "
                f"{profile_id}"
            )
        if int(row["attempt_action_group_id"]) != group_id:
            raise ValueError(
                f"creation_item_id {creation_item_id} belongs to action group "
                f"{row['attempt_action_group_id']}, not {group_id}"
            )
        if row["action_id"] is None and str(row["state"]) != "pending":
            raise ValueError(
                f"creation_item_id {creation_item_id} is '{row['state']}', not pending, and has no "
                "linked action"
            )
        return row

    def mint_creation_photo_item_action(
        self, profile_id: int, group_id: int, creation_item_id: int, ordinal: int, *,
        pair_id: int, mo_observation_id: int, inat_observation_id: int, source_site: str,
        source_photo_id: str, planned_observation_photo_uuid: str, reviewed_byte_fingerprint: str,
        byte_fingerprint: str, md5: str, source_license_label: str, source_copyright_holder: str,
        preview_inat_record_fingerprint: str, preview_mo_record_fingerprint: str,
    ) -> int:
        """Finding 8: a Gate 2A photo item must enter Gate 1E's journaling
        contract completely, not partially. ``mint_creation_item_action``
        alone left ``reviewed_byte_fingerprint`` blank (so
        ``PhotoSyncService._execute``'s byte-pinning check failed OPEN — a
        stable MO photo id whose content changed after review would upload
        silently) and created no ``sync_photo_transfers`` row at all (so
        ``finish_photo_transfer`` was a silent zero-row UPDATE and no
        license/copyright-holder decision or duplicate-ledger entry was ever
        recorded). This mints the action AND the transfer-ledger row
        atomically, in the SAME transaction as the item linkage — exactly
        the same one-crash-safe discipline as ``mint_creation_item_action``.

        The transfer row records ``planned_observation_photo_uuid`` too, for
        the same reason ``journal_photo_actions`` does: it is the only token
        that ties a transfer to the observation_photo it created, and it must
        be durable BEFORE the upload. Leaving it blank here (and blanking it
        on a failed-row reuse) left the Gate 2A ledger unable to say which
        remote row any of its transfers produced.
        """
        with self.transaction() as conn:
            existing = self._require_scoped_creation_item(conn, profile_id, group_id, creation_item_id)
            if existing["action_id"] is not None:
                return int(existing["action_id"])
            now = _utc_now()
            # Round-3 finding 5: never let ON CONFLICT overwrite an existing
            # transfer row — that would disconnect it from its own action,
            # rewrite the provenance of a successful or unknown transfer, and
            # exclude the OLD transfer from duplicate detection as though it
            # were this new action's own row. Mirrors journal_photo_actions'
            # (Gate 1E) proven pattern exactly: an existing row that is not
            # definitively 'failed' blocks outright; only a 'failed' row is
            # explicitly reset and reused (the UNIQUE constraint on
            # (profile,source_site,source_photo_id,destination_site,
            # destination_observation_id) otherwise makes a legitimate retry
            # impossible without inventing a second identity for the same
            # source->destination photo).
            existing_transfer = conn.execute(
                "SELECT transfer_id,state FROM sync_photo_transfers WHERE profile_id=? AND source_site=? "
                "AND source_photo_id=? AND destination_site='inat' AND destination_observation_id=?",
                (profile_id, source_site, source_photo_id, inat_observation_id),
            ).fetchone()
            if existing_transfer and str(existing_transfer["state"]) != "failed":
                raise ValueError(
                    f"Photo {source_photo_id} already has transfer record "
                    f"{int(existing_transfer['transfer_id'])} in state '{existing_transfer['state']}' for "
                    "this destination observation — resume/verify that transfer rather than minting a new one."
                )
            action_id = self._insert_creation_followup_action(
                conn, profile_id, group_id, ordinal, "inat_photo_attach",
                pair_id=pair_id, mo_observation_id=mo_observation_id, inat_observation_id=inat_observation_id,
                site="inat", source_site=source_site, source_photo_id=source_photo_id,
                planned_observation_photo_uuid=planned_observation_photo_uuid,
                reviewed_byte_fingerprint=reviewed_byte_fingerprint,
                preview_inat_record_fingerprint=preview_inat_record_fingerprint,
                preview_mo_record_fingerprint=preview_mo_record_fingerprint,
            )
            conn.execute(
                "UPDATE sync_creation_items SET action_id=?,updated_at=? WHERE creation_item_id=?",
                (action_id, now, creation_item_id),
            )
            if existing_transfer:
                # Definitively failed: reset and reuse the SAME row (never a
                # second row for the same source->destination photo).
                conn.execute(
                    "UPDATE sync_photo_transfers SET pair_id=?,action_id=?,destination_photo_id='',"
                    "destination_observation_photo_uuid=?,byte_fingerprint=?,md5=?,"
                    "source_license_label=?,source_copyright_holder=?,destination_license_code='',"
                    "state='pending',updated_at=? WHERE transfer_id=?",
                    (pair_id, action_id, planned_observation_photo_uuid, byte_fingerprint, md5,
                     source_license_label, source_copyright_holder, now,
                     int(existing_transfer["transfer_id"])),
                )
            else:
                conn.execute(
                    "INSERT INTO sync_photo_transfers(profile_id,pair_id,action_id,source_site,source_photo_id,"
                    "destination_site,destination_observation_id,destination_observation_photo_uuid,"
                    "byte_fingerprint,md5,source_license_label,"
                    "source_copyright_holder,state,created_at,updated_at) VALUES "
                    "(?,?,?,?,?,'inat',?,?,?,?,?,?,'pending',?,?)",
                    (profile_id, pair_id, action_id, source_site, source_photo_id, inat_observation_id,
                     planned_observation_photo_uuid, byte_fingerprint, md5, source_license_label,
                     source_copyright_holder, now, now),
                )
            return action_id

    # Gate 1D name-proposal tracking -----------------------------------

    def record_name_delegation(
        self, profile_id: int, pair_id: int, identify_action_id: int,
    ) -> int:
        """Link a confirmed pair to its delegated Identify action id only.

        The proposed taxon, name, and intent live in the Identify journal and are
        deliberately not duplicated here. Idempotent by ``identify_action_id`` so
        a retry after a partially-failed delegation never double-records.
        """
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO sync_name_delegations(profile_id,pair_id,"
                "identify_action_id,created_at,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(profile_id,pair_id,identify_action_id) DO UPDATE SET "
                "updated_at=excluded.updated_at",
                (profile_id, pair_id, identify_action_id, now, now),
            )
            row = conn.execute(
                "SELECT delegation_id FROM sync_name_delegations "
                "WHERE profile_id=? AND pair_id=? AND identify_action_id=?",
                (profile_id, pair_id, identify_action_id),
            ).fetchone()
            return int(row["delegation_id"]) if row else 0

    def name_delegation_for_pair(
        self, profile_id: int, pair_id: int,
    ) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_name_delegations WHERE profile_id=? AND pair_id=? "
            "ORDER BY updated_at DESC, delegation_id DESC LIMIT 1",
            (profile_id, pair_id),
        ).fetchone()
        return dict(row) if row else None

    def record_mo_proposal(
        self, profile_id: int, pair_id: int, mo_observation_id: int,
        proposed_name: str, proposed_name_id: Optional[int], current_effective_name: str,
    ) -> int:
        """Create or update an MO proposal tracking row as a fresh pending draft.

        Re-recording an existing (profile,pair,proposed_name) row resets it fully
        to pending: a row previously marked ``effective`` must not retain that
        status (or its ``became_effective_at`` / reserved submission fields) once
        the consensus has moved and a new draft is recorded.
        """
        now = _utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_mo_proposals(profile_id,pair_id,mo_observation_id,proposed_name,"
                "proposed_name_id,current_effective_name,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(profile_id,pair_id,proposed_name) DO UPDATE SET "
                "proposed_name_id=excluded.proposed_name_id,"
                "current_effective_name=excluded.current_effective_name,"
                "status='pending',became_effective_at='',"
                "proposal_submitted=0,proposal_remote_id='',submitted_at='',"
                "updated_at=excluded.updated_at",
                (profile_id, pair_id, mo_observation_id, proposed_name,
                 proposed_name_id, current_effective_name, now, now),
            )
            row = conn.execute(
                "SELECT proposal_id FROM sync_mo_proposals WHERE profile_id=? AND pair_id=? AND proposed_name=?",
                (profile_id, pair_id, proposed_name),
            ).fetchone()
            return int(row["proposal_id"]) if row else int(cursor.lastrowid)

    def update_mo_proposal_status(
        self, profile_id: int, proposal_id: int, status: str,
        current_effective_name: str = "", became_effective_at: str = "",
    ) -> None:
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sync_mo_proposals SET status=?,current_effective_name=?,"
                "became_effective_at=CASE WHEN ?<>'' THEN ? ELSE became_effective_at END,updated_at=? "
                "WHERE profile_id=? AND proposal_id=?",
                (status, current_effective_name, became_effective_at, became_effective_at,
                 now, profile_id, proposal_id),
            )

    def mo_proposal_for_pair(
        self, profile_id: int, pair_id: int,
    ) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_mo_proposals WHERE profile_id=? AND pair_id=? "
            "ORDER BY updated_at DESC, proposal_id DESC LIMIT 1",
            (profile_id, pair_id),
        ).fetchone()
        return dict(row) if row else None

    def mo_proposal(self, profile_id: int, proposal_id: int) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_mo_proposals WHERE profile_id=? AND proposal_id=?",
            (profile_id, proposal_id),
        ).fetchone()
        return dict(row) if row else None

    def action(self, profile_id: int, action_id: int) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_actions WHERE profile_id=? AND action_id=?",
            (profile_id, action_id),
        ).fetchone()
        return dict(row) if row else None

    def action_detail(self, profile_id: int, action_id: int) -> Optional[dict[str, Any]]:
        result = self.action(profile_id, action_id)
        if not result:
            return None
        group_id = int(result["action_group_id"])
        result["group_actions"] = self.action_group_rows(profile_id, group_id)
        result["preview_rows"] = self.action_snapshot_rows(profile_id, group_id)
        return result

    def action_group_rows(self, profile_id: int, group_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection().execute(
            "SELECT * FROM sync_actions WHERE profile_id=? AND action_group_id=? ORDER BY ordinal,action_id",
            (profile_id, group_id),
        ).fetchall()]

    def action_group(self, profile_id: int, group_id: int) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_action_groups WHERE profile_id=? AND action_group_id=?",
            (profile_id, group_id),
        ).fetchone()
        return dict(row) if row else None

    def action_snapshot_rows(self, profile_id: int, group_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection().execute(
            "SELECT * FROM sync_action_snapshot_rows WHERE profile_id=? AND action_group_id=? "
            "ORDER BY site,observation_id,remote_row_id,remote_row_uuid",
            (profile_id, group_id),
        ).fetchall()]

    def unresolved_action_groups(self, profile_id: int) -> list[int]:
        return [int(row[0]) for row in self.connection().execute(
            "SELECT DISTINCT action_group_id FROM sync_actions WHERE profile_id=? "
            "AND state IN ('pending','running','outcome_unknown') ORDER BY action_group_id",
            (profile_id,),
        ).fetchall()]

    def claim_action(self, profile_id: int, action_id: int, phase: str) -> bool:
        now = _utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE sync_actions SET state='running',last_phase=?,attempt_count=attempt_count+1,"
                "attempt_started_at=?,updated_at=?,last_error_code='',outcome_unknown=0 "
                "WHERE profile_id=? AND action_id=? AND state='pending'",
                (phase, now, now, profile_id, action_id),
            )
            return cursor.rowcount == 1

    def release_finalize_action_to_pending(self, profile_id: int, action_id: int) -> bool:
        """Return a claimed ``pair_finalize`` row to 'pending' so a resume can
        retry it. Returns True when the row was actually released.

        Round-8 review finding: every failure inside ``_execute_pair_finalize``
        used to settle 'failed', including a purely transient one (either side
        momentarily unreadable, a 5xx, a dropped connection). Nothing in this
        module ever moves an action back to 'pending', and ``claim_action``
        only ever claims a 'pending' row, so a single transient blip left a
        REAL created remote observation permanently half-finished: reciprocally
        linked, but with its pair stuck at ``review_state='provisional'``
        (hidden from every dashboard listing by design) and no way to retry —
        ``journal_observation_creation_actions`` also refuses a fresh attempt
        once the identity has a ``destination_observation_id``.

        Deliberately restricted to ``pair_finalize``, and additionally to a row
        with NO ``write_started_at``: finalize performs no remote write at all
        (it only re-reads both sides and makes a local promote-or-not
        decision), so re-running it is provably free of duplicate-write risk
        under any interleaving. No other action type may use this — for
        anything that writes remotely, an unfinished row is ambiguous and must
        go through ``verify_unknown``, never a blind retry.
        """
        now = _utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE sync_actions SET state='pending',last_phase='preview',updated_at=? "
                "WHERE profile_id=? AND action_id=? AND action_type='pair_finalize' "
                "AND state='running' AND write_started_at IS NULL",
                (now, profile_id, action_id),
            )
            return cursor.rowcount == 1

    def mark_action_write_started(self, profile_id: int, action_id: int) -> bool:
        """Stamp the durable write boundary; True only when it actually took.

        ``write_started_at IS NULL`` is what ``recover_running_actions``,
        ``_mark_action_groups_stale_if_written``, and
        ``fail_creation_item_preflight``'s
        ``WHERE state='pending' AND write_started_at IS NULL`` guard all read
        as "nothing was ever sent". A silent zero-row UPDATE here — the row is
        no longer 'running' because something else already recovered, cancelled,
        or settled it — would therefore let an unsafe write go out carrying the
        never-written marker, and a later resume would happily send it again.
        So this reports whether the marker landed, exactly like its destructive
        twin ``mark_deletion_write_started``, and every caller must refuse to
        write when it returns False.
        """
        now = _utc_now()
        cursor = self.connection().execute(
            "UPDATE sync_actions SET last_phase='unsafe_write',write_started_at=?,updated_at=? "
            "WHERE profile_id=? AND action_id=? AND state='running'",
            (now, now, profile_id, action_id),
        )
        return cursor.rowcount == 1

    def clear_action_write_boundary(self, profile_id: int, action_id: int) -> bool:
        """Give back a write marker for a request that provably never went out.

        The ONLY legitimate caller is a cancellation raised by a client before
        it issued the request — ``MOClient._write`` performs its rate-limit wait
        and its final ``cancelled()`` check strictly before
        ``self._client.request``, so ``ReconciliationCancelled`` from there means
        no bytes left this process. Without this, such a cancel settled the row
        'cancelled' while still carrying ``write_started_at``, which every
        staleness check reads as "a write may have been applied": it marked the
        pair ``stale_after_remote_write`` and opened a refresh issue for a
        request that was never sent.

        Restricted to a still-'running' row so it can never rewind a settled
        action, and never a row whose write actually completed (those settle
        through verification, not here).
        """
        now = _utc_now()
        cursor = self.connection().execute(
            "UPDATE sync_actions SET write_started_at=NULL,last_phase='resource_preflight',"
            "updated_at=? WHERE profile_id=? AND action_id=? AND state='running' "
            "AND write_started_at IS NOT NULL",
            (now, profile_id, action_id),
        )
        return cursor.rowcount == 1

    def finish_action(
        self, profile_id: int, action_id: int, state: str, *, phase: str,
        error_code: str = "", http_status: Optional[int] = None,
        verification_state: str = "", server_row_id: str = "", server_row_uuid: str = "",
    ) -> bool:
        """Record one terminal outcome for a still-resolvable action.

        Only 'pending', 'running' and 'outcome_unknown' rows may transition:
        'outcome_unknown' is included because verify_unknown legitimately
        settles an ambiguous write once its remote state is proven. An
        already-settled row is never rewritten. Without that guard a second
        call — e.g. a failure raised after the first finish_action already
        committed, landing in an outer ``except`` handler that finishes the
        same action again — could downgrade a verified 'succeeded' or an
        ambiguous 'outcome_unknown' to a definitive 'failed', which is exactly
        the hazard fail_creation_item_preflight was introduced to avoid.

        Returns True when this call performed the transition. False means the
        row was missing, belonged to another profile, or had already settled;
        the caller's view of the outcome is then stale rather than authoritative.
        """
        if state not in {"succeeded", "failed", "cancelled", "outcome_unknown"}:
            raise ValueError("Invalid terminal action state")
        now = _utc_now()
        cursor = self.connection().execute(
            "UPDATE sync_actions SET state=?,last_phase=?,last_error_code=?,last_http_status=?,"
            "verification_state=?,verified_at=CASE WHEN ?!='' THEN ? ELSE verified_at END,"
            "server_row_id=?,server_row_uuid=?,outcome_unknown=?,finished_at=?,updated_at=? "
            "WHERE profile_id=? AND action_id=? "
            "AND state IN ('pending','running','outcome_unknown')",
            (state, phase, error_code[:80], http_status, verification_state,
             verification_state, now, server_row_id, server_row_uuid,
             int(state == "outcome_unknown"), now, now, profile_id, action_id),
        )
        if cursor.rowcount == 1:
            return True
        log.warning(
            "finish_action(%s) did not apply to action %s of profile %s: "
            "the row is missing or already settled",
            state, action_id, profile_id,
        )
        return False

    def cancel_pending_action(self, profile_id: int, action_id: int) -> bool:
        now = _utc_now()
        group_id: Optional[int] = None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT action_group_id FROM sync_actions WHERE profile_id=? AND action_id=? AND state='pending'",
                (profile_id, action_id),
            ).fetchone()
            if not row:
                return False
            group_id = int(row["action_group_id"])
            conn.execute(
                "UPDATE sync_actions SET state='cancelled',last_error_code='user_cancelled_group',"
                "finished_at=?,updated_at=? WHERE profile_id=? AND action_group_id=? AND state='pending'",
                (now, now, profile_id, group_id),
            )
        self._mark_action_groups_stale_if_written(((profile_id, group_id),))
        return True

    def cancel_action_group_tail(
        self, profile_id: int, group_id: int, after_ordinal: int, error_code: str,
    ) -> int:
        now = _utc_now()
        cursor = self.connection().execute(
            "UPDATE sync_actions SET state='cancelled',last_error_code=?,last_phase='preview',"
            "finished_at=?,updated_at=? WHERE profile_id=? AND action_group_id=? "
            "AND ordinal>? AND state='pending'",
            (error_code[:80], now, now, profile_id, group_id, after_ordinal),
        )
        self._mark_action_groups_stale_if_written(((profile_id, group_id),))
        return cursor.rowcount

    def fail_pending_action_and_cancel_tail(
        self, profile_id: int, group_id: int, action_id: int, ordinal: int,
        error_code: str,
    ) -> None:
        """Record a deterministic whole-group preflight failure atomically."""
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sync_actions SET state='failed',last_phase='account_preflight',"
                "last_error_code=?,finished_at=?,updated_at=? WHERE profile_id=? "
                "AND action_group_id=? AND action_id=? AND state='pending'",
                (error_code[:80], now, now, profile_id, group_id, action_id),
            )
            conn.execute(
                "UPDATE sync_actions SET state='cancelled',last_phase='preview',"
                "last_error_code='predecessor_group_preflight_failed',finished_at=?,updated_at=? "
                "WHERE profile_id=? AND action_group_id=? AND ordinal>? AND state='pending'",
                (now, now, profile_id, group_id, ordinal),
            )
        self._mark_action_groups_stale_if_written(((profile_id, group_id),))

    def cancel_pending_actions(
        self, profile_ids: Iterable[int], error_code: str,
    ) -> int:
        ids = tuple(sorted({int(value) for value in profile_ids if int(value) > 0}))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        groups = [
            (int(row[0]), int(row[1]))
            for row in self.connection().execute(
                "SELECT DISTINCT profile_id,action_group_id FROM sync_actions "
                f"WHERE state='pending' AND profile_id IN ({placeholders})",
                ids,
            ).fetchall()
        ]
        now = _utc_now()
        cursor = self.connection().execute(
            "UPDATE sync_actions SET state='cancelled',last_error_code=?,finished_at=?,updated_at=? "
            f"WHERE state='pending' AND profile_id IN ({placeholders})",
            (error_code[:80], now, now, *ids),
        )
        self._mark_action_groups_stale_if_written(groups)
        return cursor.rowcount

    def cancel_pending_groups_for_site(
        self, profile_id: int, site: str, error_code: str,
    ) -> int:
        if site not in {"inat", "mo"}:
            raise ValueError("Invalid action site")
        groups = [
            (profile_id, int(row[0]))
            for row in self.connection().execute(
                "SELECT DISTINCT action_group_id FROM sync_actions WHERE profile_id=? AND site=? "
                "AND state IN ('pending','running','outcome_unknown')",
                (profile_id, site),
            ).fetchall()
        ]
        now = _utc_now()
        cursor = self.connection().execute(
            "UPDATE sync_actions SET state='cancelled',last_error_code=?,finished_at=?,updated_at=? "
            "WHERE profile_id=? AND state='pending' AND action_group_id IN "
            "(SELECT action_group_id FROM sync_actions WHERE profile_id=? AND site=? "
            "AND state IN ('pending','running','outcome_unknown'))",
            (error_code[:80], now, now, profile_id, profile_id, site),
        )
        self._mark_action_groups_stale_if_written(groups)
        return cursor.rowcount

    def recover_running_actions(self) -> int:
        groups = [
            (int(row[0]), int(row[1]))
            for row in self.connection().execute(
                "SELECT g.profile_id,g.action_group_id FROM sync_action_groups g "
                "WHERE EXISTS (SELECT 1 FROM sync_actions written "
                "WHERE written.profile_id=g.profile_id AND written.action_group_id=g.action_group_id "
                "AND written.write_started_at IS NOT NULL) AND NOT EXISTS ("
                "SELECT 1 FROM sync_runs r WHERE r.profile_id=g.profile_id "
                "AND r.outcome IN ('success','succeeded') AND r.finished_at IS NOT NULL "
                "AND r.finished_at >= (SELECT MAX(written_again.write_started_at) "
                "FROM sync_actions written_again WHERE written_again.profile_id=g.profile_id "
                "AND written_again.action_group_id=g.action_group_id))"
            ).fetchall()
        ]
        now = _utc_now()
        # A claimed action is only ambiguous once its write boundary was
        # crossed. claim_action sets state='running' before the preflight
        # re-read, which does network I/O and is the longest window in which
        # the process can die, so treating every interrupted 'running' row as
        # outcome_unknown marks writes that provably never left this process
        # as possibly-applied. That is not merely pessimistic: it keeps the
        # row's deduplication_key held by uq_sync_unresolved_action, flips the
        # owning sync_consolidation_attempts row below, and trips both
        # journal_consolidation_attempt's outcome_unknown=1 guard and v17's
        # trg_deletion_attempt_blocks_phase2b_activity — none of which clear
        # without a remote verify_unknown round trip.
        #
        # Split on write_started_at exactly as normalize_running_deletion_action
        # does for the Gate 2C ledger, and as _migration_v10 already does when
        # classifying a creation action interrupted mid-attempt.
        pre_write = self.connection().execute(
            "UPDATE sync_actions SET state='pending',outcome_unknown=0,"
            "attempt_started_at=NULL,finished_at=NULL,"
            "last_error_code='interrupted_before_write',last_phase='preview',"
            "updated_at=? WHERE state='running' AND write_started_at IS NULL",
            (now,),
        )
        post_write = self.connection().execute(
            "UPDATE sync_actions SET state='outcome_unknown',outcome_unknown=1,"
            "last_error_code='interrupted_during_execution',finished_at=?,updated_at=? "
            "WHERE state='running' AND write_started_at IS NOT NULL",
            (now, now),
        )
        recovered = int(pre_write.rowcount) + int(post_write.rowcount)
        self.connection().execute(
            "UPDATE sync_consolidation_attempts SET state='outcome_unknown',updated_at=? "
            "WHERE state='pending' AND EXISTS (SELECT 1 FROM sync_actions a "
            "WHERE a.profile_id=sync_consolidation_attempts.profile_id "
            "AND a.action_group_id=sync_consolidation_attempts.action_group_id "
            "AND a.state='outcome_unknown')",
            (now,),
        )
        self._mark_action_groups_stale_if_written(groups)
        return recovered

    def _mark_action_groups_stale_if_written(
        self, groups: Iterable[tuple[int, int]],
    ) -> None:
        """Retain a visible refresh requirement for every possibly written group."""
        for profile_id, group_id in sorted(set(groups)):
            row = self.connection().execute(
                "SELECT g.mo_observation_id,g.inat_observation_id FROM sync_action_groups g "
                "WHERE g.profile_id=? AND g.action_group_id=? AND EXISTS ("
                "SELECT 1 FROM sync_actions a WHERE a.profile_id=g.profile_id "
                "AND a.action_group_id=g.action_group_id AND a.write_started_at IS NOT NULL "
                "AND a.action_type IN ('inat_ofv_add','inat_ofv_repair','inat_ofv_remove',"
                "'mo_external_link_add','mo_external_link_repair','mo_external_link_remove'))",
                (profile_id, group_id),
            ).fetchone()
            if row:
                self.mark_link_reconciliation_stale(
                    profile_id, int(row["mo_observation_id"]),
                    int(row["inat_observation_id"]),
                )
            # ITS action groups keep a separate, ITS-specific refresh requirement.
            self.mark_its_reconciliation_stale(profile_id, group_id)

    def advance_pending_action_fingerprints(
        self, profile_id: int, group_id: int, live_state: object,
    ) -> None:
        """Advance only pending siblings after a verified write from their group.

        This keeps per-action optimistic concurrency strict while allowing the
        next selected action to observe the verified result of an earlier one.
        """
        now = _utc_now()
        self.connection().execute(
            "UPDATE sync_actions SET preview_inat_record_fingerprint=?,"
            "preview_mo_record_fingerprint=?,preview_inat_links_fingerprint=?,"
            "preview_mo_links_fingerprint=?,updated_at=? WHERE profile_id=? "
            "AND action_group_id=? AND state='pending'",
            (
                str(getattr(live_state, "inat_record_fingerprint")),
                str(getattr(live_state, "mo_record_fingerprint")),
                str(getattr(live_state, "inat_links_fingerprint")),
                str(getattr(live_state, "mo_links_fingerprint")),
                now, profile_id, group_id,
            ),
        )

    def refresh_authoritative_link_rows(self, profile_id: int, live_state: object) -> None:
        """Apply a verified, targeted Gate 1B link-resource reread locally."""
        now = _utc_now()
        with self.transaction() as conn:
            for site, observation_id, rows in (
                ("inat", int(getattr(live_state, "inat_observation_id")), getattr(live_state, "inat_rows")),
                ("mo", int(getattr(live_state, "mo_observation_id")), getattr(live_state, "mo_rows")),
            ):
                conn.execute(
                    "DELETE FROM sync_links WHERE profile_id=? AND source_site=? AND source_observation_id=?",
                    (profile_id, site, observation_id),
                )
                malformed = False
                for row in rows:
                    malformed = malformed or str(row.parse_state) != "valid"
                    target_site = "mo" if site == "inat" else "inat"
                    conn.execute(
                        "INSERT INTO sync_links(profile_id,link_row_id,source_site,source_observation_id,"
                        "target_site,target_observation_id,direction,link_state,external_site_id,parse_state,fingerprint) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (profile_id, str(row.row_uuid or row.row_id), site, observation_id,
                         target_site, row.target_observation_id, f"{site}_to_{target_site}",
                         str(row.parse_state), row.binding_id, str(row.parse_state),
                         str(row.row_fingerprint)),
                    )
                conn.execute(
                    "UPDATE sync_records SET link_malformed=?,last_seen_at=? WHERE profile_id=? "
                    "AND site=? AND remote_observation_id=?",
                    (int(malformed), now, profile_id, site, observation_id),
                )

    def mark_link_reconciliation_stale(
        self, profile_id: int, mo_observation_id: int, inat_observation_id: int,
    ) -> None:
        """Make post-write dashboard staleness explicit until read-only reconciliation."""
        fingerprint = _local_fingerprint(
            "link_state_refresh_required", mo_observation_id, inat_observation_id,
            _utc_now(),
        )
        with self.transaction() as conn:
            # ``link_state`` and ``updated_at`` are two of the six columns
            # pair_source_fingerprint() covers, so marking staleness silently
            # invalidated the source_fingerprint of every action group built
            # from this pair — including the group whose own write triggered
            # this call. Any later resume then died in _require_current_source
            # with "The confirmed pair changed after preview", which stranded
            # the still-pending tail of a partially executed group (the exact
            # state the recover/resume button exists to finish). Advance the
            # stored fingerprint in the same transaction, and ONLY for a group
            # still holding the pre-update value: a group previewed against a
            # genuinely different pair state keeps its stale fingerprint and
            # still fails closed.
            pair = conn.execute(
                "SELECT * FROM sync_pairs WHERE profile_id=? AND mo_observation_id=? "
                "AND inat_observation_id=?",
                (profile_id, mo_observation_id, inat_observation_id),
            ).fetchone()
            previous_source = pair_source_fingerprint(pair) if pair else ""
            conn.execute(
                "UPDATE sync_pairs SET link_state='stale_after_remote_write',updated_at=? "
                "WHERE profile_id=? AND mo_observation_id=? AND inat_observation_id=?",
                (_utc_now(), profile_id, mo_observation_id, inat_observation_id),
            )
            if pair is not None:
                updated = conn.execute(
                    "SELECT * FROM sync_pairs WHERE profile_id=? AND pair_id=?",
                    (profile_id, int(pair["pair_id"])),
                ).fetchone()
                if updated is not None:
                    conn.execute(
                        "UPDATE sync_action_groups SET source_fingerprint=?,updated_at=? "
                        "WHERE profile_id=? AND pair_id=? AND source_fingerprint=?",
                        (
                            pair_source_fingerprint(updated), _utc_now(),
                            profile_id, int(pair["pair_id"]), previous_source,
                        ),
                    )
            self._upsert_issue_tx(
                conn, profile_id, "link_state_refresh_required", "warning",
                f"Read-only link refresh required: MO {mo_observation_id} ↔ iNat {inat_observation_id}",
                "A verified Gate 1B write changed authoritative link state. Run a read-only scan "
                "before relying on pair or issue categories.",
                fingerprint, (("mo", mo_observation_id), ("inat", inat_observation_id)),
            )

    def mark_its_reconciliation_stale(self, profile_id: int, group_id: int) -> None:
        """Raise a durable ITS refresh requirement for any possibly written group.

        Applies to every ITS action group that has a ``write_started_at`` marker,
        covering startup recovery, crash interruption, direct unknown-outcome
        verification, manual cancellation after a write, and journal errors after
        a verified write. No raw sequence data is stored in the issue.
        """
        row = self.connection().execute(
            "SELECT g.mo_observation_id,g.inat_observation_id FROM sync_action_groups g "
            "WHERE g.profile_id=? AND g.action_group_id=? AND EXISTS ("
            "SELECT 1 FROM sync_actions a WHERE a.profile_id=g.profile_id "
            "AND a.action_group_id=g.action_group_id AND a.write_started_at IS NOT NULL "
            "AND a.action_type IN ('inat_its_add','inat_its_repair','inat_its_remove',"
            "'mo_sequence_add','mo_sequence_repair'))",
            (profile_id, group_id),
        ).fetchone()
        if not row:
            return
        mo_observation_id = int(row["mo_observation_id"])
        inat_observation_id = int(row["inat_observation_id"])
        # Stable fingerprint: keyed on the pair and the latest ITS write. Repeated
        # marks for the same write keep the same fingerprint (they do not churn the
        # issue), while a genuinely newer write bumps it and reopens the issue.
        latest_write = self.connection().execute(
            "SELECT MAX(a.write_started_at) FROM sync_actions a JOIN sync_action_groups g "
            "ON g.profile_id=a.profile_id AND g.action_group_id=a.action_group_id "
            "WHERE a.profile_id=? AND g.mo_observation_id=? AND g.inat_observation_id=? "
            "AND a.write_started_at IS NOT NULL AND a.action_type IN ("
            "'inat_its_add','inat_its_repair','inat_its_remove','mo_sequence_add','mo_sequence_repair')",
            (profile_id, mo_observation_id, inat_observation_id),
        ).fetchone()[0]
        fingerprint = _local_fingerprint(
            "its_state_refresh_required", mo_observation_id, inat_observation_id, latest_write or "",
        )
        title = (
            f"Read-only ITS refresh required: MO {mo_observation_id} ↔ iNat {inat_observation_id}"
        )
        # If this exact write was already resolved by a fresh comparison, a repeat
        # mark for the same write must not reopen it; only a newer write (which
        # changes the fingerprint) reopens.
        existing = self.connection().execute(
            "SELECT fingerprint,state FROM sync_issues WHERE profile_id=? "
            "AND issue_type='its_state_refresh_required' AND title=?",
            (profile_id, title),
        ).fetchone()
        if existing and str(existing["state"]) == "resolved" and str(existing["fingerprint"]) == fingerprint:
            return
        with self.transaction() as conn:
            self._upsert_issue_tx(
                conn, profile_id, "its_state_refresh_required", "warning", title,
                "A Gate 1C ITS write may have changed remote ITS state for this confirmed pair. Run a "
                "fresh ITS comparison or read-only reconciliation refresh before relying on ITS evidence.",
                fingerprint, (("mo", mo_observation_id), ("inat", inat_observation_id)),
            )

    def resolve_its_reconciliation_stale(
        self, profile_id: int, mo_observation_id: int, inat_observation_id: int,
    ) -> None:
        """Resolve the ITS refresh issue once a fresh comparison rebuilt the evidence."""
        title = (
            f"Read-only ITS refresh required: MO {mo_observation_id} ↔ iNat {inat_observation_id}"
        )
        now = _utc_now()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sync_issues SET state='resolved',updated_at=? WHERE profile_id=? "
                "AND issue_type='its_state_refresh_required' AND title=? AND state!='resolved'",
                (now, profile_id, title),
            )

    # Dashboard --------------------------------------------------------

    def dashboard_count(self, profile_id: int, category: str) -> int:
        sql, params = self._dashboard_query(profile_id, category, count=True)
        return int(self.connection().execute(sql, params).fetchone()[0])

    def dashboard_rows(
        self, profile_id: int, category: str, offset: int, limit: int,
        *, sort_column: str = "updated_at", descending: bool = True,
    ) -> list[dict[str, Any]]:
        sql, params = self._dashboard_query(profile_id, category, count=False)
        # Must stay in step with ReconciliationTableModel.KEYS: a key the model
        # offers as a sortable column but that is missing here is silently
        # replaced by updated_at, so the header paints a sort indicator on a
        # column the rows are not actually ordered by. Every dashboard branch
        # projects all of these.
        allowed = {
            "updated_at", "kind", "site", "remote_id", "other_id",
            "score", "state", "title",
        }
        column = sort_column if sort_column in allowed else "updated_at"
        sql += f" ORDER BY {column} {'DESC' if descending else 'ASC'}, row_key LIMIT ? OFFSET ?"
        rows = self.connection().execute(sql, (*params, int(limit), int(offset))).fetchall()
        return [dict(row) for row in rows]

    def _dashboard_query(self, profile_id: int, category: str, *, count: bool) -> tuple[str, tuple[Any, ...]]:
        projection = "COUNT(*)" if count else "*"
        if category == "consolidation_history":
            sql = (
                f"SELECT {projection} FROM (SELECT 'consolidation:'||c.consolidation_id AS row_key,"
                "'consolidation' AS kind,NULL AS issue_id,c.canonical_pair_id AS pair_id,"
                "NULL AS action_id,'mo' AS site,c.canonical_mo_observation_id AS remote_id,"
                "c.canonical_inat_observation_id AS other_id,'inat' AS other_site,NULL AS score,"
                "c.state AS state,'Duplicate consolidation #'||c.consolidation_id AS title,"
                # "Retained online" is only true of donors Gate 2C has NOT
                # deleted remotely. Counting every role='donor' row would
                # assert that irreversibly deleted observations still exist.
                "(SELECT COUNT(*) FROM sync_consolidation_members m "
                " WHERE m.consolidation_id=c.consolidation_id AND m.role='donor' "
                " AND COALESCE(m.remote_state,'online')='online')||"
                "' donor observation(s) retained online'||"
                "CASE WHEN (SELECT COUNT(*) FROM sync_consolidation_members m2 "
                " WHERE m2.consolidation_id=c.consolidation_id AND m2.role='donor' "
                " AND m2.remote_state='deleted')>0 THEN ', '||"
                "(SELECT COUNT(*) FROM sync_consolidation_members m3 "
                " WHERE m3.consolidation_id=c.consolidation_id AND m3.role='donor' "
                " AND m3.remote_state='deleted')||' deleted remotely' ELSE '' END "
                "AS detail,c.updated_at "
                "FROM sync_consolidations c WHERE c.profile_id=?)"
            )
            return sql, (profile_id,)
        if category == "link_actions":
            # Gate 2C donor deletions are journaled in sync_deletion_actions,
            # NOT sync_actions, so reading only sync_actions would leave the
            # single most destructive operation in the app with no row in the
            # journal users read to audit what was written. They carry their
            # own ``kind`` because they are cancelled/recovered through the
            # deletion entry points, never through cancel_pending_action or
            # verify-unknown, which address sync_actions.action_id — an id
            # space that collides with deletion_action_id.
            sql = (
                f"SELECT {projection} FROM (SELECT 'action:'||action_id AS row_key,'action' AS kind,"
                "NULL AS issue_id,pair_id,action_id,site,"
                "CASE WHEN site='mo' THEN mo_observation_id ELSE inat_observation_id END AS remote_id,"
                "CASE WHEN site='mo' THEN inat_observation_id ELSE mo_observation_id END AS other_id,"
                "CASE WHEN site='mo' THEN 'inat' ELSE 'mo' END AS other_site,NULL AS score,state,"
                "action_type AS title,verification_state||CASE WHEN last_error_code!='' THEN ' — '||last_error_code ELSE '' END AS detail,"
                "updated_at FROM sync_actions WHERE profile_id=? "
                "UNION ALL "
                "SELECT 'deletion_action:'||deletion_action_id,'deletion_action',"
                "NULL,NULL,deletion_action_id,site,"
                "observation_id,NULL,NULL,NULL,state,"
                "action_type,verification_state||CASE WHEN last_error_code!='' THEN ' — '||last_error_code ELSE '' END,"
                "updated_at FROM sync_deletion_actions WHERE profile_id=?)"
            )
            return sql, (profile_id, profile_id)
        if category in {"link_issues", "same_site_duplicates", "changed_deleted", "ignored_resolved"}:
            where = {
                "link_issues": (
                    "state='open' AND (issue_type LIKE '%link%' "
                    "OR issue_type='mo_external_site_configuration')"
                ),
                "same_site_duplicates": "state='open' AND issue_type='same_site_duplicate'",
                "changed_deleted": "state='open' AND issue_type IN ('record_changed','record_deleted')",
                "ignored_resolved": "state IN ('ignored','resolved')",
            }[category]
            sql = (
                f"SELECT {projection} FROM (SELECT 'issue:'||i.issue_id AS row_key,'issue' AS kind,i.issue_id,"
                "NULL AS pair_id,"
                "COALESCE((SELECT ir.site FROM sync_issue_records ir WHERE ir.profile_id=i.profile_id "
                "AND ir.issue_id=i.issue_id ORDER BY ir.site,ir.observation_id LIMIT 1),'') AS site,"
                "(SELECT ir.observation_id FROM sync_issue_records ir WHERE ir.profile_id=i.profile_id "
                "AND ir.issue_id=i.issue_id ORDER BY ir.site,ir.observation_id LIMIT 1) AS remote_id,"
                "(SELECT ir.observation_id FROM sync_issue_records ir WHERE ir.profile_id=i.profile_id "
                "AND ir.issue_id=i.issue_id ORDER BY ir.site,ir.observation_id LIMIT 1 OFFSET 1) AS other_id,"
                "(SELECT ir.site FROM sync_issue_records ir WHERE ir.profile_id=i.profile_id "
                "AND ir.issue_id=i.issue_id ORDER BY ir.site,ir.observation_id LIMIT 1 OFFSET 1) AS other_site,"
                "NULL AS score,i.state,i.title,i.detail,i.updated_at FROM sync_issues i "
                "WHERE i.profile_id=? AND " + where + " AND NOT EXISTS ("
                "SELECT 1 FROM sync_issue_records sir JOIN sync_consolidation_members cm "
                "ON cm.profile_id=sir.profile_id AND cm.site=sir.site "
                "AND cm.observation_id=sir.observation_id "
                "WHERE sir.profile_id=i.profile_id AND sir.issue_id=i.issue_id "
                "AND cm.local_state='superseded'))"
            )
            return sql, (profile_id,)
        if category in {"candidate_pairs", "confirmed_links", "confirmed_conflicts", "rejected_excluded"}:
            where = {
                "candidate_pairs": "review_state='candidate'",
                "confirmed_links": "review_state='confirmed' AND link_state='link_confirmed'",
                "confirmed_conflicts": "link_state='link_confirmed_with_metadata_conflicts'",
                # Correlate on the ``p`` alias, never the bare table name, and
                # keep the OR wrapped: this predicate is spliced between two
                # AND terms (the profile scope and the superseded-member
                # exclusion), so an unparenthesised OR would silently drop the
                # profile scope from its right-hand branch.
                "rejected_excluded": (
                    "(review_state='rejected' OR EXISTS (SELECT 1 FROM sync_pair_exclusions e "
                    "WHERE e.profile_id=p.profile_id "
                    "AND e.mo_observation_id=p.mo_observation_id "
                    "AND e.inat_observation_id=p.inat_observation_id))"
                ),
            }[category]
            sql = (
                f"SELECT {projection} FROM (SELECT 'pair:'||pair_id AS row_key,'pair' AS kind,NULL AS issue_id,"
                "pair_id,'mo' AS site,mo_observation_id AS remote_id,inat_observation_id AS other_id,'inat' AS other_site,score,"
                "review_state AS state,classification||' candidate' AS title,link_state AS detail,updated_at "
                "FROM sync_pairs p WHERE p.profile_id=? AND " + where + " AND NOT EXISTS ("
                "SELECT 1 FROM sync_consolidation_members cm WHERE cm.profile_id=p.profile_id "
                "AND cm.local_state='superseded' AND "
                "((cm.site='mo' AND cm.observation_id=p.mo_observation_id) OR "
                "(cm.site='inat' AND cm.observation_id=p.inat_observation_id))))"
            )
            return sql, (profile_id,)
        # Unpaired is intentionally phrased as possibly missing.
        #
        # Gate 2A: a record with a 'provisional' pair (an in-flight
        # missing-observation creation saga, not yet finalized) must ALSO be
        # excluded here, or it would confusingly still show as "unpaired" and
        # invite a second creation on top of the one already in progress. See
        # the saga-architecture-rules memory, rule 2.
        # Keep the two sites in separate UNION branches. The former single
        # correlated subquery used an OR between mo_observation_id and
        # inat_observation_id. SQLite could not use one useful index for both,
        # so a 30k-record inventory repeatedly scanned the 10k-row pair table
        # while the GUI synchronously refreshed its counts. Confirming one pair
        # consequently froze the whole window for a minute or more.
        row_projection = (
            "site||':'||remote_observation_id AS row_key,'record' AS kind,"
            "NULL AS issue_id,NULL AS pair_id,site,remote_observation_id AS remote_id,"
            "NULL AS other_id,NULL AS other_site,NULL AS score,"
            "unpaired_state AS state,taxon_name AS title,public_locality AS detail,"
            "COALESCE(remote_updated_at,last_seen_at) AS updated_at "
        )
        common = (
            "scope_state='in_scope' AND NOT EXISTS ("
            "SELECT 1 FROM sync_consolidation_members cm "
            "WHERE cm.profile_id=r.profile_id AND cm.site=r.site "
            "AND cm.observation_id=r.remote_observation_id "
            "AND cm.local_state='superseded')"
        )
        inner = (
            f"SELECT {row_projection}FROM sync_records r "
            f"WHERE profile_id=? AND site='mo' AND {common} AND NOT EXISTS ("
            "SELECT 1 FROM sync_pairs p WHERE p.profile_id=r.profile_id "
            "AND p.mo_observation_id=r.remote_observation_id "
            "AND p.review_state IN ('confirmed','provisional')) "
            "UNION ALL "
            f"SELECT {row_projection}FROM sync_records r "
            f"WHERE profile_id=? AND site='inat' AND {common} AND NOT EXISTS ("
            "SELECT 1 FROM sync_pairs p WHERE p.profile_id=r.profile_id "
            "AND p.inat_observation_id=r.remote_observation_id "
            "AND p.review_state IN ('confirmed','provisional'))"
        )
        return f"SELECT {projection} FROM ({inner})", (profile_id, profile_id)

    def issue_detail(self, profile_id: int, issue_id: int) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_issues WHERE profile_id=? AND issue_id=?",
            (profile_id, issue_id),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["records"] = [dict(item) for item in self.connection().execute(
            "SELECT ir.site,ir.observation_id,r.taxon_name,r.observed_on,r.public_locality,"
            "r.fungi_status,r.scope_state,r.is_deleted FROM sync_issue_records ir "
            "LEFT JOIN sync_records r ON r.profile_id=ir.profile_id AND r.site=ir.site "
            "AND r.remote_observation_id=ir.observation_id "
            "WHERE ir.profile_id=? AND ir.issue_id=? ORDER BY ir.site,ir.observation_id",
            (profile_id, issue_id),
        ).fetchall()]
        return result

    def record_detail(self, profile_id: int, site: str, observation_id: int) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_records WHERE profile_id=? AND site=? AND remote_observation_id=?",
            (profile_id, site, observation_id),
        ).fetchone()
        return dict(row) if row else None

    def inventory_records(
        self, profile_id: int, site: str,
        *, scope_states: tuple[str, ...] = ("in_scope", "linked_context"),
    ) -> list[InventoryObservation]:
        from datetime import date, datetime
        from .types import MediaIdentity, RemoteRecordKey, RemoteSite

        placeholders = ",".join("?" for _ in scope_states)
        rows = self.connection().execute(
            f"SELECT * FROM sync_records r WHERE profile_id=? AND site=? "
            f"AND scope_state IN ({placeholders}) AND NOT EXISTS ("
            "SELECT 1 FROM sync_consolidation_members cm "
            "WHERE cm.profile_id=r.profile_id AND cm.site=r.site "
            "AND cm.observation_id=r.remote_observation_id "
            "AND cm.local_state='superseded')",
            (profile_id, site, *scope_states),
        ).fetchall()
        links_by_record: dict[int, list[sqlite3.Row]] = {}
        for row in self.connection().execute(
            "SELECT * FROM sync_links WHERE profile_id=? AND source_site=?",
            (profile_id, site),
        ).fetchall():
            links_by_record.setdefault(int(row["source_observation_id"]), []).append(row)
        identifiers_by_record: dict[int, list[sqlite3.Row]] = {}
        for row in self.connection().execute(
            "SELECT observation_id,identifier_type,normalized_value,evidence_tier FROM sync_identifiers "
            "WHERE profile_id=? AND site=?",
            (profile_id, site),
        ).fetchall():
            identifiers_by_record.setdefault(int(row["observation_id"]), []).append(row)
        media_by_record: dict[int, list[sqlite3.Row]] = {}
        for row in self.connection().execute(
            "SELECT observation_id,photo_id,rendition,source_site,source_photo_id FROM sync_media_hashes "
            "WHERE profile_id=? AND site=?",
            (profile_id, site),
        ).fetchall():
            media_by_record.setdefault(int(row["observation_id"]), []).append(row)
        sequences_by_record: dict[int, list[tuple[str, int]]] = {}
        for row in self.connection().execute(
            "SELECT observation_id,sequence_hash,evidence_tier FROM sync_sequence_hashes WHERE profile_id=? AND site=?",
            (profile_id, site),
        ).fetchall():
            sequences_by_record.setdefault(int(row["observation_id"]), []).append(
                (str(row["sequence_hash"]), int(row["evidence_tier"]))
            )
        results: list[InventoryObservation] = []
        for row in rows:
            observation_id = int(row["remote_observation_id"])
            links = links_by_record.get(observation_id, [])
            identifiers = identifiers_by_record.get(observation_id, [])
            media_rows = media_by_record.get(observation_id, [])
            results.append(InventoryObservation(
                key=RemoteRecordKey(RemoteSite(site), observation_id),
                account_id=int(row["account_id"]), owner_id=row["owner_id"], owner_login=row["owner_login"],
                observed_on=date.fromisoformat(row["observed_on"]) if row["observed_on"] else None,
                taxon_id=row["taxon_id"], taxon_name=row["taxon_name"], taxon_rank=row["taxon_rank"],
                public_locality=row["public_locality"], fungi_status=row["fungi_status"],
                updated_at=datetime.fromisoformat(row["remote_updated_at"]) if row["remote_updated_at"] else None,
                deleted=bool(row["is_deleted"]), scope_state=str(row["scope_state"]),
                availability_state=str(row["availability_state"]),
                content_fingerprint=row["content_fingerprint"],
                authoritative_targets=tuple(sorted({
                    int(item["target_observation_id"]) for item in links
                    if item["target_observation_id"] is not None
                })),
                link_malformed=bool(row["link_malformed"]),
                authoritative_links=tuple(
                    AuthoritativeLinkRow(
                        str(item["link_row_id"]), item["external_site_id"],
                        RemoteSite(str(item["target_site"])), item["target_observation_id"],
                        str(item["parse_state"]), str(item["fingerprint"]),
                    ) for item in links
                ),
                identifiers=tuple((str(item["identifier_type"]), str(item["normalized_value"])) for item in identifiers),
                inventory_identifiers=tuple(
                    (str(item["identifier_type"]), str(item["normalized_value"]))
                    for item in identifiers if int(item["evidence_tier"]) == 1
                ),
                sequence_hashes=tuple(sorted({
                    digest for digest, _tier in sequences_by_record.get(observation_id, ())
                })),
                inventory_sequence_hashes=tuple(sorted({
                    digest for digest, tier in sequences_by_record.get(observation_id, ()) if tier == 2
                })),
                media=tuple(MediaIdentity(
                    RemoteSite(site), str(item["photo_id"]), str(item["rendition"]),
                    RemoteSite(str(item["source_site"])) if item["source_site"] else None,
                    str(item["source_photo_id"] or ""),
                ) for item in media_rows),
            ))
        return results

    def pair_detail(self, profile_id: int, pair_id: int) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_pairs WHERE profile_id=? AND pair_id=?", (profile_id, pair_id)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["excluded"] = self.pair_is_excluded(profile_id, pair_id)
        result["evidence"] = [dict(item) for item in self.connection().execute(
            "SELECT evidence_type,family,score,tier,explanation FROM sync_evidence "
            "WHERE profile_id=? AND pair_id=? ORDER BY score DESC", (profile_id, pair_id)
        ).fetchall()]
        return result

    def pair_by_records(self, profile_id: int, mo_id: int, inat_id: int) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT pair_id FROM sync_pairs WHERE profile_id=? AND mo_observation_id=? AND inat_observation_id=?",
            (profile_id, mo_id, inat_id),
        ).fetchone()
        return self.pair_detail(profile_id, int(row[0])) if row else None

    def pair_snapshots(self, profile_id: int) -> dict[tuple[int, int], dict[str, Any]]:
        """Load pair review state and evidence in two queries for worker planning."""
        pairs = {
            (int(row["mo_observation_id"]), int(row["inat_observation_id"])): dict(row)
            for row in self.connection().execute(
                "SELECT * FROM sync_pairs WHERE profile_id=?", (profile_id,)
            ).fetchall()
        }
        by_id = {int(value["pair_id"]): value for value in pairs.values()}
        for value in pairs.values():
            value["evidence"] = []
        for row in self.connection().execute(
            "SELECT pair_id,evidence_type,family,score,tier,explanation FROM sync_evidence "
            "WHERE profile_id=?", (profile_id,),
        ).fetchall():
            pair = by_id.get(int(row["pair_id"]))
            if pair is not None:
                pair["evidence"].append(dict(row))
        return pairs

    def confirmed_pair_keys(self, profile_id: int) -> list[tuple[int, int]]:
        return [
            (int(row[0]), int(row[1]))
            for row in self.connection().execute(
                "SELECT mo_observation_id,inat_observation_id FROM sync_pairs "
                "WHERE profile_id=? AND review_state='confirmed'", (profile_id,),
            ).fetchall()
        ]

    def confirmed_pair_conflict(
        self, profile_id: int, mo_observation_id: int, inat_observation_id: int,
    ) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT pair_id,mo_observation_id,inat_observation_id FROM sync_pairs "
            "WHERE profile_id=? AND review_state='confirmed' AND "
            "((mo_observation_id=? AND inat_observation_id!=?) OR "
            "(inat_observation_id=? AND mo_observation_id!=?)) LIMIT 1",
            (profile_id, mo_observation_id, inat_observation_id,
             inat_observation_id, mo_observation_id),
        ).fetchone()
        return dict(row) if row else None

    # Gate 2B M2/M3: consolidation identity/membership accessors ----------

    def consolidation_membership_for_observation(
        self, profile_id: int, site: str, observation_id: int,
    ) -> Optional[dict[str, Any]]:
        """Membership of this record in ANY consolidation, if one exists.

        This includes finalized superseded donors and unresolved attempt-only
        proposals. Superseded donors stay in the stable history ledger;
        pending/outcome-unknown proposals remain temporarily reserved without
        being admitted. The pre-check gives M2 a clear error instead of relying
        on the schema's
        global UNIQUE(
        profile_id,site,observation_id) on ``sync_consolidation_members`` to
        raise ``IntegrityError`` at insert time."""
        row = self.connection().execute(
            "SELECT m.consolidation_id,m.role,m.local_state,c.state AS consolidation_state "
            "FROM sync_consolidation_members m "
            "JOIN sync_consolidations c ON c.consolidation_id=m.consolidation_id "
            "WHERE m.profile_id=? AND m.site=? AND m.observation_id=?",
            (profile_id, site, observation_id),
        ).fetchone()
        if row:
            return dict(row)
        proposed = self.connection().execute(
            "SELECT p.consolidation_id,'donor' AS role,'proposed' AS local_state,"
            "c.state AS consolidation_state,p.state AS attempt_state "
            "FROM sync_unresolved_consolidation_proposals p "
            "JOIN sync_consolidations c "
            "ON c.consolidation_id=p.consolidation_id "
            "WHERE p.profile_id=? AND p.site=? AND p.observation_id=? "
            "ORDER BY CASE p.state WHEN 'outcome_unknown' THEN 0 ELSE 1 END "
            "LIMIT 1",
            (profile_id, site, observation_id),
        ).fetchone()
        return dict(proposed) if proposed else None

    # ``retryable_consolidation_for_members`` was removed here: it required
    # every requested member to already have a sync_consolidation_members row,
    # but its only caller reached it exclusively in the branch where
    # consolidation_membership_for_observation had returned None for ALL of
    # them — i.e. where no such rows exist. It therefore returned None on every
    # possible input. Retrying a failed draft/confirmed consolidation is
    # handled in ConsolidationService._build_preview, on the branch where the
    # members do still carry their membership rows.

    def create_consolidation_with_canonical(
        self, profile_id: int, members: Sequence[tuple[str, int]],
        canonical_mo_observation_id: Optional[int],
        canonical_inat_observation_id: Optional[int],
    ) -> int:
        """Create a consolidation identity (state='draft') and its member
        rows in one transaction. ``sync_consolidations`` requires at least
        one non-NULL canonical column (schema CHECK), matching M3's model
        where the canonical choice is part of forming the identity itself.
        Only canonical rows are stable at this stage. Noncanonical values in
        ``members`` are deliberately not admitted; the production journaling
        path snapshots proposed donors at attempt scope. Raises
        ``sqlite3.IntegrityError`` unchanged if a canonical member already
        belongs to another stable consolidation (the schema-level backstop) —
        callers should run
        ``consolidation_membership_for_observation`` first for a clearer
        pre-check error, per M2.
        """
        if canonical_mo_observation_id is None and canonical_inat_observation_id is None:
            raise ValueError("At least one canonical observation id is required")
        now = _utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO sync_consolidations(profile_id,canonical_mo_observation_id,"
                "canonical_inat_observation_id,canonical_pair_id,state,created_at,updated_at) "
                "VALUES(?,?,?,NULL,'draft',?,?)",
                (profile_id, canonical_mo_observation_id, canonical_inat_observation_id, now, now),
            )
            consolidation_id = int(cursor.lastrowid)
            for site, observation_id in members:
                role = "canonical" if (
                    (site == "mo" and observation_id == canonical_mo_observation_id)
                    or (site == "inat" and observation_id == canonical_inat_observation_id)
                ) else "donor"
                if role != "canonical":
                    continue
                conn.execute(
                    "INSERT INTO sync_consolidation_members(consolidation_id,profile_id,site,"
                    "observation_id,role,remote_uuid,reviewed_record_fingerprint,local_state,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,'','',?,?,?)",
                    (
                        consolidation_id, profile_id, site, observation_id,
                        role, "canonical", now, now,
                    ),
                )
        return consolidation_id

    def get_consolidation(self, profile_id: int, consolidation_id: int) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_consolidations WHERE profile_id=? AND consolidation_id=?",
            (profile_id, consolidation_id),
        ).fetchone()
        return dict(row) if row else None

    def list_consolidation_members(self, profile_id: int, consolidation_id: int) -> list[dict[str, Any]]:
        rows = self.connection().execute(
            "SELECT * FROM sync_consolidation_members WHERE profile_id=? AND consolidation_id=? "
            "ORDER BY consolidation_member_id",
            (profile_id, consolidation_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def consolidation_attempt_members(
        self, profile_id: int, attempt_id: int,
    ) -> list[dict[str, Any]]:
        rows = self.connection().execute(
            "SELECT am.attempt_member_id,am.stable_member_id,"
            "a.consolidation_id,a.profile_id,am.site,am.observation_id,"
            "am.remote_uuid,"
            "CASE am.participation_role WHEN 'canonical_context' THEN 'canonical' "
            "ELSE 'donor' END AS role,"
            "CASE WHEN am.participation_role='canonical_context' THEN "
            "COALESCE(m.local_state,'active') "
            "WHEN a.state='succeeded' THEN 'superseded' ELSE 'proposed' END "
            "AS local_state,"
            "m.added_by_attempt_id,m.superseded_by_attempt_id,m.superseded_at,"
            "am.participation_role,am.proposal_state,"
            "am.reviewed_record_fingerprint AS attempt_record_fingerprint,"
            "am.preflight_record_fingerprint AS attempt_preflight_fingerprint,"
            "am.reviewed_account_identity,am.reviewed_owner_account_id,"
            "am.reviewed_owner_login,am.reviewed_identity_fingerprint,"
            "am.reviewed_mutable_components "
            "FROM sync_consolidation_attempt_members am "
            "JOIN sync_consolidation_attempts a ON a.attempt_id=am.attempt_id "
            "LEFT JOIN sync_consolidation_members m "
            "ON m.consolidation_member_id=am.stable_member_id "
            "WHERE a.profile_id=? AND am.attempt_id=? "
            "ORDER BY am.attempt_member_id",
            (profile_id, attempt_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def consolidation_evidence(
        self, profile_id: int, attempt_id: int,
    ) -> list[dict[str, Any]]:
        rows = self.connection().execute(
            "SELECT e.*,lm.site AS left_site,lm.observation_id AS left_observation_id,"
            "rm.site AS right_site,rm.observation_id AS right_observation_id "
            "FROM sync_consolidation_evidence e "
            "JOIN sync_consolidation_attempts a ON a.attempt_id=e.attempt_id "
            "JOIN sync_consolidation_attempt_members lm "
            "ON lm.attempt_member_id=e.left_attempt_member_id "
            "AND lm.attempt_id=e.attempt_id "
            "JOIN sync_consolidation_attempt_members rm "
            "ON rm.attempt_member_id=e.right_attempt_member_id "
            "AND rm.attempt_id=e.attempt_id "
            "WHERE a.profile_id=? AND e.attempt_id=? "
            "ORDER BY e.left_attempt_member_id,e.right_attempt_member_id,"
            "e.evidence_type",
            (profile_id, attempt_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def cancel_consolidation(self, profile_id: int, consolidation_id: int) -> None:
        """User-cancelled preview (M11 scenario 4): no journal rows exist yet
        at this point in the flow, so this simply marks the draft cancelled
        rather than deleting it — provenance of "a set was proposed and
        cancelled" is kept, matching the immutable-history spirit used
        elsewhere in Phase 2A/2B."""
        self.connection().execute(
            "UPDATE sync_consolidations SET state='cancelled',updated_at=? "
            "WHERE profile_id=? AND consolidation_id=? AND state='draft'",
            (_utc_now(), profile_id, consolidation_id),
        )

    def journal_consolidation_attempt(
        self, preview: ConsolidationPreview,
    ) -> tuple[int, int, int]:
        """Persist one explicitly approved immutable consolidation plan.

        Remote-write rows are deliberately not minted here. The owning
        service creates each link action from its own fresh destination
        snapshot immediately before that one action executes.
        """
        if not preview.eligibility.eligible or preview.eligibility.blocking_reasons:
            raise ValueError("An ineligible duplicate set cannot be journaled")
        if any(item.blocking for item in preview.conflicts):
            raise ValueError("Resolve every blocking consolidation conflict before approval")
        if not preview.members:
            raise ValueError("A consolidation requires members")
        sites = {member.site for member in preview.members}
        if RemoteSite.MO in sites and preview.canonical_mo_observation_id is None:
            raise ValueError("Select the canonical Mushroom Observer observation")
        if RemoteSite.INAT in sites and preview.canonical_inat_observation_id is None:
            raise ValueError("Select the canonical iNaturalist observation")
        canonical_keys = {
            (RemoteSite.MO, preview.canonical_mo_observation_id),
            (RemoteSite.INAT, preview.canonical_inat_observation_id),
        }
        member_keys = {(member.site, member.observation_id) for member in preview.members}
        if len(member_keys) != len(preview.members):
            raise ValueError("The durable plan contains duplicate members")
        if any(
            observation_id is not None and (site, observation_id) not in member_keys
            for site, observation_id in canonical_keys
        ):
            raise ValueError("A selected canonical observation is not in the reviewed set")
        if any(
            not member.record_fingerprint
            or not member.preflight_fingerprint
            or not member.identity_fingerprint
            or member.owner_id <= 0
            or member.owner_id != member.account_id
            or not member.owner_login
            or member.identity_fingerprint
            != canonical_stable_identity_fingerprint(
                member.site,
                member.observation_id,
                member.remote_uuid,
                member.owner_id,
            )
            for member in preview.members
        ):
            raise ValueError(
                "Every consolidation member requires consistent reviewed, "
                "preflight, and numeric-owner identity fingerprints"
            )
        if not preview.evidence_edges:
            raise ValueError("A consolidation requires an immutable evidence graph")
        graph_validation = validate_consolidation_graph(
            list(member_keys),
            preview.evidence_edges,
            canonical_mo_id=preview.canonical_mo_observation_id,
            canonical_inat_id=preview.canonical_inat_observation_id,
        )
        if not graph_validation.valid:
            raise ValueError(
                "The durable evidence graph is invalid: "
                + "; ".join(graph_validation.reasons)
            )
        if len(graph_validation.donor_paths) != len(preview.donor_members):
            raise ValueError(
                "The durable graph does not contain a strong-anchored path "
                "for every donor"
            )

        now = _utc_now()
        import uuid as uuidlib
        correlation_marker = f"consolidation:{uuidlib.uuid4()}"
        member_fingerprints = sorted(
            (member.site.value, member.observation_id, member.record_fingerprint)
            for member in preview.members
        )
        pair_fingerprint = _local_fingerprint(
            "consolidation_pair",
            preview.canonical_mo_observation_id,
            preview.canonical_inat_observation_id,
            *("|".join(map(str, item)) for item in member_fingerprints),
        )
        donor_fingerprints = json.dumps(
            [
                {
                    "site": member.site.value,
                    "observation_id": member.observation_id,
                    "fingerprint": member.record_fingerprint,
                }
                for member in preview.donor_members
            ],
            sort_keys=True, separators=(",", ":"),
        )
        donor_preflight_fingerprints = json.dumps(
            [
                {
                    "site": member.site.value,
                    "observation_id": member.observation_id,
                    "fingerprint": member.preflight_fingerprint,
                }
                for member in preview.donor_members
            ],
            sort_keys=True, separators=(",", ":"),
        )
        approved_gaps = json.dumps(
            [
                {"item_type": item.item_type, "disabled_reason": item.disabled_reason}
                for item in preview.unsupported_items
            ],
            sort_keys=True, separators=(",", ":"),
        )
        evidence_graph_fingerprint = consolidation_evidence_graph_fingerprint(
            (
                edge.left_site.value, edge.left_observation_id,
                edge.right_site.value, edge.right_observation_id,
                edge.evidence_type, edge.evidence_strength,
                edge.reviewed_evidence_fingerprint,
            )
            for edge in preview.evidence_edges
        )
        canonical_by_site = {
            RemoteSite.MO: preview.canonical_mo_observation_id,
            RemoteSite.INAT: preview.canonical_inat_observation_id,
        }
        canonical_fingerprints = {
            member.site: member.record_fingerprint for member in preview.canonical_members
        }
        canonical_preflight_fingerprints = {
            member.site: member.preflight_fingerprint
            for member in preview.canonical_members
        }
        with self.transaction() as conn:
            if preview.consolidation_id is not None:
                deletion_unresolved = conn.execute(
                    "SELECT 1 FROM sync_deletion_attempts "
                    "WHERE profile_id=? AND consolidation_id=? "
                    "AND state IN ('pending','partial','outcome_unknown') "
                    "LIMIT 1",
                    (preview.profile_id, int(preview.consolidation_id)),
                ).fetchone()
                if deletion_unresolved:
                    raise ValueError(
                        "Phase 2C deletion activity serializes this consolidation"
                    )
            supersedes_attempt_id: Optional[int] = None
            base_finalized_attempt_id: Optional[int] = None
            is_extension = preview.is_extension
            is_retry = preview.consolidation_id is not None and not is_extension
            if is_extension:
                consolidation_id = int(preview.consolidation_id or 0)
                consolidation = conn.execute(
                    "SELECT * FROM sync_consolidations WHERE profile_id=? "
                    "AND consolidation_id=?",
                    (preview.profile_id, consolidation_id),
                ).fetchone()
                latest = conn.execute(
                    "SELECT attempt_id,state FROM sync_consolidation_attempts "
                    "WHERE profile_id=? AND consolidation_id=? "
                    "ORDER BY attempt_id DESC LIMIT 1",
                    (preview.profile_id, consolidation_id),
                ).fetchone()
                unresolved = conn.execute(
                    "SELECT 1 FROM sync_consolidation_attempts WHERE profile_id=? "
                    "AND consolidation_id=? AND state IN ('pending','outcome_unknown')",
                    (preview.profile_id, consolidation_id),
                ).fetchone()
                latest_state = str(latest["state"]) if latest else ""
                if latest_state in {"failed", "cancelled"}:
                    supersedes_attempt_id = int(latest["attempt_id"])
                if consolidation:
                    base_finalized_attempt_id = (
                        int(consolidation["current_finalized_attempt_id"])
                        if consolidation["current_finalized_attempt_id"] is not None
                        else None
                    )
                if (
                    not consolidation
                    or str(consolidation["state"]) != "finalized"
                    or consolidation["canonical_mo_observation_id"]
                    != preview.canonical_mo_observation_id
                    or consolidation["canonical_inat_observation_id"]
                    != preview.canonical_inat_observation_id
                    or not latest
                    or latest_state not in {"succeeded", "failed", "cancelled"}
                    or unresolved
                    or base_finalized_attempt_id is None
                ):
                    raise ValueError(
                        "The stable consolidation is not safely extendable"
                    )
                baseline_evidence_rows = conn.execute(
                    "SELECT e.evidence_type,e.evidence_strength,"
                    "e.reviewed_evidence_fingerprint,e.display_summary,"
                    "lm.site AS left_site,lm.observation_id AS left_observation_id,"
                    "rm.site AS right_site,rm.observation_id AS right_observation_id "
                    "FROM sync_consolidation_evidence e "
                    "JOIN sync_consolidation_attempt_members lm "
                    "ON lm.attempt_member_id=e.left_attempt_member_id "
                    "AND lm.attempt_id=e.attempt_id "
                    "JOIN sync_consolidation_attempt_members rm "
                    "ON rm.attempt_member_id=e.right_attempt_member_id "
                    "AND rm.attempt_id=e.attempt_id "
                    "WHERE e.attempt_id=?",
                    (base_finalized_attempt_id,),
                ).fetchall()
                baseline_edges = tuple(
                    ConsolidationEvidenceEdge(
                        left_site=RemoteSite(str(row["left_site"])),
                        left_observation_id=int(row["left_observation_id"]),
                        right_site=RemoteSite(str(row["right_site"])),
                        right_observation_id=int(row["right_observation_id"]),
                        evidence_type=str(row["evidence_type"]),
                        evidence_strength=str(row["evidence_strength"]),
                        reviewed_evidence_fingerprint=str(
                            row["reviewed_evidence_fingerprint"]
                        ),
                        display_summary=str(row["display_summary"]),
                    )
                    for row in baseline_evidence_rows
                )
                baseline_anchors = canonical_strong_anchor_signatures(
                    baseline_edges,
                    canonical_mo_id=preview.canonical_mo_observation_id,
                    canonical_inat_id=preview.canonical_inat_observation_id,
                )
                current_anchors = canonical_strong_anchor_signatures(
                    preview.evidence_edges,
                    canonical_mo_id=preview.canonical_mo_observation_id,
                    canonical_inat_id=preview.canonical_inat_observation_id,
                )
                if (
                    preview.canonical_mo_observation_id is not None
                    and preview.canonical_inat_observation_id is not None
                    and (
                        not baseline_anchors
                        or not baseline_anchors.issubset(current_anchors)
                    )
                ):
                    raise ValueError(
                        "A finalized strong canonical identity anchor changed "
                        "before extension"
                    )
                for member in preview.members:
                    existing = conn.execute(
                        "SELECT role,local_state "
                        "FROM sync_consolidation_members "
                        "WHERE profile_id=? AND site=? AND observation_id=?",
                        (
                            preview.profile_id, member.site.value,
                            member.observation_id,
                        ),
                    ).fetchone()
                    is_canonical_member = (
                        member.site, member.observation_id
                    ) in canonical_keys
                    if is_canonical_member:
                        baseline = conn.execute(
                            "SELECT reviewed_identity_fingerprint "
                            "FROM sync_consolidation_attempt_members "
                            "WHERE attempt_id=? AND site=? AND observation_id=? "
                            "AND participation_role='canonical_context'",
                            (
                                base_finalized_attempt_id, member.site.value,
                                member.observation_id,
                            ),
                        ).fetchone()
                        if (
                            not existing or str(existing["role"]) != "canonical"
                            or str(existing["local_state"]) != "canonical"
                            or not baseline
                            or str(baseline["reviewed_identity_fingerprint"] or "")
                            != member.identity_fingerprint
                        ):
                            raise ValueError(
                                "An existing canonical identity changed before extension"
                            )
                    elif existing:
                        raise ValueError(
                            f"{member.site.value} #{member.observation_id} already "
                            "belongs to a stable consolidation"
                        )
            if is_retry:
                consolidation_id = int(preview.consolidation_id)
                consolidation = conn.execute(
                    "SELECT * FROM sync_consolidations WHERE profile_id=? "
                    "AND consolidation_id=?",
                    (preview.profile_id, consolidation_id),
                ).fetchone()
                existing_members = conn.execute(
                    "SELECT site,observation_id,role FROM sync_consolidation_members "
                    "WHERE profile_id=? AND consolidation_id=?",
                    (preview.profile_id, consolidation_id),
                ).fetchall()
                latest = conn.execute(
                    "SELECT attempt_id,state FROM sync_consolidation_attempts "
                    "WHERE profile_id=? AND consolidation_id=? "
                    "ORDER BY attempt_id DESC LIMIT 1",
                    (preview.profile_id, consolidation_id),
                ).fetchone()
                existing_roles = {
                    (str(row["site"]), int(row["observation_id"])): str(row["role"])
                    for row in existing_members
                }
                expected_canonical_roles = {
                    (member.site.value, member.observation_id): (
                        "canonical"
                    )
                    for member in preview.members
                    if (member.site, member.observation_id) in canonical_keys
                }
                canonical_changed = bool(
                    consolidation
                    and (
                        consolidation["canonical_mo_observation_id"]
                        != preview.canonical_mo_observation_id
                        or consolidation["canonical_inat_observation_id"]
                        != preview.canonical_inat_observation_id
                    )
                )
                if (
                    not consolidation
                    or str(consolidation["state"]) not in {"draft", "confirmed"}
                    or not latest
                    or str(latest["state"]) not in {"failed", "cancelled"}
                ):
                    raise ValueError(
                        "Only a definitively failed or cancelled initial plan "
                        "may be superseded"
                    )
                supersedes_attempt_id = int(latest["attempt_id"])
                if canonical_changed:
                    remote_write_started = conn.execute(
                        "SELECT 1 FROM sync_consolidation_attempts a "
                        "JOIN sync_actions sa "
                        "ON sa.action_group_id=a.action_group_id "
                        "AND sa.profile_id=a.profile_id "
                        "WHERE a.profile_id=? AND a.consolidation_id=? AND ("
                        "sa.write_started_at IS NOT NULL "
                        "OR sa.state IN ('succeeded','outcome_unknown') "
                        "OR sa.outcome_unknown=1) LIMIT 1",
                        (preview.profile_id, consolidation_id),
                    ).fetchone()
                    unresolved = conn.execute(
                        "SELECT 1 FROM sync_consolidation_attempts "
                        "WHERE profile_id=? AND consolidation_id=? "
                        "AND state IN ('pending','outcome_unknown') LIMIT 1",
                        (preview.profile_id, consolidation_id),
                    ).fetchone()
                    if (
                        consolidation["current_finalized_attempt_id"] is not None
                        or remote_write_started
                        or unresolved
                    ):
                        raise ValueError(
                            "Canonical choices are locked because a write may "
                            "have started or the plan is unresolved"
                        )
                    cursor = conn.execute(
                        "UPDATE sync_consolidations SET state='cancelled',"
                        "updated_at=? WHERE profile_id=? AND consolidation_id=? "
                        "AND state IN ('draft','confirmed') "
                        "AND current_finalized_attempt_id IS NULL",
                        (now, preview.profile_id, consolidation_id),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            "The abandoned initial consolidation changed concurrently"
                        )
                    cursor = conn.execute(
                        "DELETE FROM sync_consolidation_members "
                        "WHERE profile_id=? AND consolidation_id=? AND role='canonical'",
                        (preview.profile_id, consolidation_id),
                    )
                    if cursor.rowcount != len(existing_members):
                        raise RuntimeError(
                            "The abandoned canonical membership was not released atomically"
                        )
                    consolidation_cursor = conn.execute(
                        "INSERT INTO sync_consolidations("
                        "profile_id,canonical_mo_observation_id,"
                        "canonical_inat_observation_id,canonical_pair_id,state,"
                        "created_at,updated_at) VALUES(?,?,?,NULL,'draft',?,?)",
                        (
                            preview.profile_id,
                            preview.canonical_mo_observation_id,
                            preview.canonical_inat_observation_id,
                            now, now,
                        ),
                    )
                    consolidation_id = int(consolidation_cursor.lastrowid)
                    is_retry = False
                elif existing_roles != expected_canonical_roles:
                    raise ValueError(
                        "The retry canonical membership changed unexpectedly"
                    )
            elif not is_extension:
                for member in preview.members:
                    existing = conn.execute(
                        "SELECT consolidation_id FROM sync_consolidation_members "
                        "WHERE profile_id=? AND site=? AND observation_id=?",
                        (preview.profile_id, member.site.value, member.observation_id),
                    ).fetchone()
                    if existing:
                        raise ValueError(
                            f"{member.site.value} #{member.observation_id} already belongs to "
                            f"consolidation {int(existing['consolidation_id'])}"
                        )
                consolidation_cursor = conn.execute(
                    "INSERT INTO sync_consolidations(profile_id,canonical_mo_observation_id,"
                    "canonical_inat_observation_id,canonical_pair_id,state,created_at,updated_at) "
                    "VALUES(?,?,?,NULL,'draft',?,?)",
                    (
                        preview.profile_id, preview.canonical_mo_observation_id,
                        preview.canonical_inat_observation_id, now, now,
                    ),
                )
                consolidation_id = int(consolidation_cursor.lastrowid)
            for donor in preview.donor_members:
                reserved = conn.execute(
                    "SELECT consolidation_id,state "
                    "FROM sync_unresolved_consolidation_proposals "
                    "WHERE profile_id=? AND site=? AND observation_id=? LIMIT 1",
                    (
                        preview.profile_id, donor.site.value,
                        donor.observation_id,
                    ),
                ).fetchone()
                admitted = conn.execute(
                    "SELECT consolidation_id FROM sync_consolidation_members "
                    "WHERE profile_id=? AND site=? AND observation_id=?",
                    (
                        preview.profile_id, donor.site.value,
                        donor.observation_id,
                    ),
                ).fetchone()
                if reserved or admitted:
                    raise ValueError(
                        f"{donor.site.value} #{donor.observation_id} became "
                        "reserved or admitted before attempt journaling"
                    )
            # "creation" is the existing dynamically-sized saga group shape:
            # unlike pair/issue groups it permits one nullable site id. The
            # dedicated consolidation-attempt FK below is the authoritative
            # source contract and LinkRepairService checks it before the older
            # Gate 2A creation contract.
            group_cursor = conn.execute(
                "INSERT INTO sync_action_groups(profile_id,source_kind,pair_id,issue_id,"
                "source_fingerprint,mo_observation_id,inat_observation_id,previewed_at,"
                "confirmed_at,created_at,updated_at) "
                "VALUES(?,'creation',NULL,NULL,?,?,?,?,?,?,?)",
                (
                    preview.profile_id, pair_fingerprint,
                    preview.canonical_mo_observation_id,
                    preview.canonical_inat_observation_id,
                    now, now, now, now,
                ),
            )
            group_id = int(group_cursor.lastrowid)
            attempt_cursor = conn.execute(
                "INSERT INTO sync_consolidation_attempts(consolidation_id,profile_id,"
                "action_group_id,correlation_marker,canonical_mo_fingerprint,"
                "canonical_inat_fingerprint,canonical_mo_preflight_fingerprint,"
                "canonical_inat_preflight_fingerprint,donor_fingerprints,"
                "donor_preflight_fingerprints,canonical_pair_fingerprint,"
                "approved_unsupported_gaps,reviewed_evidence_graph_fingerprint,"
                "destination_mo_account,destination_inat_account,"
                "state,supersedes_attempt_id,base_finalized_attempt_id,"
                "created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)",
                (
                    consolidation_id, preview.profile_id, group_id, correlation_marker,
                    canonical_fingerprints.get(RemoteSite.MO, ""),
                    canonical_fingerprints.get(RemoteSite.INAT, ""),
                    canonical_preflight_fingerprints.get(RemoteSite.MO, ""),
                    canonical_preflight_fingerprints.get(RemoteSite.INAT, ""),
                    donor_fingerprints, donor_preflight_fingerprints,
                    pair_fingerprint, approved_gaps, evidence_graph_fingerprint,
                    next(
                        (
                            f"{member.account_id}:{member.owner_login}"
                            for member in preview.canonical_members
                            if member.site is RemoteSite.MO
                        ),
                        "",
                    ),
                    next(
                        (
                            f"{member.account_id}:{member.owner_login}"
                            for member in preview.canonical_members
                            if member.site is RemoteSite.INAT
                        ),
                        "",
                    ),
                    supersedes_attempt_id, base_finalized_attempt_id, now, now,
                ),
            )
            attempt_id = int(attempt_cursor.lastrowid)
            if not is_retry:
                for member in preview.members:
                    role = (
                        "canonical"
                        if (member.site, member.observation_id) in canonical_keys
                        else "donor"
                    )
                    if role != "canonical" or is_extension:
                        continue
                    conn.execute(
                        "INSERT INTO sync_consolidation_members(consolidation_id,profile_id,"
                        "site,observation_id,role,remote_uuid,reviewed_record_fingerprint,"
                        "preflight_record_fingerprint,local_state,created_at,updated_at,"
                        "added_by_attempt_id,stable_owner_account_id) "
                        "VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?)",
                        (
                            consolidation_id, preview.profile_id, member.site.value,
                            member.observation_id, role, member.remote_uuid or None,
                            member.record_fingerprint, member.preflight_fingerprint,
                            now, now, attempt_id, member.owner_id,
                        ),
                    )
            member_rows = conn.execute(
                "SELECT * FROM sync_consolidation_members WHERE profile_id=? "
                "AND consolidation_id=?",
                (preview.profile_id, consolidation_id),
            ).fetchall()
            member_lookup = {
                (str(row["site"]), int(row["observation_id"])): row
                for row in member_rows
            }
            for member in preview.members:
                stable = member_lookup.get(
                    (member.site.value, member.observation_id)
                )
                is_canonical_member = (
                    member.site, member.observation_id
                ) in canonical_keys
                if is_canonical_member and stable is None:
                    raise RuntimeError(
                        "A reviewed canonical member is missing from the stable identity"
                    )
                cursor = conn.execute(
                    "INSERT INTO sync_consolidation_attempt_members("
                    "attempt_id,stable_member_id,site,observation_id,remote_uuid,"
                    "participation_role,proposal_state,reviewed_record_fingerprint,"
                    "preflight_record_fingerprint,reviewed_account_identity,"
                    "reviewed_owner_account_id,reviewed_owner_login,"
                    "reviewed_identity_fingerprint,reviewed_mutable_components,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        attempt_id,
                        int(stable["consolidation_member_id"]) if stable else None,
                        member.site.value, member.observation_id,
                        member.remote_uuid or None,
                        (
                            "canonical_context"
                            if is_canonical_member
                            else "new_donor"
                        ),
                        "canonical_context" if is_canonical_member else "proposed",
                        member.record_fingerprint, member.preflight_fingerprint,
                        f"{member.account_id}:{member.owner_login}",
                        member.owner_id, member.owner_login,
                        member.identity_fingerprint,
                        json.dumps(
                            dict(member.mutable_component_fingerprints),
                            sort_keys=True, separators=(",", ":"),
                        ),
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("The attempt member snapshot was not inserted")
            attempt_member_rows = conn.execute(
                "SELECT attempt_member_id,site,observation_id "
                "FROM sync_consolidation_attempt_members WHERE attempt_id=?",
                (attempt_id,),
            ).fetchall()
            attempt_member_lookup = {
                (str(row["site"]), int(row["observation_id"])): int(
                    row["attempt_member_id"]
                )
                for row in attempt_member_rows
            }
            for edge in preview.evidence_edges:
                left_id = attempt_member_lookup.get(
                    (edge.left_site.value, edge.left_observation_id)
                )
                right_id = attempt_member_lookup.get(
                    (edge.right_site.value, edge.right_observation_id)
                )
                if left_id is None or right_id is None:
                    raise ValueError("An evidence edge references an unreviewed member")
                if left_id > right_id:
                    left_id, right_id = right_id, left_id
                conn.execute(
                    "INSERT INTO sync_consolidation_evidence("
                    "attempt_id,left_attempt_member_id,right_attempt_member_id,evidence_type,"
                    "evidence_strength,reviewed_evidence_fingerprint,display_summary,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        attempt_id, left_id, right_id, edge.evidence_type,
                        edge.evidence_strength,
                        edge.reviewed_evidence_fingerprint,
                        edge.display_summary, now,
                    ),
                )
            for member in preview.members:
                for row in member.reciprocal_links:
                    conn.execute(
                        "INSERT INTO sync_action_snapshot_rows(profile_id,action_group_id,site,"
                        "observation_id,remote_row_id,remote_row_uuid,binding_id,"
                        "normalized_target_id,parse_state,row_fingerprint) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            preview.profile_id, group_id, member.site.value,
                            member.observation_id, row.row_id, row.row_uuid,
                            row.binding_id, row.target_observation_id,
                            row.parse_state, row.row_fingerprint,
                        ),
                    )
            for donor in preview.donor_members:
                for destination_site, destination_id in canonical_by_site.items():
                    if destination_id is None:
                        continue
                    destination = next(
                        member for member in preview.canonical_members
                        if member.site is destination_site
                        and member.observation_id == destination_id
                    )
                    for disclosure in preview.unsupported_items:
                        identity = (
                            f"{donor.site.value}:{donor.observation_id}:"
                            f"{disclosure.item_type}->{destination_site.value}:{destination_id}"
                        )
                        conn.execute(
                            "INSERT INTO sync_consolidation_items(attempt_id,source_site,"
                            "source_observation_id,destination_site,destination_observation_id,"
                            "item_type,source_item_identity,reviewed_metadata_fingerprint,"
                            "reviewed_byte_fingerprint,action_id,state,disabled_reason,created_at,"
                            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,NULL,'disabled',?,?,?)",
                            (
                                attempt_id, donor.site.value, donor.observation_id,
                                destination_site.value, destination_id,
                                disclosure.item_type, identity,
                                _local_fingerprint(
                                    identity, donor.record_fingerprint,
                                    destination.record_fingerprint,
                                    disclosure.disabled_reason,
                                ),
                                "", disclosure.disabled_reason, now, now,
                            ),
                        )
        return consolidation_id, attempt_id, group_id

    def consolidation_ledger_for_group(
        self, profile_id: int, group_id: int,
    ) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT ca.*,c.canonical_mo_observation_id,c.canonical_inat_observation_id,"
            "c.canonical_pair_id,c.state AS consolidation_state "
            "FROM sync_consolidation_attempts ca "
            "JOIN sync_consolidations c ON c.consolidation_id=ca.consolidation_id "
            "WHERE ca.profile_id=? AND ca.action_group_id=?",
            (profile_id, group_id),
        ).fetchone()
        return dict(row) if row else None

    def consolidation_attempt(
        self, profile_id: int, attempt_id: int,
    ) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT ca.*,c.canonical_mo_observation_id,c.canonical_inat_observation_id,"
            "c.canonical_pair_id,c.state AS consolidation_state "
            "FROM sync_consolidation_attempts ca "
            "JOIN sync_consolidations c ON c.consolidation_id=ca.consolidation_id "
            "WHERE ca.profile_id=? AND ca.attempt_id=?",
            (profile_id, attempt_id),
        ).fetchone()
        return dict(row) if row else None

    def consolidation_items(
        self, profile_id: int, attempt_id: int,
    ) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.connection().execute(
                "SELECT ci.* FROM sync_consolidation_items ci "
                "JOIN sync_consolidation_attempts ca ON ca.attempt_id=ci.attempt_id "
                "WHERE ca.profile_id=? AND ci.attempt_id=? "
                "ORDER BY ci.consolidation_item_id",
                (profile_id, attempt_id),
            ).fetchall()
        ]

    def set_consolidation_attempt_state(
        self, profile_id: int, attempt_id: int, state: str,
    ) -> bool:
        if state not in {"failed", "cancelled", "outcome_unknown"}:
            raise ValueError("Invalid consolidation attempt terminal state")
        cursor = self.connection().execute(
            "UPDATE sync_consolidation_attempts SET state=?,updated_at=? "
            "WHERE profile_id=? AND attempt_id=? AND state='pending'",
            (state, _utc_now(), profile_id, attempt_id),
        )
        return cursor.rowcount == 1

    def resolve_consolidation_unknown(
        self, profile_id: int, attempt_id: int, *, applied: bool,
    ) -> bool:
        """Resolve an ambiguous action without ever resending it."""
        target = "pending" if applied else "failed"
        cursor = self.connection().execute(
            "UPDATE sync_consolidation_attempts SET state=?,updated_at=? "
            "WHERE profile_id=? AND attempt_id=? AND state='outcome_unknown'",
            (target, _utc_now(), profile_id, attempt_id),
        )
        return cursor.rowcount == 1

    def ensure_consolidation_pair(
        self, profile_id: int, consolidation_id: int,
    ) -> Optional[int]:
        """Create or reuse the exact canonical pair as provisional.

        Same-site-only consolidations return ``None``; no sentinel remote id
        is ever invented.
        """
        now = _utc_now()
        with self.transaction() as conn:
            consolidation = conn.execute(
                "SELECT canonical_mo_observation_id,canonical_inat_observation_id,"
                "canonical_pair_id,state "
                "FROM sync_consolidations WHERE profile_id=? AND consolidation_id=?",
                (profile_id, consolidation_id),
            ).fetchone()
            if not consolidation or str(consolidation["state"]) not in {
                "draft", "confirmed", "finalized",
            }:
                raise ValueError("The consolidation identity is not executable")
            mo_id = consolidation["canonical_mo_observation_id"]
            inat_id = consolidation["canonical_inat_observation_id"]
            if mo_id is None or inat_id is None:
                return None
            pair = conn.execute(
                "SELECT pair_id,review_state FROM sync_pairs WHERE profile_id=? "
                "AND mo_observation_id=? AND inat_observation_id=?",
                (profile_id, mo_id, inat_id),
            ).fetchone()
            if str(consolidation["state"]) == "finalized":
                if (
                    pair is None
                    or str(pair["review_state"]) != "confirmed"
                    or consolidation["canonical_pair_id"] is None
                    or int(consolidation["canonical_pair_id"])
                    != int(pair["pair_id"])
                ):
                    raise ValueError(
                        "The finalized consolidation's canonical pair changed"
                    )
                return int(pair["pair_id"])
            if pair is None:
                cursor = conn.execute(
                    "INSERT INTO sync_pairs(profile_id,mo_observation_id,"
                    "inat_observation_id,link_state,score,classification,review_state,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        profile_id, mo_id, inat_id, "", 0,
                        "gate_2b_consolidation", "provisional", now, now,
                    ),
                )
                pair_id = int(cursor.lastrowid)
            else:
                pair_id = int(pair["pair_id"])
                if str(pair["review_state"]) != "confirmed":
                    conn.execute(
                        "UPDATE sync_pairs SET review_state='provisional',confirmed_by='',"
                        "classification='gate_2b_consolidation',updated_at=? "
                        "WHERE profile_id=? AND pair_id=?",
                        (now, profile_id, pair_id),
                    )
            cursor = conn.execute(
                "UPDATE sync_consolidations SET canonical_pair_id=?,state='confirmed',"
                "updated_at=? WHERE profile_id=? AND consolidation_id=? "
                "AND canonical_mo_observation_id=? AND canonical_inat_observation_id=?",
                (pair_id, now, profile_id, consolidation_id, mo_id, inat_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Canonical pair linkage changed during consolidation")
            return pair_id

    def mint_consolidation_action(
        self, profile_id: int, group_id: int, action_type: str, *,
        site: str, pair_id: Optional[int],
        mo_observation_id: Optional[int], inat_observation_id: Optional[int],
        inat_observation_uuid: str = "", binding_id: Optional[int] = None,
        desired_target_id: Optional[int] = None,
        preview_inat_record_fingerprint: str = "",
        preview_mo_record_fingerprint: str = "",
        preview_inat_links_fingerprint: str = "",
        preview_mo_links_fingerprint: str = "",
    ) -> int:
        """Atomically find-or-mint one dynamic Gate 2B saga ordinal."""
        permitted = {
            "mo_external_link_add", "inat_ofv_add", "consolidation_finalize",
        }
        if action_type not in permitted:
            raise ValueError("This action type is not permitted in a consolidation")
        with self.transaction() as conn:
            ledger = conn.execute(
                "SELECT ca.attempt_id,ca.state,c.consolidation_id,"
                "c.canonical_mo_observation_id,c.canonical_inat_observation_id,"
                "c.canonical_pair_id,c.state AS consolidation_state "
                "FROM sync_consolidation_attempts ca JOIN sync_consolidations c "
                "ON c.consolidation_id=ca.consolidation_id "
                "WHERE ca.profile_id=? AND ca.action_group_id=?",
                (profile_id, group_id),
            ).fetchone()
            if (
                not ledger
                or str(ledger["state"]) != "pending"
                or str(ledger["consolidation_state"])
                not in {"draft", "confirmed", "finalized"}
                or ledger["canonical_mo_observation_id"] != mo_observation_id
                or ledger["canonical_inat_observation_id"] != inat_observation_id
                or ledger["canonical_pair_id"] != pair_id
            ):
                raise ValueError("The consolidation ledger changed before action minting")
            existing = conn.execute(
                "SELECT action_id FROM sync_actions WHERE profile_id=? "
                "AND action_group_id=? AND action_type=? ORDER BY action_id LIMIT 1",
                (profile_id, group_id, action_type),
            ).fetchone()
            if existing:
                return int(existing["action_id"])
            ordinal = int(conn.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM sync_actions "
                "WHERE profile_id=? AND action_group_id=?",
                (profile_id, group_id),
            ).fetchone()[0])
            return self._insert_creation_followup_action(
                conn, profile_id, group_id, ordinal, action_type,
                pair_id=pair_id,  # type: ignore[arg-type]
                mo_observation_id=mo_observation_id,  # type: ignore[arg-type]
                inat_observation_id=inat_observation_id,  # type: ignore[arg-type]
                inat_observation_uuid=inat_observation_uuid, site=site,
                binding_id=binding_id, desired_target_id=desired_target_id,
                preview_inat_record_fingerprint=preview_inat_record_fingerprint,
                preview_mo_record_fingerprint=preview_mo_record_fingerprint,
                preview_inat_links_fingerprint=preview_inat_links_fingerprint,
                preview_mo_links_fingerprint=preview_mo_links_fingerprint,
            )

    def settle_consolidation_finalize_success(
        self, profile_id: int, action_id: int, group_id: int,
        attempt_id: int, consolidation_id: int,
    ) -> Optional[int]:
        """M8 all-or-nothing local canonicalization boundary."""
        now = _utc_now()
        with self.transaction() as conn:
            deletion_unresolved = conn.execute(
                "SELECT 1 FROM sync_deletion_attempts "
                "WHERE profile_id=? AND consolidation_id=? "
                "AND state IN ('pending','partial','outcome_unknown') LIMIT 1",
                (profile_id, consolidation_id),
            ).fetchone()
            if deletion_unresolved:
                raise ValueError(
                    "Phase 2C deletion activity blocks Phase 2B finalization"
                )
            action = conn.execute(
                "SELECT * FROM sync_actions WHERE profile_id=? AND action_id=?",
                (profile_id, action_id),
            ).fetchone()
            attempt = conn.execute(
                "SELECT * FROM sync_consolidation_attempts WHERE profile_id=? "
                "AND attempt_id=? AND consolidation_id=?",
                (profile_id, attempt_id, consolidation_id),
            ).fetchone()
            group = conn.execute(
                "SELECT * FROM sync_action_groups WHERE profile_id=? "
                "AND action_group_id=?",
                (profile_id, group_id),
            ).fetchone()
            consolidation = conn.execute(
                "SELECT * FROM sync_consolidations WHERE profile_id=? AND consolidation_id=?",
                (profile_id, consolidation_id),
            ).fetchone()
            members = conn.execute(
                "SELECT am.attempt_member_id,am.stable_member_id,"
                "a.consolidation_id,a.profile_id,am.site,am.observation_id,"
                "am.remote_uuid,"
                "m.remote_uuid AS stable_remote_uuid,"
                "m.stable_owner_account_id,"
                "m.profile_id AS stable_profile_id,"
                "m.consolidation_id AS stable_consolidation_id,"
                "m.site AS stable_site,"
                "m.observation_id AS stable_observation_id,"
                "CASE am.participation_role WHEN 'canonical_context' THEN "
                "'canonical' ELSE 'donor' END AS role,"
                "COALESCE(m.local_state,'proposed') AS local_state,"
                "am.participation_role,am.proposal_state,"
                "am.reviewed_account_identity,am.reviewed_owner_account_id,"
                "am.reviewed_owner_login,"
                "am.reviewed_identity_fingerprint,"
                "am.reviewed_mutable_components,"
                "am.reviewed_record_fingerprint AS attempt_record_fingerprint,"
                "am.preflight_record_fingerprint AS attempt_preflight_fingerprint "
                "FROM sync_consolidation_attempt_members am "
                "JOIN sync_consolidation_attempts a ON a.attempt_id=am.attempt_id "
                "LEFT JOIN sync_consolidation_members m "
                "ON m.consolidation_member_id=am.stable_member_id "
                "WHERE am.attempt_id=? AND a.profile_id=? "
                "AND a.consolidation_id=? ORDER BY am.attempt_member_id",
                (attempt_id, profile_id, consolidation_id),
            ).fetchall()
            evidence_rows = conn.execute(
                "SELECT e.evidence_type,e.evidence_strength,"
                "e.reviewed_evidence_fingerprint,"
                "lm.site AS left_site,lm.observation_id AS left_observation_id,"
                "rm.site AS right_site,rm.observation_id AS right_observation_id "
                "FROM sync_consolidation_evidence e "
                "JOIN sync_consolidation_attempt_members lm "
                "ON lm.attempt_member_id=e.left_attempt_member_id "
                "AND lm.attempt_id=e.attempt_id "
                "JOIN sync_consolidation_attempt_members rm "
                "ON rm.attempt_member_id=e.right_attempt_member_id "
                "AND rm.attempt_id=e.attempt_id "
                "WHERE e.attempt_id=?",
                (attempt_id,),
            ).fetchall()
            evidence_signatures: list[tuple[str, int, str, int, str, str, str]] = []
            for evidence in evidence_rows:
                left = (
                    str(evidence["left_site"]),
                    int(evidence["left_observation_id"]),
                )
                right = (
                    str(evidence["right_site"]),
                    int(evidence["right_observation_id"]),
                )
                if right < left:
                    left, right = right, left
                evidence_signatures.append((
                    left[0], left[1], right[0], right[1],
                    str(evidence["evidence_type"]),
                    str(evidence["evidence_strength"]),
                    str(evidence["reviewed_evidence_fingerprint"]),
                ))
            evidence_graph_fingerprint = consolidation_evidence_graph_fingerprint(
                evidence_signatures
            )
            structured_edges = tuple(
                ConsolidationEvidenceEdge(
                    left_site=RemoteSite(str(evidence["left_site"])),
                    left_observation_id=int(evidence["left_observation_id"]),
                    right_site=RemoteSite(str(evidence["right_site"])),
                    right_observation_id=int(evidence["right_observation_id"]),
                    evidence_type=str(evidence["evidence_type"]),
                    evidence_strength=str(evidence["evidence_strength"]),
                    reviewed_evidence_fingerprint=str(
                        evidence["reviewed_evidence_fingerprint"]
                    ),
                    display_summary="Persisted evidence",
                )
                for evidence in evidence_rows
            )
            graph_validation = validate_consolidation_graph(
                [
                    (RemoteSite(str(member["site"])), int(member["observation_id"]))
                    for member in members
                ],
                structured_edges,
                canonical_mo_id=(
                    int(consolidation["canonical_mo_observation_id"])
                    if consolidation
                    and consolidation["canonical_mo_observation_id"] is not None
                    else None
                ),
                canonical_inat_id=(
                    int(consolidation["canonical_inat_observation_id"])
                    if consolidation
                    and consolidation["canonical_inat_observation_id"] is not None
                    else None
                ),
            )
            if (
                not action
                or int(action["action_group_id"]) != group_id
                or str(action["action_type"]) != "consolidation_finalize"
                or str(action["state"]) != "running"
                or not attempt
                or int(attempt["action_group_id"]) != group_id
                or str(attempt["state"]) != "pending"
                or not group
                or str(group["source_fingerprint"])
                != str(attempt["canonical_pair_fingerprint"])
                or not consolidation
                or str(consolidation["state"])
                not in {"draft", "confirmed", "finalized"}
                or not members
                or not evidence_rows
                or not str(attempt["reviewed_evidence_graph_fingerprint"] or "")
                or evidence_graph_fingerprint
                != str(attempt["reviewed_evidence_graph_fingerprint"])
                or consolidation["current_finalized_attempt_id"]
                != attempt["base_finalized_attempt_id"]
                or not graph_validation.valid
            ):
                raise RuntimeError("The consolidation finalization ledger is inconsistent")
            mo_id = consolidation["canonical_mo_observation_id"]
            inat_id = consolidation["canonical_inat_observation_id"]
            if (
                group["mo_observation_id"] != mo_id
                or group["inat_observation_id"] != inat_id
            ):
                raise RuntimeError("The action group canonical ids do not match")
            if action["mo_observation_id"] != mo_id or action["inat_observation_id"] != inat_id:
                raise RuntimeError("The finalization action canonical ids do not match")
            canonical_members = [
                member for member in members if str(member["role"]) == "canonical"
            ]
            expected_canonical_count = int(mo_id is not None) + int(inat_id is not None)
            if len(canonical_members) != expected_canonical_count:
                raise RuntimeError("The canonical member set is incomplete")
            if any(member["stable_member_id"] is None for member in canonical_members):
                raise RuntimeError("A canonical attempt snapshot has no stable member")
            for member in canonical_members:
                if (
                    int(member["stable_profile_id"]) != profile_id
                    or int(member["stable_consolidation_id"]) != consolidation_id
                    or str(member["stable_site"]) != str(member["site"])
                    or int(member["stable_observation_id"])
                    != int(member["observation_id"])
                    or int(member["stable_owner_account_id"] or 0)
                    != int(member["reviewed_owner_account_id"] or 0)
                    or str(member["stable_remote_uuid"] or "")
                    != str(member["remote_uuid"] or "")
                ):
                    raise RuntimeError(
                        "A canonical snapshot no longer matches its stable member identity"
                    )
            base_attempt_id = attempt["base_finalized_attempt_id"]
            if base_attempt_id is not None:
                base_attempt = conn.execute(
                    "SELECT b.* FROM sync_consolidation_attempts b "
                    "WHERE b.attempt_id=? AND b.profile_id=? "
                    "AND b.consolidation_id=? AND b.state='succeeded' "
                    "AND b.attempt_id<? AND EXISTS ("
                    "SELECT 1 FROM sync_actions sa "
                    "WHERE sa.profile_id=b.profile_id "
                    "AND sa.action_group_id=b.action_group_id "
                    "AND sa.action_type='consolidation_finalize' "
                    "AND sa.state='succeeded')",
                    (
                        int(base_attempt_id), profile_id,
                        consolidation_id, attempt_id,
                    ),
                ).fetchone()
                if base_attempt is None:
                    raise RuntimeError(
                        "The finalized baseline attempt is not valid for this consolidation"
                    )
                baseline_canonical_members = conn.execute(
                    "SELECT am.attempt_member_id,am.stable_member_id,am.site,"
                    "am.observation_id,am.remote_uuid,"
                    "am.reviewed_owner_account_id,m.profile_id,m.consolidation_id,"
                    "m.site AS stable_site,m.observation_id AS stable_observation_id,"
                    "m.remote_uuid AS stable_remote_uuid,m.stable_owner_account_id,"
                    "m.role,m.local_state "
                    "FROM sync_consolidation_attempt_members am "
                    "JOIN sync_consolidation_members m "
                    "ON m.consolidation_member_id=am.stable_member_id "
                    "WHERE am.attempt_id=? "
                    "AND am.participation_role='canonical_context'",
                    (int(base_attempt_id),),
                ).fetchall()
                if len(baseline_canonical_members) != expected_canonical_count:
                    raise RuntimeError(
                        "The finalized baseline canonical member set is incomplete"
                    )
                baseline_by_key = {}
                for baseline_member in baseline_canonical_members:
                    key = (
                        str(baseline_member["site"]),
                        int(baseline_member["observation_id"]),
                    )
                    if (
                        int(baseline_member["profile_id"]) != profile_id
                        or int(baseline_member["consolidation_id"])
                        != consolidation_id
                        or str(baseline_member["role"]) != "canonical"
                        or str(baseline_member["local_state"]) != "canonical"
                        or str(baseline_member["stable_site"]) != key[0]
                        or int(baseline_member["stable_observation_id"]) != key[1]
                        or int(baseline_member["stable_owner_account_id"] or 0)
                        != int(baseline_member["reviewed_owner_account_id"] or 0)
                        or str(baseline_member["stable_remote_uuid"] or "")
                        != str(baseline_member["remote_uuid"] or "")
                    ):
                        raise RuntimeError(
                            "The finalized baseline has invalid canonical ownership"
                        )
                    baseline_by_key[key] = baseline_member
                for member in canonical_members:
                    key = (str(member["site"]), int(member["observation_id"]))
                    baseline_member = baseline_by_key.get(key)
                    if (
                        baseline_member is None
                        or int(baseline_member["stable_member_id"])
                        != int(member["stable_member_id"])
                        or int(baseline_member["reviewed_owner_account_id"])
                        != int(member["reviewed_owner_account_id"])
                        or str(baseline_member["remote_uuid"] or "")
                        != str(member["remote_uuid"] or "")
                    ):
                        raise RuntimeError(
                            "The current canonical snapshot does not match the "
                            "finalized baseline identity"
                        )
                baseline_evidence_rows = conn.execute(
                    "SELECT e.evidence_type,e.evidence_strength,"
                    "e.reviewed_evidence_fingerprint,e.display_summary,"
                    "lm.site AS left_site,lm.observation_id AS left_observation_id,"
                    "rm.site AS right_site,rm.observation_id AS right_observation_id "
                    "FROM sync_consolidation_evidence e "
                    "JOIN sync_consolidation_attempt_members lm "
                    "ON lm.attempt_member_id=e.left_attempt_member_id "
                    "AND lm.attempt_id=e.attempt_id "
                    "JOIN sync_consolidation_attempt_members rm "
                    "ON rm.attempt_member_id=e.right_attempt_member_id "
                    "AND rm.attempt_id=e.attempt_id "
                    "WHERE e.attempt_id=?",
                    (int(base_attempt_id),),
                ).fetchall()
                baseline_edges = tuple(
                    ConsolidationEvidenceEdge(
                        left_site=RemoteSite(str(row["left_site"])),
                        left_observation_id=int(row["left_observation_id"]),
                        right_site=RemoteSite(str(row["right_site"])),
                        right_observation_id=int(row["right_observation_id"]),
                        evidence_type=str(row["evidence_type"]),
                        evidence_strength=str(row["evidence_strength"]),
                        reviewed_evidence_fingerprint=str(
                            row["reviewed_evidence_fingerprint"]
                        ),
                        display_summary=str(row["display_summary"]),
                    )
                    for row in baseline_evidence_rows
                )
                baseline_anchors = canonical_strong_anchor_signatures(
                    baseline_edges,
                    canonical_mo_id=(
                        int(mo_id) if mo_id is not None else None
                    ),
                    canonical_inat_id=(
                        int(inat_id) if inat_id is not None else None
                    ),
                )
                current_anchors = canonical_strong_anchor_signatures(
                    structured_edges,
                    canonical_mo_id=(
                        int(mo_id) if mo_id is not None else None
                    ),
                    canonical_inat_id=(
                        int(inat_id) if inat_id is not None else None
                    ),
                )
                if (
                    mo_id is not None
                    and inat_id is not None
                    and (
                        not baseline_anchors
                        or not baseline_anchors.issubset(current_anchors)
                    )
                ):
                    raise RuntimeError(
                        "The finalized canonical strong anchors do not match"
                    )
            if inat_id is not None:
                canonical_inat_member = next(
                    (
                        member
                        for member in canonical_members
                        if str(member["site"]) == "inat"
                        and int(member["observation_id"]) == int(inat_id)
                    ),
                    None,
                )
                if (
                    canonical_inat_member is None
                    or not str(canonical_inat_member["remote_uuid"] or "")
                    or str(action["inat_observation_uuid"] or "")
                    != str(canonical_inat_member["remote_uuid"])
                ):
                    raise RuntimeError(
                        "The finalization action iNaturalist UUID does not match "
                        "the canonical member"
                    )
            for member in members:
                site = str(member["site"])
                observation_id = int(member["observation_id"])
                if str(member["role"]) == "canonical":
                    suffix = "mo" if site == "mo" else "inat"
                    expected_full = str(
                        attempt[f"canonical_{suffix}_fingerprint"] or ""
                    )
                    expected_preflight = str(
                        attempt[
                            f"canonical_{suffix}_preflight_fingerprint"
                        ]
                        or ""
                    )
                else:
                    expected_full = str(
                        member["attempt_record_fingerprint"] or ""
                    )
                    expected_preflight = str(
                        member["attempt_preflight_fingerprint"] or ""
                    )
                if (
                    not expected_full
                    or not expected_preflight
                    or str(member["attempt_record_fingerprint"]) != expected_full
                    or str(member["attempt_preflight_fingerprint"])
                    != expected_preflight
                ):
                    raise RuntimeError(
                        "The immutable attempt and consolidation member "
                        "fingerprints do not match"
                    )
                expected_identity = canonical_stable_identity_fingerprint(
                    site,
                    observation_id,
                    str(member["remote_uuid"] or ""),
                    int(member["reviewed_owner_account_id"]),
                )
                if (
                    int(member["reviewed_owner_account_id"] or 0) <= 0
                    or not str(member["reviewed_owner_login"] or "")
                    or not str(member["reviewed_identity_fingerprint"] or "")
                    or str(member["reviewed_identity_fingerprint"])
                    != expected_identity
                ):
                    raise RuntimeError(
                        "The immutable attempt identity fingerprint is inconsistent"
                    )
            proposed_donors = [
                member for member in members
                if str(member["participation_role"]) == "new_donor"
            ]
            if len(graph_validation.donor_paths) != len(proposed_donors):
                raise RuntimeError(
                    "The finalization graph lacks a strong-anchored donor path"
                )
            for member in proposed_donors:
                conflict = conn.execute(
                    "SELECT consolidation_id FROM sync_consolidation_members "
                    "WHERE profile_id=? AND site=? AND observation_id=?",
                    (
                        profile_id, str(member["site"]),
                        int(member["observation_id"]),
                    ),
                ).fetchone()
                unresolved_elsewhere = conn.execute(
                    "SELECT a.consolidation_id FROM "
                    "sync_consolidation_attempt_members am "
                    "JOIN sync_consolidation_attempts a "
                    "ON a.attempt_id=am.attempt_id "
                    "WHERE a.profile_id=? AND am.site=? AND am.observation_id=? "
                    "AND am.participation_role='new_donor' "
                    "AND a.state IN ('pending','outcome_unknown') "
                    "AND a.attempt_id!=? LIMIT 1",
                    (
                        profile_id, str(member["site"]),
                        int(member["observation_id"]), attempt_id,
                    ),
                ).fetchone()
                if conflict or unresolved_elsewhere:
                    raise RuntimeError(
                        "A proposed donor was admitted or reserved elsewhere "
                        "after review"
                    )
            for member in proposed_donors:
                cursor = conn.execute(
                    "INSERT INTO sync_consolidation_members("
                    "consolidation_id,profile_id,site,observation_id,role,"
                    "remote_uuid,reviewed_record_fingerprint,"
                    "preflight_record_fingerprint,local_state,created_at,"
                    "updated_at,added_by_attempt_id,superseded_by_attempt_id,"
                    "superseded_at,originally_proposed_by_attempt_id,"
                    "admitted_from_attempt_member_id,stable_owner_account_id) "
                    "VALUES(?,?,?,?, 'donor',?,?,?,'superseded',?,?,?,?,?,?,?,?)",
                    (
                        consolidation_id, profile_id, str(member["site"]),
                        int(member["observation_id"]),
                        str(member["remote_uuid"] or "") or None,
                        str(member["attempt_record_fingerprint"]),
                        str(member["attempt_preflight_fingerprint"]),
                        now, now, attempt_id, attempt_id, now, attempt_id,
                        int(member["attempt_member_id"]),
                        int(member["reviewed_owner_account_id"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("A proposed donor was not admitted")
            stable_members = conn.execute(
                "SELECT site,observation_id FROM sync_consolidation_members "
                "WHERE profile_id=? AND consolidation_id=?",
                (profile_id, consolidation_id),
            ).fetchall()
            member_mo_ids = {
                int(member["observation_id"])
                for member in stable_members if member["site"] == "mo"
            }
            member_inat_ids = {
                int(member["observation_id"])
                for member in stable_members if member["site"] == "inat"
            }
            pair_id: Optional[int] = None
            if mo_id is not None and inat_id is not None:
                pair = conn.execute(
                    "SELECT * FROM sync_pairs WHERE profile_id=? AND mo_observation_id=? "
                    "AND inat_observation_id=?",
                    (profile_id, mo_id, inat_id),
                ).fetchone()
                if not pair:
                    raise RuntimeError("The exact canonical pair is missing")
                pair_id = int(pair["pair_id"])
                if (
                    consolidation["canonical_pair_id"] is None
                    or int(consolidation["canonical_pair_id"]) != pair_id
                    or action["pair_id"] is None
                    or int(action["pair_id"]) != pair_id
                ):
                    raise RuntimeError("Canonical pair provenance does not match")
                conflicts = conn.execute(
                    "SELECT pair_id,mo_observation_id,inat_observation_id FROM sync_pairs "
                    "WHERE profile_id=? AND review_state='confirmed' AND pair_id!=? AND "
                    "(mo_observation_id=? OR inat_observation_id=?)",
                    (profile_id, pair_id, mo_id, inat_id),
                ).fetchall()
                for conflict in conflicts:
                    if (
                        int(conflict["mo_observation_id"]) not in member_mo_ids
                        or int(conflict["inat_observation_id"]) not in member_inat_ids
                    ):
                        raise RuntimeError(
                            "An external confirmed one-to-one conflict appeared after review"
                        )
                for conflict in conflicts:
                    cursor = conn.execute(
                        "UPDATE sync_pairs SET review_state='rejected',confirmed_by='',"
                        "updated_at=? WHERE profile_id=? AND pair_id=? "
                        "AND review_state='confirmed'",
                        (now, profile_id, int(conflict["pair_id"])),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("An internal donor pair changed during finalization")
                cursor = conn.execute(
                    "UPDATE sync_pairs SET review_state='confirmed',"
                    "confirmed_by='consolidation',ever_reviewed=1,ever_confirmed=1,"
                    "historical_confirmed_by='consolidation',updated_at=? "
                    "WHERE profile_id=? AND pair_id=? "
                    "AND review_state IN ('candidate','rejected','provisional','confirmed')",
                    (now, profile_id, pair_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("The canonical pair could not be confirmed")
            elif action["pair_id"] is not None:
                raise RuntimeError("A same-site consolidation must not carry a pair id")

            for member in canonical_members:
                desired = "canonical"
                current_state = str(member["local_state"])
                if desired == "canonical" and current_state == "canonical":
                    continue
                cursor = conn.execute(
                    "UPDATE sync_consolidation_members SET local_state=?,updated_at=?,"
                    "superseded_by_attempt_id=CASE WHEN ?='superseded' THEN ? "
                    "ELSE superseded_by_attempt_id END,"
                    "superseded_at=CASE WHEN ?='superseded' THEN ? ELSE superseded_at END "
                    "WHERE consolidation_member_id=? AND profile_id=? AND local_state='active'",
                    (
                        desired, now, desired, attempt_id, desired, now,
                        int(member["stable_member_id"]), profile_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("A consolidation member changed during finalization")
            if attempt["base_finalized_attempt_id"] is None:
                baseline_predicate = "current_finalized_attempt_id IS NULL"
                baseline_values: tuple[object, ...] = ()
            else:
                baseline_predicate = "current_finalized_attempt_id=?"
                baseline_values = (int(attempt["base_finalized_attempt_id"]),)
            cursor = conn.execute(
                "UPDATE sync_actions SET state='succeeded',last_phase='verification',"
                "last_error_code='',outcome_unknown=0,"
                "verification_state='verified_local_finalize',verified_at=?,finished_at=?,"
                "updated_at=? WHERE profile_id=? AND action_id=? AND state='running'",
                (now, now, now, profile_id, action_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The finalization action update did not affect one row")
            cursor = conn.execute(
                "UPDATE sync_consolidation_attempts SET state='succeeded',updated_at=? "
                "WHERE profile_id=? AND attempt_id=? AND state='pending'",
                (now, profile_id, attempt_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The consolidation attempt update did not affect one row")
            cursor = conn.execute(
                "UPDATE sync_consolidations SET canonical_pair_id=?,state='finalized',"
                "current_finalized_attempt_id=?,updated_at=? "
                "WHERE profile_id=? AND consolidation_id=? "
                "AND state IN ('draft','confirmed','finalized') AND "
                + baseline_predicate,
                (
                    pair_id, attempt_id, now, profile_id, consolidation_id,
                    *baseline_values,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The consolidation identity could not be finalized")
            return pair_id

    # Gate 2C deletion ledger ----------------------------------------

    def journal_deletion_attempt(
        self, preview: Any, selected_member_ids: Sequence[int],
    ) -> tuple[int, int]:
        """Persist one immutable reviewed donor set; no delete action is minted."""
        selected = tuple(dict.fromkeys(int(value) for value in selected_member_ids))
        if not selected:
            raise ValueError("Select at least one eligible donor")
        lookup = {
            int(item.stable_member_id): item for item in preview.donors
        }
        if set(selected) - set(lookup):
            raise ValueError("The deletion selection is not part of this preview")
        chosen = [lookup[value] for value in selected]
        if any(not item.eligible or item.blocking_reasons for item in chosen):
            raise ValueError("A blocked donor cannot enter a deletion attempt")
        now = _utc_now()
        with self.transaction() as conn:
            consolidation = conn.execute(
                "SELECT * FROM sync_consolidations WHERE profile_id=? "
                "AND consolidation_id=?",
                (preview.profile_id, preview.consolidation_id),
            ).fetchone()
            if (
                consolidation is None
                or str(consolidation["state"]) != "finalized"
                or int(consolidation["current_finalized_attempt_id"] or 0)
                != int(preview.base_finalized_attempt_id)
            ):
                raise ValueError("The finalized canonical baseline changed")
            phase_2b_unresolved = conn.execute(
                "SELECT 1 FROM sync_consolidation_attempts "
                "WHERE profile_id=? AND consolidation_id=? "
                "AND state IN ('pending','outcome_unknown') LIMIT 1",
                (preview.profile_id, preview.consolidation_id),
            ).fetchone()
            if phase_2b_unresolved:
                raise ValueError(
                    "Phase 2B activity is unresolved for this consolidation"
                )
            placeholders = ",".join("?" for _ in selected)
            unresolved = conn.execute(
                "SELECT * FROM sync_deletion_attempts WHERE profile_id=? "
                "AND consolidation_id=? "
                "AND state IN ('pending','partial','outcome_unknown')",
                (preview.profile_id, preview.consolidation_id),
            ).fetchone()
            supersedes_attempt_id: Optional[int] = None
            if unresolved:
                unresolved_id = int(unresolved["deletion_attempt_id"])
                unresolved_items = conn.execute(
                    "SELECT stable_member_id,state FROM sync_deletion_items "
                    "WHERE deletion_attempt_id=?",
                    (unresolved_id,),
                ).fetchall()
                selected_states = {
                    int(row["stable_member_id"]): str(row["state"])
                    for row in unresolved_items
                    if int(row["stable_member_id"]) in selected
                }
                can_supersede_partial = (
                    str(unresolved["state"]) == "partial"
                    and set(selected_states) == set(selected)
                    and all(
                        state in {"failed", "cancelled", "retry_required"}
                        for state in selected_states.values()
                    )
                    and not any(
                        str(row["state"]) in {
                            "pending", "running", "outcome_unknown",
                        }
                        for row in unresolved_items
                    )
                )
                if not can_supersede_partial:
                    raise ValueError(
                        "An unresolved deletion attempt already serializes "
                        "this consolidation"
                    )
                supersedes_attempt_id = unresolved_id
            retry_rows = conn.execute(
                "SELECT DISTINCT da.deletion_attempt_id "
                "FROM sync_deletion_items di JOIN sync_deletion_attempts da "
                "ON da.deletion_attempt_id=di.deletion_attempt_id "
                f"WHERE di.stable_member_id IN ({placeholders}) "
                "AND da.state='retry_required' "
                "ORDER BY da.deletion_attempt_id DESC",
                selected,
            ).fetchall()
            retry_attempt_ids = {
                int(row["deletion_attempt_id"]) for row in retry_rows
            }
            if len(retry_attempt_ids) > 1:
                raise ValueError(
                    "Selected donors require separate fresh retry reviews"
                )
            retry_supersedes = (
                next(iter(retry_attempt_ids)) if retry_attempt_ids else None
            )
            if (
                supersedes_attempt_id is not None
                and retry_supersedes is not None
                and supersedes_attempt_id != retry_supersedes
            ):
                raise ValueError("The retry provenance is inconsistent")
            supersedes_attempt_id = (
                supersedes_attempt_id
                if supersedes_attempt_id is not None
                else retry_supersedes
            )
            if supersedes_attempt_id is not None:
                cursor = conn.execute(
                    "UPDATE sync_deletion_attempts SET state='superseded',"
                    "updated_at=? WHERE deletion_attempt_id=? "
                    "AND state IN ('partial','retry_required')",
                    (now, supersedes_attempt_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "The prior retry review could not be superseded"
                    )
            # Deletion groups live in their own negative id space so an older
            # generic sync_actions dispatcher can never pick one up. The value
            # is predicted from the next rowid because the attempt row cannot
            # be inserted without it; the prediction is ASSERTED against the
            # real lastrowid below rather than trusted, since
            # deletion_attempt_for_group -- the only way the executor ever
            # finds this attempt again -- silently returns None if the two
            # ever diverge (an AUTOINCREMENT column or a deleted attempt row
            # would be enough).
            group_id = -int(conn.execute(
                "SELECT COALESCE(MAX(deletion_attempt_id),0)+1 "
                "FROM sync_deletion_attempts"
            ).fetchone()[0])
            cursor = conn.execute(
                "INSERT INTO sync_deletion_attempts("
                "profile_id,consolidation_id,base_finalized_attempt_id,"
                "action_group_id,state,supersedes_attempt_id,"
                "canonical_stable_identity_fingerprint,"
                "canonical_mutable_snapshot_fingerprint,"
                "parity_report_fingerprint,reviewed_auth_generation,"
                "reviewed_mo_key_generation,confirmation_fingerprint,"
                "created_at,updated_at) VALUES(?,?,?,?,'pending',?,?,?,?,?,?,?,?,?)",
                (
                    preview.profile_id, preview.consolidation_id,
                    preview.base_finalized_attempt_id,
                    group_id,
                    supersedes_attempt_id,
                    preview.canonical_stable_identity_fingerprint,
                    preview.canonical_mutable_snapshot_fingerprint,
                    preview.parity_report_fingerprint,
                    preview.auth_generation, preview.mo_key_generation,
                    preview.confirmation_fingerprint(selected),
                    now, now,
                ),
            )
            attempt_id = int(cursor.lastrowid)
            if attempt_id != -group_id:
                raise RuntimeError(
                    f"Deletion attempt {attempt_id} does not match its predicted action "
                    f"group id {group_id}; refusing to journal an attempt the executor "
                    "could not find again."
                )
            for ordinal, item in enumerate(chosen, 1):
                cursor = conn.execute(
                    "INSERT INTO sync_deletion_items("
                    "deletion_attempt_id,stable_member_id,ordinal,site,"
                    "observation_id,remote_uuid,"
                    "reviewed_remote_record_fingerprint,"
                    "reviewed_content_inventory_fingerprint,"
                    "reviewed_parity_fingerprint,"
                    "reviewed_third_party_activity_fingerprint,"
                    "reviewed_dependency_fingerprint,action_id,state,"
                    "disabled_reason,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,'pending','',?,?)",
                    (
                        attempt_id, item.stable_member_id, ordinal,
                        item.site.value, item.observation_id, item.remote_uuid,
                        item.remote_record_fingerprint,
                        item.content_inventory_fingerprint,
                        item.parity_fingerprint,
                        item.third_party_activity_fingerprint,
                        item.dependency_fingerprint, now, now,
                    ),
                )
                deletion_item_id = int(cursor.lastrowid)
                for parity_ordinal, parity in enumerate(item.parity_items, 1):
                    conn.execute(
                        "INSERT INTO sync_deletion_parity_items("
                        "deletion_item_id,ordinal,source_content_type,"
                        "source_content_identity,canonical_matching_identity,"
                        "match_method,reviewed_source_fingerprint,"
                        "reviewed_canonical_fingerprint,eligibility_result,"
                        "blocking_reason,safe_summary,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            deletion_item_id, parity_ordinal,
                            parity.content_type, parity.source_identity,
                            parity.canonical_identity, parity.match_method,
                            parity.source_fingerprint,
                            parity.canonical_fingerprint,
                            "preserved" if parity.preserved else "blocked",
                            parity.blocking_reason, parity.safe_summary, now,
                        ),
                    )
        return attempt_id, group_id

    def deletion_attempt_for_group(
        self, profile_id: int, group_id: int,
    ) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_deletion_attempts WHERE profile_id=? "
            "AND action_group_id=?",
            (profile_id, group_id),
        ).fetchone()
        return dict(row) if row else None

    def deletion_attempt(
        self, profile_id: int, attempt_id: int,
    ) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_deletion_attempts WHERE profile_id=? "
            "AND deletion_attempt_id=?",
            (profile_id, attempt_id),
        ).fetchone()
        return dict(row) if row else None

    def unresolved_deletion_for_consolidation(
        self, profile_id: int, consolidation_id: int,
    ) -> Optional[dict[str, Any]]:
        rows = self.connection().execute(
            "SELECT * FROM sync_deletion_attempts WHERE profile_id=? "
            "AND consolidation_id=? AND state IN "
            "('pending','partial','outcome_unknown') "
            "ORDER BY deletion_attempt_id",
            (profile_id, consolidation_id),
        ).fetchall()
        if len(rows) > 1:
            raise RuntimeError(
                "Deletion ledger corruption: multiple unresolved attempts "
                "exist for one consolidation"
            )
        if not rows:
            return None
        row = rows[0]
        result = dict(row)
        unknown = self.connection().execute(
            "SELECT deletion_action_id FROM sync_deletion_actions "
            "WHERE profile_id=? AND deletion_attempt_id=? "
            "AND state='outcome_unknown' ORDER BY ordinal LIMIT 1",
            (profile_id, int(row["deletion_attempt_id"])),
        ).fetchone()
        result["unknown_action_id"] = (
            int(unknown["deletion_action_id"]) if unknown else None
        )
        resumable = self.connection().execute(
            "SELECT 1 FROM sync_deletion_items "
            "WHERE deletion_attempt_id=? "
            "AND state IN ('pending','running','outcome_unknown') LIMIT 1",
            (int(row["deletion_attempt_id"]),),
        ).fetchone()
        result["resumable"] = resumable is not None
        return result

    def deletion_items(
        self, profile_id: int, attempt_id: int,
    ) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.connection().execute(
                "SELECT di.* FROM sync_deletion_items di "
                "JOIN sync_deletion_attempts da "
                "ON da.deletion_attempt_id=di.deletion_attempt_id "
                "WHERE da.profile_id=? AND di.deletion_attempt_id=? "
                "ORDER BY di.ordinal",
                (profile_id, attempt_id),
            ).fetchall()
        ]

    def deletion_parity_items(
        self, deletion_item_id: int,
    ) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.connection().execute(
                "SELECT * FROM sync_deletion_parity_items "
                "WHERE deletion_item_id=? ORDER BY ordinal",
                (deletion_item_id,),
            ).fetchall()
        ]

    def deletion_action(
        self, profile_id: int, action_id: int,
    ) -> Optional[dict[str, Any]]:
        row = self.connection().execute(
            "SELECT * FROM sync_deletion_actions WHERE profile_id=? "
            "AND deletion_action_id=?",
            (profile_id, action_id),
        ).fetchone()
        return dict(row) if row else None

    def deletion_actions_for_attempt(
        self, profile_id: int, attempt_id: int,
    ) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.connection().execute(
                "SELECT * FROM sync_deletion_actions WHERE profile_id=? "
                "AND deletion_attempt_id=? ORDER BY ordinal",
                (profile_id, attempt_id),
            ).fetchall()
        ]

    def mint_deletion_action(
        self, profile_id: int, attempt_id: int, deletion_item_id: int,
        *, site: str, observation_id: int, remote_uuid: str,
        reviewed_identity_fingerprint: str, request_correlation: str,
    ) -> int:
        """Journal exactly one donor delete immediately before its execution."""
        now = _utc_now()
        with self.transaction() as conn:
            attempt = conn.execute(
                "SELECT * FROM sync_deletion_attempts WHERE profile_id=? "
                "AND deletion_attempt_id=?",
                (profile_id, attempt_id),
            ).fetchone()
            item = conn.execute(
                "SELECT * FROM sync_deletion_items WHERE deletion_attempt_id=? "
                "AND deletion_item_id=?",
                (attempt_id, deletion_item_id),
            ).fetchone()
            if (
                attempt is None or item is None
                or str(attempt["state"]) not in {"pending", "partial"}
                or str(item["state"]) != "pending"
                or str(item["site"]) != site
                or int(item["observation_id"]) != observation_id
                or str(item["remote_uuid"] or "") != remote_uuid
                or item["action_id"] is not None
            ):
                raise ValueError("The reviewed donor changed before journaling")
            ordinal = int(conn.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM sync_deletion_actions "
                "WHERE deletion_attempt_id=?",
                (attempt_id,),
            ).fetchone()[0])
            action_type = (
                "inat_observation_delete" if site == "inat"
                else "mo_observation_delete"
            )
            cursor = conn.execute(
                "INSERT INTO sync_deletion_actions("
                "profile_id,deletion_attempt_id,deletion_item_id,ordinal,"
                "action_type,site,observation_id,remote_uuid,state,"
                "request_correlation,reviewed_identity_fingerprint,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,"
                "'pending',?,?,?,?)",
                (
                    profile_id, attempt_id, deletion_item_id, ordinal,
                    action_type, site, observation_id, remote_uuid,
                    request_correlation, reviewed_identity_fingerprint,
                    now, now,
                ),
            )
            action_id = int(cursor.lastrowid)
            cursor = conn.execute(
                "UPDATE sync_deletion_items SET action_id=?,updated_at=? "
                "WHERE deletion_item_id=? AND action_id IS NULL AND state='pending'",
                (action_id, now, deletion_item_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The delete action was not attached atomically")
            return action_id

    def claim_deletion_action(
        self, profile_id: int, action_id: int,
    ) -> bool:
        now = _utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE sync_deletion_actions SET state='running',"
                "attempt_count=attempt_count+1,attempt_started_at=?,"
                "last_phase='preflight',updated_at=? WHERE profile_id=? "
                "AND deletion_action_id=? AND state='pending'",
                (now, now, profile_id, action_id),
            )
            if cursor.rowcount != 1:
                return False
            cursor = conn.execute(
                "UPDATE sync_deletion_items SET state='running',updated_at=? "
                "WHERE action_id=? AND state='pending'",
                (now, action_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The deletion item could not be claimed")
            return True

    def mark_deletion_write_started(
        self, profile_id: int, action_id: int,
    ) -> bool:
        now = _utc_now()
        cursor = self.connection().execute(
            "UPDATE sync_deletion_actions SET write_started_at=?,"
            "last_phase='unsafe_write',updated_at=? WHERE profile_id=? "
            "AND deletion_action_id=? AND state='running' "
            "AND write_started_at IS NULL",
            (now, now, profile_id, action_id),
        )
        return cursor.rowcount == 1

    def mark_deletion_verified(
        self, profile_id: int, action_id: int, verification_state: str,
    ) -> bool:
        if verification_state != "verified_deleted":
            raise ValueError("Only definitive deletion may enter finalization")
        now = _utc_now()
        cursor = self.connection().execute(
            "UPDATE sync_deletion_actions SET verification_state=?,"
            "verified_at=?,last_phase='verification',updated_at=? "
            "WHERE profile_id=? AND deletion_action_id=? "
            "AND state IN ('running','outcome_unknown')",
            (verification_state, now, now, profile_id, action_id),
        )
        return cursor.rowcount == 1

    def cancel_pending_deletion_action(
        self, profile_id: int, action_id: int,
    ) -> bool:
        now = _utc_now()
        with self.transaction() as conn:
            action = conn.execute(
                "SELECT deletion_attempt_id,deletion_item_id,ordinal FROM "
                "sync_deletion_actions WHERE profile_id=? "
                "AND deletion_action_id=? AND state='pending' "
                "AND write_started_at IS NULL",
                (profile_id, action_id),
            ).fetchone()
            if action is None:
                return False
            cursor = conn.execute(
                "UPDATE sync_deletion_actions SET state='cancelled',"
                "last_phase='cancelled_before_write',finished_at=?,updated_at=? "
                "WHERE profile_id=? AND deletion_action_id=? "
                "AND state='pending' AND write_started_at IS NULL",
                (now, now, profile_id, action_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The pending delete action was not cancelled")
            cursor = conn.execute(
                "UPDATE sync_deletion_items SET state='cancelled',updated_at=? "
                "WHERE deletion_item_id=? AND state='pending'",
                (now, int(action["deletion_item_id"])),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The pending deletion item was not cancelled")
            conn.execute(
                "UPDATE sync_deletion_items SET state='cancelled',updated_at=? "
                "WHERE deletion_attempt_id=? AND ordinal>? AND state='pending'",
                (
                    now, int(action["deletion_attempt_id"]),
                    int(action["ordinal"]),
                ),
            )
            succeeded = int(conn.execute(
                "SELECT COUNT(*) FROM sync_deletion_items "
                "WHERE deletion_attempt_id=? AND state='succeeded'",
                (int(action["deletion_attempt_id"]),),
            ).fetchone()[0])
            cursor = conn.execute(
                "UPDATE sync_deletion_attempts SET state=?,updated_at=? "
                "WHERE deletion_attempt_id=? AND state IN ('pending','partial')",
                (
                    "partial" if succeeded else "cancelled",
                    now, int(action["deletion_attempt_id"]),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The cancelled deletion attempt did not settle")
            return True

    def cancel_deletion_tail(
        self, profile_id: int, attempt_id: int, from_ordinal: int,
    ) -> int:
        """Stop an unminted tail and preserve partial-completion semantics."""
        now = _utc_now()
        with self.transaction() as conn:
            attempt = conn.execute(
                "SELECT state FROM sync_deletion_attempts "
                "WHERE profile_id=? AND deletion_attempt_id=?",
                (profile_id, attempt_id),
            ).fetchone()
            if attempt is None or str(attempt["state"]) not in {
                "pending", "partial",
            }:
                return 0
            cursor = conn.execute(
                "UPDATE sync_deletion_items SET state='cancelled',updated_at=? "
                "WHERE deletion_attempt_id=? AND ordinal>=? AND state='pending' "
                "AND action_id IS NULL",
                (now, attempt_id, from_ordinal),
            )
            changed = int(cursor.rowcount)
            succeeded = int(conn.execute(
                "SELECT COUNT(*) FROM sync_deletion_items "
                "WHERE deletion_attempt_id=? AND state='succeeded'",
                (attempt_id,),
            ).fetchone()[0])
            cursor = conn.execute(
                "UPDATE sync_deletion_attempts SET state=?,updated_at=? "
                "WHERE deletion_attempt_id=? AND state IN ('pending','partial')",
                ("partial" if succeeded else "cancelled", now, attempt_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The unminted deletion tail did not settle")
            return changed

    def recover_running_deletion_actions(self) -> int:
        """Crash recovery: pre-write claims are retryable; sent writes are unknown."""
        rows = self.connection().execute(
            "SELECT profile_id,deletion_action_id FROM sync_deletion_actions "
            "WHERE state='running'"
        ).fetchall()
        for row in rows:
            self.normalize_running_deletion_action(
                int(row["profile_id"]), int(row["deletion_action_id"]),
                error_code="process_interrupted",
            )
        return len(rows)

    def normalize_running_deletion_action(
        self, profile_id: int, action_id: int, *,
        error_code: str = "interrupted",
    ) -> Optional[str]:
        """Resolve a discovered running row from its durable write boundary.

        A pre-write claim is returned to ``pending``.  Once write_started_at
        exists, no exception or cancellation can safely imply failure, so the
        action becomes ``outcome_unknown`` and its untouched tail is stopped.
        """
        now = _utc_now()
        with self.transaction() as conn:
            action = conn.execute(
                "SELECT deletion_attempt_id,deletion_item_id,ordinal,"
                "write_started_at FROM sync_deletion_actions "
                "WHERE profile_id=? AND deletion_action_id=? AND state='running'",
                (profile_id, action_id),
            ).fetchone()
            if action is None:
                current = conn.execute(
                    "SELECT state FROM sync_deletion_actions "
                    "WHERE profile_id=? AND deletion_action_id=?",
                    (profile_id, action_id),
                ).fetchone()
                return str(current["state"]) if current else None
            sent = action["write_started_at"] is not None
            state = "outcome_unknown" if sent else "pending"
            cursor = conn.execute(
                "UPDATE sync_deletion_actions SET state=?,last_phase=?,"
                "last_error_code=?,updated_at=? "
                "WHERE profile_id=? AND deletion_action_id=? AND state='running'",
                (
                    state, "verification" if sent else "journaled",
                    (
                        f"{error_code}_after_write"
                        if sent else f"{error_code}_before_write"
                    )[:80],
                    now, profile_id, action_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The running deletion action was not normalized")
            cursor = conn.execute(
                "UPDATE sync_deletion_items SET state=?,updated_at=? "
                "WHERE deletion_item_id=? AND state='running'",
                (state, now, int(action["deletion_item_id"])),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The running deletion item was not normalized")
            if sent:
                conn.execute(
                    "UPDATE sync_deletion_items SET state='cancelled',updated_at=? "
                    "WHERE deletion_attempt_id=? AND ordinal>? AND state='pending'",
                    (
                        now, int(action["deletion_attempt_id"]),
                        int(action["ordinal"]),
                    ),
                )
                cursor = conn.execute(
                    "UPDATE sync_deletion_attempts "
                    "SET state='outcome_unknown',updated_at=? "
                    "WHERE deletion_attempt_id=? "
                    "AND state IN ('pending','partial')",
                    (now, int(action["deletion_attempt_id"])),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "The unknown deletion attempt was not preserved"
                    )
            return state

    def finish_deletion_action(
        self, profile_id: int, action_id: int, state: str, *,
        verification_state: str = "", error_code: str = "",
        http_status: Optional[int] = None,
    ) -> bool:
        if state not in {
            "failed", "cancelled", "outcome_unknown", "retry_required",
        }:
            raise ValueError("Invalid non-success deletion action state")
        now = _utc_now()
        with self.transaction() as conn:
            action = conn.execute(
                "SELECT deletion_attempt_id,deletion_item_id,ordinal,state "
                "FROM sync_deletion_actions WHERE profile_id=? "
                "AND deletion_action_id=?",
                (profile_id, action_id),
            ).fetchone()
            if action is None or str(action["state"]) not in {"running", "outcome_unknown"}:
                return False
            cursor = conn.execute(
                "UPDATE sync_deletion_actions SET state=?,last_phase='verification',"
                "last_error_code=?,last_http_status=?,verification_state=?,"
                "verified_at=CASE WHEN ?!='' THEN ? ELSE verified_at END,"
                "finished_at=?,updated_at=? WHERE profile_id=? "
                "AND deletion_action_id=? AND state=?",
                (
                    state, error_code[:80], http_status, verification_state,
                    verification_state, now, now, now, profile_id, action_id,
                    str(action["state"]),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The deletion action terminal update failed")
            cursor = conn.execute(
                "UPDATE sync_deletion_items SET state=?,updated_at=? "
                "WHERE deletion_item_id=? AND state IN ('running','outcome_unknown')",
                (state, now, int(action["deletion_item_id"])),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The deletion item terminal update failed")
            conn.execute(
                "UPDATE sync_deletion_items SET state='cancelled',updated_at=? "
                "WHERE deletion_attempt_id=? AND ordinal>? AND state='pending'",
                (
                    now, int(action["deletion_attempt_id"]),
                    int(action["ordinal"]),
                ),
            )
            succeeded = int(conn.execute(
                "SELECT COUNT(*) FROM sync_deletion_items "
                "WHERE deletion_attempt_id=? AND state='succeeded'",
                (int(action["deletion_attempt_id"]),),
            ).fetchone()[0])
            attempt_state = (
                "outcome_unknown" if state == "outcome_unknown"
                else "partial" if succeeded else state
            )
            cursor = conn.execute(
                "UPDATE sync_deletion_attempts SET state=?,updated_at=? "
                "WHERE deletion_attempt_id=? "
                "AND state IN ('pending','partial','outcome_unknown')",
                (attempt_state, now, int(action["deletion_attempt_id"])),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The deletion attempt terminal update failed")
            return True

    def settle_deletion_success(
        self, profile_id: int, action_id: int,
    ) -> bool:
        """M7 exact-count, all-or-nothing local tombstone finalization."""
        now = _utc_now()
        with self.transaction() as conn:
            action = conn.execute(
                "SELECT * FROM sync_deletion_actions WHERE profile_id=? "
                "AND deletion_action_id=?",
                (profile_id, action_id),
            ).fetchone()
            if action is not None and str(action["state"]) == "succeeded":
                settled = conn.execute(
                    "SELECT 1 FROM sync_deletion_items di "
                    "JOIN sync_consolidation_members m "
                    "ON m.consolidation_member_id=di.stable_member_id "
                    "WHERE di.deletion_item_id=? "
                    "AND di.deletion_attempt_id=? AND di.action_id=? "
                    "AND di.state='succeeded' AND m.remote_state='deleted' "
                    "AND m.deleted_by_deletion_attempt_id=di.deletion_attempt_id "
                    "AND m.deleted_by_deletion_item_id=di.deletion_item_id",
                    (
                        int(action["deletion_item_id"]),
                        int(action["deletion_attempt_id"]), action_id,
                    ),
                ).fetchone()
                if settled is not None:
                    return True
                raise ValueError(
                    "A succeeded delete action lacks its exact tombstone"
                )
            if (
                action is None
                or str(action["state"]) not in {"running", "outcome_unknown"}
                or str(action["verification_state"]) != "verified_deleted"
            ):
                raise ValueError("The exact delete action is not verified")
            item = conn.execute(
                "SELECT * FROM sync_deletion_items WHERE deletion_item_id=? "
                "AND deletion_attempt_id=? AND action_id=?",
                (
                    int(action["deletion_item_id"]),
                    int(action["deletion_attempt_id"]), action_id,
                ),
            ).fetchone()
            attempt = conn.execute(
                "SELECT * FROM sync_deletion_attempts WHERE profile_id=? "
                "AND deletion_attempt_id=?",
                (profile_id, int(action["deletion_attempt_id"])),
            ).fetchone()
            if item is None or attempt is None:
                raise ValueError("Deletion attempt provenance is missing")
            consolidation = conn.execute(
                "SELECT * FROM sync_consolidations WHERE profile_id=? "
                "AND consolidation_id=? AND state='finalized'",
                (profile_id, int(attempt["consolidation_id"])),
            ).fetchone()
            member = conn.execute(
                "SELECT * FROM sync_consolidation_members "
                "WHERE consolidation_member_id=? AND profile_id=?",
                (int(item["stable_member_id"]), profile_id),
            ).fetchone()
            profile = conn.execute(
                "SELECT inat_user_id,mo_user_id FROM sync_profiles "
                "WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            expected_action_type = (
                "inat_observation_delete"
                if str(item["site"]) == "inat"
                else "mo_observation_delete"
            )
            expected_owner_id = (
                int(profile["inat_user_id"])
                if profile is not None and str(item["site"]) == "inat"
                else int(profile["mo_user_id"]) if profile is not None else 0
            )
            expected_identity = canonical_stable_identity_fingerprint(
                RemoteSite(str(item["site"])),
                int(item["observation_id"]),
                str(item["remote_uuid"] or ""),
                expected_owner_id,
            )
            if (
                consolidation is None or member is None or profile is None
                or int(consolidation["current_finalized_attempt_id"] or 0)
                != int(attempt["base_finalized_attempt_id"])
                or str(action["action_type"]) != expected_action_type
                or str(action["site"]) != str(item["site"])
                or str(action["site"]) != str(member["site"])
                or int(action["observation_id"]) != int(item["observation_id"])
                or int(action["observation_id"]) != int(member["observation_id"])
                or str(action["remote_uuid"] or "")
                != str(item["remote_uuid"] or "")
                or str(action["remote_uuid"] or "")
                != str(member["remote_uuid"] or "")
                or str(action["reviewed_identity_fingerprint"])
                != expected_identity
                or action["write_started_at"] is None
                or str(member["role"]) != "donor"
                or str(member["local_state"]) != "superseded"
                or str(member["remote_state"]) != "online"
                or str(member["site"]) != str(item["site"])
                or int(member["observation_id"]) != int(item["observation_id"])
                or str(member["remote_uuid"] or "") != str(item["remote_uuid"] or "")
            ):
                raise ValueError("A tombstone finalization invariant changed")
            canonical_id = (
                consolidation["canonical_inat_observation_id"]
                if str(item["site"]) == "inat"
                else consolidation["canonical_mo_observation_id"]
            )
            if canonical_id is not None and int(canonical_id) == int(item["observation_id"]):
                raise ValueError("A canonical observation cannot be tombstoned")
            cursor = conn.execute(
                "UPDATE sync_consolidation_members SET remote_state='deleted',"
                "deleted_remotely_at=?,deleted_by_deletion_attempt_id=?,"
                "deleted_by_deletion_item_id=?,"
                "last_deletion_reviewed_record_fingerprint=?,"
                "canonical_destination_mo_id=?,canonical_destination_inat_id=?,"
                "updated_at=? WHERE consolidation_member_id=? "
                "AND remote_state='online' AND role='donor' "
                "AND local_state='superseded'",
                (
                    now, int(attempt["deletion_attempt_id"]),
                    int(item["deletion_item_id"]),
                    str(item["reviewed_remote_record_fingerprint"]),
                    consolidation["canonical_mo_observation_id"],
                    consolidation["canonical_inat_observation_id"], now,
                    int(member["consolidation_member_id"]),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The stable donor tombstone update failed")
            cursor = conn.execute(
                "UPDATE sync_deletion_actions SET state='succeeded',"
                "last_phase='finalization',finished_at=?,verified_at=?,"
                "updated_at=? WHERE deletion_action_id=? AND state=? "
                "AND verification_state='verified_deleted'",
                (now, now, now, action_id, str(action["state"])),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The verified delete action did not settle")
            cursor = conn.execute(
                "UPDATE sync_deletion_items SET state='succeeded',updated_at=? "
                "WHERE deletion_item_id=? AND state IN ('running','outcome_unknown')",
                (now, int(item["deletion_item_id"])),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The deletion item did not settle")
            remaining = int(conn.execute(
                "SELECT COUNT(*) FROM sync_deletion_items "
                "WHERE deletion_attempt_id=? AND state!='succeeded'",
                (int(attempt["deletion_attempt_id"]),),
            ).fetchone()[0])
            attempt_state = "succeeded" if remaining == 0 else "partial"
            cursor = conn.execute(
                "UPDATE sync_deletion_attempts SET state=?,updated_at=? "
                "WHERE deletion_attempt_id=? AND state IN "
                "('pending','partial','outcome_unknown')",
                (attempt_state, now, int(attempt["deletion_attempt_id"])),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The deletion attempt did not advance")
            return True

    def consolidation_history(self, profile_id: int) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.connection().execute(
                "SELECT c.consolidation_id,c.state,c.canonical_mo_observation_id,"
                "c.canonical_inat_observation_id,c.canonical_pair_id,"
                "c.current_finalized_attempt_id,c.updated_at,"
                "ca.attempt_id,ca.action_group_id,ca.state AS attempt_state,"
                "(SELECT COUNT(*) FROM sync_consolidation_members m "
                " WHERE m.consolidation_id=c.consolidation_id AND m.role='donor') AS donor_count,"
                # Split out so no caller has to assume a donor still exists
                # remotely: Gate 2C deletion leaves role='donor' in place and
                # only moves remote_state.
                "(SELECT COUNT(*) FROM sync_consolidation_members m4 "
                " WHERE m4.consolidation_id=c.consolidation_id AND m4.role='donor' "
                " AND COALESCE(m4.remote_state,'online')='online') AS donor_online_count,"
                "(SELECT COUNT(*) FROM sync_consolidation_members m5 "
                " WHERE m5.consolidation_id=c.consolidation_id AND m5.role='donor' "
                " AND m5.remote_state='deleted') AS donor_deleted_count "
                "FROM sync_consolidations c LEFT JOIN sync_consolidation_attempts ca "
                "ON ca.attempt_id=(SELECT ca2.attempt_id FROM sync_consolidation_attempts ca2 "
                "WHERE ca2.consolidation_id=c.consolidation_id "
                "ORDER BY ca2.attempt_id DESC LIMIT 1) "
                "WHERE c.profile_id=? ORDER BY c.updated_at DESC,c.consolidation_id DESC",
                (profile_id,),
            ).fetchall()
        ]

    def consolidation_detail(
        self, profile_id: int, consolidation_id: int,
    ) -> Optional[dict[str, Any]]:
        result = self.get_consolidation(profile_id, consolidation_id)
        if result is None:
            return None
        result["members"] = self.list_consolidation_members(profile_id, consolidation_id)
        canonical_parts = []
        if result.get("canonical_mo_observation_id") is not None:
            canonical_parts.append(f"MO {int(result['canonical_mo_observation_id'])}")
        if result.get("canonical_inat_observation_id") is not None:
            canonical_parts.append(
                f"iNat {int(result['canonical_inat_observation_id'])}"
            )
        canonical_label = " / ".join(canonical_parts)
        for member in result["members"]:
            site = str(member["site"])
            observation_id = int(member["observation_id"])
            member["remote_url"] = (
                f"https://www.inaturalist.org/observations/{observation_id}"
                if site == "inat"
                else f"https://mushroomobserver.org/obs/{observation_id}"
            )
            member["superseded_by"] = (
                f"Superseded by {canonical_label}."
                if str(member["local_state"]) == "superseded"
                else ""
            )
        result["attempts"] = [
            dict(row) for row in self.connection().execute(
                "SELECT * FROM sync_consolidation_attempts WHERE profile_id=? "
                "AND consolidation_id=? ORDER BY attempt_id",
                (profile_id, consolidation_id),
            ).fetchall()
        ]
        original_attempt_id = (
            int(result["attempts"][0]["attempt_id"])
            if result["attempts"] else None
        )
        for attempt in result["attempts"]:
            attempt["is_original_attempt"] = (
                int(attempt["attempt_id"]) == original_attempt_id
            )
            attempt["is_current_finalized_baseline"] = (
                result.get("current_finalized_attempt_id") is not None
                and int(attempt["attempt_id"])
                == int(result["current_finalized_attempt_id"])
            )
            attempt["members"] = self.consolidation_attempt_members(
                profile_id, int(attempt["attempt_id"])
            )
            attempt["evidence"] = self.consolidation_evidence(
                profile_id, int(attempt["attempt_id"])
            )
            attempt["items"] = self.consolidation_items(
                profile_id, int(attempt["attempt_id"])
            )
            attempt["actions"] = self.action_group_rows(
                profile_id, int(attempt["action_group_id"])
            )
        result["deletion_attempts"] = [
            dict(row) for row in self.connection().execute(
                "SELECT * FROM sync_deletion_attempts WHERE profile_id=? "
                "AND consolidation_id=? ORDER BY deletion_attempt_id",
                (profile_id, consolidation_id),
            ).fetchall()
        ]
        for deletion_attempt in result["deletion_attempts"]:
            items = self.deletion_items(
                profile_id, int(deletion_attempt["deletion_attempt_id"])
            )
            for item in items:
                item["parity_items"] = self.deletion_parity_items(
                    int(item["deletion_item_id"])
                )
            deletion_attempt["items"] = items
            deletion_attempt["actions"] = self.deletion_actions_for_attempt(
                profile_id, int(deletion_attempt["deletion_attempt_id"])
            )
        return result

    def superseded_member_keys(self, profile_id: int) -> set[tuple[str, int]]:
        return {
            (str(row["site"]), int(row["observation_id"]))
            for row in self.connection().execute(
                "SELECT site,observation_id FROM sync_consolidation_members "
                "WHERE profile_id=? AND local_state='superseded'",
                (profile_id,),
            ).fetchall()
        }

    def _upsert_issue_tx(
        self, conn: sqlite3.Connection, profile_id: int, issue_type: str, severity: str,
        title: str, detail: str, fingerprint: str, records: Sequence[tuple[str, int]],
    ) -> int:
        row = conn.execute(
            "SELECT issue_id,fingerprint,state FROM sync_issues WHERE profile_id=? AND issue_type=? AND title=?",
            (profile_id, issue_type, title),
        ).fetchone()
        now = _utc_now()
        if row:
            issue_id = int(row[0])
            state = (
                "open" if str(row[1]) != fingerprint or str(row[2]) == "resolved"
                else str(row[2])
            )
            conn.execute(
                "UPDATE sync_issues SET severity=?,detail=?,fingerprint=?,state=?,updated_at=? WHERE issue_id=?",
                (severity, detail, fingerprint, state, now, issue_id),
            )
            conn.execute("DELETE FROM sync_issue_records WHERE profile_id=? AND issue_id=?", (profile_id, issue_id))
        else:
            cur = conn.execute(
                "INSERT INTO sync_issues(profile_id,issue_type,severity,title,detail,fingerprint,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (profile_id, issue_type, severity, title, detail, fingerprint, "open", now, now),
            )
            issue_id = int(cur.lastrowid)
        for site, observation_id in records:
            conn.execute(
                "INSERT INTO sync_issue_records(profile_id,issue_id,site,observation_id) VALUES(?,?,?,?)",
                (profile_id, issue_id, site, observation_id),
            )
        return issue_id

    def _invalidate_pairs_tx(self, conn: sqlite3.Connection, profile_id: int) -> None:
        conn.execute(
            "UPDATE sync_pairs SET review_state='candidate',confirmed_by='',updated_at=? WHERE profile_id=? "
            "AND review_state='confirmed' AND (EXISTS (SELECT 1 FROM sync_records r WHERE r.profile_id=sync_pairs.profile_id "
            "AND r.site='mo' AND r.remote_observation_id=sync_pairs.mo_observation_id AND r.scope_state!='in_scope') "
            "OR EXISTS (SELECT 1 FROM sync_records r WHERE r.profile_id=sync_pairs.profile_id AND r.site='inat' "
            "AND r.remote_observation_id=sync_pairs.inat_observation_id AND r.scope_state!='in_scope'))",
            (_utc_now(), profile_id),
        )


def _classification(score: int) -> str:
    return "strong" if score >= 70 else "possible" if score >= 35 else "hidden"


def _local_fingerprint(*parts: object) -> str:
    import hashlib
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def consolidation_evidence_graph_fingerprint(
    edges: Iterable[tuple[str, int, str, int, str, str, str]],
) -> str:
    """Fingerprint one reviewed evidence graph.

    ``edges`` are 7-tuples of ``(left_site, left_observation_id, right_site,
    right_observation_id, evidence_type, evidence_strength, reviewed_evidence_
    fingerprint)`` whose ends are already canonically ordered.

    Every producer and every verifier of this value MUST route through here.
    Ordering is by the numeric-aware tuple key, never by the joined string: a
    lexicographic sort of the joined parts orders "inat|101|..." before
    "inat|9|..." and so diverges from this one as soon as two edges' ids differ
    in digit count — which silently broke finalization for real observation ids.
    """
    return _local_fingerprint(
        "consolidation_evidence_graph_v1",
        *(
            "|".join((
                left_site, str(left_id), right_site, str(right_id),
                evidence_type, evidence_strength, reviewed_fingerprint,
            ))
            for (
                left_site, left_id, right_site, right_id,
                evidence_type, evidence_strength, reviewed_fingerprint,
            ) in sorted(
                edges,
                key=lambda item: (item[0], item[1], item[2], item[3], item[4]),
            )
        ),
    )


def pair_source_fingerprint(pair: Any) -> str:
    """The canonical confirmed-pair source fingerprint.

    Every gate journals this value as its action group's ``source_fingerprint``
    and recomputes it before each write to prove the reviewed pair has not
    changed. It lives here — accepting either a ``sqlite3.Row`` or a
    ``pair_detail`` dict — so the recompute can never drift from the definition,
    and so ``mark_link_reconciliation_stale`` can advance the stored value in
    lockstep with the very columns it mutates.
    """
    from .normalization import public_fingerprint

    return public_fingerprint(
        "pair", pair["pair_id"], pair["updated_at"], pair["review_state"],
        pair["link_state"], pair["confirmed_by"],
    )


def _review_fingerprint(review: object) -> str:
    return _local_fingerprint(
        review["issue_fingerprint"], review["review_intent"],  # type: ignore[index]
        review["mo_observation_id"], review["inat_observation_id"],  # type: ignore[index]
        review["reviewed_at"],  # type: ignore[index]
    )


def _migration_v1(conn: sqlite3.Connection) -> None:
    statements = [
        """CREATE TABLE sync_profiles(
            profile_id INTEGER PRIMARY KEY, inat_user_id INTEGER NOT NULL, inat_login TEXT NOT NULL,
            mo_user_id INTEGER NOT NULL, mo_login TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT NOT NULL,
            UNIQUE(inat_user_id,mo_user_id))""",
        """CREATE TABLE sync_profile_field_bindings(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            purpose TEXT NOT NULL, field_id INTEGER NOT NULL, exact_name TEXT NOT NULL, datatype TEXT NOT NULL,
            verification_state TEXT NOT NULL, is_override INTEGER NOT NULL DEFAULT 0, verified_at TEXT NOT NULL,
            PRIMARY KEY(profile_id,purpose))""",
        """CREATE TABLE sync_runs(
            run_id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            mode TEXT NOT NULL, outcome TEXT NOT NULL, scan_started_at TEXT NOT NULL, started_at TEXT NOT NULL,
            finished_at TEXT, error_summary TEXT NOT NULL DEFAULT '', capabilities TEXT NOT NULL DEFAULT '')""",
        """CREATE TABLE sync_cursors(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            stream TEXT NOT NULL, successful_scan_started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(profile_id,stream))""",
        """CREATE TABLE sync_records(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            site TEXT NOT NULL CHECK(site IN ('inat','mo')), remote_observation_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL, owner_id INTEGER, owner_login TEXT NOT NULL DEFAULT '', observed_on TEXT,
            taxon_id INTEGER, taxon_name TEXT NOT NULL DEFAULT '', taxon_rank TEXT NOT NULL DEFAULT '',
            public_locality TEXT NOT NULL DEFAULT '', fungi_status TEXT NOT NULL,
            remote_updated_at TEXT, content_fingerprint TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0,
            scope_state TEXT NOT NULL, unpaired_state TEXT NOT NULL DEFAULT 'unpaired_no_candidate', last_seen_at TEXT NOT NULL,
            PRIMARY KEY(profile_id,site,remote_observation_id))""",
        """CREATE TABLE sync_links(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            link_row_id TEXT NOT NULL, source_site TEXT NOT NULL, source_observation_id INTEGER NOT NULL,
            target_site TEXT NOT NULL, target_observation_id INTEGER, direction TEXT NOT NULL, link_state TEXT NOT NULL,
            PRIMARY KEY(profile_id,source_site,link_row_id))""",
        """CREATE TABLE sync_identifiers(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            site TEXT NOT NULL, observation_id INTEGER NOT NULL, identifier_type TEXT NOT NULL, normalized_value TEXT NOT NULL,
            PRIMARY KEY(profile_id,site,observation_id,identifier_type,normalized_value))""",
        """CREATE TABLE sync_sequence_hashes(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            site TEXT NOT NULL, observation_id INTEGER NOT NULL, sequence_hash TEXT NOT NULL,
            PRIMARY KEY(profile_id,site,observation_id,sequence_hash))""",
        """CREATE TABLE sync_media_hashes(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            site TEXT NOT NULL, observation_id INTEGER NOT NULL, photo_id TEXT NOT NULL, rendition TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL DEFAULT '', exact_pixel_hash TEXT NOT NULL DEFAULT '', perceptual_hash TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(profile_id,site,observation_id,photo_id,rendition))""",
        """CREATE TABLE sync_pairs(
            pair_id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            mo_observation_id INTEGER NOT NULL, inat_observation_id INTEGER NOT NULL, link_state TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL DEFAULT 0, classification TEXT NOT NULL, review_state TEXT NOT NULL DEFAULT 'candidate',
            confirmed_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(profile_id,mo_observation_id,inat_observation_id))""",
        """CREATE TABLE sync_evidence(
            evidence_id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            pair_id INTEGER NOT NULL REFERENCES sync_pairs(pair_id) ON DELETE CASCADE, evidence_type TEXT NOT NULL,
            family TEXT NOT NULL, score INTEGER NOT NULL, tier INTEGER NOT NULL, explanation TEXT NOT NULL)""",
        """CREATE TABLE sync_pair_exclusions(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            mo_observation_id INTEGER NOT NULL, inat_observation_id INTEGER NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(profile_id,mo_observation_id,inat_observation_id))""",
        """CREATE TABLE sync_issues(
            issue_id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            issue_type TEXT NOT NULL, severity TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL,
            fingerprint TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(profile_id,issue_type,title))""",
        """CREATE TABLE sync_issue_records(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            issue_id INTEGER NOT NULL REFERENCES sync_issues(issue_id) ON DELETE CASCADE,
            site TEXT NOT NULL, observation_id INTEGER NOT NULL,
            PRIMARY KEY(profile_id,issue_id,site,observation_id))""",
        "CREATE INDEX idx_sync_records_scope ON sync_records(profile_id,scope_state,site,remote_updated_at)",
        "CREATE INDEX idx_sync_pairs_review ON sync_pairs(profile_id,review_state,classification,score)",
        "CREATE INDEX idx_sync_issues_state ON sync_issues(profile_id,state,issue_type,updated_at)",
        "CREATE UNIQUE INDEX uq_sync_confirmed_mo ON sync_pairs(profile_id,mo_observation_id) WHERE review_state='confirmed'",
        "CREATE UNIQUE INDEX uq_sync_confirmed_inat ON sync_pairs(profile_id,inat_observation_id) WHERE review_state='confirmed'",
    ]
    for statement in statements:
        conn.execute(statement)


def _migration_v2(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE sync_records ADD COLUMN link_malformed INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE sync_records ADD COLUMN changed_at TEXT")
    conn.execute("ALTER TABLE sync_links ADD COLUMN external_site_id INTEGER")
    conn.execute("ALTER TABLE sync_links ADD COLUMN parse_state TEXT NOT NULL DEFAULT 'valid'")
    conn.execute("ALTER TABLE sync_links ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE sync_pair_exclusions ADD COLUMN source_fingerprint TEXT NOT NULL DEFAULT ''")

    conn.execute("ALTER TABLE sync_identifiers RENAME TO sync_identifiers_v1")
    conn.execute(
        """CREATE TABLE sync_identifiers(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            site TEXT NOT NULL, observation_id INTEGER NOT NULL, identifier_type TEXT NOT NULL,
            normalized_value TEXT NOT NULL, evidence_tier INTEGER NOT NULL,
            PRIMARY KEY(profile_id,site,observation_id,identifier_type,normalized_value,evidence_tier))"""
    )
    # The v1 table did not record acquisition provenance. Do not guess a tier.
    conn.execute("DROP TABLE sync_identifiers_v1")

    conn.execute("ALTER TABLE sync_sequence_hashes RENAME TO sync_sequence_hashes_v1")
    conn.execute(
        """CREATE TABLE sync_sequence_hashes(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            site TEXT NOT NULL, observation_id INTEGER NOT NULL, sequence_hash TEXT NOT NULL,
            evidence_tier INTEGER NOT NULL,
            PRIMARY KEY(profile_id,site,observation_id,sequence_hash,evidence_tier))"""
    )
    # The v1 table did not distinguish inventory, enrichment, or deep reads.
    conn.execute("DROP TABLE sync_sequence_hashes_v1")
    conn.execute(
        "CREATE INDEX idx_sync_links_source ON sync_links(profile_id,source_site,source_observation_id)"
    )
    conn.execute(
        "CREATE INDEX idx_sync_identifiers_value ON sync_identifiers(profile_id,identifier_type,normalized_value,site)"
    )
    conn.execute(
        "CREATE INDEX idx_sync_sequences_hash ON sync_sequence_hashes(profile_id,sequence_hash,site)"
    )


def _migration_v3(conn: sqlite3.Connection) -> None:
    """Add durable review history and discard evidence with unknowable v1 provenance."""
    conn.execute(
        "ALTER TABLE sync_records ADD COLUMN availability_state TEXT NOT NULL DEFAULT 'available'"
    )
    conn.execute("ALTER TABLE sync_pairs ADD COLUMN ever_reviewed INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE sync_pairs ADD COLUMN ever_confirmed INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "ALTER TABLE sync_pairs ADD COLUMN historical_confirmed_by TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "UPDATE sync_pairs SET ever_reviewed=CASE WHEN review_state IN ('confirmed','rejected') THEN 1 ELSE 0 END,"
        "ever_confirmed=CASE WHEN review_state='confirmed' THEN 1 ELSE 0 END,"
        "historical_confirmed_by=CASE WHEN review_state='confirmed' THEN confirmed_by ELSE '' END"
    )
    conn.execute("ALTER TABLE sync_media_hashes ADD COLUMN source_site TEXT")
    conn.execute("ALTER TABLE sync_media_hashes ADD COLUMN source_photo_id TEXT NOT NULL DEFAULT ''")
    # Version 1 did not record acquisition provenance. Keeping these rows could
    # turn hydrated evidence into inventory evidence indefinitely, so rebuild it.
    conn.execute("DELETE FROM sync_identifiers")
    conn.execute("DELETE FROM sync_sequence_hashes")
    conn.execute(
        "UPDATE sync_pairs SET score=0,classification='hidden' WHERE pair_id IN "
        "(SELECT DISTINCT pair_id FROM sync_evidence WHERE family IN ('specimen','barcode'))"
    )
    conn.execute(
        "DELETE FROM sync_evidence WHERE family IN ('specimen','barcode')"
    )


def _migration_v4(conn: sqlite3.Connection) -> None:
    """Add the credential-free Gate 1B reciprocal-link action journal."""
    statements = [
        """CREATE TABLE sync_link_issue_reviews(
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            issue_id INTEGER NOT NULL REFERENCES sync_issues(issue_id) ON DELETE CASCADE,
            review_intent TEXT NOT NULL CHECK(review_intent IN ('reciprocal','remove_only')),
            mo_observation_id INTEGER, inat_observation_id INTEGER,
            issue_fingerprint TEXT NOT NULL, review_state TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            PRIMARY KEY(profile_id,issue_id))""",
        """CREATE TABLE sync_action_groups(
            action_group_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('pair','issue')),
            pair_id INTEGER REFERENCES sync_pairs(pair_id),
            issue_id INTEGER REFERENCES sync_issues(issue_id),
            source_fingerprint TEXT NOT NULL,
            mo_observation_id INTEGER NOT NULL, inat_observation_id INTEGER NOT NULL,
            previewed_at TEXT NOT NULL, confirmed_at TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(profile_id,action_group_id))""",
        """CREATE TABLE sync_actions(
            action_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            action_group_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            action_type TEXT NOT NULL CHECK(action_type IN (
                'inat_ofv_add','inat_ofv_repair','inat_ofv_remove',
                'mo_external_link_add','mo_external_link_repair','mo_external_link_remove')),
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            state TEXT NOT NULL CHECK(state IN (
                'pending','running','succeeded','failed','cancelled','outcome_unknown')),
            last_phase TEXT NOT NULL,
            pair_id INTEGER REFERENCES sync_pairs(pair_id),
            issue_id INTEGER REFERENCES sync_issues(issue_id),
            mo_observation_id INTEGER NOT NULL, inat_observation_id INTEGER NOT NULL,
            inat_observation_uuid TEXT NOT NULL,
            binding_id INTEGER, remote_row_id TEXT NOT NULL DEFAULT '',
            remote_row_uuid TEXT NOT NULL DEFAULT '',
            current_target_id INTEGER, desired_target_id INTEGER,
            destructive INTEGER NOT NULL DEFAULT 0,
            preview_inat_record_fingerprint TEXT NOT NULL,
            preview_mo_record_fingerprint TEXT NOT NULL,
            preview_inat_links_fingerprint TEXT NOT NULL,
            preview_mo_links_fingerprint TEXT NOT NULL,
            deduplication_key TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            attempt_started_at TEXT, write_started_at TEXT, finished_at TEXT,
            last_error_code TEXT NOT NULL DEFAULT '', last_http_status INTEGER,
            verification_state TEXT NOT NULL DEFAULT '', verified_at TEXT,
            server_row_id TEXT NOT NULL DEFAULT '', server_row_uuid TEXT NOT NULL DEFAULT '',
            outcome_unknown INTEGER NOT NULL DEFAULT 0,
            supersedes_action_id INTEGER REFERENCES sync_actions(action_id),
            created_at TEXT NOT NULL, confirmed_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id) ON DELETE CASCADE,
            UNIQUE(profile_id,action_id), UNIQUE(profile_id,action_group_id,ordinal))""",
        """CREATE TABLE sync_action_snapshot_rows(
            snapshot_row_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            action_group_id INTEGER NOT NULL,
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            observation_id INTEGER NOT NULL,
            remote_row_id TEXT NOT NULL DEFAULT '', remote_row_uuid TEXT NOT NULL DEFAULT '',
            binding_id INTEGER, normalized_target_id INTEGER,
            parse_state TEXT NOT NULL, row_fingerprint TEXT NOT NULL,
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id) ON DELETE CASCADE,
            UNIQUE(profile_id,action_group_id,site,remote_row_id,remote_row_uuid))""",
        "CREATE INDEX idx_sync_actions_state ON sync_actions(profile_id,state,created_at)",
        "CREATE INDEX idx_sync_actions_group ON sync_actions(profile_id,action_group_id,ordinal)",
        "CREATE UNIQUE INDEX uq_sync_unresolved_action ON sync_actions(profile_id,deduplication_key) "
        "WHERE state IN ('pending','running','outcome_unknown')",
    ]
    for statement in statements:
        conn.execute(statement)


def _migration_v5(conn: sqlite3.Connection) -> None:
    """Extend the shared action journal with persistence-safe Gate 1C evidence."""
    conn.execute("ALTER TABLE sync_actions RENAME TO sync_actions_v4")
    conn.execute(
        """CREATE TABLE sync_actions(
            action_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            action_group_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            action_type TEXT NOT NULL CHECK(action_type IN (
                'inat_ofv_add','inat_ofv_repair','inat_ofv_remove',
                'mo_external_link_add','mo_external_link_repair','mo_external_link_remove',
                'inat_its_add','inat_its_repair','inat_its_remove',
                'mo_sequence_add','mo_sequence_repair')),
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            state TEXT NOT NULL CHECK(state IN (
                'pending','running','succeeded','failed','cancelled','outcome_unknown')),
            last_phase TEXT NOT NULL,
            pair_id INTEGER REFERENCES sync_pairs(pair_id),
            issue_id INTEGER REFERENCES sync_issues(issue_id),
            mo_observation_id INTEGER NOT NULL, inat_observation_id INTEGER NOT NULL,
            inat_observation_uuid TEXT NOT NULL,
            binding_id INTEGER, remote_row_id TEXT NOT NULL DEFAULT '',
            remote_row_uuid TEXT NOT NULL DEFAULT '',
            current_target_id INTEGER, desired_target_id INTEGER,
            destructive INTEGER NOT NULL DEFAULT 0,
            preview_inat_record_fingerprint TEXT NOT NULL,
            preview_mo_record_fingerprint TEXT NOT NULL,
            preview_inat_links_fingerprint TEXT NOT NULL,
            preview_mo_links_fingerprint TEXT NOT NULL,
            deduplication_key TEXT NOT NULL,
            source_site TEXT CHECK(source_site IN ('inat','mo')),
            source_record_id INTEGER,
            source_sequence_remote_id TEXT NOT NULL DEFAULT '',
            sequence_fingerprint TEXT NOT NULL DEFAULT '',
            normalized_accession TEXT NOT NULL DEFAULT '',
            source_metadata_fingerprint TEXT NOT NULL DEFAULT '',
            destination_preflight_fingerprint TEXT NOT NULL DEFAULT '',
            evidence_type TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            attempt_started_at TEXT, write_started_at TEXT, finished_at TEXT,
            last_error_code TEXT NOT NULL DEFAULT '', last_http_status INTEGER,
            verification_state TEXT NOT NULL DEFAULT '', verified_at TEXT,
            server_row_id TEXT NOT NULL DEFAULT '', server_row_uuid TEXT NOT NULL DEFAULT '',
            outcome_unknown INTEGER NOT NULL DEFAULT 0,
            supersedes_action_id INTEGER REFERENCES sync_actions(action_id),
            created_at TEXT NOT NULL, confirmed_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id) ON DELETE CASCADE,
            UNIQUE(profile_id,action_id), UNIQUE(profile_id,action_group_id,ordinal))"""
    )
    old_columns = (
        "action_id,profile_id,action_group_id,ordinal,action_type,site,state,last_phase,"
        "pair_id,issue_id,mo_observation_id,inat_observation_id,inat_observation_uuid,"
        "binding_id,remote_row_id,remote_row_uuid,current_target_id,desired_target_id,destructive,"
        "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
        "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
        "attempt_count,attempt_started_at,write_started_at,finished_at,last_error_code,last_http_status,"
        "verification_state,verified_at,server_row_id,server_row_uuid,outcome_unknown,"
        "supersedes_action_id,created_at,confirmed_at,updated_at"
    )
    conn.execute(
        f"INSERT INTO sync_actions({old_columns}) SELECT {old_columns} FROM sync_actions_v4"
    )
    conn.execute("DROP TABLE sync_actions_v4")
    conn.execute("CREATE INDEX idx_sync_actions_state ON sync_actions(profile_id,state,created_at)")
    conn.execute("CREATE INDEX idx_sync_actions_group ON sync_actions(profile_id,action_group_id,ordinal)")
    conn.execute(
        "CREATE UNIQUE INDEX uq_sync_unresolved_action ON sync_actions(profile_id,deduplication_key) "
        "WHERE state IN ('pending','running','outcome_unknown')"
    )


def _migration_v6(conn: sqlite3.Connection) -> None:
    """Add the archive-qualified accession column for Gate 1C ITS actions.

    A dedicated migration (rather than editing v5 in place) so that any database
    that already completed an earlier schema-v5 build gains the column too.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(sync_actions)").fetchall()}
    if "normalized_archive" not in columns:
        conn.execute(
            "ALTER TABLE sync_actions ADD COLUMN normalized_archive TEXT NOT NULL DEFAULT ''"
        )


def _migration_v7(conn: sqlite3.Connection) -> None:
    """Gate 1D: coordinate action types/columns plus name-proposal tracking.

    Extending the ``action_type`` CHECK requires a table rebuild (SQLite cannot
    ALTER a CHECK), so ``sync_actions`` is rebuilt exactly as ``_migration_v5``
    did. New privacy-safe coordinate columns are added; raw coordinates are never
    stored. Two new tables track iNaturalist identify delegation and the separate
    Mushroom Observer proposal lifecycle.
    """
    conn.execute("ALTER TABLE sync_actions RENAME TO sync_actions_v6")
    conn.execute(
        """CREATE TABLE sync_actions(
            action_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            action_group_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            action_type TEXT NOT NULL CHECK(action_type IN (
                'inat_ofv_add','inat_ofv_repair','inat_ofv_remove',
                'mo_external_link_add','mo_external_link_repair','mo_external_link_remove',
                'inat_its_add','inat_its_repair','inat_its_remove',
                'mo_sequence_add','mo_sequence_repair',
                'inat_coordinate_set','inat_coordinate_replace')),
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            state TEXT NOT NULL CHECK(state IN (
                'pending','running','succeeded','failed','cancelled','outcome_unknown')),
            last_phase TEXT NOT NULL,
            pair_id INTEGER REFERENCES sync_pairs(pair_id),
            issue_id INTEGER REFERENCES sync_issues(issue_id),
            mo_observation_id INTEGER NOT NULL, inat_observation_id INTEGER NOT NULL,
            inat_observation_uuid TEXT NOT NULL,
            binding_id INTEGER, remote_row_id TEXT NOT NULL DEFAULT '',
            remote_row_uuid TEXT NOT NULL DEFAULT '',
            current_target_id INTEGER, desired_target_id INTEGER,
            destructive INTEGER NOT NULL DEFAULT 0,
            preview_inat_record_fingerprint TEXT NOT NULL,
            preview_mo_record_fingerprint TEXT NOT NULL,
            preview_inat_links_fingerprint TEXT NOT NULL,
            preview_mo_links_fingerprint TEXT NOT NULL,
            deduplication_key TEXT NOT NULL,
            source_site TEXT CHECK(source_site IN ('inat','mo')),
            source_record_id INTEGER,
            source_sequence_remote_id TEXT NOT NULL DEFAULT '',
            sequence_fingerprint TEXT NOT NULL DEFAULT '',
            normalized_accession TEXT NOT NULL DEFAULT '',
            normalized_archive TEXT NOT NULL DEFAULT '',
            source_metadata_fingerprint TEXT NOT NULL DEFAULT '',
            destination_preflight_fingerprint TEXT NOT NULL DEFAULT '',
            evidence_type TEXT NOT NULL DEFAULT '',
            source_privacy_state TEXT NOT NULL DEFAULT '',
            proposed_privacy_state TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            attempt_started_at TEXT, write_started_at TEXT, finished_at TEXT,
            last_error_code TEXT NOT NULL DEFAULT '', last_http_status INTEGER,
            verification_state TEXT NOT NULL DEFAULT '', verified_at TEXT,
            server_row_id TEXT NOT NULL DEFAULT '', server_row_uuid TEXT NOT NULL DEFAULT '',
            outcome_unknown INTEGER NOT NULL DEFAULT 0,
            supersedes_action_id INTEGER REFERENCES sync_actions(action_id),
            created_at TEXT NOT NULL, confirmed_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id) ON DELETE CASCADE,
            UNIQUE(profile_id,action_id), UNIQUE(profile_id,action_group_id,ordinal))"""
    )
    old_columns = (
        "action_id,profile_id,action_group_id,ordinal,action_type,site,state,last_phase,"
        "pair_id,issue_id,mo_observation_id,inat_observation_id,inat_observation_uuid,"
        "binding_id,remote_row_id,remote_row_uuid,current_target_id,desired_target_id,destructive,"
        "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
        "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
        "source_site,source_record_id,source_sequence_remote_id,sequence_fingerprint,"
        "normalized_accession,normalized_archive,source_metadata_fingerprint,"
        "destination_preflight_fingerprint,evidence_type,"
        "attempt_count,attempt_started_at,write_started_at,finished_at,last_error_code,last_http_status,"
        "verification_state,verified_at,server_row_id,server_row_uuid,outcome_unknown,"
        "supersedes_action_id,created_at,confirmed_at,updated_at"
    )
    conn.execute(
        f"INSERT INTO sync_actions({old_columns}) SELECT {old_columns} FROM sync_actions_v6"
    )
    conn.execute("DROP TABLE sync_actions_v6")
    conn.execute("CREATE INDEX idx_sync_actions_state ON sync_actions(profile_id,state,created_at)")
    conn.execute("CREATE INDEX idx_sync_actions_group ON sync_actions(profile_id,action_group_id,ordinal)")
    conn.execute(
        "CREATE UNIQUE INDEX uq_sync_unresolved_action ON sync_actions(profile_id,deduplication_key) "
        "WHERE state IN ('pending','running','outcome_unknown')"
    )
    conn.execute(
        """CREATE TABLE sync_name_delegations(
            delegation_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            pair_id INTEGER NOT NULL REFERENCES sync_pairs(pair_id) ON DELETE CASCADE,
            identify_action_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(profile_id,pair_id,identify_action_id))"""
    )
    conn.execute(
        """CREATE TABLE sync_mo_proposals(
            proposal_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            pair_id INTEGER NOT NULL REFERENCES sync_pairs(pair_id) ON DELETE CASCADE,
            mo_observation_id INTEGER NOT NULL,
            proposed_name TEXT NOT NULL,
            proposed_name_id INTEGER,
            current_effective_name TEXT NOT NULL DEFAULT '',
            -- Reserved for a future real submission path. This gate is draft-only
            -- (MO has no per-observation submission endpoint), so nothing sets
            -- these; they exist so a later submission step needs no migration.
            proposal_submitted INTEGER NOT NULL DEFAULT 0,
            proposal_remote_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','effective','rejected','superseded')),
            submitted_at TEXT NOT NULL DEFAULT '',
            became_effective_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(profile_id,pair_id,proposed_name))"""
    )


def _migration_v8(conn: sqlite3.Connection) -> None:
    """Gate 1E: the photo-transfer action type plus the transfer ledger.

    ``action_type`` carries a CHECK constraint and SQLite cannot ALTER one, so
    ``sync_actions`` is rebuilt exactly as ``_migration_v7`` did. Two
    privacy-safe photo columns are added; no photo bytes and no signed or
    credential-bearing URL is ever stored.
    """
    conn.execute("ALTER TABLE sync_actions RENAME TO sync_actions_v7")
    conn.execute(
        """CREATE TABLE sync_actions(
            action_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            action_group_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            action_type TEXT NOT NULL CHECK(action_type IN (
                'inat_ofv_add','inat_ofv_repair','inat_ofv_remove',
                'mo_external_link_add','mo_external_link_repair','mo_external_link_remove',
                'inat_its_add','inat_its_repair','inat_its_remove',
                'mo_sequence_add','mo_sequence_repair',
                'inat_coordinate_set','inat_coordinate_replace',
                'inat_photo_attach')),
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            state TEXT NOT NULL CHECK(state IN (
                'pending','running','succeeded','failed','cancelled','outcome_unknown')),
            last_phase TEXT NOT NULL DEFAULT 'preview',
            pair_id INTEGER REFERENCES sync_pairs(pair_id) ON DELETE CASCADE,
            issue_id INTEGER REFERENCES sync_issues(issue_id),
            mo_observation_id INTEGER NOT NULL, inat_observation_id INTEGER NOT NULL,
            inat_observation_uuid TEXT NOT NULL,
            binding_id INTEGER, remote_row_id TEXT NOT NULL DEFAULT '',
            remote_row_uuid TEXT NOT NULL DEFAULT '',
            current_target_id INTEGER, desired_target_id INTEGER,
            destructive INTEGER NOT NULL DEFAULT 0,
            preview_inat_record_fingerprint TEXT NOT NULL,
            preview_mo_record_fingerprint TEXT NOT NULL,
            preview_inat_links_fingerprint TEXT NOT NULL,
            preview_mo_links_fingerprint TEXT NOT NULL,
            deduplication_key TEXT NOT NULL,
            source_site TEXT CHECK(source_site IN ('inat','mo')),
            source_record_id INTEGER,
            source_sequence_remote_id TEXT NOT NULL DEFAULT '',
            sequence_fingerprint TEXT NOT NULL DEFAULT '',
            normalized_accession TEXT NOT NULL DEFAULT '',
            normalized_archive TEXT NOT NULL DEFAULT '',
            source_metadata_fingerprint TEXT NOT NULL DEFAULT '',
            destination_preflight_fingerprint TEXT NOT NULL DEFAULT '',
            evidence_type TEXT NOT NULL DEFAULT '',
            source_privacy_state TEXT NOT NULL DEFAULT '',
            proposed_privacy_state TEXT NOT NULL DEFAULT '',
            -- Gate 1E. ``source_photo_id`` is the remote id at the SOURCE site;
            -- identity is always the (site, id) pair, never a bare number.
            source_photo_id TEXT NOT NULL DEFAULT '',
            -- The client-generated observation_photo uuid. Written BEFORE the
            -- request is sent so a lost response is still recoverable: the
            -- destination re-read returns this uuid, while a bare photo's own
            -- uuid is not readable back at all (report 12.1a).
            planned_observation_photo_uuid TEXT NOT NULL DEFAULT '',
            -- SHA-256 of the image bytes as downloaded during the explicit
            -- preview. The write refuses to proceed unless the source still
            -- hashes to this, so a changed file behind a stable Mushroom
            -- Observer id can never upload content the user did not review.
            reviewed_byte_fingerprint TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            attempt_started_at TEXT, write_started_at TEXT, finished_at TEXT,
            last_error_code TEXT NOT NULL DEFAULT '', last_http_status INTEGER,
            verification_state TEXT NOT NULL DEFAULT '', verified_at TEXT,
            server_row_id TEXT NOT NULL DEFAULT '', server_row_uuid TEXT NOT NULL DEFAULT '',
            outcome_unknown INTEGER NOT NULL DEFAULT 0,
            supersedes_action_id INTEGER REFERENCES sync_actions(action_id),
            created_at TEXT NOT NULL, confirmed_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id) ON DELETE CASCADE,
            UNIQUE(profile_id,action_id), UNIQUE(profile_id,action_group_id,ordinal))"""
    )
    old_columns = (
        "action_id,profile_id,action_group_id,ordinal,action_type,site,state,last_phase,"
        "pair_id,issue_id,mo_observation_id,inat_observation_id,inat_observation_uuid,"
        "binding_id,remote_row_id,remote_row_uuid,current_target_id,desired_target_id,destructive,"
        "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
        "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
        "source_site,source_record_id,source_sequence_remote_id,sequence_fingerprint,"
        "normalized_accession,normalized_archive,source_metadata_fingerprint,"
        "destination_preflight_fingerprint,evidence_type,"
        "source_privacy_state,proposed_privacy_state,"
        "attempt_count,attempt_started_at,write_started_at,finished_at,last_error_code,last_http_status,"
        "verification_state,verified_at,server_row_id,server_row_uuid,outcome_unknown,"
        "supersedes_action_id,created_at,confirmed_at,updated_at"
    )
    conn.execute(
        f"INSERT INTO sync_actions({old_columns}) SELECT {old_columns} FROM sync_actions_v7"
    )
    conn.execute("DROP TABLE sync_actions_v7")
    conn.execute("CREATE INDEX idx_sync_actions_state ON sync_actions(profile_id,state,created_at)")
    conn.execute("CREATE INDEX idx_sync_actions_group ON sync_actions(profile_id,action_group_id,ordinal)")
    conn.execute(
        "CREATE UNIQUE INDEX uq_sync_unresolved_action ON sync_actions(profile_id,deduplication_key) "
        "WHERE state IN ('pending','running','outcome_unknown')"
    )
    conn.execute(
        """CREATE TABLE sync_photo_transfers(
            transfer_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            pair_id INTEGER REFERENCES sync_pairs(pair_id) ON DELETE CASCADE,
            action_id INTEGER REFERENCES sync_actions(action_id),
            -- Identity is ALWAYS (site, id). The same integer means different
            -- photos on the two sites, so a bare id is never sufficient.
            source_site TEXT NOT NULL CHECK(source_site IN ('inat','mo')),
            source_photo_id TEXT NOT NULL,
            destination_site TEXT NOT NULL CHECK(destination_site IN ('inat','mo')),
            destination_observation_id INTEGER NOT NULL,
            destination_photo_id TEXT NOT NULL DEFAULT '',
            destination_observation_photo_uuid TEXT NOT NULL DEFAULT '',
            -- One-way digests of image content, safe to persist. sha256 is our
            -- own duplicate signal; md5 exists because Mushroom Observer accepts
            -- an md5sum natively on create and can dedup server-side with it.
            byte_fingerprint TEXT NOT NULL DEFAULT '',
            md5 TEXT NOT NULL DEFAULT '',
            source_license_label TEXT NOT NULL DEFAULT '',
            source_copyright_holder TEXT NOT NULL DEFAULT '',
            destination_license_code TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','succeeded','failed','outcome_unknown')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            -- The authoritative "have we already sent this one?" key: one source
            -- photo lands at most once on a given destination observation.
            UNIQUE(profile_id,source_site,source_photo_id,
                   destination_site,destination_observation_id))"""
    )
    conn.execute(
        "CREATE INDEX idx_photo_transfers_pair ON sync_photo_transfers(profile_id,pair_id)"
    )
    conn.execute(
        "CREATE INDEX idx_photo_transfers_fingerprint "
        "ON sync_photo_transfers(profile_id,byte_fingerprint)"
    )


def _migration_v9(conn: sqlite3.Connection) -> None:
    """Gate 2A: missing-observation creation.

    Extends the ``action_type`` CHECK again (same rebuild as v5/v7/v8) with
    two brand-new-identity action types (``inat_observation_create``,
    ``mo_observation_create``), the saga's local-only finalize step
    (``pair_finalize``), and a reserved placeholder value
    (``mo_photo_attach``) for the not-yet-implemented iNat->MO photo
    direction. ``mo_photo_attach`` is kept in the CHECK for forward schema
    compatibility ONLY — it is never minted and every executor that could
    reach it rejects it explicitly before any network call (see
    ``PhotoActionType`` in types.py and section 3 of the Gate 2A review).

    ``mo_observation_id``/``inat_observation_id`` were NOT NULL for every
    prior action type because every prior write targeted an observation that
    already existed on both sides. A creation row has no destination id until
    its own write succeeds. Per the saga-architecture-rules memory, this is
    handled with a COMPOUND CONDITIONAL CHECK naming exactly the three new
    action types that may have a NULL destination id — every pre-existing
    action type keeps its original invariant untouched. Global nullability
    (dropping the constraint for all rows) was deliberately rejected.

    ``sync_action_groups`` has the identical NOT NULL problem one level up (a
    creation saga's group also has no destination id yet), so it gets the
    same conditional-CHECK rebuild first, since ``sync_actions`` has a FK to
    it and must find every existing group row already present when its own
    rows are copied forward.
    """
    # Dependency-safe rebuild order: rename EVERY table involved before
    # creating any new one, so no rename ever retargets a live child's foreign
    # key onto a table that's about to be created fresh.
    #
    # sync_action_groups is the parent being rebuilt, and TWO tables carry a
    # foreign key into it: sync_actions and sync_action_snapshot_rows. Both are
    # renamed aside first, so that when sync_action_groups is renamed SQLite
    # rewrites only the FKs of the already-set-aside _v8 copies — every old
    # table still exists under its temporary name, so nothing dangles.
    #
    # sync_action_snapshot_rows MUST be included here. Its FK is ON DELETE
    # CASCADE, so leaving it under its live name means the rename silently
    # retargets it at sync_action_groups_v8 and the eventual DROP of that
    # table performs an implicit DELETE that CASCADES away every reviewed
    # link-repair snapshot row, leaving the live table both empty and pointing
    # at a table that no longer exists (every subsequent INSERT then fails with
    # "no such table: main.sync_action_groups_v8"). _migration_v12 repairs the
    # dangling reference for databases that already ran the broken order, but
    # the deleted rows are unrecoverable — hence the rebuild here.
    #
    # Only once the new tables are created and populated are the old ones
    # dropped, children (sync_action_snapshot_rows_v8, sync_actions_v8) before
    # parent (sync_action_groups_v8), so ON DELETE CASCADE never fires against
    # a parent that's mid-rebuild.
    conn.execute("ALTER TABLE sync_action_snapshot_rows RENAME TO sync_action_snapshot_rows_v8")
    conn.execute("ALTER TABLE sync_actions RENAME TO sync_actions_v8")
    conn.execute("ALTER TABLE sync_action_groups RENAME TO sync_action_groups_v8")
    conn.execute(
        """CREATE TABLE sync_action_groups(
            action_group_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('pair','issue','creation')),
            pair_id INTEGER REFERENCES sync_pairs(pair_id),
            issue_id INTEGER REFERENCES sync_issues(issue_id),
            source_fingerprint TEXT NOT NULL,
            mo_observation_id INTEGER, inat_observation_id INTEGER,
            previewed_at TEXT NOT NULL, confirmed_at TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(profile_id,action_group_id),
            CHECK (source_kind='creation' OR (mo_observation_id IS NOT NULL AND inat_observation_id IS NOT NULL)))"""
    )
    conn.execute(
        "INSERT INTO sync_action_groups(action_group_id,profile_id,source_kind,pair_id,issue_id,"
        "source_fingerprint,mo_observation_id,inat_observation_id,previewed_at,confirmed_at,"
        "created_at,updated_at) SELECT action_group_id,profile_id,source_kind,pair_id,issue_id,"
        "source_fingerprint,mo_observation_id,inat_observation_id,previewed_at,confirmed_at,"
        "created_at,updated_at FROM sync_action_groups_v8"
    )
    # Rebuilt unchanged apart from its foreign key, which now points at the
    # new sync_action_groups. Recreated (and repopulated) here rather than
    # left alone so that no reviewed link-repair snapshot is lost to the
    # cascade described above.
    conn.execute(
        """CREATE TABLE sync_action_snapshot_rows(
            snapshot_row_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            action_group_id INTEGER NOT NULL,
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            observation_id INTEGER NOT NULL,
            remote_row_id TEXT NOT NULL DEFAULT '', remote_row_uuid TEXT NOT NULL DEFAULT '',
            binding_id INTEGER, normalized_target_id INTEGER,
            parse_state TEXT NOT NULL, row_fingerprint TEXT NOT NULL,
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id) ON DELETE CASCADE,
            UNIQUE(profile_id,action_group_id,site,remote_row_id,remote_row_uuid))"""
    )
    snapshot_columns = (
        "snapshot_row_id,profile_id,action_group_id,site,observation_id,"
        "remote_row_id,remote_row_uuid,binding_id,normalized_target_id,"
        "parse_state,row_fingerprint"
    )
    conn.execute(
        f"INSERT INTO sync_action_snapshot_rows({snapshot_columns}) "
        f"SELECT {snapshot_columns} FROM sync_action_snapshot_rows_v8"
    )
    conn.execute(
        """CREATE TABLE sync_actions(
            action_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            action_group_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            action_type TEXT NOT NULL CHECK(action_type IN (
                'inat_ofv_add','inat_ofv_repair','inat_ofv_remove',
                'mo_external_link_add','mo_external_link_repair','mo_external_link_remove',
                'inat_its_add','inat_its_repair','inat_its_remove',
                'mo_sequence_add','mo_sequence_repair',
                'inat_coordinate_set','inat_coordinate_replace',
                'inat_photo_attach','mo_photo_attach',
                'inat_observation_create','mo_observation_create','pair_finalize')),
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            state TEXT NOT NULL CHECK(state IN (
                'pending','running','succeeded','failed','cancelled','outcome_unknown')),
            last_phase TEXT NOT NULL DEFAULT 'preview',
            pair_id INTEGER REFERENCES sync_pairs(pair_id) ON DELETE CASCADE,
            issue_id INTEGER REFERENCES sync_issues(issue_id),
            -- Gate 2A: a creation/finalize row has no destination id until its
            -- own write succeeds. Every other action type still requires both.
            mo_observation_id INTEGER, inat_observation_id INTEGER,
            inat_observation_uuid TEXT NOT NULL,
            binding_id INTEGER, remote_row_id TEXT NOT NULL DEFAULT '',
            remote_row_uuid TEXT NOT NULL DEFAULT '',
            current_target_id INTEGER, desired_target_id INTEGER,
            destructive INTEGER NOT NULL DEFAULT 0,
            preview_inat_record_fingerprint TEXT NOT NULL,
            preview_mo_record_fingerprint TEXT NOT NULL,
            preview_inat_links_fingerprint TEXT NOT NULL,
            preview_mo_links_fingerprint TEXT NOT NULL,
            deduplication_key TEXT NOT NULL,
            source_site TEXT CHECK(source_site IN ('inat','mo')),
            source_record_id INTEGER,
            source_sequence_remote_id TEXT NOT NULL DEFAULT '',
            sequence_fingerprint TEXT NOT NULL DEFAULT '',
            normalized_accession TEXT NOT NULL DEFAULT '',
            normalized_archive TEXT NOT NULL DEFAULT '',
            source_metadata_fingerprint TEXT NOT NULL DEFAULT '',
            destination_preflight_fingerprint TEXT NOT NULL DEFAULT '',
            evidence_type TEXT NOT NULL DEFAULT '',
            source_privacy_state TEXT NOT NULL DEFAULT '',
            proposed_privacy_state TEXT NOT NULL DEFAULT '',
            source_photo_id TEXT NOT NULL DEFAULT '',
            planned_observation_photo_uuid TEXT NOT NULL DEFAULT '',
            reviewed_byte_fingerprint TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            attempt_started_at TEXT, write_started_at TEXT, finished_at TEXT,
            last_error_code TEXT NOT NULL DEFAULT '', last_http_status INTEGER,
            verification_state TEXT NOT NULL DEFAULT '', verified_at TEXT,
            server_row_id TEXT NOT NULL DEFAULT '', server_row_uuid TEXT NOT NULL DEFAULT '',
            outcome_unknown INTEGER NOT NULL DEFAULT 0,
            supersedes_action_id INTEGER REFERENCES sync_actions(action_id),
            created_at TEXT NOT NULL, confirmed_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id) ON DELETE CASCADE,
            UNIQUE(profile_id,action_id), UNIQUE(profile_id,action_group_id,ordinal),
            CHECK (
                action_type IN ('inat_observation_create','mo_observation_create','pair_finalize')
                OR (mo_observation_id IS NOT NULL AND inat_observation_id IS NOT NULL)
            ))"""
    )
    old_columns = (
        "action_id,profile_id,action_group_id,ordinal,action_type,site,state,last_phase,"
        "pair_id,issue_id,mo_observation_id,inat_observation_id,inat_observation_uuid,"
        "binding_id,remote_row_id,remote_row_uuid,current_target_id,desired_target_id,destructive,"
        "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
        "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
        "source_site,source_record_id,source_sequence_remote_id,sequence_fingerprint,"
        "normalized_accession,normalized_archive,source_metadata_fingerprint,"
        "destination_preflight_fingerprint,evidence_type,"
        "source_privacy_state,proposed_privacy_state,"
        "source_photo_id,planned_observation_photo_uuid,reviewed_byte_fingerprint,"
        "attempt_count,attempt_started_at,write_started_at,finished_at,last_error_code,last_http_status,"
        "verification_state,verified_at,server_row_id,server_row_uuid,outcome_unknown,"
        "supersedes_action_id,created_at,confirmed_at,updated_at"
    )
    conn.execute(
        f"INSERT INTO sync_actions({old_columns}) SELECT {old_columns} FROM sync_actions_v8"
    )
    # sync_photo_transfers (created in v8) has its own FK to sync_actions. The
    # RENAME above silently retargeted that FK onto sync_actions_v8 (SQLite
    # rewrites every referencing table's schema when the target of a foreign
    # key is renamed), so it must be rebuilt now — pointing at the new
    # sync_actions — before sync_actions_v8 is dropped, or the drop fails with
    # "FOREIGN KEY constraint failed" against the still-referencing old rows.
    conn.execute("ALTER TABLE sync_photo_transfers RENAME TO sync_photo_transfers_v8")
    conn.execute(
        """CREATE TABLE sync_photo_transfers(
            transfer_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            pair_id INTEGER REFERENCES sync_pairs(pair_id) ON DELETE CASCADE,
            action_id INTEGER REFERENCES sync_actions(action_id),
            source_site TEXT NOT NULL CHECK(source_site IN ('inat','mo')),
            source_photo_id TEXT NOT NULL,
            destination_site TEXT NOT NULL CHECK(destination_site IN ('inat','mo')),
            destination_observation_id INTEGER NOT NULL,
            destination_photo_id TEXT NOT NULL DEFAULT '',
            destination_observation_photo_uuid TEXT NOT NULL DEFAULT '',
            byte_fingerprint TEXT NOT NULL DEFAULT '',
            md5 TEXT NOT NULL DEFAULT '',
            source_license_label TEXT NOT NULL DEFAULT '',
            source_copyright_holder TEXT NOT NULL DEFAULT '',
            destination_license_code TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','succeeded','failed','outcome_unknown')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(profile_id,source_site,source_photo_id,
                   destination_site,destination_observation_id))"""
    )
    photo_transfer_columns = (
        "transfer_id,profile_id,pair_id,action_id,source_site,source_photo_id,"
        "destination_site,destination_observation_id,destination_photo_id,"
        "destination_observation_photo_uuid,byte_fingerprint,md5,source_license_label,"
        "source_copyright_holder,destination_license_code,state,created_at,updated_at"
    )
    conn.execute(
        f"INSERT INTO sync_photo_transfers({photo_transfer_columns}) "
        f"SELECT {photo_transfer_columns} FROM sync_photo_transfers_v8"
    )
    conn.execute("DROP TABLE sync_photo_transfers_v8")
    conn.execute(
        "CREATE INDEX idx_photo_transfers_pair ON sync_photo_transfers(profile_id,pair_id)"
    )
    conn.execute(
        "CREATE INDEX idx_photo_transfers_fingerprint "
        "ON sync_photo_transfers(profile_id,byte_fingerprint)"
    )
    conn.execute("DROP TABLE sync_action_snapshot_rows_v8")
    conn.execute("DROP TABLE sync_actions_v8")
    conn.execute("DROP TABLE sync_action_groups_v8")
    conn.execute("CREATE INDEX idx_sync_actions_state ON sync_actions(profile_id,state,created_at)")
    conn.execute("CREATE INDEX idx_sync_actions_group ON sync_actions(profile_id,action_group_id,ordinal)")
    conn.execute(
        "CREATE UNIQUE INDEX uq_sync_unresolved_action ON sync_actions(profile_id,deduplication_key) "
        "WHERE state IN ('pending','running','outcome_unknown')"
    )
    conn.execute(
        """CREATE TABLE sync_created_observations(
            creation_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            -- SET NULL, not CASCADE: deleting the local pair must never erase
            -- the provenance needed to recover or explain a remotely-created
            -- observation (saga-architecture-rules rule 4).
            pair_id INTEGER REFERENCES sync_pairs(pair_id) ON DELETE SET NULL,
            action_group_id INTEGER NOT NULL,
            source_site TEXT NOT NULL CHECK(source_site IN ('inat','mo')),
            source_observation_id INTEGER NOT NULL,
            destination_site TEXT NOT NULL CHECK(destination_site IN ('inat','mo')),
            -- NULL, not a sentinel like 0, for "not yet known".
            destination_observation_id INTEGER,
            destination_observation_uuid TEXT,
            -- Generated and journaled BEFORE the create write is ever sent
            -- (saga-architecture-rules rule 3). iNat: the client-supplied
            -- observation uuid. MO: the opaque token embedded in ``notes``.
            correlation_marker TEXT NOT NULL,
            marker_location TEXT NOT NULL CHECK(marker_location IN ('client_uuid_field','public_notes')),
            -- JSON list of fields the preview disclosed as non-transferable to
            -- this destination; the pair_finalize validator tolerates exactly
            -- these and nothing else.
            approved_field_gaps TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id) ON DELETE CASCADE,
            UNIQUE(profile_id,destination_site,correlation_marker),
            UNIQUE(profile_id,source_site,source_observation_id,destination_site))"""
    )
    conn.execute(
        "CREATE INDEX idx_created_observations_group ON sync_created_observations(profile_id,action_group_id)"
    )
    conn.execute(
        """CREATE TABLE sync_creation_items(
            creation_item_id INTEGER PRIMARY KEY,
            creation_id INTEGER NOT NULL REFERENCES sync_created_observations(creation_id) ON DELETE CASCADE,
            action_id INTEGER REFERENCES sync_actions(action_id) ON DELETE SET NULL,
            item_type TEXT NOT NULL,
            source_item_identity TEXT NOT NULL,
            -- Digest of the reviewed metadata (license, holder, identifier
            -- value, etc.), re-checked at write time. Raw content (notes text,
            -- image bytes) is re-read remotely and re-fingerprinted at write
            -- time; it is never stored here (saga-architecture-rules rule 5).
            reviewed_metadata_fingerprint TEXT NOT NULL,
            planned_remote_uuid_or_marker TEXT NOT NULL DEFAULT '',
            destination_remote_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','succeeded','failed','outcome_unknown')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
    )
    conn.execute(
        "CREATE INDEX idx_creation_items_creation ON sync_creation_items(creation_id)"
    )


def _migration_v10(conn: sqlite3.Connection) -> None:
    """Gate 2A follow-up review: durable multi-attempt creation provenance
    plus exact taxon pinning (sections 5-7).

    The v9 design stored one MUTABLE row per source->destination creation:
    retrying an attempt UPDATEd that row's ``action_group_id``,
    ``correlation_marker``, and ``approved_field_gaps`` in place and DELETEd
    its ``sync_creation_items`` rows -- destroying the prior attempt's
    correlation marker, reviewed item plan, and provenance the moment a
    retry was journaled. This splits that single row into:

      * ``sync_created_observations`` -- the STABLE source->destination
        creation IDENTITY. Touched again only once a destination id is
        actually known (``finish_observation_creation``); never repointed to
        a different action group.
      * ``sync_creation_attempts`` -- one IMMUTABLE row per reviewed/approved
        attempt: its own correlation marker (globally unique, never reused),
        its own reviewed-taxon pin (``reviewed_destination_taxon_id`` etc,
        section 5), its own ``action_group_id``, and its own terminal
        ``state``. A retry INSERTs a new attempt linked to the prior one via
        ``supersedes_attempt_id`` -- the prior attempt row, its marker, and
        its items are never deleted or overwritten.
      * ``sync_creation_items`` -- now scoped to ONE ``attempt_id`` instead of
        one ``creation_id``, since the reviewed item plan belongs to a
        specific attempt, not the identity as a whole.

    Every pre-v10 row becomes exactly one identity + one attempt (the only
    attempt that model ever allowed), so no history is lost by the migration
    itself -- only the mutate-in-place behavior going forward is fixed. The
    attempt's ``state`` is derived from its ordinal-0 create action's current
    ``sync_actions.state`` (falling back to 'pending' if that row is somehow
    already gone). The new reviewed-taxon columns have no prior data and are
    migrated empty/NULL with ``resolution_mode=''`` -- disclosed as unknown
    provenance, never fabricated after the fact.

    Table rebuild order follows the same dependency-safe pattern the v9
    migration fix established: rename the child (items) before the parent
    (created_observations), so SQLite's implicit FK-retarget-on-rename never
    leaves a dangling reference; create and populate the new tables before
    dropping the old ones; drop child before parent.
    """
    conn.execute("ALTER TABLE sync_creation_items RENAME TO sync_creation_items_v9")
    conn.execute("ALTER TABLE sync_created_observations RENAME TO sync_created_observations_v9")

    conn.execute(
        """CREATE TABLE sync_created_observations(
            creation_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            pair_id INTEGER REFERENCES sync_pairs(pair_id) ON DELETE SET NULL,
            source_site TEXT NOT NULL CHECK(source_site IN ('inat','mo')),
            source_observation_id INTEGER NOT NULL,
            destination_site TEXT NOT NULL CHECK(destination_site IN ('inat','mo')),
            destination_observation_id INTEGER,
            destination_observation_uuid TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(profile_id,source_site,source_observation_id,destination_site))"""
    )
    conn.execute(
        """CREATE TABLE sync_creation_attempts(
            attempt_id INTEGER PRIMARY KEY,
            creation_id INTEGER NOT NULL REFERENCES sync_created_observations(creation_id) ON DELETE CASCADE,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            action_group_id INTEGER NOT NULL,
            destination_site TEXT NOT NULL CHECK(destination_site IN ('inat','mo')),
            -- Generated and journaled BEFORE the create write is ever sent
            -- (saga-architecture-rules rule 3). NEVER reused across attempts:
            -- globally unique per (profile,destination_site,marker).
            correlation_marker TEXT NOT NULL,
            marker_location TEXT NOT NULL CHECK(marker_location IN ('client_uuid_field','public_notes')),
            approved_field_gaps TEXT NOT NULL DEFAULT '[]',
            -- The exact source-record fingerprint (site/id/unpaired_state/
            -- updated_at) this attempt was reviewed against. Re-checked
            -- immediately before the write (finding 4): the record must
            -- still be confirmed-missing and unchanged, not just at journal
            -- time -- a saga can resume long after journaling (app restart).
            source_fingerprint TEXT NOT NULL DEFAULT '',
            -- Round-3 finding 3: source_fingerprint only covers the local
            -- inventory record's (site, id, unpaired_state, local update
            -- timestamp) -- it says nothing about whether the actual
            -- reviewed date/locality/coordinates/accuracy/description/
            -- attribution/gap decisions are still what the user approved. A
            -- remote change with no corresponding local timestamp bump could
            -- otherwise create with values nobody reviewed. This is a
            -- one-way hash (public_fingerprint) over the complete derived
            -- creation payload — never raw notes or coordinates themselves.
            reviewed_payload_fingerprint TEXT NOT NULL DEFAULT '',
            -- Section 5: the EXACT taxon the user reviewed and approved,
            -- pinned at journal time. The create write and final
            -- verification both use/require this id, never a value
            -- recomputed fresh at write or finalize time.
            reviewed_destination_taxon_id INTEGER,
            reviewed_destination_taxon_name TEXT NOT NULL DEFAULT '',
            reviewed_source_taxon_name TEXT NOT NULL DEFAULT '',
            reviewed_source_taxon_rank TEXT NOT NULL DEFAULT '',
            resolution_mode TEXT NOT NULL DEFAULT ''
                CHECK(resolution_mode IN ('','exact','provisional_exact','disclosed_rank_approximation')),
            taxon_resolution_fingerprint TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','succeeded','failed','cancelled','outcome_unknown','superseded')),
            supersedes_attempt_id INTEGER REFERENCES sync_creation_attempts(attempt_id),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            -- Round-3 smaller issue: ON DELETE CASCADE here would mean
            -- deleting the action group (nothing does today, but future
            -- journal-cleanup code plausibly could) silently erases this
            -- attempt's supposedly-immutable provenance along with it. RESTRICT
            -- makes that impossible: a group with attempt history can never be
            -- deleted out from under it, only the attempt itself could ever be
            -- deliberately purged first.
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id) ON DELETE RESTRICT,
            UNIQUE(profile_id,destination_site,correlation_marker))"""
    )
    conn.execute(
        """CREATE TABLE sync_creation_items(
            creation_item_id INTEGER PRIMARY KEY,
            attempt_id INTEGER NOT NULL REFERENCES sync_creation_attempts(attempt_id) ON DELETE CASCADE,
            action_id INTEGER REFERENCES sync_actions(action_id) ON DELETE SET NULL,
            item_type TEXT NOT NULL,
            source_item_identity TEXT NOT NULL,
            reviewed_metadata_fingerprint TEXT NOT NULL,
            -- Round-3 finding 4: for a photo item, the byte-content
            -- fingerprint of the image actually downloaded and shown during
            -- PREVIEW — pinned before the user ever approves the saga, never
            -- established later at item-mint time. Blank for non-photo items.
            reviewed_byte_fingerprint TEXT NOT NULL DEFAULT '',
            planned_remote_uuid_or_marker TEXT NOT NULL DEFAULT '',
            destination_remote_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','succeeded','failed','outcome_unknown')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            -- Round-3 finding 5: one approved attempt can never contain two
            -- entries for the same source item (e.g. the same MO photo id
            -- reviewed twice).
            UNIQUE(attempt_id,item_type,source_item_identity))"""
    )

    old_identities = conn.execute("SELECT * FROM sync_created_observations_v9").fetchall()
    for old in old_identities:
        conn.execute(
            "INSERT INTO sync_created_observations(creation_id,profile_id,pair_id,source_site,"
            "source_observation_id,destination_site,destination_observation_id,"
            "destination_observation_uuid,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (old["creation_id"], old["profile_id"], old["pair_id"], old["source_site"],
             old["source_observation_id"], old["destination_site"], old["destination_observation_id"],
             old["destination_observation_uuid"], old["created_at"], old["updated_at"]),
        )
        create_action = conn.execute(
            "SELECT action_id,state,write_started_at FROM sync_actions WHERE profile_id=? "
            "AND action_group_id=? AND action_type IN ('inat_observation_create','mo_observation_create') "
            "LIMIT 1",
            (old["profile_id"], old["action_group_id"]),
        ).fetchone()
        if create_action is None:
            attempt_state = "pending"
        elif str(create_action["state"]) == "running":
            # sync_actions.state allows 'running'; sync_creation_attempts.state
            # does not (a mid-attempt CHECK failure would abort the whole
            # migration). A database upgraded while a creation action was
            # actively running (app closed mid-write) must be classified
            # conservatively rather than crash the migration: no write ever
            # started -> safe to resume as 'pending'; a write may have been
            # sent -> 'outcome_unknown', recoverable only via verify_unknown,
            # never silently retried. The underlying action row itself is
            # normalized to match — claim_action only ever claims a 'pending'
            # row, so a row left at 'running' would otherwise be permanently
            # unclaimable after migration.
            if create_action["write_started_at"]:
                attempt_state = "outcome_unknown"
                conn.execute(
                    "UPDATE sync_actions SET state='outcome_unknown',outcome_unknown=1,updated_at=? "
                    "WHERE profile_id=? AND action_id=?",
                    (_utc_now(), old["profile_id"], int(create_action["action_id"])),
                )
            else:
                attempt_state = "pending"
                conn.execute(
                    "UPDATE sync_actions SET state='pending',attempt_started_at=NULL,updated_at=? "
                    "WHERE profile_id=? AND action_id=?",
                    (_utc_now(), old["profile_id"], int(create_action["action_id"])),
                )
        else:
            attempt_state = str(create_action["state"])
        conn.execute(
            "INSERT INTO sync_creation_attempts(attempt_id,creation_id,profile_id,action_group_id,"
            "destination_site,correlation_marker,marker_location,approved_field_gaps,"
            "reviewed_destination_taxon_id,reviewed_destination_taxon_name,reviewed_source_taxon_name,"
            "reviewed_source_taxon_rank,resolution_mode,taxon_resolution_fingerprint,state,"
            "supersedes_attempt_id,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,NULL,'','','','','',?,NULL,?,?)",
            (old["creation_id"], old["creation_id"], old["profile_id"], old["action_group_id"],
             old["destination_site"], old["correlation_marker"], old["marker_location"],
             old["approved_field_gaps"], attempt_state, old["created_at"], old["updated_at"]),
        )
    old_items = conn.execute("SELECT * FROM sync_creation_items_v9").fetchall()
    for item in old_items:
        conn.execute(
            "INSERT INTO sync_creation_items(creation_item_id,attempt_id,action_id,item_type,"
            "source_item_identity,reviewed_metadata_fingerprint,planned_remote_uuid_or_marker,"
            "destination_remote_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (item["creation_item_id"], item["creation_id"], item["action_id"], item["item_type"],
             item["source_item_identity"], item["reviewed_metadata_fingerprint"],
             item["planned_remote_uuid_or_marker"], item["destination_remote_id"], item["state"],
             item["created_at"], item["updated_at"]),
        )

    conn.execute("DROP TABLE sync_creation_items_v9")
    conn.execute("DROP TABLE sync_created_observations_v9")
    conn.execute(
        "CREATE INDEX idx_created_observations_identity "
        "ON sync_created_observations(profile_id,source_site,source_observation_id,destination_site)"
    )
    conn.execute(
        "CREATE INDEX idx_creation_attempts_group ON sync_creation_attempts(profile_id,action_group_id)"
    )
    conn.execute(
        "CREATE INDEX idx_creation_attempts_creation ON sync_creation_attempts(profile_id,creation_id)"
    )
    conn.execute(
        "CREATE INDEX idx_creation_items_attempt ON sync_creation_items(attempt_id)"
    )
    conn.execute(
        # Section 9: a schema-level backstop, not just application ordering
        # inside mint_creation_item_action's transaction -- no two creation
        # items may ever point at the same action row. NULLs (not-yet-minted
        # items) are unconstrained, since SQLite excludes them from a partial
        # index's WHERE-filtered rows.
        "CREATE UNIQUE INDEX uq_creation_items_action ON sync_creation_items(action_id) "
        "WHERE action_id IS NOT NULL"
    )


def _migration_v11(conn: sqlite3.Connection) -> None:
    """Gate 2B-M1: durable consolidation domain model (duplicate observation
    consolidation).

    Adds four new tables separating the STABLE canonicalization identity
    from IMMUTABLE reviewed attempts, mirroring the v10 split between
    ``sync_created_observations`` and ``sync_creation_attempts``:

      * ``sync_consolidations`` -- one stable canonicalization identity per
        duplicate set: which observation is canonical on each site, and the
        ``sync_pairs`` row that identity resolves to once confirmed.
      * ``sync_consolidation_members`` -- every record (canonical or donor)
        known to belong to the duplicate set. Globally unique per
        (profile,site,observation) ACROSS ALL consolidations (not just this
        one) -- this is M2's "not belong to another unresolved
        consolidation" rule enforced as a schema backstop, not merely
        application logic.
      * ``sync_consolidation_attempts`` -- one IMMUTABLE row per
        reviewed/approved consolidation plan, following the exact same
        shape as ``sync_creation_attempts``: its own correlation marker,
        its own pinned fingerprints (canonical records, donor records, the
        canonical pair, approved unsupported-field gaps, destination
        account identities), its own action group, and a
        ``supersedes_attempt_id`` chain so a retry never overwrites a prior
        attempt's provenance. ``ON DELETE RESTRICT`` on the action-group FK
        for the same reason v10 uses it: a group with attempt history must
        never be deletable out from under it.
        NOTE: "an outcome_unknown attempt blocks a new attempt until
        resolved" (M1) is deliberately NOT a schema constraint -- there is
        no clean CHECK/index expression for "no unresolved outcome_unknown
        row exists for this consolidation_id"; it is enforced in the
        service layer before a new attempt is ever inserted.
      * ``sync_consolidation_items`` -- one reviewed optional transfer item
        per attempt (photo/identifier/sequence/etc; Phase 2B v1 ships with
        none actually selectable per the Gate 2B-M0 capability note, but
        the table exists so a future capability-proofed item type has
        somewhere to land without another migration). Same action_id
        backstop pattern as ``sync_creation_items``: ``ON DELETE SET NULL``
        plus a partial unique index so no two item rows ever share one
        action row.

    ``sync_actions.action_type`` also gains ``'consolidation_finalize'`` --
    the M8 local-finalization row that atomically confirms the canonical
    pair and marks donors superseded. This requires a full table rebuild
    (SQLite cannot ALTER a CHECK constraint). Two other live tables carry a
    direct FK to ``sync_actions(action_id)`` -- ``sync_photo_transfers`` and
    ``sync_creation_items`` -- and renaming ``sync_actions`` would silently
    retarget both onto the renamed table (the same gotcha documented at the
    v9->v10 rebuild, db.py ~4132-4138), so both are rebuilt too: renamed
    aside before the rename, recreated pointing at the new ``sync_actions``,
    repopulated, then their old copies dropped before the old
    ``sync_actions`` copy is dropped (child before parent, matching the v10
    precedent).
    """
    # --- Extend sync_actions.action_type, rebuilding it and every table with
    # a direct FK into it (child tables renamed aside first so the parent
    # rename doesn't silently retarget them). ---
    conn.execute("ALTER TABLE sync_photo_transfers RENAME TO sync_photo_transfers_v10")
    conn.execute("ALTER TABLE sync_creation_items RENAME TO sync_creation_items_v10")
    conn.execute("ALTER TABLE sync_actions RENAME TO sync_actions_v10")

    conn.execute(
        """CREATE TABLE sync_actions(
            action_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            action_group_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            action_type TEXT NOT NULL CHECK(action_type IN (
                'inat_ofv_add','inat_ofv_repair','inat_ofv_remove',
                'mo_external_link_add','mo_external_link_repair','mo_external_link_remove',
                'inat_its_add','inat_its_repair','inat_its_remove',
                'mo_sequence_add','mo_sequence_repair',
                'inat_coordinate_set','inat_coordinate_replace',
                'inat_photo_attach','mo_photo_attach',
                'inat_observation_create','mo_observation_create','pair_finalize',
                'consolidation_finalize')),
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            state TEXT NOT NULL CHECK(state IN (
                'pending','running','succeeded','failed','cancelled','outcome_unknown')),
            last_phase TEXT NOT NULL DEFAULT 'preview',
            pair_id INTEGER REFERENCES sync_pairs(pair_id) ON DELETE SET NULL,
            issue_id INTEGER REFERENCES sync_issues(issue_id),
            -- Gate 2A: a creation/finalize row has no destination id until its
            -- own write succeeds. Every other action type still requires both.
            -- Gate 2B: a consolidation_finalize row may likewise represent a
            -- same-site-only duplicate set (M1: canonical_mo/inat_observation_id
            -- are each independently nullable), so it is exempted the same way.
            mo_observation_id INTEGER, inat_observation_id INTEGER,
            inat_observation_uuid TEXT NOT NULL,
            binding_id INTEGER, remote_row_id TEXT NOT NULL DEFAULT '',
            remote_row_uuid TEXT NOT NULL DEFAULT '',
            current_target_id INTEGER, desired_target_id INTEGER,
            destructive INTEGER NOT NULL DEFAULT 0,
            preview_inat_record_fingerprint TEXT NOT NULL,
            preview_mo_record_fingerprint TEXT NOT NULL,
            preview_inat_links_fingerprint TEXT NOT NULL,
            preview_mo_links_fingerprint TEXT NOT NULL,
            deduplication_key TEXT NOT NULL,
            source_site TEXT CHECK(source_site IN ('inat','mo')),
            source_record_id INTEGER,
            source_sequence_remote_id TEXT NOT NULL DEFAULT '',
            sequence_fingerprint TEXT NOT NULL DEFAULT '',
            normalized_accession TEXT NOT NULL DEFAULT '',
            normalized_archive TEXT NOT NULL DEFAULT '',
            source_metadata_fingerprint TEXT NOT NULL DEFAULT '',
            destination_preflight_fingerprint TEXT NOT NULL DEFAULT '',
            evidence_type TEXT NOT NULL DEFAULT '',
            source_privacy_state TEXT NOT NULL DEFAULT '',
            proposed_privacy_state TEXT NOT NULL DEFAULT '',
            source_photo_id TEXT NOT NULL DEFAULT '',
            planned_observation_photo_uuid TEXT NOT NULL DEFAULT '',
            reviewed_byte_fingerprint TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            attempt_started_at TEXT, write_started_at TEXT, finished_at TEXT,
            last_error_code TEXT NOT NULL DEFAULT '', last_http_status INTEGER,
            verification_state TEXT NOT NULL DEFAULT '', verified_at TEXT,
            server_row_id TEXT NOT NULL DEFAULT '', server_row_uuid TEXT NOT NULL DEFAULT '',
            outcome_unknown INTEGER NOT NULL DEFAULT 0,
            supersedes_action_id INTEGER REFERENCES sync_actions(action_id),
            created_at TEXT NOT NULL, confirmed_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id) ON DELETE CASCADE,
            UNIQUE(profile_id,action_id), UNIQUE(profile_id,action_group_id,ordinal),
            CHECK (
                action_type IN ('inat_observation_create','mo_observation_create',
                                 'pair_finalize','consolidation_finalize')
                OR (mo_observation_id IS NOT NULL AND inat_observation_id IS NOT NULL)
            ))"""
    )
    action_columns = (
        "action_id,profile_id,action_group_id,ordinal,action_type,site,state,last_phase,"
        "pair_id,issue_id,mo_observation_id,inat_observation_id,inat_observation_uuid,"
        "binding_id,remote_row_id,remote_row_uuid,current_target_id,desired_target_id,destructive,"
        "preview_inat_record_fingerprint,preview_mo_record_fingerprint,"
        "preview_inat_links_fingerprint,preview_mo_links_fingerprint,deduplication_key,"
        "source_site,source_record_id,source_sequence_remote_id,sequence_fingerprint,"
        "normalized_accession,normalized_archive,source_metadata_fingerprint,"
        "destination_preflight_fingerprint,evidence_type,"
        "source_privacy_state,proposed_privacy_state,"
        "source_photo_id,planned_observation_photo_uuid,reviewed_byte_fingerprint,"
        "attempt_count,attempt_started_at,write_started_at,finished_at,last_error_code,last_http_status,"
        "verification_state,verified_at,server_row_id,server_row_uuid,outcome_unknown,"
        "supersedes_action_id,created_at,confirmed_at,updated_at"
    )
    conn.execute(
        f"INSERT INTO sync_actions({action_columns}) SELECT {action_columns} FROM sync_actions_v10"
    )

    conn.execute(
        """CREATE TABLE sync_photo_transfers(
            transfer_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            pair_id INTEGER REFERENCES sync_pairs(pair_id) ON DELETE CASCADE,
            action_id INTEGER REFERENCES sync_actions(action_id),
            source_site TEXT NOT NULL CHECK(source_site IN ('inat','mo')),
            source_photo_id TEXT NOT NULL,
            destination_site TEXT NOT NULL CHECK(destination_site IN ('inat','mo')),
            destination_observation_id INTEGER NOT NULL,
            destination_photo_id TEXT NOT NULL DEFAULT '',
            destination_observation_photo_uuid TEXT NOT NULL DEFAULT '',
            byte_fingerprint TEXT NOT NULL DEFAULT '',
            md5 TEXT NOT NULL DEFAULT '',
            source_license_label TEXT NOT NULL DEFAULT '',
            source_copyright_holder TEXT NOT NULL DEFAULT '',
            destination_license_code TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','succeeded','failed','outcome_unknown')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(profile_id,source_site,source_photo_id,
                   destination_site,destination_observation_id))"""
    )
    photo_transfer_columns = (
        "transfer_id,profile_id,pair_id,action_id,source_site,source_photo_id,"
        "destination_site,destination_observation_id,destination_photo_id,"
        "destination_observation_photo_uuid,byte_fingerprint,md5,source_license_label,"
        "source_copyright_holder,destination_license_code,state,created_at,updated_at"
    )
    conn.execute(
        f"INSERT INTO sync_photo_transfers({photo_transfer_columns}) "
        f"SELECT {photo_transfer_columns} FROM sync_photo_transfers_v10"
    )

    conn.execute(
        """CREATE TABLE sync_creation_items(
            creation_item_id INTEGER PRIMARY KEY,
            attempt_id INTEGER NOT NULL REFERENCES sync_creation_attempts(attempt_id) ON DELETE CASCADE,
            action_id INTEGER REFERENCES sync_actions(action_id) ON DELETE SET NULL,
            item_type TEXT NOT NULL,
            source_item_identity TEXT NOT NULL,
            reviewed_metadata_fingerprint TEXT NOT NULL,
            reviewed_byte_fingerprint TEXT NOT NULL DEFAULT '',
            planned_remote_uuid_or_marker TEXT NOT NULL DEFAULT '',
            destination_remote_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','succeeded','failed','outcome_unknown')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(attempt_id,item_type,source_item_identity))"""
    )
    creation_item_columns = (
        "creation_item_id,attempt_id,action_id,item_type,source_item_identity,"
        "reviewed_metadata_fingerprint,reviewed_byte_fingerprint,planned_remote_uuid_or_marker,"
        "destination_remote_id,state,created_at,updated_at"
    )
    conn.execute(
        f"INSERT INTO sync_creation_items({creation_item_columns}) "
        f"SELECT {creation_item_columns} FROM sync_creation_items_v10"
    )

    # Child tables dropped before the parent they referenced (same order as
    # the v9->v10 precedent).
    conn.execute("DROP TABLE sync_photo_transfers_v10")
    conn.execute("DROP TABLE sync_creation_items_v10")
    conn.execute("DROP TABLE sync_actions_v10")

    conn.execute("CREATE INDEX idx_sync_actions_state ON sync_actions(profile_id,state,created_at)")
    conn.execute("CREATE INDEX idx_sync_actions_group ON sync_actions(profile_id,action_group_id,ordinal)")
    conn.execute(
        "CREATE UNIQUE INDEX uq_sync_unresolved_action ON sync_actions(profile_id,deduplication_key) "
        "WHERE state IN ('pending','running','outcome_unknown')"
    )
    conn.execute(
        "CREATE INDEX idx_photo_transfers_pair ON sync_photo_transfers(profile_id,pair_id)"
    )
    conn.execute(
        "CREATE INDEX idx_photo_transfers_fingerprint "
        "ON sync_photo_transfers(profile_id,byte_fingerprint)"
    )
    conn.execute(
        "CREATE INDEX idx_creation_items_attempt ON sync_creation_items(attempt_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX uq_creation_items_action ON sync_creation_items(action_id) "
        "WHERE action_id IS NOT NULL"
    )

    # --- New consolidation domain model. ---
    conn.execute(
        """CREATE TABLE sync_consolidations(
            consolidation_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            canonical_mo_observation_id INTEGER,
            canonical_inat_observation_id INTEGER,
            canonical_pair_id INTEGER REFERENCES sync_pairs(pair_id) ON DELETE SET NULL,
            state TEXT NOT NULL DEFAULT 'draft'
                CHECK(state IN ('draft','confirmed','finalized','cancelled')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(profile_id,consolidation_id),
            CHECK (canonical_mo_observation_id IS NOT NULL
                   OR canonical_inat_observation_id IS NOT NULL))"""
    )
    conn.execute(
        "CREATE INDEX idx_consolidations_profile ON sync_consolidations(profile_id,state)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX uq_consolidation_canonical_mo "
        "ON sync_consolidations(profile_id,canonical_mo_observation_id) "
        "WHERE state IN ('draft','confirmed') AND canonical_mo_observation_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX uq_consolidation_canonical_inat "
        "ON sync_consolidations(profile_id,canonical_inat_observation_id) "
        "WHERE state IN ('draft','confirmed') AND canonical_inat_observation_id IS NOT NULL"
    )

    conn.execute(
        """CREATE TABLE sync_consolidation_members(
            consolidation_member_id INTEGER PRIMARY KEY,
            consolidation_id INTEGER NOT NULL,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            observation_id INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('canonical','donor')),
            remote_uuid TEXT,
            reviewed_record_fingerprint TEXT NOT NULL DEFAULT '',
            preflight_record_fingerprint TEXT NOT NULL DEFAULT '',
            local_state TEXT NOT NULL DEFAULT 'active'
                CHECK(local_state IN ('active','canonical','superseded')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id,consolidation_id)
                REFERENCES sync_consolidations(profile_id,consolidation_id) ON DELETE CASCADE,
            -- Deliberately global per (profile,site,observation), NOT scoped to
            -- one consolidation_id: schema-level backstop for M2's "must not
            -- belong to another unresolved consolidation" eligibility rule.
            UNIQUE(profile_id,site,observation_id))"""
    )
    conn.execute(
        "CREATE INDEX idx_consolidation_members_consolidation "
        "ON sync_consolidation_members(consolidation_id)"
    )

    conn.execute(
        """CREATE TABLE sync_consolidation_attempts(
            attempt_id INTEGER PRIMARY KEY,
            consolidation_id INTEGER NOT NULL,
            profile_id INTEGER NOT NULL REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            action_group_id INTEGER NOT NULL,
            correlation_marker TEXT NOT NULL,
            canonical_mo_fingerprint TEXT NOT NULL DEFAULT '',
            canonical_inat_fingerprint TEXT NOT NULL DEFAULT '',
            canonical_mo_preflight_fingerprint TEXT NOT NULL DEFAULT '',
            canonical_inat_preflight_fingerprint TEXT NOT NULL DEFAULT '',
            donor_fingerprints TEXT NOT NULL DEFAULT '[]',
            donor_preflight_fingerprints TEXT NOT NULL DEFAULT '[]',
            canonical_pair_fingerprint TEXT NOT NULL DEFAULT '',
            approved_unsupported_gaps TEXT NOT NULL DEFAULT '[]',
            destination_mo_account TEXT NOT NULL DEFAULT '',
            destination_inat_account TEXT NOT NULL DEFAULT '',
            -- No 'running' state: transience lives on sync_actions rows, not
            -- here (v10 precedent, sync_creation_attempts). An outcome_unknown
            -- attempt blocking a new attempt for the same consolidation_id is
            -- an application-layer rule (M1), not expressible as a clean
            -- CHECK/partial-index -- enforced in the consolidation service
            -- before INSERTing a new attempt row.
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','succeeded','failed','cancelled','outcome_unknown','superseded')),
            supersedes_attempt_id INTEGER REFERENCES sync_consolidation_attempts(attempt_id),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id) ON DELETE RESTRICT,
            FOREIGN KEY(profile_id,consolidation_id)
                REFERENCES sync_consolidations(profile_id,consolidation_id) ON DELETE RESTRICT,
            UNIQUE(profile_id,correlation_marker))"""
    )
    conn.execute(
        "CREATE INDEX idx_consolidation_attempts_consolidation "
        "ON sync_consolidation_attempts(profile_id,consolidation_id)"
    )
    conn.execute(
        "CREATE INDEX idx_consolidation_attempts_group "
        "ON sync_consolidation_attempts(profile_id,action_group_id)"
    )

    conn.execute(
        """CREATE TABLE sync_consolidation_items(
            consolidation_item_id INTEGER PRIMARY KEY,
            attempt_id INTEGER NOT NULL REFERENCES sync_consolidation_attempts(attempt_id) ON DELETE CASCADE,
            source_site TEXT NOT NULL CHECK(source_site IN ('inat','mo')),
            source_observation_id INTEGER NOT NULL,
            destination_site TEXT NOT NULL CHECK(destination_site IN ('inat','mo')),
            destination_observation_id INTEGER NOT NULL,
            item_type TEXT NOT NULL,
            source_item_identity TEXT NOT NULL,
            reviewed_metadata_fingerprint TEXT NOT NULL DEFAULT '',
            reviewed_byte_fingerprint TEXT NOT NULL DEFAULT '',
            action_id INTEGER REFERENCES sync_actions(action_id) ON DELETE SET NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN ('pending','succeeded','failed','outcome_unknown','disabled')),
            disabled_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(attempt_id,item_type,source_item_identity))"""
    )
    conn.execute(
        "CREATE INDEX idx_consolidation_items_attempt ON sync_consolidation_items(attempt_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX uq_consolidation_items_action ON sync_consolidation_items(action_id) "
        "WHERE action_id IS NOT NULL"
    )


def _migration_v12(conn: sqlite3.Connection) -> None:
    """Attempt-scoped evidence graphs and append-only consolidation members.

    Existing v11 identities, attempts, member ids, action groups, and action
    rows remain in place.  New association tables avoid rebuilding any parent
    table with action-history dependants.
    """
    # Repair pass for databases that ran the original _migration_v9, which
    # rebuilt sync_action_groups without setting this child aside first: SQLite
    # retargeted the FK to the temporary parent name, the parent's DROP then
    # CASCADE-deleted every row here, and the dangling reference made all later
    # INSERTs fail. _migration_v9 no longer does that, so for a database
    # upgrading from v8 or earlier this rebuild is a value-preserving no-op;
    # for one already sitting at v9-v11 it restores a usable parent reference.
    # The rows those databases already lost cannot be recovered here.
    # Rebuild only the child and preserve every id/row.
    conn.execute(
        "ALTER TABLE sync_action_snapshot_rows "
        "RENAME TO sync_action_snapshot_rows_v11"
    )
    conn.execute(
        """CREATE TABLE sync_action_snapshot_rows(
            snapshot_row_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL
                REFERENCES sync_profiles(profile_id) ON DELETE CASCADE,
            action_group_id INTEGER NOT NULL,
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            observation_id INTEGER NOT NULL,
            remote_row_id TEXT NOT NULL DEFAULT '',
            remote_row_uuid TEXT NOT NULL DEFAULT '',
            binding_id INTEGER,
            normalized_target_id INTEGER,
            parse_state TEXT NOT NULL,
            row_fingerprint TEXT NOT NULL,
            FOREIGN KEY(profile_id,action_group_id)
                REFERENCES sync_action_groups(profile_id,action_group_id)
                ON DELETE CASCADE,
            UNIQUE(profile_id,action_group_id,site,remote_row_id,remote_row_uuid))"""
    )
    conn.execute(
        "INSERT INTO sync_action_snapshot_rows("
        "snapshot_row_id,profile_id,action_group_id,site,observation_id,"
        "remote_row_id,remote_row_uuid,binding_id,normalized_target_id,"
        "parse_state,row_fingerprint) "
        "SELECT snapshot_row_id,profile_id,action_group_id,site,observation_id,"
        "remote_row_id,remote_row_uuid,binding_id,normalized_target_id,"
        "parse_state,row_fingerprint FROM sync_action_snapshot_rows_v11"
    )
    conn.execute("DROP TABLE sync_action_snapshot_rows_v11")

    conn.execute(
        "ALTER TABLE sync_consolidation_members "
        "ADD COLUMN added_by_attempt_id INTEGER "
        "REFERENCES sync_consolidation_attempts(attempt_id)"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_members "
        "ADD COLUMN superseded_by_attempt_id INTEGER "
        "REFERENCES sync_consolidation_attempts(attempt_id)"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_members "
        "ADD COLUMN superseded_at TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_attempts "
        "ADD COLUMN reviewed_evidence_graph_fingerprint "
        "TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        """CREATE TABLE sync_consolidation_attempt_members(
            attempt_id INTEGER NOT NULL
                REFERENCES sync_consolidation_attempts(attempt_id) ON DELETE CASCADE,
            consolidation_member_id INTEGER NOT NULL
                REFERENCES sync_consolidation_members(consolidation_member_id)
                ON DELETE RESTRICT,
            participation_role TEXT NOT NULL
                CHECK(participation_role IN ('canonical_context','new_donor')),
            reviewed_record_fingerprint TEXT NOT NULL,
            preflight_record_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(attempt_id,consolidation_member_id))"""
    )
    conn.execute(
        "CREATE INDEX idx_consolidation_attempt_members_member "
        "ON sync_consolidation_attempt_members(consolidation_member_id,attempt_id)"
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_attempt_member_identity
        BEFORE INSERT ON sync_consolidation_attempt_members
        WHEN NOT EXISTS (
            SELECT 1
            FROM sync_consolidation_attempts a
            JOIN sync_consolidation_members m
              ON m.consolidation_member_id=NEW.consolidation_member_id
            WHERE a.attempt_id=NEW.attempt_id
              AND a.profile_id=m.profile_id
              AND a.consolidation_id=m.consolidation_id
        )
        BEGIN
            SELECT RAISE(ABORT,
                'attempt member must belong to the attempt consolidation');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_attempt_member_immutable
        BEFORE UPDATE ON sync_consolidation_attempt_members
        BEGIN
            SELECT RAISE(ABORT, 'attempt member evidence is immutable');
        END"""
    )
    conn.execute(
        """CREATE TABLE sync_consolidation_evidence(
            consolidation_evidence_id INTEGER PRIMARY KEY,
            attempt_id INTEGER NOT NULL,
            left_member_id INTEGER NOT NULL,
            right_member_id INTEGER NOT NULL,
            evidence_type TEXT NOT NULL,
            evidence_strength TEXT NOT NULL
                CHECK(evidence_strength IN ('strong','corroborating')),
            reviewed_evidence_fingerprint TEXT NOT NULL,
            display_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(attempt_id,left_member_id)
                REFERENCES sync_consolidation_attempt_members(
                    attempt_id,consolidation_member_id) ON DELETE CASCADE,
            FOREIGN KEY(attempt_id,right_member_id)
                REFERENCES sync_consolidation_attempt_members(
                    attempt_id,consolidation_member_id) ON DELETE CASCADE,
            CHECK(left_member_id < right_member_id),
            UNIQUE(attempt_id,left_member_id,right_member_id,evidence_type))"""
    )
    conn.execute(
        "CREATE INDEX idx_consolidation_evidence_attempt "
        "ON sync_consolidation_evidence(attempt_id,left_member_id,right_member_id)"
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_evidence_immutable
        BEFORE UPDATE ON sync_consolidation_evidence
        BEGIN
            SELECT RAISE(ABORT, 'consolidation evidence is immutable');
        END"""
    )

    attempts = conn.execute(
        "SELECT * FROM sync_consolidation_attempts ORDER BY attempt_id"
    ).fetchall()
    first_attempt_by_consolidation: dict[int, int] = {}
    successful_attempt_by_consolidation: dict[int, tuple[int, str]] = {}
    for attempt in attempts:
        attempt_id = int(attempt["attempt_id"])
        consolidation_id = int(attempt["consolidation_id"])
        first_attempt_by_consolidation.setdefault(consolidation_id, attempt_id)
        if str(attempt["state"]) == "succeeded":
            successful_attempt_by_consolidation[consolidation_id] = (
                attempt_id, str(attempt["updated_at"]),
            )
        donor_full = {
            (str(item["site"]), int(item["observation_id"])): str(item["fingerprint"])
            for item in json.loads(str(attempt["donor_fingerprints"] or "[]"))
        }
        donor_preflight = {
            (str(item["site"]), int(item["observation_id"])): str(item["fingerprint"])
            for item in json.loads(
                str(attempt["donor_preflight_fingerprints"] or "[]")
            )
        }
        members = conn.execute(
            "SELECT * FROM sync_consolidation_members WHERE consolidation_id=? "
            "ORDER BY consolidation_member_id",
            (consolidation_id,),
        ).fetchall()
        for member in members:
            site = str(member["site"])
            observation_id = int(member["observation_id"])
            if str(member["role"]) == "canonical":
                suffix = "mo" if site == "mo" else "inat"
                reviewed = str(attempt[f"canonical_{suffix}_fingerprint"] or "")
                preflight = str(
                    attempt[f"canonical_{suffix}_preflight_fingerprint"] or ""
                )
                participation_role = "canonical_context"
            else:
                reviewed = donor_full.get((site, observation_id), "")
                preflight = donor_preflight.get((site, observation_id), "")
                participation_role = "new_donor"
            if reviewed and preflight:
                conn.execute(
                    "INSERT INTO sync_consolidation_attempt_members("
                    "attempt_id,consolidation_member_id,participation_role,"
                    "reviewed_record_fingerprint,preflight_record_fingerprint,created_at"
                    ") VALUES(?,?,?,?,?,?)",
                    (
                        attempt_id, int(member["consolidation_member_id"]),
                        participation_role, reviewed, preflight,
                        str(attempt["created_at"]),
                    ),
                )
    for consolidation_id, attempt_id in first_attempt_by_consolidation.items():
        conn.execute(
            "UPDATE sync_consolidation_members SET added_by_attempt_id=? "
            "WHERE consolidation_id=?",
            (attempt_id, consolidation_id),
        )
    for consolidation_id, (attempt_id, superseded_at) in (
        successful_attempt_by_consolidation.items()
    ):
        conn.execute(
            "UPDATE sync_consolidation_members SET superseded_by_attempt_id=?,"
            "superseded_at=? WHERE consolidation_id=? AND role='donor' "
            "AND local_state='superseded'",
            (attempt_id, superseded_at, consolidation_id),
        )

    # Finalized canonical identities remain reserved for extension; they
    # cannot seed a second stable identity.
    conn.execute("DROP INDEX uq_consolidation_canonical_mo")
    conn.execute("DROP INDEX uq_consolidation_canonical_inat")
    conn.execute(
        "CREATE UNIQUE INDEX uq_consolidation_canonical_mo "
        "ON sync_consolidations(profile_id,canonical_mo_observation_id) "
        "WHERE state!='cancelled' AND canonical_mo_observation_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX uq_consolidation_canonical_inat "
        "ON sync_consolidations(profile_id,canonical_inat_observation_id) "
        "WHERE state!='cancelled' AND canonical_inat_observation_id IS NOT NULL"
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_member_identity_immutable
        BEFORE UPDATE OF consolidation_id,profile_id,site,observation_id,role,
            remote_uuid,reviewed_record_fingerprint,preflight_record_fingerprint,
            created_at,added_by_attempt_id
        ON sync_consolidation_members
        BEGIN
            SELECT RAISE(ABORT, 'stable consolidation member identity is immutable');
        END"""
    )

def _migration_v13(conn: sqlite3.Connection) -> None:
    """Finalized baselines and attempt-scoped proposed consolidation donors.

    v12 evidence endpoints were stable-member ids, which forced a proposed
    donor into global stable membership before its attempt succeeded.  v13
    snapshots member identity directly on the immutable attempt row and makes
    evidence reference those snapshots.  Only successful finalization admits a
    donor to ``sync_consolidation_members``.
    """
    conn.execute(
        "ALTER TABLE sync_consolidations ADD COLUMN "
        "current_finalized_attempt_id INTEGER "
        "REFERENCES sync_consolidation_attempts(attempt_id)"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_attempts ADD COLUMN "
        "base_finalized_attempt_id INTEGER "
        "REFERENCES sync_consolidation_attempts(attempt_id)"
    )

    conn.execute(
        """CREATE TABLE sync_consolidation_attempt_members_v13(
            attempt_member_id INTEGER PRIMARY KEY,
            attempt_id INTEGER NOT NULL
                REFERENCES sync_consolidation_attempts(attempt_id) ON DELETE CASCADE,
            stable_member_id INTEGER
                REFERENCES sync_consolidation_members(consolidation_member_id)
                ON DELETE SET NULL,
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            observation_id INTEGER NOT NULL,
            remote_uuid TEXT,
            participation_role TEXT NOT NULL
                CHECK(participation_role IN ('canonical_context','new_donor')),
            proposal_state TEXT NOT NULL
                CHECK(proposal_state IN ('canonical_context','proposed')),
            reviewed_record_fingerprint TEXT NOT NULL,
            preflight_record_fingerprint TEXT NOT NULL,
            reviewed_account_identity TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(attempt_id,site,observation_id),
            UNIQUE(attempt_id,attempt_member_id))"""
    )
    conn.execute(
        "INSERT INTO sync_consolidation_attempt_members_v13("
        "attempt_id,stable_member_id,site,observation_id,remote_uuid,"
        "participation_role,proposal_state,reviewed_record_fingerprint,"
        "preflight_record_fingerprint,reviewed_account_identity,created_at) "
        "SELECT am.attempt_id,m.consolidation_member_id,m.site,m.observation_id,"
        "m.remote_uuid,am.participation_role,"
        "CASE am.participation_role WHEN 'canonical_context' THEN "
        "'canonical_context' ELSE 'proposed' END,"
        "am.reviewed_record_fingerprint,am.preflight_record_fingerprint,"
        "CASE m.site WHEN 'mo' THEN a.destination_mo_account "
        "ELSE a.destination_inat_account END,am.created_at "
        "FROM sync_consolidation_attempt_members am "
        "JOIN sync_consolidation_members m "
        "ON m.consolidation_member_id=am.consolidation_member_id "
        "JOIN sync_consolidation_attempts a ON a.attempt_id=am.attempt_id"
    )
    conn.execute(
        """CREATE TABLE sync_consolidation_evidence_v13(
            consolidation_evidence_id INTEGER PRIMARY KEY,
            attempt_id INTEGER NOT NULL,
            left_attempt_member_id INTEGER NOT NULL,
            right_attempt_member_id INTEGER NOT NULL,
            evidence_type TEXT NOT NULL,
            evidence_strength TEXT NOT NULL
                CHECK(evidence_strength IN ('strong','corroborating')),
            reviewed_evidence_fingerprint TEXT NOT NULL,
            display_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(attempt_id,left_attempt_member_id)
                REFERENCES sync_consolidation_attempt_members_v13(
                    attempt_id,attempt_member_id) ON DELETE CASCADE,
            FOREIGN KEY(attempt_id,right_attempt_member_id)
                REFERENCES sync_consolidation_attempt_members_v13(
                    attempt_id,attempt_member_id) ON DELETE CASCADE,
            CHECK(left_attempt_member_id < right_attempt_member_id),
            UNIQUE(attempt_id,left_attempt_member_id,right_attempt_member_id,evidence_type))"""
    )
    conn.execute(
        "INSERT INTO sync_consolidation_evidence_v13("
        "consolidation_evidence_id,attempt_id,left_attempt_member_id,"
        "right_attempt_member_id,evidence_type,evidence_strength,"
        "reviewed_evidence_fingerprint,display_summary,created_at) "
        "SELECT e.consolidation_evidence_id,e.attempt_id,lam.attempt_member_id,"
        "ram.attempt_member_id,e.evidence_type,e.evidence_strength,"
        "e.reviewed_evidence_fingerprint,e.display_summary,e.created_at "
        "FROM sync_consolidation_evidence e "
        "JOIN sync_consolidation_attempt_members_v13 lam "
        "ON lam.attempt_id=e.attempt_id AND lam.stable_member_id=e.left_member_id "
        "JOIN sync_consolidation_attempt_members_v13 ram "
        "ON ram.attempt_id=e.attempt_id AND ram.stable_member_id=e.right_member_id"
    )
    old_evidence_count = int(conn.execute(
        "SELECT COUNT(*) FROM sync_consolidation_evidence"
    ).fetchone()[0])
    new_evidence_count = int(conn.execute(
        "SELECT COUNT(*) FROM sync_consolidation_evidence_v13"
    ).fetchone()[0])
    if old_evidence_count != new_evidence_count:
        raise RuntimeError(
            "v13 migration could not preserve every consolidation evidence edge"
        )

    conn.execute("DROP TABLE sync_consolidation_evidence")
    conn.execute("DROP TRIGGER trg_consolidation_attempt_member_identity")
    conn.execute("DROP TRIGGER trg_consolidation_attempt_member_immutable")
    conn.execute("DROP TABLE sync_consolidation_attempt_members")
    conn.execute(
        "ALTER TABLE sync_consolidation_attempt_members_v13 "
        "RENAME TO sync_consolidation_attempt_members"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_evidence_v13 "
        "RENAME TO sync_consolidation_evidence"
    )
    conn.execute(
        "CREATE INDEX idx_consolidation_attempt_members_attempt "
        "ON sync_consolidation_attempt_members(attempt_id,attempt_member_id)"
    )
    conn.execute(
        "CREATE INDEX idx_consolidation_attempt_members_stable "
        "ON sync_consolidation_attempt_members(stable_member_id,attempt_id)"
    )
    conn.execute(
        "CREATE INDEX idx_consolidation_attempt_members_observation "
        "ON sync_consolidation_attempt_members(site,observation_id,attempt_id)"
    )
    conn.execute(
        "CREATE INDEX idx_consolidation_evidence_attempt "
        "ON sync_consolidation_evidence("
        "attempt_id,left_attempt_member_id,right_attempt_member_id)"
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_attempt_member_identity
        BEFORE INSERT ON sync_consolidation_attempt_members
        WHEN NEW.stable_member_id IS NOT NULL AND NOT EXISTS (
            SELECT 1
            FROM sync_consolidation_attempts a
            JOIN sync_consolidation_members m
              ON m.consolidation_member_id=NEW.stable_member_id
            WHERE a.attempt_id=NEW.attempt_id
              AND a.profile_id=m.profile_id
              AND a.consolidation_id=m.consolidation_id
              AND m.site=NEW.site
              AND m.observation_id=NEW.observation_id
        )
        BEGIN
            SELECT RAISE(ABORT,
                'attempt member must match its stable consolidation member');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_attempt_member_immutable
        BEFORE UPDATE ON sync_consolidation_attempt_members
        BEGIN
            SELECT RAISE(ABORT, 'attempt member evidence is immutable');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_evidence_immutable
        BEFORE UPDATE ON sync_consolidation_evidence
        BEGIN
            SELECT RAISE(ABORT, 'consolidation evidence is immutable');
        END"""
    )

    # Establish the ordered baseline chain only from attempts whose local
    # finalization action is durably succeeded.
    consolidations = conn.execute(
        "SELECT consolidation_id,state FROM sync_consolidations "
        "ORDER BY consolidation_id"
    ).fetchall()
    for consolidation in consolidations:
        consolidation_id = int(consolidation["consolidation_id"])
        successful = conn.execute(
            "SELECT a.attempt_id FROM sync_consolidation_attempts a "
            "WHERE a.consolidation_id=? AND a.state='succeeded' "
            "AND EXISTS (SELECT 1 FROM sync_actions sa "
            "WHERE sa.action_group_id=a.action_group_id "
            "AND sa.action_type='consolidation_finalize' "
            "AND sa.state='succeeded') ORDER BY a.attempt_id",
            (consolidation_id,),
        ).fetchall()
        successful_ids = {int(row["attempt_id"]) for row in successful}
        previous: Optional[int] = None
        attempts = conn.execute(
            "SELECT attempt_id FROM sync_consolidation_attempts "
            "WHERE consolidation_id=? ORDER BY attempt_id",
            (consolidation_id,),
        ).fetchall()
        for row in attempts:
            attempt_id = int(row["attempt_id"])
            conn.execute(
                "UPDATE sync_consolidation_attempts "
                "SET base_finalized_attempt_id=? WHERE attempt_id=?",
                (previous, attempt_id),
            )
            if attempt_id in successful_ids:
                previous = attempt_id
        if str(consolidation["state"]) == "finalized" and previous is None:
            raise RuntimeError(
                "v13 migration found a finalized consolidation without a "
                "successfully finalized attempt"
            )
        if previous is not None:
            cursor = conn.execute(
                "UPDATE sync_consolidations "
                "SET current_finalized_attempt_id=? WHERE consolidation_id=?",
                (previous, consolidation_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("v13 migration could not set the finalized baseline")

    admitted_donors = conn.execute(
        "SELECT consolidation_member_id FROM sync_consolidation_members "
        "WHERE role='donor' AND local_state='superseded'"
    ).fetchall()
    for row in admitted_donors:
        member_id = int(row["consolidation_member_id"])
        finalized = conn.execute(
            "SELECT a.attempt_id,a.updated_at "
            "FROM sync_consolidation_attempt_members am "
            "JOIN sync_consolidation_attempts a ON a.attempt_id=am.attempt_id "
            "WHERE am.stable_member_id=? "
            "AND am.participation_role='new_donor' AND a.state='succeeded' "
            "AND EXISTS (SELECT 1 FROM sync_actions sa "
            "WHERE sa.action_group_id=a.action_group_id "
            "AND sa.action_type='consolidation_finalize' "
            "AND sa.state='succeeded') "
            "ORDER BY a.attempt_id LIMIT 1",
            (member_id,),
        ).fetchone()
        if finalized is None:
            raise RuntimeError(
                "v13 migration found a superseded donor without a successful "
                "admission finalization"
            )
        cursor = conn.execute(
            "UPDATE sync_consolidation_members "
            "SET superseded_by_attempt_id=?,superseded_at=? "
            "WHERE consolidation_member_id=? AND role='donor' "
            "AND local_state='superseded'",
            (
                int(finalized["attempt_id"]), str(finalized["updated_at"]),
                member_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("v13 migration could not preserve donor admission")

    # Active v12 donors were only proposals. Preserve their attempt snapshots,
    # then release stable membership. Pending/outcome-unknown attempts still
    # reserve them through the query below until ambiguity is resolved.
    conn.execute("DROP TRIGGER trg_consolidation_attempt_member_immutable")
    active_donors = conn.execute(
        "SELECT consolidation_member_id FROM sync_consolidation_members "
        "WHERE role='donor' AND local_state='active'"
    ).fetchall()
    for row in active_donors:
        member_id = int(row["consolidation_member_id"])
        attempts = conn.execute(
            "SELECT a.state FROM sync_consolidation_attempt_members am "
            "JOIN sync_consolidation_attempts a ON a.attempt_id=am.attempt_id "
            "WHERE am.stable_member_id=? AND am.participation_role='new_donor'",
            (member_id,),
        ).fetchall()
        if not attempts or any(str(item["state"]) == "succeeded" for item in attempts):
            raise RuntimeError(
                "v13 migration cannot safely classify an active legacy donor proposal"
            )
        cursor = conn.execute(
            "DELETE FROM sync_consolidation_members "
            "WHERE consolidation_member_id=? AND role='donor' "
            "AND local_state='active'",
            (member_id,),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("v13 migration could not release a legacy donor proposal")
    conn.execute(
        """CREATE TRIGGER trg_consolidation_attempt_member_immutable
        BEFORE UPDATE ON sync_consolidation_attempt_members
        BEGIN
            SELECT RAISE(ABORT, 'attempt member evidence is immutable');
        END"""
    )

    # A pending or outcome-unknown proposal is globally reserved without being
    # mislabeled as admitted stable membership.
    conn.execute(
        """CREATE VIEW sync_unresolved_consolidation_proposals AS
        SELECT a.profile_id,a.consolidation_id,am.site,am.observation_id,a.state
        FROM sync_consolidation_attempt_members am
        JOIN sync_consolidation_attempts a ON a.attempt_id=am.attempt_id
        WHERE am.participation_role='new_donor'
          AND a.state IN ('pending','outcome_unknown')"""
    )


def _migration_v14(conn: sqlite3.Connection) -> None:
    """Stable identity baselines, abandonment, and enforced donor admission."""
    conn.execute(
        "ALTER TABLE sync_consolidation_attempt_members ADD COLUMN "
        "reviewed_identity_fingerprint TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_attempt_members ADD COLUMN "
        "reviewed_mutable_components TEXT NOT NULL DEFAULT '{}'"
    )
    rows = conn.execute(
        "SELECT attempt_member_id,site,observation_id,remote_uuid,"
        "reviewed_account_identity FROM sync_consolidation_attempt_members"
    ).fetchall()
    conn.execute("DROP TRIGGER trg_consolidation_attempt_member_immutable")
    for row in rows:
        fingerprint = canonical_identity_fingerprint(
            str(row["site"]),
            int(row["observation_id"]),
            str(row["remote_uuid"] or ""),
            str(row["reviewed_account_identity"] or ""),
        )
        cursor = conn.execute(
            "UPDATE sync_consolidation_attempt_members "
            "SET reviewed_identity_fingerprint=? WHERE attempt_member_id=?",
            (fingerprint, int(row["attempt_member_id"])),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "v14 migration could not establish an attempt identity fingerprint"
            )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_attempt_member_immutable
        BEFORE UPDATE OF attempt_id,site,observation_id,remote_uuid,
            participation_role,proposal_state,reviewed_record_fingerprint,
            preflight_record_fingerprint,reviewed_account_identity,created_at,
            reviewed_identity_fingerprint,reviewed_mutable_components
        ON sync_consolidation_attempt_members
        BEGIN
            SELECT RAISE(ABORT, 'attempt member evidence is immutable');
        END"""
    )

    conn.execute(
        "ALTER TABLE sync_consolidation_members ADD COLUMN "
        "originally_proposed_by_attempt_id INTEGER "
        "REFERENCES sync_consolidation_attempts(attempt_id)"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_members ADD COLUMN "
        "admitted_from_attempt_member_id INTEGER "
        "REFERENCES sync_consolidation_attempt_members(attempt_member_id)"
    )
    conn.execute("DROP TRIGGER trg_consolidation_member_identity_immutable")
    conn.execute(
        "UPDATE sync_consolidation_members "
        "SET originally_proposed_by_attempt_id=added_by_attempt_id"
    )
    donors = conn.execute(
        "SELECT consolidation_member_id,profile_id,consolidation_id,site,"
        "observation_id,superseded_by_attempt_id "
        "FROM sync_consolidation_members WHERE role='donor'"
    ).fetchall()
    for donor in donors:
        admission_attempt = donor["superseded_by_attempt_id"]
        if admission_attempt is None:
            raise RuntimeError(
                "v14 migration found a stable donor without an admission attempt"
            )
        snapshot = conn.execute(
            "SELECT am.attempt_member_id FROM sync_consolidation_attempt_members am "
            "JOIN sync_consolidation_attempts a ON a.attempt_id=am.attempt_id "
            "WHERE am.attempt_id=? AND am.site=? AND am.observation_id=? "
            "AND am.participation_role='new_donor' "
            "AND a.profile_id=? AND a.consolidation_id=?",
            (
                int(admission_attempt), str(donor["site"]),
                int(donor["observation_id"]), int(donor["profile_id"]),
                int(donor["consolidation_id"]),
            ),
        ).fetchone()
        if snapshot is None:
            raise RuntimeError(
                "v14 migration could not prove a stable donor's attempt snapshot"
            )
        cursor = conn.execute(
            "UPDATE sync_consolidation_members "
            "SET added_by_attempt_id=?,admitted_from_attempt_member_id=? "
            "WHERE consolidation_member_id=? AND role='donor'",
            (
                int(admission_attempt), int(snapshot["attempt_member_id"]),
                int(donor["consolidation_member_id"]),
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("v14 migration could not link donor admission")
    conn.execute(
        """CREATE TRIGGER trg_consolidation_member_admission_insert
        BEFORE INSERT ON sync_consolidation_members
        WHEN NEW.role='donor' AND (
            NEW.added_by_attempt_id IS NULL
            OR NEW.admitted_from_attempt_member_id IS NULL
            OR NOT EXISTS (
                SELECT 1
                FROM sync_consolidation_attempt_members am
                JOIN sync_consolidation_attempts a
                  ON a.attempt_id=am.attempt_id
                WHERE am.attempt_member_id=NEW.admitted_from_attempt_member_id
                  AND am.attempt_id=NEW.added_by_attempt_id
                  AND am.site=NEW.site
                  AND am.observation_id=NEW.observation_id
                  AND am.participation_role='new_donor'
                  AND a.profile_id=NEW.profile_id
                  AND a.consolidation_id=NEW.consolidation_id
                  AND a.state='pending'
            )
        )
        BEGIN
            SELECT RAISE(ABORT,
                'stable donor must match its admitting attempt member');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_member_admission_update
        BEFORE UPDATE OF added_by_attempt_id,admitted_from_attempt_member_id,
            profile_id,consolidation_id,site,observation_id,role
        ON sync_consolidation_members
        WHEN NEW.role='donor' AND (
            NEW.added_by_attempt_id IS NULL
            OR NEW.admitted_from_attempt_member_id IS NULL
            OR NOT EXISTS (
                SELECT 1
                FROM sync_consolidation_attempt_members am
                JOIN sync_consolidation_attempts a
                  ON a.attempt_id=am.attempt_id
                WHERE am.attempt_member_id=NEW.admitted_from_attempt_member_id
                  AND am.attempt_id=NEW.added_by_attempt_id
                  AND am.site=NEW.site
                  AND am.observation_id=NEW.observation_id
                  AND am.participation_role='new_donor'
                  AND a.profile_id=NEW.profile_id
                  AND a.consolidation_id=NEW.consolidation_id
                  AND a.state='pending'
            )
        )
        BEGIN
            SELECT RAISE(ABORT,
                'stable donor must match its admitting attempt member');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_member_identity_immutable
        BEFORE UPDATE OF consolidation_id,profile_id,site,observation_id,role,
            remote_uuid,reviewed_record_fingerprint,preflight_record_fingerprint,
            created_at,added_by_attempt_id,originally_proposed_by_attempt_id,
            admitted_from_attempt_member_id
        ON sync_consolidation_members
        BEGIN
            SELECT RAISE(ABORT, 'stable consolidation member identity is immutable');
        END"""
    )


def _migration_v15(conn: sqlite3.Connection) -> None:
    """Constrain finalized baselines and split stable owner ID from login."""
    conn.execute(
        "ALTER TABLE sync_consolidation_attempt_members ADD COLUMN "
        "reviewed_owner_account_id INTEGER NOT NULL DEFAULT 0"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_attempt_members ADD COLUMN "
        "reviewed_owner_login TEXT NOT NULL DEFAULT ''"
    )
    rows = conn.execute(
        "SELECT attempt_member_id,site,observation_id,remote_uuid,"
        "reviewed_account_identity FROM sync_consolidation_attempt_members"
    ).fetchall()
    conn.execute("DROP TRIGGER trg_consolidation_attempt_member_immutable")
    for row in rows:
        try:
            owner_account_id, owner_login = parse_legacy_account_identity(
                row["reviewed_account_identity"]
            )
        except ValueError as exc:
            raise RuntimeError(
                "v15 migration cannot safely normalize a legacy account identity"
            ) from exc
        fingerprint = canonical_stable_identity_fingerprint(
            str(row["site"]),
            int(row["observation_id"]),
            str(row["remote_uuid"] or ""),
            owner_account_id,
        )
        cursor = conn.execute(
            "UPDATE sync_consolidation_attempt_members "
            "SET reviewed_owner_account_id=?,reviewed_owner_login=?,"
            "reviewed_identity_fingerprint=? WHERE attempt_member_id=?",
            (
                owner_account_id, owner_login, fingerprint,
                int(row["attempt_member_id"]),
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "v15 migration could not establish stable attempt ownership"
            )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_attempt_member_immutable
        BEFORE UPDATE OF attempt_id,site,observation_id,remote_uuid,
            participation_role,proposal_state,reviewed_record_fingerprint,
            preflight_record_fingerprint,reviewed_account_identity,created_at,
            reviewed_owner_account_id,reviewed_owner_login,
            reviewed_identity_fingerprint,reviewed_mutable_components
        ON sync_consolidation_attempt_members
        BEGIN
            SELECT RAISE(ABORT, 'attempt member evidence is immutable');
        END"""
    )

    conn.execute(
        "ALTER TABLE sync_consolidation_members ADD COLUMN "
        "stable_owner_account_id INTEGER NOT NULL DEFAULT 0"
    )
    stable_members = conn.execute(
        "SELECT consolidation_member_id FROM sync_consolidation_members"
    ).fetchall()
    for stable_member in stable_members:
        member_id = int(stable_member["consolidation_member_id"])
        owners = conn.execute(
            "SELECT DISTINCT reviewed_owner_account_id "
            "FROM sync_consolidation_attempt_members "
            "WHERE stable_member_id=? AND reviewed_owner_account_id>0 "
            "ORDER BY reviewed_owner_account_id",
            (member_id,),
        ).fetchall()
        if len(owners) != 1:
            raise RuntimeError(
                "v15 migration cannot unambiguously prove a stable member's "
                "numeric owner identity"
            )
        cursor = conn.execute(
            "UPDATE sync_consolidation_members SET stable_owner_account_id=? "
            "WHERE consolidation_member_id=?",
            (int(owners[0]["reviewed_owner_account_id"]), member_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "v15 migration could not establish stable numeric ownership"
            )
    conn.execute("DROP TRIGGER trg_consolidation_member_identity_immutable")
    conn.execute(
        """CREATE TRIGGER trg_consolidation_member_identity_immutable
        BEFORE UPDATE OF consolidation_id,profile_id,site,observation_id,role,
            remote_uuid,reviewed_record_fingerprint,preflight_record_fingerprint,
            created_at,added_by_attempt_id,originally_proposed_by_attempt_id,
            admitted_from_attempt_member_id,stable_owner_account_id
        ON sync_consolidation_members
        BEGIN
            SELECT RAISE(ABORT, 'stable consolidation member identity is immutable');
        END"""
    )
    _install_v15_baseline_guards(conn)


def _install_v15_baseline_guards(conn: sqlite3.Connection) -> None:
    invalid_current = conn.execute(
        "SELECT c.consolidation_id FROM sync_consolidations c "
        "WHERE c.current_finalized_attempt_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM sync_consolidation_attempts a "
        "WHERE a.attempt_id=c.current_finalized_attempt_id "
        "AND a.profile_id=c.profile_id "
        "AND a.consolidation_id=c.consolidation_id "
        "AND a.state='succeeded' AND EXISTS ("
        "SELECT 1 FROM sync_actions sa "
        "WHERE sa.profile_id=a.profile_id "
        "AND sa.action_group_id=a.action_group_id "
        "AND sa.action_type='consolidation_finalize' "
        "AND sa.state='succeeded')) LIMIT 1"
    ).fetchone()
    if invalid_current is not None:
        raise RuntimeError(
            "v15 migration found an invalid current finalized-attempt pointer"
        )
    invalid_base = conn.execute(
        "SELECT a.attempt_id FROM sync_consolidation_attempts a "
        "JOIN sync_consolidation_attempts b "
        "ON b.attempt_id=a.base_finalized_attempt_id "
        "WHERE a.base_finalized_attempt_id IS NOT NULL AND ("
        "b.profile_id!=a.profile_id OR b.consolidation_id!=a.consolidation_id "
        "OR b.attempt_id>=a.attempt_id OR b.state!='succeeded' "
        "OR NOT EXISTS (SELECT 1 FROM sync_actions sa "
        "WHERE sa.profile_id=b.profile_id "
        "AND sa.action_group_id=b.action_group_id "
        "AND sa.action_type='consolidation_finalize' "
        "AND sa.state='succeeded')) LIMIT 1"
    ).fetchone()
    if invalid_base is not None:
        raise RuntimeError(
            "v15 migration found an invalid finalized-attempt baseline chain"
        )
    invalid_admission = conn.execute(
        "SELECT m.consolidation_member_id "
        "FROM sync_consolidation_members m "
        "JOIN sync_consolidation_attempt_members am "
        "ON am.attempt_member_id=m.admitted_from_attempt_member_id "
        "JOIN sync_consolidation_attempts a ON a.attempt_id=am.attempt_id "
        "WHERE m.role='donor' AND ("
        "a.attempt_id!=m.added_by_attempt_id "
        "OR a.profile_id!=m.profile_id "
        "OR a.consolidation_id!=m.consolidation_id "
        "OR am.site!=m.site OR am.observation_id!=m.observation_id "
        "OR am.participation_role!='new_donor') LIMIT 1"
    ).fetchone()
    if invalid_admission is not None:
        raise RuntimeError(
            "v15 migration found invalid admitted-donor provenance"
        )

    current_condition = """
        NEW.current_finalized_attempt_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM sync_consolidation_attempts a
            WHERE a.attempt_id=NEW.current_finalized_attempt_id
              AND a.profile_id=NEW.profile_id
              AND a.consolidation_id=NEW.consolidation_id
              AND a.state='succeeded'
              AND EXISTS (
                  SELECT 1 FROM sync_actions sa
                  WHERE sa.profile_id=a.profile_id
                    AND sa.action_group_id=a.action_group_id
                    AND sa.action_type='consolidation_finalize'
                    AND sa.state='succeeded'
              )
        )
    """
    for event in (
        "BEFORE INSERT",
        "BEFORE UPDATE OF current_finalized_attempt_id,profile_id,consolidation_id",
    ):
        suffix = "insert" if event == "BEFORE INSERT" else "update"
        conn.execute(
            f"""CREATE TRIGGER trg_consolidation_current_baseline_{suffix}
            {event} ON sync_consolidations
            WHEN {current_condition}
            BEGIN
                SELECT RAISE(ABORT,
                    'current baseline must be a successfully finalized attempt');
            END"""
        )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_current_baseline_monotonic
        BEFORE UPDATE OF current_finalized_attempt_id
        ON sync_consolidations
        WHEN OLD.current_finalized_attempt_id IS NOT NULL AND (
            NEW.current_finalized_attempt_id IS NULL
            OR NEW.current_finalized_attempt_id<=OLD.current_finalized_attempt_id
        )
        BEGIN
            SELECT RAISE(ABORT,
                'current finalized baseline can only advance');
        END"""
    )

    base_condition = """
        NOT EXISTS (
            SELECT 1 FROM sync_consolidations c
            WHERE c.profile_id=NEW.profile_id
              AND c.consolidation_id=NEW.consolidation_id
              AND (
                  (NEW.base_finalized_attempt_id IS NULL
                   AND c.current_finalized_attempt_id IS NULL)
                  OR (
                      NEW.base_finalized_attempt_id IS NOT NULL
                      AND c.current_finalized_attempt_id=
                          NEW.base_finalized_attempt_id
                      AND NEW.base_finalized_attempt_id<NEW.attempt_id
                      AND EXISTS (
                          SELECT 1 FROM sync_consolidation_attempts b
                          WHERE b.attempt_id=NEW.base_finalized_attempt_id
                            AND b.profile_id=NEW.profile_id
                            AND b.consolidation_id=NEW.consolidation_id
                            AND b.state='succeeded'
                            AND EXISTS (
                                SELECT 1 FROM sync_actions sa
                                WHERE sa.profile_id=b.profile_id
                                  AND sa.action_group_id=b.action_group_id
                                  AND sa.action_type='consolidation_finalize'
                                  AND sa.state='succeeded'
                            )
                      )
                  )
              )
        )
    """
    conn.execute(
        f"""CREATE TRIGGER trg_consolidation_attempt_base_insert
        AFTER INSERT ON sync_consolidation_attempts
        WHEN {base_condition}
        BEGIN
            SELECT RAISE(ABORT,
                'attempt base must equal its consolidation current baseline');
        END"""
    )
    conn.execute(
        f"""CREATE TRIGGER trg_consolidation_attempt_base_update
        BEFORE UPDATE OF base_finalized_attempt_id,profile_id,consolidation_id
        ON sync_consolidation_attempts
        WHEN {base_condition}
        BEGIN
            SELECT RAISE(ABORT,
                'attempt base must equal its consolidation current baseline');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_current_attempt_immutable
        BEFORE UPDATE OF profile_id,consolidation_id,state,action_group_id
        ON sync_consolidation_attempts
        WHEN (
            EXISTS (
                SELECT 1 FROM sync_consolidations c
                WHERE c.current_finalized_attempt_id=OLD.attempt_id
            )
            OR EXISTS (
                SELECT 1 FROM sync_consolidation_attempts child
                WHERE child.base_finalized_attempt_id=OLD.attempt_id
            )
        ) AND (
            NEW.profile_id!=OLD.profile_id
            OR NEW.consolidation_id!=OLD.consolidation_id
            OR NEW.action_group_id!=OLD.action_group_id
            OR NEW.state!='succeeded'
        )
        BEGIN
            SELECT RAISE(ABORT,
                'a current finalized baseline attempt is immutable');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_current_finalize_action_immutable
        BEFORE UPDATE OF profile_id,action_group_id,action_type,state
        ON sync_actions
        WHEN OLD.action_type='consolidation_finalize'
          AND OLD.state='succeeded'
          AND EXISTS (
              SELECT 1 FROM sync_consolidation_attempts a
              WHERE a.profile_id=OLD.profile_id
                AND a.action_group_id=OLD.action_group_id
                AND (
                    EXISTS (
                        SELECT 1 FROM sync_consolidations c
                        WHERE c.current_finalized_attempt_id=a.attempt_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM sync_consolidation_attempts child
                        WHERE child.base_finalized_attempt_id=a.attempt_id
                    )
                )
          )
          AND (
              NEW.profile_id!=OLD.profile_id
              OR NEW.action_group_id!=OLD.action_group_id
              OR NEW.action_type!='consolidation_finalize'
              OR NEW.state!='succeeded'
          )
        BEGIN
            SELECT RAISE(ABORT,
                'a current baseline finalization action is immutable');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_consolidation_current_finalize_action_delete
        BEFORE DELETE ON sync_actions
        WHEN OLD.action_type='consolidation_finalize'
          AND OLD.state='succeeded'
          AND EXISTS (
              SELECT 1 FROM sync_consolidation_attempts a
              WHERE a.profile_id=OLD.profile_id
                AND a.action_group_id=OLD.action_group_id
                AND (
                    EXISTS (
                        SELECT 1 FROM sync_consolidations c
                        WHERE c.current_finalized_attempt_id=a.attempt_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM sync_consolidation_attempts child
                        WHERE child.base_finalized_attempt_id=a.attempt_id
                    )
                )
          )
        BEGIN
            SELECT RAISE(ABORT,
                'a current baseline finalization action is immutable');
        END"""
    )


def _migration_v16(conn: sqlite3.Connection) -> None:
    """Gate 2C: immutable lossless-deletion reviews and remote tombstones.

    Destructive actions deliberately have their own journal.  This keeps a
    deletion's exact remote identity and request lifecycle separate from the
    additive-write journal and makes it impossible for an older generic action
    dispatcher to execute a deletion accidentally.
    """
    conn.execute(
        "ALTER TABLE sync_consolidation_members ADD COLUMN "
        "remote_state TEXT NOT NULL DEFAULT 'online' "
        "CHECK(remote_state IN ('online','deleted'))"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_members ADD COLUMN deleted_remotely_at TEXT"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_members ADD COLUMN "
        "deleted_by_deletion_attempt_id INTEGER"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_members ADD COLUMN "
        "deleted_by_deletion_item_id INTEGER"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_members ADD COLUMN "
        "last_deletion_reviewed_record_fingerprint TEXT NOT NULL DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_members ADD COLUMN "
        "canonical_destination_mo_id INTEGER"
    )
    conn.execute(
        "ALTER TABLE sync_consolidation_members ADD COLUMN "
        "canonical_destination_inat_id INTEGER"
    )

    conn.execute(
        """CREATE TABLE sync_deletion_attempts(
            deletion_attempt_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL
                REFERENCES sync_profiles(profile_id) ON DELETE RESTRICT,
            consolidation_id INTEGER NOT NULL,
            base_finalized_attempt_id INTEGER NOT NULL,
            action_group_id INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN (
                    'pending','succeeded','partial','failed','cancelled',
                    'outcome_unknown','retry_required','superseded')),
            supersedes_attempt_id INTEGER
                REFERENCES sync_deletion_attempts(deletion_attempt_id)
                ON DELETE RESTRICT,
            canonical_stable_identity_fingerprint TEXT NOT NULL,
            canonical_mutable_snapshot_fingerprint TEXT NOT NULL,
            parity_report_fingerprint TEXT NOT NULL,
            reviewed_auth_generation INTEGER NOT NULL,
            reviewed_mo_key_generation INTEGER NOT NULL,
            confirmation_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(profile_id,consolidation_id)
                REFERENCES sync_consolidations(profile_id,consolidation_id)
                ON DELETE RESTRICT,
            FOREIGN KEY(base_finalized_attempt_id)
                REFERENCES sync_consolidation_attempts(attempt_id)
                ON DELETE RESTRICT,
            UNIQUE(profile_id,action_group_id))"""
    )
    conn.execute(
        "CREATE INDEX idx_deletion_attempts_consolidation "
        "ON sync_deletion_attempts(profile_id,consolidation_id,state)"
    )

    conn.execute(
        """CREATE TABLE sync_deletion_items(
            deletion_item_id INTEGER PRIMARY KEY,
            deletion_attempt_id INTEGER NOT NULL
                REFERENCES sync_deletion_attempts(deletion_attempt_id)
                ON DELETE RESTRICT,
            stable_member_id INTEGER NOT NULL
                REFERENCES sync_consolidation_members(consolidation_member_id)
                ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL,
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            observation_id INTEGER NOT NULL,
            remote_uuid TEXT NOT NULL DEFAULT '',
            reviewed_remote_record_fingerprint TEXT NOT NULL,
            reviewed_content_inventory_fingerprint TEXT NOT NULL,
            reviewed_parity_fingerprint TEXT NOT NULL,
            reviewed_third_party_activity_fingerprint TEXT NOT NULL,
            reviewed_dependency_fingerprint TEXT NOT NULL,
            action_id INTEGER,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN (
                    'pending','running','succeeded','failed','cancelled',
                    'outcome_unknown','retry_required')),
            disabled_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(deletion_attempt_id,stable_member_id),
            UNIQUE(deletion_attempt_id,ordinal),
            UNIQUE(action_id))"""
    )
    conn.execute(
        "CREATE INDEX idx_deletion_items_member "
        "ON sync_deletion_items(stable_member_id,state)"
    )

    conn.execute(
        """CREATE TABLE sync_deletion_actions(
            deletion_action_id INTEGER PRIMARY KEY,
            profile_id INTEGER NOT NULL
                REFERENCES sync_profiles(profile_id) ON DELETE RESTRICT,
            deletion_attempt_id INTEGER NOT NULL
                REFERENCES sync_deletion_attempts(deletion_attempt_id)
                ON DELETE RESTRICT,
            deletion_item_id INTEGER
                REFERENCES sync_deletion_items(deletion_item_id)
                ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL,
            action_type TEXT NOT NULL CHECK(action_type IN (
                'mo_observation_delete','inat_observation_delete',
                'deletion_finalize')),
            site TEXT NOT NULL CHECK(site IN ('inat','mo')),
            observation_id INTEGER NOT NULL,
            remote_uuid TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK(state IN (
                    'pending','running','succeeded','failed','cancelled',
                    'outcome_unknown','retry_required')),
            request_correlation TEXT NOT NULL,
            reviewed_identity_fingerprint TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            attempt_started_at TEXT,
            write_started_at TEXT,
            finished_at TEXT,
            last_phase TEXT NOT NULL DEFAULT 'journaled',
            last_error_code TEXT NOT NULL DEFAULT '',
            last_http_status INTEGER,
            verification_state TEXT NOT NULL DEFAULT '',
            verified_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(profile_id,deletion_action_id),
            UNIQUE(deletion_attempt_id,ordinal),
            UNIQUE(request_correlation))"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX uq_unresolved_deletion_member "
        "ON sync_deletion_actions(profile_id,site,observation_id) "
        "WHERE state IN ('pending','running','outcome_unknown') "
        "AND action_type!='deletion_finalize'"
    )
    conn.execute(
        "CREATE INDEX idx_deletion_actions_state "
        "ON sync_deletion_actions(profile_id,state,created_at)"
    )

    conn.execute(
        """CREATE TABLE sync_deletion_parity_items(
            deletion_parity_item_id INTEGER PRIMARY KEY,
            deletion_item_id INTEGER NOT NULL
                REFERENCES sync_deletion_items(deletion_item_id)
                ON DELETE RESTRICT,
            ordinal INTEGER NOT NULL,
            source_content_type TEXT NOT NULL,
            source_content_identity TEXT NOT NULL,
            canonical_matching_identity TEXT NOT NULL DEFAULT '',
            match_method TEXT NOT NULL DEFAULT '',
            reviewed_source_fingerprint TEXT NOT NULL,
            reviewed_canonical_fingerprint TEXT NOT NULL DEFAULT '',
            eligibility_result TEXT NOT NULL
                CHECK(eligibility_result IN ('preserved','blocked')),
            blocking_reason TEXT NOT NULL DEFAULT '',
            safe_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(deletion_item_id,ordinal),
            UNIQUE(deletion_item_id,source_content_type,source_content_identity))"""
    )

    conn.execute(
        """CREATE TRIGGER trg_deletion_attempt_review_immutable
        BEFORE UPDATE OF profile_id,consolidation_id,base_finalized_attempt_id,
            action_group_id,supersedes_attempt_id,
            canonical_stable_identity_fingerprint,
            canonical_mutable_snapshot_fingerprint,parity_report_fingerprint,
            reviewed_auth_generation,reviewed_mo_key_generation,
            confirmation_fingerprint,created_at
        ON sync_deletion_attempts
        BEGIN
            SELECT RAISE(ABORT, 'deletion review is immutable');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_deletion_item_review_immutable
        BEFORE UPDATE OF deletion_attempt_id,stable_member_id,ordinal,site,
            observation_id,remote_uuid,reviewed_remote_record_fingerprint,
            reviewed_content_inventory_fingerprint,reviewed_parity_fingerprint,
            reviewed_third_party_activity_fingerprint,
            reviewed_dependency_fingerprint,disabled_reason,created_at
        ON sync_deletion_items
        BEGIN
            SELECT RAISE(ABORT, 'deletion item review is immutable');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_deletion_parity_immutable
        BEFORE UPDATE ON sync_deletion_parity_items
        BEGIN
            SELECT RAISE(ABORT, 'deletion parity proof is immutable');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_deletion_parity_no_delete
        BEFORE DELETE ON sync_deletion_parity_items
        BEGIN
            SELECT RAISE(ABORT, 'deletion parity history cannot be removed');
        END"""
    )


def _migration_v17(conn: sqlite3.Connection) -> None:
    """Serialize Phase 2B/2C and harden irreversible tombstone provenance."""
    duplicate = conn.execute(
        "SELECT profile_id,consolidation_id,COUNT(*) AS count "
        "FROM sync_deletion_attempts "
        "WHERE state IN ('pending','partial','outcome_unknown') "
        "GROUP BY profile_id,consolidation_id HAVING COUNT(*)>1 LIMIT 1"
    ).fetchone()
    if duplicate is not None:
        raise RuntimeError(
            "v17 migration found multiple unresolved deletion attempts for "
            "one consolidation; manual ledger review is required"
        )
    invalid_tombstone = conn.execute(
        """SELECT 1
        FROM sync_consolidation_members m
        WHERE m.remote_state='deleted' AND NOT EXISTS (
            SELECT 1
            FROM sync_deletion_items di
            JOIN sync_deletion_attempts da
              ON da.deletion_attempt_id=di.deletion_attempt_id
            JOIN sync_deletion_actions act
              ON act.deletion_action_id=di.action_id
            WHERE di.deletion_item_id=m.deleted_by_deletion_item_id
              AND di.deletion_attempt_id=m.deleted_by_deletion_attempt_id
              AND di.stable_member_id=m.consolidation_member_id
              AND di.state='succeeded'
              AND da.profile_id=m.profile_id
              AND da.consolidation_id=m.consolidation_id
              AND act.profile_id=m.profile_id
              AND act.deletion_attempt_id=di.deletion_attempt_id
              AND act.deletion_item_id=di.deletion_item_id
              AND act.state='succeeded'
              AND act.site=m.site
              AND act.observation_id=m.observation_id
              AND COALESCE(act.remote_uuid,'')=COALESCE(m.remote_uuid,'')
              AND act.action_type=CASE m.site
                    WHEN 'inat' THEN 'inat_observation_delete'
                    ELSE 'mo_observation_delete' END
              AND act.write_started_at IS NOT NULL
              AND act.verification_state='verified_deleted'
        ) LIMIT 1"""
    ).fetchone()
    if invalid_tombstone is not None:
        raise RuntimeError(
            "v17 migration found a remote tombstone without exact deletion "
            "provenance; manual ledger review is required"
        )
    conn.execute(
        "CREATE UNIQUE INDEX uq_unresolved_deletion_consolidation "
        "ON sync_deletion_attempts(profile_id,consolidation_id) "
        "WHERE state IN ('pending','partial','outcome_unknown')"
    )

    conn.execute(
        """CREATE TRIGGER trg_deletion_attempt_blocks_phase2b_activity
        BEFORE INSERT ON sync_deletion_attempts
        WHEN EXISTS (
            SELECT 1 FROM sync_consolidation_attempts a
            WHERE a.profile_id=NEW.profile_id
              AND a.consolidation_id=NEW.consolidation_id
              AND (
                  a.state IN ('pending','outcome_unknown')
                  OR EXISTS (
                      SELECT 1 FROM sync_actions sa
                      WHERE sa.profile_id=a.profile_id
                        AND sa.action_group_id=a.action_group_id
                        AND (
                            sa.state IN ('pending','running','outcome_unknown')
                            OR sa.outcome_unknown=1
                        )
                  )
              )
        )
        BEGIN
            SELECT RAISE(ABORT,
                'Phase 2B activity blocks deletion journaling');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_phase2b_attempt_blocks_deletion_activity
        BEFORE INSERT ON sync_consolidation_attempts
        WHEN EXISTS (
            SELECT 1 FROM sync_deletion_attempts da
            WHERE da.profile_id=NEW.profile_id
              AND da.consolidation_id=NEW.consolidation_id
              AND da.state IN ('pending','partial','outcome_unknown')
        )
        BEGIN
            SELECT RAISE(ABORT,
                'Phase 2C deletion activity blocks Phase 2B journaling');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_deletion_blocks_baseline_advance
        BEFORE UPDATE OF current_finalized_attempt_id
        ON sync_consolidations
        WHEN NEW.current_finalized_attempt_id IS NOT OLD.current_finalized_attempt_id
         AND EXISTS (
            SELECT 1 FROM sync_deletion_attempts da
            WHERE da.profile_id=OLD.profile_id
              AND da.consolidation_id=OLD.consolidation_id
              AND da.state IN ('pending','partial','outcome_unknown')
        )
        BEGIN
            SELECT RAISE(ABORT,
                'Phase 2C deletion activity blocks canonical baseline changes');
        END"""
    )

    conn.execute(
        """CREATE TRIGGER trg_deletion_tombstone_exact_provenance
        BEFORE UPDATE OF remote_state,deleted_by_deletion_attempt_id,
            deleted_by_deletion_item_id
        ON sync_consolidation_members
        WHEN NEW.remote_state='deleted'
        BEGIN
            SELECT CASE WHEN
                NEW.role!='donor'
                OR NEW.local_state!='superseded'
                OR NEW.deleted_by_deletion_attempt_id IS NULL
                OR NEW.deleted_by_deletion_item_id IS NULL
                OR NOT EXISTS (
                    SELECT 1
                    FROM sync_deletion_items di
                    JOIN sync_deletion_attempts da
                      ON da.deletion_attempt_id=di.deletion_attempt_id
                    JOIN sync_deletion_actions act
                      ON act.deletion_action_id=di.action_id
                    WHERE di.deletion_item_id=
                              NEW.deleted_by_deletion_item_id
                      AND di.deletion_attempt_id=
                              NEW.deleted_by_deletion_attempt_id
                      AND di.stable_member_id=NEW.consolidation_member_id
                      AND da.profile_id=NEW.profile_id
                      AND da.consolidation_id=NEW.consolidation_id
                      AND di.site=NEW.site
                      AND di.observation_id=NEW.observation_id
                      AND COALESCE(di.remote_uuid,'')=
                          COALESCE(NEW.remote_uuid,'')
                      AND act.profile_id=NEW.profile_id
                      AND act.deletion_attempt_id=di.deletion_attempt_id
                      AND act.deletion_item_id=di.deletion_item_id
                      AND act.site=di.site
                      AND act.observation_id=di.observation_id
                      AND COALESCE(act.remote_uuid,'')=
                          COALESCE(di.remote_uuid,'')
                      AND act.action_type=CASE di.site
                          WHEN 'inat' THEN 'inat_observation_delete'
                          ELSE 'mo_observation_delete' END
                      AND act.write_started_at IS NOT NULL
                      AND act.verification_state='verified_deleted'
                )
            THEN RAISE(ABORT,
                'deleted member provenance does not match exact delete chain')
            END;
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_deletion_action_identity_immutable
        BEFORE UPDATE OF profile_id,deletion_attempt_id,deletion_item_id,
            ordinal,action_type,site,observation_id,remote_uuid,
            request_correlation,reviewed_identity_fingerprint,created_at
        ON sync_deletion_actions
        BEGIN
            SELECT RAISE(ABORT, 'deletion action identity is immutable');
        END"""
    )
    conn.execute(
        """CREATE TRIGGER trg_deletion_tombstone_provenance_immutable
        BEFORE UPDATE OF remote_state,deleted_remotely_at,
            deleted_by_deletion_attempt_id,deleted_by_deletion_item_id,
            last_deletion_reviewed_record_fingerprint,
            canonical_destination_mo_id,canonical_destination_inat_id
        ON sync_consolidation_members
        WHEN OLD.remote_state='deleted' AND (
            NEW.remote_state IS NOT OLD.remote_state
            OR NEW.deleted_remotely_at IS NOT OLD.deleted_remotely_at
            OR NEW.deleted_by_deletion_attempt_id
                IS NOT OLD.deleted_by_deletion_attempt_id
            OR NEW.deleted_by_deletion_item_id
                IS NOT OLD.deleted_by_deletion_item_id
            OR NEW.last_deletion_reviewed_record_fingerprint
                IS NOT OLD.last_deletion_reviewed_record_fingerprint
            OR NEW.canonical_destination_mo_id
                IS NOT OLD.canonical_destination_mo_id
            OR NEW.canonical_destination_inat_id
                IS NOT OLD.canonical_destination_inat_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'remote deletion tombstone is immutable');
        END"""
    )


def _migration_v18(conn: sqlite3.Connection) -> None:
    """Index both pair directions used by the unpaired dashboard query.

    The original unique pair index begins with ``mo_observation_id`` and is
    therefore no help when the correlated lookup starts from an iNaturalist
    record. Review state belongs in both covering indexes because provisional
    and confirmed pairs are the only rows that remove a record from Unpaired.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_pairs_mo_review "
        "ON sync_pairs(profile_id,mo_observation_id,review_state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sync_pairs_inat_review "
        "ON sync_pairs(profile_id,inat_observation_id,review_state)"
    )
