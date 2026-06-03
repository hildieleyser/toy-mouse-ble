# Running on a Raspberry Pi 5

Setup for the field rig: a Pi 5 (4GB) driving toy mice over BLE, with the V2
touch dashboard (`show_dashboard.py`) on a 1280×480 HDMI touchscreen, off the
repo's bundled offline clips (no Backblaze needed).

## 0. TL;DR

One-time, install the system libraries:

```bash
sudo apt update
sudo apt install -y python3-tk python3-opencv python3-pil python3-pil.imagetk python3-venv
```

Then every time, just run the launcher — it creates the venv on first use and
starts the dashboard:

```bash
cd ~/toy-mouse-ble
./run.sh             # fullscreen on the touchscreen
./run.sh --pull      # git pull first, then run
./run.sh --windowed  # windowed, for testing
```

(Run `python calibrate_speed.py` once first — see §4.) The rest of this doc
explains what `run.sh` does and how to do it by hand.

## 1. Dependencies

On Raspberry Pi OS **Bookworm** the system Python is "externally managed"
(PEP 668), so a plain `pip install` is blocked. Install OpenCV / Pillow / Tk from
apt (the ARM pip wheels are painful) and put only `bleak` in a venv that can see
the apt packages:

```bash
# system libraries (pulls numpy with opencv)
sudo apt update
sudo apt install -y python3-tk python3-opencv python3-pil python3-pil.imagetk python3-venv

# a venv that INHERITS the apt packages (the --system-site-packages flag is the key)
cd ~/toy-mouse-ble
python3 -m venv --system-site-packages .venv
source .venv/bin/activate

# the only pip dependency
pip install -r requirements.txt          # == bleak
```

Quicker alternative if you don't want a venv: after the apt install,
`pip install bleak --break-system-packages`.

**Sanity check** (the classic failure is a missing `python3-pil.imagetk`):

```bash
python -c "import bleak, cv2, PIL.ImageTk, tkinter; print('deps OK')"
```

## 2. Bluetooth

`bleak` talks to BlueZ over D-Bus (no code change needed). Make sure:

```bash
rfkill unblock bluetooth
systemctl status bluetooth          # should be active
sudo usermod -aG bluetooth $USER    # then log out/in, if connects hit permission errors
```

The Pi 5's built-in radio handles a few links; if connecting 3+ mice is flaky,
use a USB BT dongle.

## 3. Display

Tkinter needs the desktop session. Run from the touchscreen's desktop, or over
SSH after `export DISPLAY=:0`.

## 4. Run it

Easiest — the launcher handles the venv + display for you:

```bash
./run.sh                             # fullscreen (creates .venv on first run)
./run.sh --windowed                  # windowed, for testing
./run.sh --pull                      # git pull first, then run
```

Calibrate once before the first show:

```bash
source .venv/bin/activate
python calibrate_speed.py            # scans, pick a mouse, measure speeds
```

Equivalent manual launch:

```bash
source .venv/bin/activate
python show_dashboard.py             # or: --windowed
```

- `calibrate_speed.py` now **scans and connects to any mouse in range** (picker if
  several; `--mac AA:BB:..` to force one). Do this first so the speed model isn't
  *provisional* — the dashboard's setup status line shows `PROVISIONAL` vs
  `calibrated`.
- In the dashboard: tap mice to connect, pick/shuffle clips, **START**, then PLAY.
  Cage defaults to 0.5×0.5 m / 0.05 m clearance; tune the **min pulse** stepper on
  the hardware if the mice don't move reliably (short-pulse driving in a small cage).

## Notes

- The bundled clips under `clips/` are all the Pi needs — no rclone/Backblaze, no
  `imageio-ffmpeg`. Those are only for (re)extracting new clips on a dev box.
- The Pi 5 has **no hardware H.264 decoder**, so video tiles are software-decoded;
  ~3 simultaneous cameras is comfortable.
