"""
SQLite metadata cache for Observation Workbench.

Tables:
  - query_cache: keyed by username filters or observation URL query + page
  - image_access_log: for LRU eviction of disk images

Performance: WAL journal mode + synchronous=NORMAL to avoid UI stutters.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
try:
    import sqlite3
    sqlite3.connect(":memory:").close()  # verify it actually works
except Exception:
    # Fallback for conda environments with broken _sqlite3.so
    import pysqlite3 as sqlite3  # type: ignore[no-redef]
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS query_cache (
    cache_key   TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    total       INTEGER NOT NULL DEFAULT 0,
    cached_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS image_access_log (
    cache_key   TEXT PRIMARY KEY,
    file_path   TEXT NOT NULL,
    file_size   INTEGER NOT NULL DEFAULT 0,
    last_access REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS taxon_summary_cache (
    cache_key   TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    cached_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bulk_agree_skip (
    obs_id      INTEGER PRIMARY KEY,
    skipped_at  REAL NOT NULL,
    reason      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bulk_disagree_skip (
    obs_id      INTEGER PRIMARY KEY,
    skipped_at  REAL NOT NULL,
    reason      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_image_access ON image_access_log(last_access);
"""

# How long query cache entries are considered fresh (seconds)
QUERY_CACHE_TTL = 60 * 60  # 1 hour

IDENTIFY_ACTION_TYPES = frozenset(
    {"identification", "comment", "reviewed", "favorite", "quality_metric"}
)
# This gate supports only the "wild" Data Quality Assessment metric (the
# Captive/Cultivated vote). Every quality_metric payload carries exactly one
# of these three explicit operations; there is no boolean coercion here.
QUALITY_METRICS = frozenset({"wild"})
QUALITY_METRIC_VOTES = frozenset({"agree", "disagree", "remove"})
IDENTIFY_ACTION_STATES = frozenset(
    {
        "queued",
        "submitting",
        "confirmed",
        "submitted_unverified",
        "failed_retryable",
        "failed_terminal",
        "ambiguous",
        "cancelled",
        "tracking_cancelled",
        # Kept only so databases produced by the first Gate 2 backend can be
        # migrated safely at startup. New code never creates this state.
        "manual_retry_queued",
    }
)
IDENTIFY_UNRESOLVED_STATES = (
    "queued",
    "submitting",
    "submitted_unverified",
    "failed_retryable",
    "ambiguous",
)
_IDENTIFY_MUTABLE_COLUMNS = frozenset(
    {
        "attempt_count",
        "attempt_started_at",
        "last_error_json",
        "server_object_id",
        "confirmed_at",
        "verification_attempt_count",
        "last_verification_at",
        "last_operation_phase",
        "manual_retry_count",
        "outcome_unknown",
        "parent_action_id",
        "write_response_id",
        "write_response_uuid",
        "verification_status",
        "verification_diagnostic",
    }
)
# Every column of identify_actions, in declaration order.  The rebuild
# migration below copies exactly these, so a new column must be added here and
# to the table SQL together.
_IDENTIFY_COLUMNS = (
    "local_action_id",
    "account_login",
    "observation_id",
    "observation_uuid",
    "action_type",
    "payload_json",
    "desired_state",
    "deduplication_key",
    "state",
    "created_at",
    "updated_at",
    "attempt_count",
    "attempt_started_at",
    "last_error_json",
    "server_object_id",
    "confirmed_at",
    "verification_attempt_count",
    "last_verification_at",
    "last_operation_phase",
    "manual_retry_count",
    "outcome_unknown",
    "parent_action_id",
    "write_response_id",
    "write_response_uuid",
    "verification_status",
    "verification_diagnostic",
)


def _sql_string_set(values: Iterable[str]) -> str:
    """Render a closed Python set as a sorted SQL ``IN`` list."""
    return ",".join("'" + value.replace("'", "''") + "'" for value in sorted(values))


def _identify_actions_table_sql(table_name: str) -> str:
    """Return the identify_actions DDL, targeting `table_name`.

    The CHECK constraints are generated from the same frozensets the Python
    validators use, so the database and ``_validate_identify_state`` cannot
    drift apart.  They matter because several journal writes are raw INSERTs
    (``create_manual_retry``, the legacy manual-retry migration) that bypass
    ``transition_identify_action`` and its validation entirely.
    """
    return f"""
CREATE TABLE IF NOT EXISTS {table_name} (
    local_action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_login TEXT NOT NULL,
    observation_id INTEGER NOT NULL,
    observation_uuid TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ({_sql_string_set(IDENTIFY_ACTION_TYPES)})),
    payload_json TEXT NOT NULL,
    desired_state INTEGER CHECK(desired_state IS NULL OR desired_state IN (0, 1)),
    deduplication_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ({_sql_string_set(IDENTIFY_ACTION_STATES)})),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    attempt_started_at REAL,
    last_error_json TEXT NOT NULL DEFAULT '',
    server_object_id TEXT NOT NULL DEFAULT '',
    confirmed_at REAL,
    verification_attempt_count INTEGER NOT NULL DEFAULT 0,
    last_verification_at REAL,
    last_operation_phase TEXT NOT NULL DEFAULT '',
    manual_retry_count INTEGER NOT NULL DEFAULT 0,
    outcome_unknown INTEGER NOT NULL DEFAULT 0 CHECK(outcome_unknown IN (0, 1)),
    parent_action_id INTEGER,
    write_response_id TEXT NOT NULL DEFAULT '',
    write_response_uuid TEXT NOT NULL DEFAULT '',
    verification_status TEXT NOT NULL DEFAULT '',
    verification_diagnostic TEXT NOT NULL DEFAULT ''
);
"""


