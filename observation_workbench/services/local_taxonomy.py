"""Local fungal name index for instant Identify taxon autocomplete.

All active and inactive Fungi taxa (~90k rows) are mirrored into
``taxonomy.db`` beside the image cache and searched in memory, so suggestions
appear on each keystroke without a network round-trip.

Refresh policy:

* **Daily delta** — taxa created since the last pass are fetched with
  ``id_above=<max known id>`` (usually one request per active/inactive pass).
* **Weekly full refresh** — taxon changes (swaps, splits, renames, common-name
  edits) modify existing IDs, which no cheap delta can see, so the whole tree
  is re-read (~180 paced requests) into a fresh snapshot and swapped in at the
  end.  An interrupted refresh leaves the previous snapshot untouched.  Once
  a snapshot exists, this waits until the app has had no keyboard/mouse input
  for 15 minutes, so a refresh rarely competes with active identifying; the
  data may run a little past a week old meanwhile.  The very first download
  starts immediately because there is nothing local to search yet.

Taxa outside Fungi that the user selects through the network fallback are
remembered as ``extra`` rows so occasional names (Homo sapiens, Bryophyta)
become instant too.
"""

from __future__ import annotations

import bisect
import heapq
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    Signal,
)

from observation_workbench.api.client import INatClient

log = logging.getLogger(__name__)

FUNGI_TAXON_ID = 47170
TAXONOMY_DB_NAME = "taxonomy.db"
_SCHEMA_VERSION = "1"
_FULL_REFRESH_SECONDS = 7 * 24 * 3600
_DELTA_REFRESH_SECONDS = 24 * 3600
_PAGE_SIZE = 500
# Extra pause between pages on top of the shared 1 req/s limiter, so a
# multi-minute refresh leaves most request slots free for interactive reads.
_PAGE_PAUSE_SECONDS = 1.0
_IDLE_BEFORE_FULL_REFRESH_SECONDS = 15 * 60
_IDLE_CHECK_INTERVAL_MS = 60 * 1000
# Minimum spacing of idle-time worker runs, so an offline machine does not
# retry a failing refresh every minute.
_IDLE_ATTEMPT_SPACING_SECONDS = 30 * 60
_STRIP_CHARS = "'\"()[],"


@dataclass(frozen=True)
class LocalTaxonMatch:
    """One suggestion from the local index."""

    taxon_id: int
    scientific_name: str
    preferred_common_name: str = ""
    rank: str = ""
    # Synonym the query matched when it is not the current name.
    matched_name: str = ""


def _words(text: str) -> tuple[str, ...]:
    words = []
    for raw in text.casefold().split():
        word = raw.strip(_STRIP_CHARS)
        if word:
            words.append(word)
    return tuple(words)


