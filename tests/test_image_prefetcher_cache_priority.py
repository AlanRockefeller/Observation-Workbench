import unittest
from types import SimpleNamespace

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication

from observation_workbench.services.prefetcher import ImagePrefetcher


class _DummyDiskCache:
    def __init__(self, initial=None) -> None:
        self._data = initial or {}

    def get(self, photo_id: int, size: str):
        return self._data.get((photo_id, size))

    def has(self, photo_id: int, size: str) -> bool:
        return (photo_id, size) in self._data


class ImagePrefetcherCachePriorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _image_bytes(self, width: int, height: int) -> bytes:
        img = QImage(width, height, QImage.Format.Format_ARGB32)
        img.fill(0xFF336699)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.ReadWrite)
        img.save(buffer, "PNG")
        return bytes(buffer.data())

    def test_best_cached_prefers_highest_quality_exact_size(self) -> None:
        photo_id = 123
        disk_cache = _DummyDiskCache(
            {
                (photo_id, "original"): self._image_bytes(12, 12),
            }
        )
        prefetcher = ImagePrefetcher(
            client=SimpleNamespace(),
            disk_cache=disk_cache,
            max_memory_bytes=1024 * 1024,
        )

        small_pixmap = QPixmap(2, 2)
        small_pixmap.fill()
        prefetcher._memory[(photo_id, "small")] = small_pixmap
        prefetcher._memory_bytes = 16

        cached = prefetcher.get_best_cached(photo_id)

        self.assertIsNotNone(cached)
        assert cached is not None
        pixmap, size = cached
        self.assertEqual(size, "original")
        self.assertEqual((pixmap.width(), pixmap.height()), (12, 12))


if __name__ == "__main__":
    unittest.main()
