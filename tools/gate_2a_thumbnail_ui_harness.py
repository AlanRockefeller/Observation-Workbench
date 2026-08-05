#!/usr/bin/env python3
"""Offscreen Qt smoke harness for section-3 thumbnail-failure handling in
``ObservationCreationPreviewDialog`` (observation_workbench/ui/reconciliation.py).

Runs with QT_QPA_PLATFORM=offscreen, no display, no network — every
"download" is a local stub function. Never touches real credentials or the
real app database/QSettings.

Scenarios covered: successful matching image, fingerprint mismatch,
download error (raises), empty bytes, decode failure (garbage bytes),
selected item becoming disabled, and dialog close before the callback runs.

Run:
    QT_QPA_PLATFORM=offscreen ./.venv/bin/python tools/gate_2a_thumbnail_ui_harness.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from observation_workbench.reconciliation.photo_license import photo_byte_fingerprint  # noqa: E402
from observation_workbench.reconciliation.types import (  # noqa: E402
    ObservationCreationItem, ObservationCreationPreview, RemoteSite,
)
from observation_workbench.ui.reconciliation import ObservationCreationPreviewDialog  # noqa: E402


def _make_png(color: str) -> bytes:
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


_REAL_PNG: bytes
_OTHER_PNG: bytes

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str]] = []


def check(name: str, condition: bool) -> None:
    _results.append((name, PASS if condition else FAIL))
    print(f"[{PASS if condition else FAIL}] {name}")


def _make_preview(items: tuple[ObservationCreationItem, ...]) -> ObservationCreationPreview:
    return ObservationCreationPreview(
        profile_id=1, source_site=RemoteSite.MO, source_observation_id=1,
        destination_site=RemoteSite.INAT, auth_generation=1, mo_key_generation=1,
        source_fingerprint="fp", destination_account_login="tester",
        observed_on_string="2026-01-01", taxon_name="Amanita sp.", taxon_id=1,
        place_guess="somewhere", description="", items=items,
    )


def _item(key: str, expected_fp: str) -> ObservationCreationItem:
    return ObservationCreationItem(
        item_type="photo", source_site=RemoteSite.MO, source_identity=key,
        description=f"MO photo {key}", source_url=f"stub://{key}",
        reviewed_byte_fingerprint=expected_fp, enabled=True,
    )


def _wait_for_workers(app: QApplication, seconds: float = 2.0) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    global _REAL_PNG, _OTHER_PNG
    app = QApplication.instance() or QApplication([])
    _REAL_PNG = _make_png("red")
    _OTHER_PNG = _make_png("blue")

    real_fp = photo_byte_fingerprint(_REAL_PNG)

    def downloader(url: str) -> bytes:
        if url.endswith("mismatch"):
            return _OTHER_PNG
        if url.endswith("match"):
            return _REAL_PNG
        if url.endswith("error"):
            raise RuntimeError("simulated network failure")
        if url.endswith("empty"):
            return b""
        if url.endswith("garbage"):
            return b"not-an-image-at-all"
        raise AssertionError(f"unexpected stub url {url}")

    items = (
        _item("match", real_fp),
        _item("mismatch", real_fp),
        _item("error", real_fp),
        _item("empty", real_fp),
        _item("garbage", real_fp),
    )
    preview = _make_preview(items)
    dialog = ObservationCreationPreviewDialog(preview, downloader)

    key_of = {it.source_identity: f"item:{i}:{it.source_identity}" for i, it in enumerate(items)}

    # --- Section 1 (release blocker): every photo checkbox must start
    # DISABLED, with a visible loading-state reason, before any thumbnail
    # callback has arrived -- this is the very first thing to check, before
    # any worker has had a chance to run. ------------------------------------
    check(
        "every photo checkbox starts disabled before any thumbnail callback arrives",
        all(not dialog._check_by_key[key_of[k]].isEnabled() for k in ("match", "mismatch", "error", "empty", "garbage")),  # noqa: SLF001
    )
    check(
        "a not-yet-loaded photo checkbox shows a visible loading-state reason",
        "loading image" in dialog._check_by_key[key_of["match"]].text(),  # noqa: SLF001
    )

    # --- The race from the bug report: the user checks (and would confirm)
    # every item immediately, before any thumbnail worker has reported back.
    # setChecked() is a programmatic call and succeeds even on a disabled
    # QCheckBox (disabled only blocks *user* interaction), which is exactly
    # what makes the underlying race dangerous if selected_items() trusted
    # checkbox state alone. -----------------------------------------------
    for box, _checked_item, _key in dialog._checks:  # noqa: SLF001
        box.setChecked(True)

    check(
        "selected_items() excludes every photo checked before its thumbnail callback arrives",
        dialog.selected_items() == [],
    )

    _wait_for_workers(app)

    check("match: checkbox stays enabled and checked", dialog._check_by_key[key_of["match"]].isEnabled() and dialog._check_by_key[key_of["match"]].isChecked())  # noqa: SLF001
    check("mismatch: checkbox disabled and unchecked", not dialog._check_by_key[key_of["mismatch"]].isEnabled() and not dialog._check_by_key[key_of["mismatch"]].isChecked())  # noqa: SLF001
    check("error: checkbox disabled and unchecked", not dialog._check_by_key[key_of["error"]].isEnabled() and not dialog._check_by_key[key_of["error"]].isChecked())  # noqa: SLF001
    check("empty: checkbox disabled and unchecked", not dialog._check_by_key[key_of["empty"]].isEnabled() and not dialog._check_by_key[key_of["empty"]].isChecked())  # noqa: SLF001
    check("garbage/decode-failure: checkbox disabled and unchecked", not dialog._check_by_key[key_of["garbage"]].isEnabled() and not dialog._check_by_key[key_of["garbage"]].isChecked())  # noqa: SLF001

    selected = {it.source_identity for it in dialog.selected_items()}
    check("selected_items() excludes every disabled item", selected == {"match"})

    check(
        "only the successfully-verified photo key ends up in the verified set",
        dialog._visually_verified_photo_keys == {key_of["match"]},  # noqa: SLF001
    )

    check(
        "disabled reason text present for undisplayable image",
        "could not be displayed and reviewed" in dialog._check_by_key[key_of["error"]].text(),  # noqa: SLF001
    )

    # --- Selected item becoming disabled: prove the earlier `setChecked(True)`
    # up-front selection was actually reverted for every failure case above
    # (already implied by the checks, restated explicitly here). ------------
    check(
        "a previously-checked item that failed to load ends unchecked (not merely disabled)",
        all(not dialog._check_by_key[key_of[k]].isChecked() for k in ("mismatch", "error", "empty", "garbage")),  # noqa: SLF001
    )

    # --- Closing the dialog before a callback completes must not crash. ----
    late_calls: list[str] = []

    def slow_downloader(url: str) -> bytes:
        time.sleep(0.3)
        late_calls.append(url)
        return _REAL_PNG

    late_preview = _make_preview((_item("late", real_fp),))
    late_dialog = ObservationCreationPreviewDialog(late_preview, slow_downloader)
    late_dialog.close()
    try:
        _wait_for_workers(app, seconds=1.0)
        crashed = False
    except Exception:
        crashed = True
    check("closing the dialog while a worker is in flight does not crash", not crashed)

    failed = [n for n, s in _results if s == FAIL]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