class TaxonIndex:
    """Word-prefix index built on a worker, then owned by the GUI thread.

    Every query token must prefix a distinct word of a name, in order, so
    ``"C moly"`` finds *Chlorophyllum molybdites* and ``"Boletus grand"`` finds
    *Boletus edulis grandedulis*.
    """

    def __init__(self, rows: list[tuple]) -> None:
        # rows: (id, name, rank, rank_level, is_active, common, obs_count, synonym_ids)
        self._taxa: dict[int, tuple[str, str, str, int, float]] = {}
        inactive: list[tuple[str, str]] = []
        for (
            taxon_id,
            name,
            rank,
            rank_level,
            is_active,
            common,
            obs_count,
            synonym_ids,
        ) in rows:
            if is_active:
                self._taxa[int(taxon_id)] = (
                    name,
                    common or "",
                    rank or "",
                    int(obs_count or 0),
                    float(rank_level or 0),
                )
            elif synonym_ids:
                inactive.append((name, synonym_ids))

        # term: (words, taxon_id, matched_name, kind) kind 0=name 1=synonym 2=common
        terms: list[tuple[tuple[str, ...], int, str, int]] = []
        for taxon_id, (name, common, _rank, _obs, _level) in self._taxa.items():
            terms.append((_words(name), taxon_id, "", 0))
            if common:
                terms.append((_words(common), taxon_id, "", 2))
        for name, synonym_ids in inactive:
            targets = [s for s in str(synonym_ids).split(",") if s.strip()]
            # A split has several successors; guessing one would be wrong.
            if len(targets) != 1:
                continue
            target = int(targets[0])
            target_entry = self._taxa.get(target)
            if target_entry is None or target_entry[0] == name:
                continue
            terms.append((_words(name), target, name, 1))
        self._terms = terms
        word_refs = sorted(
            (word, term_index)
            for term_index, (words, _t, _m, _k) in enumerate(terms)
            for word in set(words)
        )
        self._index_words = [word for word, _ in word_refs]
        self._index_terms = [term_index for _, term_index in word_refs]

    def __len__(self) -> int:
        return len(self._taxa)

    def contains(self, taxon_id: int) -> bool:
        return int(taxon_id) in self._taxa

    def search(self, query: str, limit: int = 7) -> list[LocalTaxonMatch]:
        tokens = _words(query)
        if not tokens:
            return []
        anchor = max(tokens, key=len)
        start = bisect.bisect_left(self._index_words, anchor)
        stop = bisect.bisect_left(self._index_words, anchor + "￿", lo=start)
        normalized = " ".join(tokens)
        best: dict[int, tuple[tuple, str]] = {}
        for term_index in {self._index_terms[i] for i in range(start, stop)}:
            words, taxon_id, matched_name, kind = self._terms[term_index]
            position = 0
            first_word_hit = False
            for token_number, token in enumerate(tokens):
                while position < len(words) and not words[position].startswith(token):
                    position += 1
                if position == len(words):
                    break
                if token_number == 0 and position == 0:
                    first_word_hit = True
                position += 1
            else:
                _name, _common, _rank, obs_count, rank_level = self._taxa[taxon_id]
                key = (
                    " ".join(words) != normalized,
                    not first_word_hit,
                    kind,
                    -obs_count,
                    -rank_level,
                )
                previous = best.get(taxon_id)
                if previous is None or key < previous[0]:
                    best[taxon_id] = (key, matched_name)
        ranked = heapq.nsmallest(limit, best.items(), key=lambda item: item[1][0])
        matches = []
        for taxon_id, (_key, matched_name) in ranked:
            name, common, rank, _obs, _level = self._taxa[taxon_id]
            matches.append(LocalTaxonMatch(taxon_id, name, common, rank, matched_name))
        return matches

    def add_taxon(
        self, taxon_id: int, name: str, common: str = "", rank: str = ""
    ) -> None:
        """Insert one active taxon in place (GUI thread; O(n) list inserts)."""
        taxon_id = int(taxon_id)
        if taxon_id in self._taxa:
            return
        self._taxa[taxon_id] = (name, common, rank, 0, 0.0)
        for words, kind in ((_words(name), 0), (_words(common), 2)):
            if not words:
                continue
            term_index = len(self._terms)
            self._terms.append((words, taxon_id, "", kind))
            for word in set(words):
                position = bisect.bisect_left(self._index_words, word)
                self._index_words.insert(position, word)
                self._index_terms.insert(position, term_index)


