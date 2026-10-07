"""Dedicated versioned SQLite state for DNA observation linking."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence

from PySide6.QtCore import QStandardPaths

from .types import CandidatePair

SCHEMA_VERSION = 1
TERMINAL_REVIEWS = frozenset(
    {"not_same", "already_linked", "kept_existing", "created", "replaced"}
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DNALinkingDB:
    """Thread-confined connections and atomic per-source discovery commits."""

    def __init__(self, path: Optional[Path] = None) -> None:
        if path is None:
            root = Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppDataLocation
                )
            )
            path = root / "dna_linking.db"
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
                raise RuntimeError(
                    f"DNA linking database version {version} is newer than supported"
                )
            if version == 0:
                with self.transaction() as tx:
                    version = int(tx.execute("PRAGMA user_version").fetchone()[0])
                    if version > SCHEMA_VERSION:
                        raise RuntimeError(
                            f"DNA linking database version {version} is newer than supported"
                        )
                    if version == 0:
                        _migration_v1(tx)
                        tx.execute("PRAGMA user_version=1")
            # Prepared work is safe to cancel after restart. Once the request
            # boundary was crossed, it is never safe to infer that a persisted
            # submitting/submitted row was not applied.
            with self.transaction() as tx:
                rows = tx.execute(
                    "SELECT operation_id,state FROM write_operations "
                    "WHERE state IN ('prepared','submitting','submitted_unverified')"
                ).fetchall()
                for row in rows:
                    operation_id = int(row[0])
                    recovered_state = "cancelled" if row[1] == "prepared" else "uncertain"
                    tx.execute(
                        "UPDATE write_operations SET state=?,updated_at=? WHERE operation_id=?",
                        (recovered_state, utc_now(), operation_id),
                    )
                    tx.execute(
                        "INSERT INTO write_events(operation_id,state,detail,created_at) VALUES(?,?,?,?)",
                        (operation_id, recovered_state, "Recovered after process restart", utc_now()),
                    )

    def get_or_create_session(
        self,
        *,
        user_id: int,
        login: str,
        fingerprint: str,
        source_query: str,
        radius_m: float,
        window_seconds: int,
        field_id: int,
        algorithm_version: str,
    ) -> sqlite3.Row:
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM scan_sessions WHERE user_id=? AND fingerprint=?",
                (int(user_id), fingerprint),
            ).fetchone()
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO scan_sessions(user_id,login,fingerprint,source_query,radius_m,"
                    "window_seconds,field_id,algorithm_version,source_cursor,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
                    (
                        int(user_id), login, fingerprint, source_query, float(radius_m),
                        int(window_seconds), int(field_id), algorithm_version, now, now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM scan_sessions WHERE session_id=?",
                    (int(cursor.lastrowid),),
                ).fetchone()
            else:
                conn.execute(
                    "UPDATE scan_sessions SET login=?,updated_at=? WHERE session_id=?",
                    (login, now, int(row["session_id"])),
                )
        assert row is not None
        return row

    def session(self, session_id: int) -> sqlite3.Row:
        row = self.connection().execute(
            "SELECT * FROM scan_sessions WHERE session_id=?", (int(session_id),)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown DNA linking session {session_id}")
        return row

    def commit_source(
        self, session_id: int, source_id: int, pairs: Sequence[CandidatePair]
    ) -> None:
        """Insert a complete source result and advance its cursor together."""
        now = utc_now()
        with self.transaction() as conn:
            for pair in pairs:
                conn.execute(
                    "INSERT OR IGNORE INTO candidates("
                    "session_id,source_id,candidate_id,source_json,candidate_json,distance_m,"
                    "time_difference_seconds,distance_score,time_score,family_score,score,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        int(session_id), pair.source.observation_id,
                        pair.candidate.observation_id,
                        _snapshot_json(pair.source), _snapshot_json(pair.candidate),
                        pair.distance_m, pair.time_difference_seconds,
                        pair.distance_score, pair.time_score, pair.family_score,
                        pair.score, now,
                    ),
                )
            conn.execute(
                "UPDATE scan_sessions SET source_cursor=MAX(source_cursor,?),updated_at=? "
                "WHERE session_id=?",
                (int(source_id), now, int(session_id)),
            )

    def queued_candidates(self, session_id: int) -> list[sqlite3.Row]:
        rows = self.connection().execute(
            """
            SELECT c.*, (
              SELECT event_type FROM review_events r
              WHERE r.candidate_pk=c.candidate_pk
              ORDER BY r.event_id DESC LIMIT 1
            ) AS latest_review
            FROM candidates c
            WHERE c.session_id=?
            ORDER BY c.score DESC,c.distance_m ASC,c.time_difference_seconds ASC,
                     c.source_id ASC,c.candidate_id ASC
            """,
            (int(session_id),),
        ).fetchall()
        return [
            row for row in rows
            if str(row["latest_review"] or "") not in TERMINAL_REVIEWS
            or str(row["latest_review"] or "") == "reopened"
        ]

    def history(self, session_id: int) -> list[sqlite3.Row]:
        return self.connection().execute(
            """
            SELECT c.candidate_pk,c.source_id,c.candidate_id,c.score,r.event_id,
                   r.revision,r.event_type,r.detail,r.created_at
            FROM candidates c JOIN review_events r ON r.candidate_pk=c.candidate_pk
            WHERE c.session_id=? ORDER BY r.event_id DESC
            """,
            (int(session_id),),
        ).fetchall()

    def append_review(self, candidate_pk: int, event_type: str, detail: str = "") -> int:
        with self.transaction() as conn:
            revision = int(conn.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM review_events WHERE candidate_pk=?",
                (int(candidate_pk),),
            ).fetchone()[0])
            cur = conn.execute(
                "INSERT INTO review_events(candidate_pk,revision,event_type,detail,created_at) "
                "VALUES(?,?,?,?,?)",
                (int(candidate_pk), revision, event_type, detail, utc_now()),
            )
            return int(cur.lastrowid)

    def reopen(self, candidate_pk: int) -> int:
        return self.append_review(candidate_pk, "reopened")

    def restart_discovery(self, session_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE scan_sessions SET source_cursor=0,updated_at=? WHERE session_id=?",
                (utc_now(), int(session_id)),
            )
            conn.execute(
                "INSERT INTO session_events(session_id,event_type,created_at) VALUES(?,?,?)",
                (int(session_id), "discovery_restarted", utc_now()),
            )

    def unresolved_for_destination(self, destination_id: int) -> Optional[sqlite3.Row]:
        return self.connection().execute(
            "SELECT * FROM write_operations WHERE destination_id=? "
            "AND state IN ('prepared','submitting','submitted_unverified','uncertain') "
            "ORDER BY operation_id DESC LIMIT 1",
            (int(destination_id),),
        ).fetchone()

    def prepare_write(
        self, *, candidate_pk: int, user_id: int, login: str,
        auth_generation: int, destination_id: int, destination_uuid: str,
        operation_type: str, expected_value: str, before_fingerprint: str,
    ) -> int:
        if self.unresolved_for_destination(destination_id) is not None:
            raise RuntimeError(
                "An active or unresolved DNA-link write already exists for this destination. "
                "Wait for it or verify it before attempting another write."
            )
        now = utc_now()
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO write_operations(candidate_pk,user_id,login,auth_generation,"
                "destination_id,destination_uuid,operation_type,expected_value,"
                "before_fingerprint,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?)",
                (
                    int(candidate_pk), int(user_id), login, int(auth_generation),
                    int(destination_id), destination_uuid, operation_type,
                    expected_value, before_fingerprint, now, now,
                ),
            )
            operation_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO write_events(operation_id,state,detail,created_at) VALUES(?,?,?,?)",
                (operation_id, "prepared", "", now),
            )
        return operation_id

    def transition_write(self, operation_id: int, state: str, detail: str = "") -> None:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE write_operations SET state=?,updated_at=? WHERE operation_id=?",
                (state, now, int(operation_id)),
            )
            conn.execute(
                "INSERT INTO write_events(operation_id,state,detail,created_at) VALUES(?,?,?,?)",
                (int(operation_id), state, detail, now),
            )

    def operation(self, operation_id: int) -> sqlite3.Row:
        row = self.connection().execute(
            "SELECT * FROM write_operations WHERE operation_id=?", (int(operation_id),)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown DNA linking write {operation_id}")
        return row

    def uncertain_operations(self) -> list[sqlite3.Row]:
        return self.connection().execute(
            "SELECT w.*,s.field_id FROM write_operations w "
            "JOIN candidates c ON c.candidate_pk=w.candidate_pk "
            "JOIN scan_sessions s ON s.session_id=c.session_id "
            "WHERE w.state='uncertain' ORDER BY w.operation_id"
        ).fetchall()


def _snapshot_json(snapshot: object) -> str:
    from dataclasses import asdict
    return json.dumps(asdict(snapshot), separators=(",", ":"), sort_keys=True)


def _migration_v1(conn: sqlite3.Connection) -> None:
    schema = """
        CREATE TABLE scan_sessions(
          session_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, login TEXT NOT NULL,
          fingerprint TEXT NOT NULL, source_query TEXT NOT NULL, radius_m REAL NOT NULL,
          window_seconds INTEGER NOT NULL, field_id INTEGER NOT NULL,
          algorithm_version TEXT NOT NULL, source_cursor INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(user_id,fingerprint)
        );
        CREATE TABLE candidates(
          candidate_pk INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES scan_sessions(session_id),
          source_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
          source_json TEXT NOT NULL, candidate_json TEXT NOT NULL,
          distance_m REAL NOT NULL, time_difference_seconds REAL NOT NULL,
          distance_score REAL NOT NULL, time_score REAL NOT NULL,
          family_score REAL NOT NULL, score INTEGER NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(session_id,source_id,candidate_id), CHECK(source_id<>candidate_id)
        );
        CREATE TABLE review_events(
          event_id INTEGER PRIMARY KEY, candidate_pk INTEGER NOT NULL REFERENCES candidates(candidate_pk),
          revision INTEGER NOT NULL, event_type TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, UNIQUE(candidate_pk,revision)
        );
        CREATE TABLE session_events(
          event_id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES scan_sessions(session_id),
          event_type TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE write_operations(
          operation_id INTEGER PRIMARY KEY, candidate_pk INTEGER NOT NULL REFERENCES candidates(candidate_pk),
          user_id INTEGER NOT NULL, login TEXT NOT NULL, auth_generation INTEGER NOT NULL,
          destination_id INTEGER NOT NULL, destination_uuid TEXT NOT NULL,
          operation_type TEXT NOT NULL, expected_value TEXT NOT NULL,
          before_fingerprint TEXT NOT NULL, state TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX one_active_dna_write_per_destination
          ON write_operations(destination_id)
          WHERE state IN ('prepared','submitting','submitted_unverified','uncertain');
        CREATE TABLE write_events(
          event_id INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL REFERENCES write_operations(operation_id),
          state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE INDEX candidates_queue_order ON candidates(
          session_id,score DESC,distance_m,time_difference_seconds,source_id,candidate_id
        );
        """
    for statement in schema.split(";"):
        if statement.strip():
            conn.execute(statement)
