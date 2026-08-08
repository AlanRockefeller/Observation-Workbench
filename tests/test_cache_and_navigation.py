import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from observation_workbench.services.image_cache import ImageCache
from observation_workbench.storage.cache_db import CacheDB
from observation_workbench.ui.main_window import MainWindow


class CacheDBTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._db = CacheDB(self._root / "metadata.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_log_image_access_updates_file_path_on_conflict(self) -> None:
        self._db.log_image_access("123_large", "/tmp/123_large.jpg", 10)
        self._db.log_image_access("123_large", "/tmp/123_large.webp", 20)

        rows = self._db.get_lru_images(limit=10)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_path"], "/tmp/123_large.webp")
        self.assertEqual(rows[0]["file_size"], 20)


class ImageCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._db = CacheDB(self._root / "metadata.db")
        self._cache = ImageCache(self._root / "images", self._db, max_bytes=1024 * 1024)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_put_replaces_old_extension_and_updates_tracked_path(self) -> None:
        self._cache.put(123, "large", b"old-jpg", "jpg")
        self._cache.put(123, "large", b"new-webp", "webp")

        self.assertFalse((self._root / "images" / "123_large.jpg").exists())
        self.assertTrue((self._root / "images" / "123_large.webp").exists())

        rows = self._db.get_lru_images(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["file_path"],
            str(self._root / "images" / "123_large.webp"),
        )

    def test_missing_file_during_eviction_is_benign_cleanup(self) -> None:
        path = self._root / "images" / "123_large.jpg"
        path.write_bytes(b"x")
        self._db.log_image_access("123_large", str(path), 1)
        self._cache._max_bytes = 0

        with mock.patch(
            "observation_workbench.services.image_cache.Path.unlink",
            side_effect=FileNotFoundError(),
        ):
            with self.assertNoLogs("observation_workbench.services.image_cache", level="WARNING"):
                self._cache._maybe_evict()

        self.assertEqual(self._db.get_total_image_cache_size(), 0)


class _DummyViewer:
    def __init__(self) -> None:
        self.loading_calls = []
        self.pixmaps = []

    def set_loading(self, value: bool) -> None:
        self.loading_calls.append(value)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self.pixmaps.append(pixmap)


class MainWindowPrefetchReadyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_prefetch_ready_ignores_stale_observation_identity(self) -> None:
        viewer = _DummyViewer()
        window = SimpleNamespace(
            _display_obs_id=42,
            _display_photo_id=84,
            _viewer=viewer,
        )
        pixmap = QPixmap(4, 4)
        pixmap.fill()

        MainWindow._on_prefetch_ready(window, 5, 0, 41, 83, pixmap)

        self.assertEqual(viewer.loading_calls, [])
        self.assertEqual(viewer.pixmaps, [])

        MainWindow._on_prefetch_ready(window, 5, 0, 42, 84, pixmap)

        self.assertEqual(viewer.loading_calls, [False])
        self.assertEqual(len(viewer.pixmaps), 1)


if __name__ == "__main__":
    unittest.main()
