#!/usr/bin/env bash
# Launch the V2 touch dashboard on the Raspberry Pi.
#
# Ensures the Python venv exists (creating it the first time, inheriting the
# apt-installed OpenCV/Pillow/Tk), then starts show_dashboard.py. One-time SYSTEM
# packages are installed separately -- see SETUP_RPI.md.
#
#   ./run.sh             # fullscreen on the touchscreen
#   ./run.sh --windowed  # windowed, for desktop testing
#   ./run.sh --pull      # git pull first, then run
#
set -eo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # repo dir = where this lives
cd "$DIR"
VENV="$DIR/.venv"

# split args: handle --pull here, pass the rest through to show_dashboard.py
ARGS=()
for a in "$@"; do
  case "$a" in
    --pull) echo "[run] git pull..."; git pull --ff-only || echo "[run] pull failed; using local copy" ;;
    *) ARGS+=("$a") ;;
  esac
done

# create the venv (with access to apt's cv2/PIL/tk) on first run
if [ ! -d "$VENV" ]; then
  echo "[run] creating venv at .venv (--system-site-packages)..."
  python3 -m venv --system-site-packages "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$DIR/requirements.txt"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# Tkinter needs a display; default to the attached screen when run over SSH
export DISPLAY="${DISPLAY:-:0}"

# fail fast with a clear message if a system package is missing
python - <<'PY'
import sys
missing = []
for label, mod in (("bleak", "bleak"), ("opencv (cv2)", "cv2"),
                   ("Pillow ImageTk", "PIL.ImageTk"), ("tkinter", "tkinter")):
    try:
        __import__(mod)
    except Exception:
        missing.append(label)
if missing:
    print("[run] MISSING: " + ", ".join(missing))
    print("[run] install system libs once:")
    print("      sudo apt install -y python3-tk python3-opencv python3-pil python3-pil.imagetk")
    sys.exit(1)
PY

echo "[run] starting show_dashboard.py ${ARGS[*]}"
exec python "$DIR/show_dashboard.py" "${ARGS[@]}"