# Kept as separate statements, not one script: the rebuild below recreates
# these inside an explicit transaction, and `executescript` would commit it.
_IDENTIFY_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_identify_actions_state"
    " ON identify_actions(state, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_identify_actions_dedup"
    " ON identify_actions(deduplication_key, state)",
    "CREATE INDEX IF NOT EXISTS idx_identify_actions_observation"
    " ON identify_actions(observation_id, created_at)",
)

IDENTIFY_SCHEMA = _identify_actions_table_sql("identify_actions") + "".join(
    f"{statement};\n" for statement in _IDENTIFY_INDEX_STATEMENTS
)

_IDENTIFY_COLUMN_MIGRATIONS = {
    "verification_attempt_count": "INTEGER NOT NULL DEFAULT 0",
    "last_verification_at": "REAL",
    "last_operation_phase": "TEXT NOT NULL DEFAULT ''",
    "manual_retry_count": "INTEGER NOT NULL DEFAULT 0",
    "outcome_unknown": "INTEGER NOT NULL DEFAULT 0",
    "parent_action_id": "INTEGER",
    "write_response_id": "TEXT NOT NULL DEFAULT ''",
    "write_response_uuid": "TEXT NOT NULL DEFAULT ''",
    "verification_status": "TEXT NOT NULL DEFAULT ''",
    "verification_diagnostic": "TEXT NOT NULL DEFAULT ''",
}

# SQLite's user_version records completion of the expensive, one-shot Identify
# migration.  Bump this when adding another versioned cache database migration.
_IDENTIFY_DEDUP_MIGRATION_VERSION = 1


@dataclass(frozen=True)
class IdentifyEnqueueResult:
    """Result of one atomic identify-journal enqueue/coalescing operation."""

    inserted_action_id: Optional[int] = None
    duplicate_action_id: Optional[int] = None
    cancelled_action_ids: tuple[int, ...] = ()

    @property
    def action_id(self) -> int:
        action_id = (
            self.inserted_action_id
            if self.inserted_action_id is not None
            else self.duplicate_action_id
        )
        if action_id is None:
            raise RuntimeError("Identify enqueue completed without an action ID")
        return action_id


@dataclass(frozen=True)
class IdentifyConfirmationResult:
    """Atomic result of confirming an action and retiring queued retries."""

    confirmed: bool = False
    cancelled_retry_action_ids: tuple[int, ...] = ()


