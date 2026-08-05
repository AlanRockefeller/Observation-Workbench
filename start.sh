#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -x ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
elif [[ -x ".venv/Scripts/python.exe" ]]; then
    PYTHON=".venv/Scripts/python.exe"
else
    PYTHON="$(command -v python3 || command -v python || true)"
fi

if [[ -z "${PYTHON}" ]]; then
    echo "Could not find Python."
    echo "Create a virtual environment with:"
    echo "  python3 -m venv .venv"
    echo "  .venv/bin/python -m pip install -r requirements.txt"
    exit 1
fi

PY_VERSION="$("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    echo "Python 3.11+ is required; found $PY_VERSION at: $PYTHON"
    if [[ "$PYTHON" == .venv/* || "$PYTHON" == .venv\\* ]]; then
        echo "Recreate the local virtual environment with Python 3.11 or newer:"
        echo "  python3.11 -m venv .venv"
        echo "  .venv/bin/python -m pip install -r requirements.txt"
    fi
    exit 1
fi

if ! "$PYTHON" -c "import PySide6" >/dev/null 2>&1; then
    echo "PySide6 is not installed for: $PYTHON"
    if [[ "$PYTHON" == .venv/* || "$PYTHON" == .venv\\* ]]; then
        echo "Install project dependencies with:"
        echo "  $PYTHON -m pip install -r requirements.txt"
    else
        echo "No usable .venv was found. Create and install one with:"
        echo "  python3 -m venv .venv"
        echo "  .venv/bin/python -m pip install -r requirements.txt"
    fi
    exit 1
fi

# HiDPI scaling.  QT_SCALE_FACTOR multiplies ON TOP of whatever the
# compositor already reports as the device pixel ratio, so a fixed default is
# wrong in both directions:
#
#   * WSLg sometimes exposes the 4K panel as a 1920x1200 dpr-2 output — Qt is
#     already rendering at native resolution, and forcing 2 gives a net 4x.
#   * WSLg (notably over RDP) sometimes exposes the same panel as a raw
#     3840x2400 dpr-1 output — nothing scales anything, and the default 9pt
#     UI font lands as ~14 physical pixels, i.e. unreadably small.
#
# Which mode you get depends on the session, so it is detected at launch rather
# than guessed.  The detection lives in observation_workbench/ui/hidpi.py and runs from
# main.py, so it applies however the app is started; this script only forwards
# an explicit SCALE=<n> / --scale=<n> / -s <n> override (SCALE=1 disables
# scaling entirely).
SCALE="${SCALE:-}"
SCALE_EXPLICIT=0
[[ -n "$SCALE" ]] && SCALE_EXPLICIT=1

args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --scale=*)
            SCALE="${1#--scale=}"
            SCALE_EXPLICIT=1
            shift
            ;;
        -s)
            SCALE="${2:-$SCALE}"
            SCALE_EXPLICIT=1
            shift 2 2>/dev/null || shift
            ;;
        *)
            args+=("$1")
            shift
            ;;
    esac
done

# Only an explicit request is exported here — including SCALE=1, which is how
# you opt out of scaling entirely.  With nothing set, main.py probes the live
# display and decides, so `python main.py` and `./start.sh` size identically.
if [[ "$SCALE_EXPLICIT" -eq 1 && -n "$SCALE" ]]; then
    export QT_SCALE_FACTOR="$SCALE"
fi

# Pin the logical font DPI. WSLg re-negotiates the virtual display on
# suspend/resume and can report a different DPI afterwards, which makes all
# text suddenly change size in a running app. Pinning keeps text consistent;
# use SCALE / --scale (or the in-app UI scale setting) to change text size.
export QT_FONT_DPI="${QT_FONT_DPI:-96}"

exec "$PYTHON" main.py "${args[@]}"