class TaxonomyStore:
    """SQLite persistence; every method opens its own short connection."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS taxa (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                rank TEXT,
                rank_level REAL,
                is_active INTEGER NOT NULL,
                common_name TEXT,
                observations_count INTEGER,
                synonym_ids TEXT,
                source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        return connection

    def load_rows(self) -> list[tuple]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT id, name, rank, rank_level, is_active, common_name, "
                "observations_count, synonym_ids FROM taxa"
            ).fetchall()

    def meta(self) -> dict[str, str]:
        with self._connect() as connection:
            return dict(connection.execute("SELECT key, value FROM meta").fetchall())

    def max_fungi_id(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(id) FROM taxa WHERE source = 'fungi'"
            ).fetchone()
        return int(row[0] or 0)

    def replace_fungi(self, rows: list[tuple]) -> None:
        now = str(time.time())
        with self._connect() as connection:
            connection.execute("DELETE FROM taxa WHERE source = 'fungi'")
            connection.executemany(
                "INSERT OR REPLACE INTO taxa VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fungi')",
                rows,
            )
            connection.executemany(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)",
                [
                    ("schema_version", _SCHEMA_VERSION),
                    ("full_sync_at", now),
                    ("delta_sync_at", now),
                ],
            )

    def upsert_fungi(self, rows: list[tuple]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO taxa VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fungi')",
                rows,
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta VALUES ('delta_sync_at', ?)",
                (str(time.time()),),
            )

    def add_extra(self, row: tuple) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO taxa VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'extra')",
                row,
            )


def _row_from_record(record: object) -> tuple | None:
    if not isinstance(record, dict):
        return None
    try:
        taxon_id = int(record.get("id"))
    except (TypeError, ValueError):
        return None
    name = str(record.get("name") or "").strip()
    if taxon_id <= 0 or not name:
        return None
    synonyms = record.get("current_synonymous_taxon_ids") or []
    synonym_ids = ",".join(
        str(int(s)) for s in synonyms if isinstance(s, (int, str)) and str(s).isdigit()
    )
    try:
        rank_level = float(record.get("rank_level") or 0)
    except (TypeError, ValueError):
        rank_level = 0.0
    try:
        obs_count = int(record.get("observations_count") or 0)
    except (TypeError, ValueError):
        obs_count = 0
    return (
        taxon_id,
        name,
        str(record.get("rank") or "").strip(),
        rank_level,
        1 if record.get("is_active", True) else 0,
        str(record.get("preferred_common_name") or "").strip(),
        obs_count,
        synonym_ids,
    )


class _SyncCancelled(Exception):
    pass


class _TaxonomySignals(QObject):
    index_ready = Signal(object, str)  # TaxonIndex, reason
    full_refresh_due = Signal()
    finished = Signal()


class _TaxonomyWorker(QRunnable):
    """Load the cached index, then refresh from iNaturalist when it is due.

    A due full refresh only runs when ``allow_full`` (the app is idle) or when
    there is no usable snapshot; otherwise it is reported via
    ``full_refresh_due`` and only the cheap delta runs.
    """

    def __init__(
        self,
        store: TaxonomyStore,
        cancel: threading.Event,
        *,
        load_first: bool,
        allow_full: bool,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._store = store
        self._cancel = cancel
        self._load_first = load_first
        self._allow_full = allow_full
        self.signals = _TaxonomySignals()

    def run(self) -> None:
        try:
            if self._load_first:
                started = time.perf_counter()
                rows = self._store.load_rows()
                if rows:
                    index = TaxonIndex(rows)
                    log.debug(
                        "Local taxonomy loaded %d taxa (%d rows) in %.0fms",
                        len(index),
                        len(rows),
                        (time.perf_counter() - started) * 1000,
                    )
                    self.signals.index_ready.emit(index, "cache")
            meta = self._store.meta()
            now = time.time()
            full_at = float(meta.get("full_sync_at") or 0)
            delta_at = float(meta.get("delta_sync_at") or 0)
            no_snapshot = (
                meta.get("schema_version") != _SCHEMA_VERSION
                or self._store.max_fungi_id() == 0
            )
            full_due = now - full_at > _FULL_REFRESH_SECONDS
            if no_snapshot or (full_due and self._allow_full):
                try:
                    self._full_refresh()
                except Exception:
                    self.signals.full_refresh_due.emit()
                    raise
            else:
                if full_due:
                    log.debug("Local taxonomy full refresh due; waiting for idle")
                    self.signals.full_refresh_due.emit()
                if now - delta_at <= _DELTA_REFRESH_SECONDS:
                    return
                self._delta_refresh()
            self.signals.index_ready.emit(
                TaxonIndex(self._store.load_rows()), "refresh"
            )
        except _SyncCancelled:
            log.debug("Local taxonomy refresh cancelled")
        except Exception as exc:
            log.warning("Local taxonomy refresh failed: %s", type(exc).__name__)
            log.debug("Local taxonomy refresh failure", exc_info=True)
        finally:
            self.signals.finished.emit()

    def _pages(self, client: INatClient, *, is_active: bool, id_above: int):
        while True:
            if self._cancel.is_set():
                raise _SyncCancelled
            response = client.get_taxa_page_v2(
                taxon_id=FUNGI_TAXON_ID,
                is_active=is_active,
                id_above=id_above,
                per_page=_PAGE_SIZE,
            )
            records = response.get("results") or []
            rows = [row for row in map(_row_from_record, records) if row]
            if rows:
                yield rows
                id_above = max(row[0] for row in rows)
            if len(records) < _PAGE_SIZE or not rows:
                return
            if self._cancel.wait(_PAGE_PAUSE_SECONDS):
                raise _SyncCancelled

    def _full_refresh(self) -> None:
        started = time.perf_counter()
        client = INatClient()
        try:
            rows: list[tuple] = []
            for is_active in (True, False):
                for page in self._pages(client, is_active=is_active, id_above=0):
                    rows.extend(page)
        finally:
            client.close()
        if not rows:
            raise RuntimeError("empty taxonomy download")
        self._store.replace_fungi(rows)
        log.info(
            "Local taxonomy full refresh stored %d Fungi taxa in %.0fs",
            len(rows),
            time.perf_counter() - started,
        )

    def _delta_refresh(self) -> None:
        max_id = self._store.max_fungi_id()
        client = INatClient()
        try:
            rows: list[tuple] = []
            for is_active in (True, False):
                for page in self._pages(client, is_active=is_active, id_above=max_id):
                    rows.extend(page)
        finally:
            client.close()
        self._store.upsert_fungi(rows)
        log.info("Local taxonomy delta refresh added %d new Fungi taxa", len(rows))


class LocalTaxonomy(QObject):
    """App-scoped owner of the index snapshot; use :func:`shared_local_taxonomy`."""

    index_changed = Signal()

    def __init__(self, db_path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = TaxonomyStore(db_path)
        self._index: TaxonIndex | None = None
        self._cancel = threading.Event()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._running = False
        self._loaded = False
        self._last_attempt = 0.0
        self._full_due = False
        self._last_idle_attempt = 0.0
        self._last_input = time.monotonic()
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(_IDLE_CHECK_INTERVAL_MS)
        self._idle_timer.timeout.connect(self._idle_check)
        self._live_signals: set[_TaxonomySignals] = set()
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._cancel.set)
            app.aboutToQuit.connect(self._idle_timer.stop)
            app.installEventFilter(self)

    @property
    def ready(self) -> bool:
        return self._index is not None

    @property
    def taxon_count(self) -> int:
        return len(self._index) if self._index is not None else 0

    def ensure_started(self) -> None:
        """Load the cache once; run the delta (and first download) when due."""
        if not self._idle_timer.isActive():
            self._idle_timer.start()
        if self._running or time.monotonic() - self._last_attempt < 3600:
            return
        self._start_worker(allow_full=False)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in _INPUT_EVENT_TYPES:
            self._last_input = time.monotonic()
        return False

    def _idle_check(self) -> None:
        """Run deferred refresh work once the user has stepped away.

        Also re-checks every few hours, so a session left open for days still
        notices when the weekly refresh becomes due.
        """
        now = time.monotonic()
        if (
            self._running
            or now - self._last_input < _IDLE_BEFORE_FULL_REFRESH_SECONDS
            or now - self._last_idle_attempt < _IDLE_ATTEMPT_SPACING_SECONDS
        ):
            return
        if not self._full_due and now - self._last_attempt < 6 * 3600:
            return
        log.debug(
            "App idle for %.0f min; starting deferred local taxonomy refresh",
            (now - self._last_input) / 60,
        )
        self._last_idle_attempt = now
        self._start_worker(allow_full=True)

    def _start_worker(self, *, allow_full: bool) -> None:
        self._last_attempt = time.monotonic()
        self._running = True
        if allow_full:
            self._full_due = False
        worker = _TaxonomyWorker(
            self._store,
            self._cancel,
            load_first=not self._loaded,
            allow_full=allow_full,
        )
        self._loaded = True
        worker.signals.full_refresh_due.connect(self._mark_full_due)
        self._live_signals.add(worker.signals)
        worker.signals.index_ready.connect(self._index_ready)
        worker.signals.finished.connect(self._worker_finished)
        self._pool.start(worker)

    def search(self, query: str, limit: int = 7) -> list[LocalTaxonMatch]:
        if self._index is None:
            return []
        return self._index.search(query, limit)

    def contains(self, taxon_id: int) -> bool:
        return self._index is not None and self._index.contains(taxon_id)

    def remember(self, match: LocalTaxonMatch) -> None:
        """Keep a network-only selection (e.g. a plant) for instant reuse."""
        if self._index is None or self._index.contains(match.taxon_id):
            return
        row = (
            int(match.taxon_id),
            match.scientific_name,
            match.rank,
            0.0,
            1,
            match.preferred_common_name,
            0,
            "",
        )
        self._index.add_taxon(
            match.taxon_id,
            match.scientific_name,
            match.preferred_common_name,
            match.rank,
        )
        store = self._store

        def write() -> None:
            try:
                store.add_extra(row)
            except Exception:
                log.debug("Could not store an extra local taxon", exc_info=True)

        QThreadPool.globalInstance().start(write)
        log.debug("Local taxonomy remembered extra taxon %s", match.taxon_id)

    def _index_ready(self, index: object, reason: str) -> None:
        if isinstance(index, TaxonIndex):
            self._index = index
            log.debug("Local taxonomy index active (%s): %d taxa", reason, len(index))
            self.index_changed.emit()

    def _mark_full_due(self) -> None:
        self._full_due = True

    def _worker_finished(self) -> None:
        self._running = False
        signals = self.sender()
        if isinstance(signals, _TaxonomySignals):
            self._live_signals.discard(signals)


_INPUT_EVENT_TYPES = frozenset(
    {
        QEvent.Type.KeyPress,
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseMove,
        QEvent.Type.Wheel,
    }
)

_shared: LocalTaxonomy | None = None


def shared_local_taxonomy(cache_dir: Path) -> LocalTaxonomy:
    global _shared
    if _shared is None:
        _shared = LocalTaxonomy(Path(cache_dir) / TAXONOMY_DB_NAME)
    return _shared