def _coerce_desired_state(value: Any) -> Optional[bool]:
    """Normalize a reviewed/favorite desired_state across its two valid forms.

    An in-memory action dict carries a Python ``bool``; a row read back from
    SQLite carries the ``INTEGER`` it was coerced to on write (0/1). Both are
    valid; anything else is not.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _parse_quality_metric_payload(raw: Any) -> Optional[Tuple[str, str]]:
    """Strictly parse one stored quality_metric payload to (metric, vote).

    Returns ``None`` for anything that is not exactly a well-formed
    quality_metric payload. A row that fails this parse is neither
    "equivalent" nor "conflicting" to any candidate operation: coalescing
    must leave it completely untouched rather than guessing its intent.
    """
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"metric", "vote"}:
        return None
    metric = payload.get("metric")
    vote = payload.get("vote")
    if metric not in QUALITY_METRICS or vote not in QUALITY_METRIC_VOTES:
        return None
    return metric, vote


def make_identify_deduplication_key(
    account_login: str,
    observation_id: int,
    action_type: str,
    payload: Dict[str, Any],
    desired_state: Optional[bool],
) -> str:
    """Return a fixed-size digest of one canonical Identify intent."""
    canonical = json.dumps(
        [
            str(account_login).strip().casefold(),
            int(observation_id),
            str(action_type),
            payload,
            desired_state,
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _validate_identify_action(action: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize every writable journal field before insertion."""
    if not isinstance(action, dict):
        raise ValueError("Identify action must be a dictionary")
    account_login = str(action.get("account_login") or "").strip()
    if not account_login:
        raise ValueError("Identify actions require an account login")
    try:
        observation_id = int(action.get("observation_id"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Identify actions require a numeric observation ID") from exc
    if observation_id <= 0:
        raise ValueError("Identify actions require a positive observation ID")
    observation_uuid = _normalise_observation_uuid(action.get("observation_uuid"))
    action_type = str(action.get("action_type") or "")
    if action_type not in IDENTIFY_ACTION_TYPES:
        raise ValueError(f"Unsupported Identify action type: {action_type}")
    payload = action.get("payload", {})
    _validate_identify_payload(action_type, payload)
    desired_state = action.get("desired_state")
    if action_type in {"reviewed", "favorite"}:
        coerced_desired_state = _coerce_desired_state(desired_state)
        if coerced_desired_state is None:
            raise ValueError(f"{action_type} actions require a boolean desired state")
        desired_state = coerced_desired_state
    elif desired_state is not None:
        raise ValueError(f"{action_type} actions cannot have a desired state")
    # quality_metric's tri-state operation (agree/disagree/remove) lives
    # entirely in the payload; desired_state must stay None so it is never
    # confused with the reviewed/favorite boolean semantics above.
    # Derive this from validated intent instead of accepting a serialized body
    # from the caller.  Identification and comment bodies can legitimately be
    # long (and non-ASCII); the indexed key must remain fixed-size regardless.
    deduplication_key = make_identify_deduplication_key(
        account_login,
        observation_id,
        action_type,
        payload,
        desired_state,
    )
    return {
        "account_login": account_login,
        "observation_id": observation_id,
        "observation_uuid": observation_uuid,
        "action_type": action_type,
        "payload_json": json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "desired_state": desired_state,
        "deduplication_key": deduplication_key,
    }


def _normalise_observation_uuid(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Identify actions require a non-empty observation UUID")
    try:
        return str(UUID(text))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("Identify actions require a valid observation UUID") from exc


def _validate_identify_payload(action_type: str, payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("Identify action payload must be a JSON object")
    if action_type == "identification":
        if set(payload) - {"taxon_id", "body", "disagreement"} or "taxon_id" not in payload:
            raise ValueError(
                "Identification payload must contain taxon_id and optional body/disagreement"
            )
        try:
            if int(payload["taxon_id"]) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("Identification taxon_id must be a positive integer") from exc
        if "body" in payload and not isinstance(payload["body"], str):
            raise ValueError("Identification body must be text")
        if "disagreement" in payload and not isinstance(payload["disagreement"], bool):
            raise ValueError("Identification disagreement must be a boolean")
        return
    if action_type == "comment":
        if set(payload) != {"body"} or not isinstance(payload.get("body"), str):
            raise ValueError("Comment payload must contain only a text body")
        if not payload["body"].strip():
            raise ValueError("Comment body cannot be empty")
        return
    if action_type == "quality_metric":
        if set(payload) != {"metric", "vote"}:
            raise ValueError("Quality metric payload must contain exactly metric and vote")
        if payload.get("metric") not in QUALITY_METRICS:
            raise ValueError("Quality metric payload metric must be 'wild' in this gate")
        if payload.get("vote") not in QUALITY_METRIC_VOTES:
            raise ValueError("Quality metric payload vote must be agree, disagree, or remove")
        return
    if payload:
        raise ValueError(f"{action_type} actions do not accept a payload")


def _validate_identify_state(state: str) -> None:
    if state not in IDENTIFY_ACTION_STATES:
        raise ValueError(f"Unsupported Identify journal state: {state}")


def _validate_identify_states(states: Iterable[str]) -> None:
    if isinstance(states, str):
        raise ValueError("Identify journal states must be an iterable of state names")
    values = tuple(states)
    if not values:
        raise ValueError("At least one expected Identify journal state is required")
    for state in values:
        _validate_identify_state(state)


def _validate_identify_transition_values(values: Dict[str, Any]) -> None:
    unexpected = set(values) - _IDENTIFY_MUTABLE_COLUMNS
    if unexpected:
        raise ValueError(f"Unsupported Identify journal update columns: {sorted(unexpected)}")
    for key, value in values.items():
        if key in {"attempt_count", "verification_attempt_count", "manual_retry_count"}:
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} must be a non-negative integer")
        elif key == "outcome_unknown":
            if value not in {0, 1, False, True}:
                raise ValueError("outcome_unknown must be boolean")
        elif key == "last_operation_phase":
            if value not in {"", "account_preflight", "unsafe_write", "verification_read"}:
                raise ValueError("Unsupported Identify operation phase")
        elif key in {"last_error_json", "verification_diagnostic"}:
            limit = 2000 if key == "last_error_json" else 800
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f"{key} must be bounded text")
            if value:
                try:
                    json.loads(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be JSON") from exc
        elif key in {"server_object_id", "write_response_id", "write_response_uuid"}:
            if not isinstance(value, str) or len(value) > 255:
                raise ValueError(f"{key} must be bounded text")
        elif key == "verification_status":
            if value not in {
                "",
                "confirmed",
                "not_found",
                "mismatched",
                "insufficient_fields",
                "read_failed",
            }:
                raise ValueError("Unsupported verification status")
        elif key in {"attempt_started_at", "confirmed_at", "last_verification_at"}:
            if value is not None and not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be a timestamp")
        elif key == "parent_action_id":
            if value is not None and (not isinstance(value, int) or value <= 0):
                raise ValueError("parent_action_id must be a positive integer or None")


def _bounded_diagnostic_json(value: Dict[str, Any]) -> str:
    """Serialize a short non-payload diagnostic for a durable journal row."""
    bounded: Dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)[:64]
        item = str(raw_value)[:160]
        candidate = {**bounded, key: item}
        if len(json.dumps(candidate, sort_keys=True, separators=(",", ":"))) > 2000:
            break
        bounded = candidate
    return json.dumps(bounded, sort_keys=True, separators=(",", ":"))


class CacheDB:
    """Thread-safe SQLite metadata cache."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()  # per-thread connection

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            # Autocommit mode: every statement runs and commits immediately
            # unless it is inside an explicit BEGIN...COMMIT/ROLLBACK. This
            # removes the "cannot start a transaction within a transaction"
            # foot-gun of mixing implicit (`with conn:`) and explicit
            # (`BEGIN IMMEDIATE`) transaction styles on the same connection.
            conn = sqlite3.connect(
                str(self._db_path), check_same_thread=False, isolation_level=None
            )
            # Two threads can race to open their first connection to the same
            # file and both attempt the migration's ALTER TABLE; without a
            # busy timeout the loser gets an immediate "database is locked"
            # instead of waiting the writer out.
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            conn.executescript(IDENTIFY_SCHEMA)
            self._migrate_identify_actions(conn)
            conn.commit()
            self._local.conn = conn
        return self._local.conn

    @staticmethod
    def _migrate_identify_actions(conn: sqlite3.Connection) -> None:
        """Add durable-journal columns to databases created by earlier gates.

        Two threads can each open their first connection concurrently and
        both read the pre-migration ``PRAGMA table_info``; the loser's
        ``ALTER TABLE ... ADD COLUMN`` then raises "duplicate column name".
        That is tolerated here so migration stays idempotent under races.
        """
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(identify_actions)").fetchall()
        }
        for name, definition in _IDENTIFY_COLUMN_MIGRATIONS.items():
            if name in columns:
                continue
            try:
                conn.execute(f"ALTER TABLE identify_actions ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
        CacheDB._add_identify_check_constraints(conn)
        for statement in _IDENTIFY_INDEX_STATEMENTS:
            conn.execute(statement)
        CacheDB._migrate_identify_deduplication_keys(conn)

    @staticmethod
    def _migrate_identify_deduplication_keys(conn: sqlite3.Connection) -> None:
        """Normalize legacy deduplication keys exactly once per database.

        The write transaction serializes concurrent first connections.  A
        waiting connection re-reads user_version after acquiring the lock and
        skips both the table scan and rewrite once another thread completed it.
        """
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= _IDENTIFY_DEDUP_MIGRATION_VERSION:
            return

        conn.execute("BEGIN IMMEDIATE")
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version >= _IDENTIFY_DEDUP_MIGRATION_VERSION:
                conn.commit()
                return

            # Early Identify builds stored the entire canonical JSON intent in
            # the indexed key. Normalize those rows in place so unresolved
            # legacy actions still deduplicate against newly queued digest-key
            # actions.
            rows = conn.execute(
                "SELECT local_action_id, account_login, observation_id, action_type, "
                "payload_json, desired_state, deduplication_key FROM identify_actions "
                "WHERE deduplication_key NOT LIKE 'sha256:%'"
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                    if not isinstance(payload, dict):
                        continue
                    desired_state = (
                        _coerce_desired_state(row["desired_state"])
                        if str(row["action_type"]) in {"reviewed", "favorite"}
                        else None
                    )
                    digest = make_identify_deduplication_key(
                        str(row["account_login"]),
                        int(row["observation_id"]),
                        str(row["action_type"]),
                        payload,
                        desired_state,
                    )
                except (TypeError, ValueError):
                    continue
                conn.execute(
                    "UPDATE identify_actions SET deduplication_key=? "
                    "WHERE local_action_id=?",
                    (digest, int(row["local_action_id"])),
                )
            conn.execute(f"PRAGMA user_version = {_IDENTIFY_DEDUP_MIGRATION_VERSION}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _add_identify_check_constraints(conn: sqlite3.Connection) -> None:
        """Rebuild identify_actions if it predates its CHECK constraints.

        SQLite cannot add a CHECK with ALTER TABLE, so the table is recreated
        and copied.  This runs after the ADD COLUMN loop above, so every column
        in ``_IDENTIFY_COLUMNS`` exists on the source table by now.

        A row whose ``state`` or ``action_type`` is outside the closed set will
        fail the copy and abort startup rather than being silently dropped: a
        journal row that cannot be represented is a bug to surface, not data to
        discard.
        """
        if not CacheDB._identify_table_needs_constraints(conn):
            return
        columns = ", ".join(_IDENTIFY_COLUMNS)
        # Two threads can each open their first connection and both see the
        # unconstrained table.  Re-check inside the write transaction so the
        # loser (released by busy_timeout) becomes a no-op instead of racing
        # the DROP/RENAME.
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not CacheDB._identify_table_needs_constraints(conn):
                conn.commit()
                return
            conn.execute("DROP TABLE IF EXISTS identify_actions_rebuild")
            conn.execute(_identify_actions_table_sql("identify_actions_rebuild"))
            conn.execute(
                f"INSERT INTO identify_actions_rebuild({columns}) "
                f"SELECT {columns} FROM identify_actions"
            )
            conn.execute("DROP TABLE identify_actions")
            conn.execute("ALTER TABLE identify_actions_rebuild RENAME TO identify_actions")
            # Dropping the table dropped its indexes with it.
            for statement in _IDENTIFY_INDEX_STATEMENTS:
                conn.execute(statement)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        log.info("Rebuilt identify_actions with state and action_type CHECK constraints")

    @staticmethod
    def _identify_table_needs_constraints(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='identify_actions'"
        ).fetchone()
        if row is None:
            return False
        # sqlite_master stores the DDL verbatim, and this table's DDL is always
        # produced by _identify_actions_table_sql, so an exact match is safe.
        return "CHECK(state IN (" not in str(row["sql"] or "")

    # ------------------------------------------------------------------
    # Query cache
    # ------------------------------------------------------------------

    # A page number identifies a different window of rows for each page size —
    # the API offset is (page - 1) * per_page — so per_page is part of the key.
    # Omitting it let a page cached at one size be served for a request at
    # another, returning rows from the wrong offset.

    @staticmethod
    def make_query_key(
        username: str,
        place_id: Optional[int],
        taxon_id: Optional[int],
        leading: Optional[bool],
        d1: Optional[str],
        d2: Optional[str],
        page: int,
        per_page: int,
    ) -> str:
        return json.dumps(
            [username, place_id, taxon_id, leading, d1, d2, page, per_page],
            sort_keys=True,
        )

    @staticmethod
    def make_observation_query_key(source_key: str, page: int, per_page: int) -> str:
        return json.dumps(["observations", source_key, page, per_page], sort_keys=True)

    def get_query_cache(self, cache_key: str) -> Optional[Tuple[List[Dict], int]]:
        """Return (results_list, total) or None if expired/missing."""
        conn = self._conn()
        row = conn.execute(
            "SELECT data, total, cached_at FROM query_cache WHERE cache_key=?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        age = time.time() - row["cached_at"]
        if age > QUERY_CACHE_TTL:
            conn.execute("DELETE FROM query_cache WHERE cache_key=?", (cache_key,))
            conn.commit()
            return None
        return json.loads(row["data"]), row["total"]

    def set_query_cache(self, cache_key: str, results: List[Dict], total: int) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO query_cache(cache_key, data, total, cached_at) VALUES(?,?,?,?)",
            (cache_key, json.dumps(results), total, time.time()),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Taxon summary cache
    # ------------------------------------------------------------------

    @staticmethod
    def make_summary_key(
        username: str,
        place_id: Optional[int],
        taxon_id: Optional[int],
        d1: Optional[str],
        d2: Optional[str],
    ) -> str:
        return json.dumps(["summary", username, place_id, taxon_id, d1, d2])

    @staticmethod
    def make_observation_summary_key(source_key: str) -> str:
        return json.dumps(["summary", "observations", source_key])

    def get_summary_cache(self, cache_key: str) -> Optional[Dict]:
        conn = self._conn()
        row = conn.execute(
            "SELECT data, cached_at FROM taxon_summary_cache WHERE cache_key=?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        age = time.time() - row["cached_at"]
        if age > QUERY_CACHE_TTL:
            conn.execute("DELETE FROM taxon_summary_cache WHERE cache_key=?", (cache_key,))
            conn.commit()
            return None
        return json.loads(row["data"])

    def set_summary_cache(self, cache_key: str, data: Dict) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO taxon_summary_cache(cache_key, data, cached_at) VALUES(?,?,?)",
            (cache_key, json.dumps(data), time.time()),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Image access log (for LRU eviction)
    # ------------------------------------------------------------------

    def log_image_access(self, cache_key: str, file_path: str, file_size: int) -> None:
        conn = self._conn()
        conn.execute(
            """INSERT INTO image_access_log(cache_key, file_path, file_size, last_access)
               VALUES(?,?,?,?)
               ON CONFLICT(cache_key) DO UPDATE SET
                 file_path=excluded.file_path,
                 last_access=excluded.last_access,
                 file_size=excluded.file_size""",
            (cache_key, file_path, file_size, time.time()),
        )
        conn.commit()

    def remove_image_log(self, cache_key: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM image_access_log WHERE cache_key=?", (cache_key,))
        conn.commit()

    def get_total_image_cache_size(self) -> int:
        """Return total bytes of tracked image files."""
        conn = self._conn()
        row = conn.execute("SELECT COALESCE(SUM(file_size),0) as total FROM image_access_log").fetchone()
        return int(row["total"])

    def get_lru_images(self, limit: int = 100) -> List[Dict]:
        """Return oldest-accessed image entries for eviction."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT cache_key, file_path, file_size FROM image_access_log ORDER BY last_access ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_all_image_logs(self) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM image_access_log")
        conn.commit()

    # ------------------------------------------------------------------
    # Durable Identify action journal
    # ------------------------------------------------------------------

    def add_identify_action(self, action: Dict[str, Any]) -> int:
        """Backward-compatible non-coalescing journal insert.

        New Identify callers should use one of the transactional enqueue
        methods below.  This retained method still validates the complete row
        shape and never permits an empty observation UUID.
        """
        prepared = _validate_identify_action(action)
        conn = self._conn()
        now = time.time()
        # A single statement is already atomic under autocommit; no explicit
        # transaction wrapper is needed here.
        cursor = self._insert_identify_action(conn, prepared, now)
        return int(cursor.lastrowid)

    def enqueue_identification_or_comment(
        self,
        action: Dict[str, Any],
    ) -> IdentifyEnqueueResult:
        """Atomically deduplicate a durable identification or comment action."""
        prepared = _validate_identify_action(action)
        if prepared["action_type"] not in {"identification", "comment"}:
            raise ValueError("This enqueue method accepts identification or comment actions only")

        conn = self._conn()
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ",".join("?" for _ in IDENTIFY_UNRESOLVED_STATES)
            row = conn.execute(
                "SELECT local_action_id FROM identify_actions "
                f"WHERE deduplication_key=? AND state IN ({placeholders}) "
                "ORDER BY CASE WHEN state='queued' THEN 0 ELSE 1 END, "
                "created_at, local_action_id LIMIT 1",
                (prepared["deduplication_key"], *IDENTIFY_UNRESOLVED_STATES),
            ).fetchone()
            if row is not None:
                conn.commit()
                return IdentifyEnqueueResult(duplicate_action_id=int(row["local_action_id"]))
            cursor = self._insert_identify_action(conn, prepared, now)
            conn.commit()
            return IdentifyEnqueueResult(inserted_action_id=int(cursor.lastrowid))
        except Exception:
            conn.rollback()
            raise

    def enqueue_desired_state_action(
        self,
        action: Dict[str, Any],
    ) -> IdentifyEnqueueResult:
        """Atomically coalesce unresolved Reviewed/Favorite intent for one account.

        A same-desired-state row in any unresolved state (not only ``queued``)
        blocks a new insert: an unsafe write that is ``submitting``,
        ``submitted_unverified``, ``failed_retryable``, or ``ambiguous`` may
        still reach or have reached the server, so a second equivalent action
        must not be journaled behind it.  Only an opposite-desired-state row
        that is still ``queued`` is safe to cancel automatically; opposite
        rows in any other unresolved state are left untouched so a later,
        explicitly requested action can still be inserted to establish the
        final state once that row resolves.  A row whose stored
        ``desired_state`` cannot be strictly coerced to ``True``/``False`` is
        neither "same" nor "opposite": it is left completely untouched for
        existing pending-action and error handling to surface instead.
        """
        prepared = _validate_identify_action(action)
        if prepared["action_type"] not in {"reviewed", "favorite"}:
            raise ValueError("This enqueue method accepts reviewed or favorite actions only")

        conn = self._conn()
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ",".join("?" for _ in IDENTIFY_UNRESOLVED_STATES)
            rows = conn.execute(
                "SELECT local_action_id, state, desired_state FROM identify_actions "
                f"WHERE state IN ({placeholders}) AND lower(account_login)=lower(?) "
                "AND observation_id=? AND action_type=? ORDER BY created_at, local_action_id",
                (
                    *IDENTIFY_UNRESOLVED_STATES,
                    prepared["account_login"],
                    prepared["observation_id"],
                    prepared["action_type"],
                ),
            ).fetchall()
            prepared_desired_state = bool(prepared["desired_state"])
            same: list[int] = []
            opposite_queued: list[int] = []
            for row in rows:
                coerced = _coerce_desired_state(row["desired_state"])
                if coerced is None:
                    # Malformed stored intent is neither equivalent nor
                    # opposite; leave it untouched rather than guessing.
                    continue
                if coerced == prepared_desired_state:
                    same.append(int(row["local_action_id"]))
                elif str(row["state"]) == "queued":
                    opposite_queued.append(int(row["local_action_id"]))
            cancelled: tuple[int, ...] = ()
            if opposite_queued:
                conn.execute(
                    "UPDATE identify_actions SET state='cancelled', updated_at=? "
                    "WHERE state='queued' AND local_action_id IN "
                    f"({','.join('?' for _ in opposite_queued)})",
                    (now, *opposite_queued),
                )
                cancelled = tuple(opposite_queued)
            if same:
                conn.commit()
                return IdentifyEnqueueResult(
                    duplicate_action_id=same[0],
                    cancelled_action_ids=cancelled,
                )
            cursor = self._insert_identify_action(conn, prepared, now)
            conn.commit()
            return IdentifyEnqueueResult(
                inserted_action_id=int(cursor.lastrowid),
                cancelled_action_ids=cancelled,
            )
        except Exception:
            conn.rollback()
            raise

    def enqueue_quality_metric_action(
        self,
        action: Dict[str, Any],
    ) -> IdentifyEnqueueResult:
        """Atomically coalesce unresolved Captive/Cultivated ("wild") intent.

        Exactly three explicit operations exist per metric: ``agree``,
        ``disagree``, ``remove``. A same-operation row in any unresolved
        state blocks a new insert, exactly as the reviewed/favorite
        coalescing above. Any *other* valid operation for the same metric is
        conflicting; only a conflicting row that is still ``queued`` is
        cancelled automatically. A conflicting row in ``submitting``,
        ``submitted_unverified``, ``failed_retryable``, or ``ambiguous``
        is left untouched and does not block the new insert: the later,
        explicitly requested operation may be exactly what is needed to
        establish the user's final intent once that earlier uncertain write
        resolves. A row whose stored payload cannot be strictly parsed as a
        valid quality_metric operation is neither "equivalent" nor
        "conflicting": it is left completely untouched for existing
        pending-action and error handling to surface instead.
        """
        prepared = _validate_identify_action(action)
        if prepared["action_type"] != "quality_metric":
            raise ValueError("This enqueue method accepts quality_metric actions only")
        payload = json.loads(prepared["payload_json"])
        metric = payload["metric"]
        vote = payload["vote"]

        conn = self._conn()
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ",".join("?" for _ in IDENTIFY_UNRESOLVED_STATES)
            rows = conn.execute(
                "SELECT local_action_id, state, payload_json FROM identify_actions "
                f"WHERE state IN ({placeholders}) AND lower(account_login)=lower(?) "
                "AND observation_id=? AND action_type='quality_metric' "
                "ORDER BY created_at, local_action_id",
                (
                    *IDENTIFY_UNRESOLVED_STATES,
                    prepared["account_login"],
                    prepared["observation_id"],
                ),
            ).fetchall()
            same: list[int] = []
            conflicting_queued: list[int] = []
            for row in rows:
                parsed = _parse_quality_metric_payload(row["payload_json"])
                if parsed is None:
                    # Malformed stored intent is neither equivalent nor
                    # conflicting; leave it untouched rather than guessing.
                    continue
                row_metric, row_vote = parsed
                if row_metric != metric:
                    continue
                if row_vote == vote:
                    same.append(int(row["local_action_id"]))
                elif str(row["state"]) == "queued":
                    conflicting_queued.append(int(row["local_action_id"]))
            cancelled: tuple[int, ...] = ()
            if conflicting_queued:
                conn.execute(
                    "UPDATE identify_actions SET state='cancelled', updated_at=? "
                    "WHERE state='queued' AND local_action_id IN "
                    f"({','.join('?' for _ in conflicting_queued)})",
                    (now, *conflicting_queued),
                )
                cancelled = tuple(conflicting_queued)
            if same:
                conn.commit()
                return IdentifyEnqueueResult(
                    duplicate_action_id=same[0],
                    cancelled_action_ids=cancelled,
                )
            cursor = self._insert_identify_action(conn, prepared, now)
            conn.commit()
            return IdentifyEnqueueResult(
                inserted_action_id=int(cursor.lastrowid),
                cancelled_action_ids=cancelled,
            )
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _insert_identify_action(
        conn: sqlite3.Connection,
        action: Dict[str, Any],
        now: float,
    ) -> sqlite3.Cursor:
        return conn.execute(
            """INSERT INTO identify_actions(
                account_login, observation_id, observation_uuid, action_type,
                payload_json, desired_state, deduplication_key, state,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                action["account_login"],
                action["observation_id"],
                action["observation_uuid"],
                action["action_type"],
                action["payload_json"],
                action["desired_state"],
                action["deduplication_key"],
                "queued",
                now,
                now,
            ),
        )

    def get_identify_actions(self, states: Optional[Tuple[str, ...]] = None) -> List[Dict]:
        """Return all actions, or only the explicitly supplied non-empty states."""
        if states is not None:
            states = tuple(states)
            _validate_identify_states(states)
        conn = self._conn()
        query = "SELECT * FROM identify_actions"
        values: tuple = ()
        if states is not None:
            query += " WHERE state IN (" + ",".join("?" for _ in states) + ")"
            values = states
        query += " ORDER BY created_at, local_action_id"
        return [dict(row) for row in conn.execute(query, values).fetchall()]

    def get_identify_action(self, action_id: int) -> Optional[Dict]:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM identify_actions WHERE local_action_id=?", (int(action_id),)
        ).fetchone()
        return dict(row) if row is not None else None

    def find_identify_action_by_dedup(self, deduplication_key: str) -> Optional[Dict]:
        """Most recent journaled action with this dedup key in ANY state, or None.

        Includes terminal states on purpose: callers inspect the returned row's
        ``state``/``outcome_unknown`` to decide whether the existing action can be
        reused, was already completed, or needs Identify retry/recovery — the
        state-filtered coalescing in ``enqueue_identification_or_comment`` cannot
        do this once the original action has resolved.
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM identify_actions WHERE deduplication_key=? "
            "ORDER BY created_at DESC, local_action_id DESC LIMIT 1",
            (str(deduplication_key),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_identify_actions_for_observation(self, observation_id: int) -> List[Dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM identify_actions WHERE observation_id=? "
            "ORDER BY created_at, local_action_id",
            (int(observation_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def transition_identify_action(
        self,
        action_id: int,
        expected_states: Iterable[str],
        new_state: str,
        **values: Any,
    ) -> bool:
        """Atomically transition one journal row only from expected states."""
        expected = tuple(expected_states)
        _validate_identify_states(expected)
        _validate_identify_state(new_state)
        _validate_identify_transition_values(values)
        conn = self._conn()
        columns = {"state": new_state, "updated_at": time.time(), **values}
        assignments = ", ".join(f"{key}=?" for key in columns)
        placeholders = ",".join("?" for _ in expected)
        # A single statement is already atomic under autocommit; no explicit
        # transaction wrapper is needed here.
        cursor = conn.execute(
            f"UPDATE identify_actions SET {assignments} "
            f"WHERE local_action_id=? AND state IN ({placeholders})",
            (*columns.values(), int(action_id), *expected),
        )
        if cursor.rowcount:
            log.debug(
                "Identify journal action %s transitioned %s -> %s",
                int(action_id),
                ",".join(expected),
                new_state,
            )
        return bool(cursor.rowcount)

    def confirm_identify_action(
        self,
        action_id: int,
        expected_states: Iterable[str],
        **values: Any,
    ) -> IdentifyConfirmationResult:
        """Confirm one row and atomically cancel its queued retry lineage.

        A source action can become confirmed during a safe verification read
        after the user has already created a linked duplicate-risk retry.  The
        source transition and cancellation therefore share one write
        transaction: no queued descendant can remain independently eligible
        once the source is known to exist on iNaturalist.
        """
        expected = tuple(expected_states)
        _validate_identify_states(expected)
        _validate_identify_transition_values(values)
        conn = self._conn()
        now = time.time()
        columns = {"state": "confirmed", "updated_at": now, **values}
        assignments = ", ".join(f"{key}=?" for key in columns)
        placeholders = ",".join("?" for _ in expected)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                f"UPDATE identify_actions SET {assignments} "
                f"WHERE local_action_id=? AND state IN ({placeholders})",
                (*columns.values(), int(action_id), *expected),
            )
            if not cursor.rowcount:
                conn.commit()
                return IdentifyConfirmationResult()

            # UNION (rather than UNION ALL) also makes this defensive against
            # a malformed legacy lineage cycle.
            retry_rows = conn.execute(
                """WITH RECURSIVE descendants(local_action_id) AS (
                       SELECT local_action_id
                       FROM identify_actions
                       WHERE parent_action_id=?
                       UNION
                       SELECT child.local_action_id
                       FROM identify_actions AS child
                       JOIN descendants AS parent
                         ON child.parent_action_id=parent.local_action_id
                   )
                   SELECT local_action_id
                   FROM identify_actions
                   WHERE state='queued'
                     AND local_action_id IN (SELECT local_action_id FROM descendants)
                   ORDER BY created_at, local_action_id""",
                (int(action_id),),
            ).fetchall()
            retry_ids = tuple(int(row["local_action_id"]) for row in retry_rows)
            if retry_ids:
                conn.execute(
                    "UPDATE identify_actions SET state='cancelled', updated_at=? "
                    "WHERE state='queued' AND local_action_id IN "
                    f"({','.join('?' for _ in retry_ids)})",
                    (now, *retry_ids),
                )
            conn.commit()
            log.debug(
                "Identify journal action %s confirmed; cancelled queued retries=%s",
                int(action_id),
                retry_ids,
            )
            return IdentifyConfirmationResult(True, retry_ids)
        except Exception:
            conn.rollback()
            raise

    def update_identify_action(self, action_id: int, state: str, **values: Any) -> None:
        """Compatibility wrapper for callers that know the current state.

        New code must call :meth:`transition_identify_action` with a specific
        expected state.  This wrapper remains intentionally conservative and
        refuses to update a row whose current state cannot be read.
        """
        action = self.get_identify_action(action_id)
        if action is None:
            return
        self.transition_identify_action(action_id, (action["state"],), state, **values)

    def recover_identify_submitting_actions(self) -> int:
        action_ids = [
            action["local_action_id"]
            for action in self.get_identify_actions(("submitting",))
        ]
        return sum(
            self.transition_identify_action(
                action_id,
                ("submitting",),
                "ambiguous",
                outcome_unknown=1,
                last_operation_phase="unsafe_write",
                last_error_json=_bounded_diagnostic_json(
                    {"phase": "unsafe_write", "reason": "startup_recovery"}
                ),
            )
            for action_id in action_ids
        )

    def migrate_legacy_manual_retry_queued_actions(self) -> int:
        """Convert the retired manual-retry state into auditable retry lineage.

        Older Gate 2 builds changed an ambiguous row itself to
        ``manual_retry_queued``.  That state was never dispatchable and, more
        importantly, hid the original uncertain write.  A migration restores
        that source row to ``ambiguous`` and creates a separate, linked queued
        row.  The manager is paused during construction, so the compatibility
        work cannot cause a write to be dispatched.
        """
        conn = self._conn()
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT * FROM identify_actions WHERE state='manual_retry_queued' "
                "ORDER BY created_at, local_action_id"
            ).fetchall()
            for row in rows:
                source_id = int(row["local_action_id"])
                conn.execute(
                    "UPDATE identify_actions SET state='ambiguous', outcome_unknown=1, "
                    "updated_at=? WHERE local_action_id=? AND state='manual_retry_queued'",
                    (now, source_id),
                )
                conn.execute(
                    """INSERT INTO identify_actions(
                        account_login, observation_id, observation_uuid, action_type,
                        payload_json, desired_state, deduplication_key, state,
                        created_at, updated_at, parent_action_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["account_login"],
                        row["observation_id"],
                        row["observation_uuid"],
                        row["action_type"],
                        row["payload_json"],
                        row["desired_state"],
                        row["deduplication_key"],
                        "queued",
                        now,
                        now,
                        source_id,
                    ),
                )
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise

    def create_manual_retry(self, action_id: int) -> Optional[int]:
        """Create one separate queued retry for an ambiguous action.

        The source row remains ambiguous as durable evidence that its previous
        unsafe write may already have succeeded.  The insert and source retry
        counter update share one SQLite transaction.
        """
        conn = self._conn()
        now = time.time()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM identify_actions WHERE local_action_id=? AND state='ambiguous'",
                (int(action_id),),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            cursor = conn.execute(
                """INSERT INTO identify_actions(
                    account_login, observation_id, observation_uuid, action_type,
                    payload_json, desired_state, deduplication_key, state,
                    created_at, updated_at, parent_action_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["account_login"],
                    row["observation_id"],
                    row["observation_uuid"],
                    row["action_type"],
                    row["payload_json"],
                    row["desired_state"],
                    row["deduplication_key"],
                    "queued",
                    now,
                    now,
                    int(row["local_action_id"]),
                ),
            )
            conn.execute(
                "UPDATE identify_actions SET manual_retry_count=?, outcome_unknown=1, "
                "updated_at=? WHERE local_action_id=? AND state='ambiguous'",
                (int(row["manual_retry_count"] or 0) + 1, now, int(row["local_action_id"])),
            )
            conn.commit()
            return int(cursor.lastrowid)
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Bulk-agree permanent skip list
    # ------------------------------------------------------------------

    def add_bulk_agree_skip(self, obs_id: int, reason: str = "") -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO bulk_agree_skip(obs_id, skipped_at, reason) VALUES(?,?,?)",
            (int(obs_id), time.time(), reason),
        )
        conn.commit()

    def remove_bulk_agree_skip(self, obs_id: int) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM bulk_agree_skip WHERE obs_id=?", (int(obs_id),))
        conn.commit()

    def is_bulk_agree_skipped(self, obs_id: int) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM bulk_agree_skip WHERE obs_id=?", (int(obs_id),)
        ).fetchone()
        return row is not None

    def get_all_bulk_agree_skips(self) -> List[int]:
        conn = self._conn()
        rows = conn.execute("SELECT obs_id FROM bulk_agree_skip ORDER BY skipped_at DESC").fetchall()
        return [r["obs_id"] for r in rows]

    # ------------------------------------------------------------------
    # Bulk-disagree permanent skip list
    # ------------------------------------------------------------------

    def add_bulk_disagree_skip(self, obs_id: int, reason: str = "") -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO bulk_disagree_skip(obs_id, skipped_at, reason) VALUES(?,?,?)",
            (int(obs_id), time.time(), reason),
        )
        conn.commit()

    def remove_bulk_disagree_skip(self, obs_id: int) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM bulk_disagree_skip WHERE obs_id=?", (int(obs_id),))
        conn.commit()

    def is_bulk_disagree_skipped(self, obs_id: int) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM bulk_disagree_skip WHERE obs_id=?", (int(obs_id),)
        ).fetchone()
        return row is not None

    def get_all_bulk_disagree_skips(self) -> List[int]:
        conn = self._conn()
        rows = conn.execute("SELECT obs_id FROM bulk_disagree_skip ORDER BY skipped_at DESC").fetchall()
        return [r["obs_id"] for r in rows]

    def clear_all_caches(self) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM query_cache")
        conn.execute("DELETE FROM taxon_summary_cache")
        conn.execute("DELETE FROM image_access_log")
        conn.commit()
