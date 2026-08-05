"""
Disk-based image cache with LRU eviction.

Layout: <cache_dir>/<photo_id>_<size>.<ext>
Eviction: remove least-recently-accessed files when total size exceeds limit.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from observation_workbench.storage.cache_db import CacheDB

log = logging.getLogger(__name__)


class ImageCache:
    _KNOWN_EXTS = ("jpg", "jpeg", "png", "webp")

    def __init__(self, cache_dir: Path, db: CacheDB, max_bytes: int) -> None:
        self._dir = cache_dir
        self._db = db
        self._max_bytes = max_bytes
        self._lock = threading.RLock()
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, photo_id: int, size: str) -> str:
        return f"{photo_id}_{size}"

    def _path(self, photo_id: int, size: str, ext: str = "jpg") -> Path:
        return self._dir / f"{photo_id}_{size}.{ext}"

    def get(self, photo_id: int, size: str) -> Optional[bytes]:
        """Return cached image bytes or None if not cached."""
        key = self._key(photo_id, size)
        for p in self._candidate_paths(key):
            if p.exists():
                try:
                    data = p.read_bytes()
                    self._db.log_image_access(key, str(p), len(data))
                    return data
                except OSError as exc:
                    log.warning("Cache read error %s: %s", p, exc)
        return None

    def put(self, photo_id: int, size: str, data: bytes, ext: str = "jpg") -> None:
        """Store image bytes. Triggers LRU eviction if needed."""
        key = self._key(photo_id, size)
        p = self._path(photo_id, size, ext)
        try:
            with self._lock:
                p.write_bytes(data)
                self._remove_stale_variants(key, keep=p)
                self._db.log_image_access(key, str(p), len(data))
                self._maybe_evict()
        except OSError as exc:
            log.warning("Cache write error %s: %s", p, exc)

    def has(self, photo_id: int, size: str) -> bool:
        key = self._key(photo_id, size)
        for p in self._candidate_paths(key):
            if p.exists():
                return True
        return False

    def remove(self, photo_id: int, size: str) -> None:
        """Remove one cached rendition, including its LRU metadata.

        A decode failure is evidence that a byte payload is unusable.  Keeping
        it would make every future image request fail before it can redownload.
        """
        key = self._key(photo_id, size)
        with self._lock:
            for candidate in self._candidate_paths(key):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    log.warning("Cache removal error %s: %s", candidate, exc)
            self._db.remove_image_log(key)

    def _candidate_paths(self, key: str) -> tuple[Path, ...]:
        return tuple(self._dir / f"{key}.{ext}" for ext in self._KNOWN_EXTS)

    def _remove_stale_variants(self, key: str, keep: Path) -> None:
        for candidate in self._candidate_paths(key):
            if candidate == keep:
                continue
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.warning("Cache cleanup error %s: %s", candidate, exc)

    def _maybe_evict(self) -> None:
        with self._lock:
            total = self._db.get_total_image_cache_size()
            if total <= self._max_bytes:
                return
            target = int(self._max_bytes * 0.85)  # evict to 85% of limit
            lru = self._db.get_lru_images(limit=500)
            for entry in lru:
                if total <= target:
                    break
                path = Path(entry["file_path"])
                try:
                    path.unlink()
                    total -= entry["file_size"]
                except FileNotFoundError:
                    # Another thread or a prior cleanup already removed it.
                    total -= entry["file_size"]
                except OSError as exc:
                    log.warning("Eviction error %s: %s", path, exc)
                    continue
                self._db.remove_image_log(entry["cache_key"])
                log.debug("Evicted %s", path)

    def total_size_bytes(self) -> int:
        return self._db.get_total_image_cache_size()

    def clear(self) -> None:
        """Remove all cached images from disk."""
        with self._lock:
            for f in self._dir.glob("*"):
                try:
                    f.unlink()
                except OSError:
                    pass
            self._db.clear_all_image_logs()
        log.info("Image cache cleared")
