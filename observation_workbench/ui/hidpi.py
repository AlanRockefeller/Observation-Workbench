"""HiDPI scale detection, applied before the QApplication is constructed.

`QT_SCALE_FACTOR` multiplies ON TOP of the device pixel ratio the compositor
reports, so a fixed default is wrong in both directions.  WSLg exposes the same
3840x2400 panel in either of two modes depending on the session:

    * 1920x1200 logical, devicePixelRatio 2.0 — the compositor scales, Qt
      already renders at native resolution, and forcing 2 gives a net 4x
      (a window larger than the screen).
    * 3840x2400 raw, devicePixelRatio 1.0 — nothing scales anything, and the
      default 9pt UI font lands as ~14 physical pixels, i.e. unreadably small.

So detect it instead of guessing.  Screen geometry is only available once a
Q*Application exists, but `QT_SCALE_FACTOR` is only read while one is being
constructed — so the probe runs in a throwaway subprocess (~0.4s, creates no
window) and this process applies the result to its own environment.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

# A logical height at or above this, on an unscaled display, means the
# compositor handed us a HiDPI panel raw.  Well above any 1080p/1200p display,
# and below both 4K and 1440p-at-1x.
HIDPI_MIN_LOGICAL_HEIGHT = 1600

# The compositor is considered to be scaling already at or above this ratio.
_SCALED_DPR = 1.5

_PROBE_TIMEOUT_SEC = 20


def recommended_scale(dpr: float, logical_height: int) -> str | None:
    """Return the QT_SCALE_FACTOR value for a display, or None for no scaling."""
    if dpr >= _SCALED_DPR:
        return None  # compositor already scales; anything here would multiply
    if logical_height >= HIDPI_MIN_LOGICAL_HEIGHT:
        return "2"
    return None


def probe_display() -> tuple[float, int] | None:
    """Ask a throwaway QGuiApplication for the primary screen's dpr and height.

    Returns (device_pixel_ratio, logical_height), or None if the probe could
    not run — a headless box, a broken display connection, a Qt that refuses to
    start.  Callers treat None as "do not scale", which is the safe direction:
    text that is too small beats a window larger than the screen.
    """
    if getattr(sys, "frozen", False):
        # In a PyInstaller build, sys.executable IS the app (there is no
        # separate `python` to invoke), and this module is not a loose file
        # on disk to run as a script. Re-invoke the frozen exe itself with a
        # sentinel flag that main.py dispatches straight to _run_probe(),
        # instead of `[sys.executable, __file__, "--probe"]` — which would
        # silently launch a second full copy of the app (main.py tolerates
        # unknown args via parse_known_args), which launches a third to probe
        # itself, recursing until the process/handle limit is hit.
        cmd = [sys.executable, "--hidpi-probe"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--probe"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("HiDPI probe could not be launched: %s", exc)
        return None

    if proc.returncode != 0:
        log.debug("HiDPI probe failed (rc=%s): %s", proc.returncode, proc.stderr.strip())
        return None

    parts = proc.stdout.split()
    if len(parts) != 2:
        log.debug("HiDPI probe produced unusable output: %r", proc.stdout)
        return None
    try:
        return float(parts[0]), int(parts[1])
    except ValueError:
        log.debug("HiDPI probe produced unparsable output: %r", proc.stdout)
        return None


def ensure_scale_factor() -> str | None:
    """Set QT_SCALE_FACTOR for this process if the display needs it.

    Must be called before the QApplication is constructed.  An existing
    QT_SCALE_FACTOR is always left alone — that is how `start.sh --scale=`, and
    anyone setting it by hand, opts out of the detection.  Returns the value in
    effect, or None if no scaling is applied.
    """
    existing = os.environ.get("QT_SCALE_FACTOR", "").strip()
    if existing:
        log.debug("QT_SCALE_FACTOR=%s set externally; skipping HiDPI probe", existing)
        return existing

    probed = probe_display()
    if probed is None:
        return None
    dpr, logical_height = probed

    scale = recommended_scale(dpr, logical_height)
    log.debug(
        "HiDPI probe: dpr=%s logical_height=%s -> QT_SCALE_FACTOR=%s",
        dpr, logical_height, scale or "unset",
    )
    if scale is None:
        return None
    os.environ["QT_SCALE_FACTOR"] = scale
    return scale


def _run_probe() -> int:
    """Print "<dpr> <logical_height>" for the primary screen. Subprocess entry."""
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication([sys.argv[0]])
    screen = app.primaryScreen()
    if screen is None:
        return 1
    print(f"{screen.devicePixelRatio()} {screen.size().height()}")
    return 0


if __name__ == "__main__":
    sys.exit(_run_probe())
