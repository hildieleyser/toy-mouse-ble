"""Measure the toy's speed at several SPEED bytes so speed_model.py can map
real-mouse velocities onto byte+duty accurately.

How it works: for each byte you choose, the toy drives FORWARD for a fixed
number of seconds, then stops. You measure how far it travelled (a tape measure
on the floor, or count floor tiles) and type the distance in metres. The result
is appended to speed_calibration.json.

Tips:
  * Give it a long straight runway and mark the start line.
  * Do the lowest bytes first to find the 'deadband' — the byte below which the
    wheels don't turn at all. Enter 0 distance for those; the model uses the
    lowest *moving* byte as the floor.
  * 3-5 points across the range (e.g. 4, 6, 8, 11, 19) is plenty.

Usage:
    python calibrate_speed.py            # interactive, default 2.0 s runs
    python calibrate_speed.py 1.5        # 1.5 s runs
"""
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from bleak import BleakClient

from mouse_protocol import (
    DEFAULT_MOUSE_MAC, WRITE_UUID, CONNECT_HANDSHAKE,
    _frame, DIR_FORWARD, DIR_STOP, SPEED_NORMAL,
)
from speed_model import CALIB_PATH

RUN_SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
PULSE_HZ = 20  # resend the drive frame at 20 Hz so the toy keeps moving

def load_existing() -> dict:
    if CALIB_PATH.exists():
        try:
            return json.loads(CALIB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"points": []}

async def drive_for(client, dir_byte, speed_byte, seconds):
    frame = _frame(dir_byte, speed_byte)
    stop = _frame(DIR_STOP, SPEED_NORMAL)
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        await client.write_gatt_char(WRITE_UUID, frame, response=False)
        await asyncio.sleep(1 / PULSE_HZ)
    for _ in range(4):
        await client.write_gatt_char(WRITE_UUID, stop, response=False)
        await asyncio.sleep(0.02)

async def main():
    data = load_existing()
    pts = {int(b): float(v) for b, v in data.get("points", [])}
    print(f"Connecting to {DEFAULT_MOUSE_MAC}...")
    async with BleakClient(DEFAULT_MOUSE_MAC) as client:
        if CONNECT_HANDSHAKE:
            await client.write_gatt_char(WRITE_UUID, CONNECT_HANDSHAKE, response=False)
        print(f"Connected. Each run drives FORWARD for {RUN_SECONDS:.1f}s.\n"
              f"Enter a byte (4..19), or 'q' to finish.\n")
        loop = asyncio.get_event_loop()
        while True:
            raw = (await loop.run_in_executor(None, input, "byte> ")).strip().lower()
            if raw in ("q", "quit", ""):
                break
            try:
                b = int(raw, 0)
            except ValueError:
                print("  enter an integer like 8 or 0x0b")
                continue
            if not (0 <= b <= 19):
                print("  out of range (0..19)")
                continue
            input(f"  Place toy on the start line, press Enter to drive at byte {b}...")
            await drive_for(client, DIR_FORWARD, b, RUN_SECONDS)
            dist = (await loop.run_in_executor(None, input,
                    "  distance travelled (metres, 0 if it didn't move): ")).strip()
            try:
                d = float(dist)
            except ValueError:
                print("  not a number, skipping")
                continue
            mps = d / RUN_SECONDS
            pts[b] = mps
            print(f"  byte 0x{b:02X} -> {mps:.3f} m/s\n")

    out = {
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "run_seconds": RUN_SECONDS,
        "points": sorted([[b, v] for b, v in pts.items()]),
    }
    # deadband = lowest byte that actually moved
    moving = [b for b, v in pts.items() if v > 0]
    if moving:
        out["deadband_byte"] = min(moving)
    CALIB_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nSaved {len(pts)} points to {CALIB_PATH.name}")
    print("Run `python speed_model.py` to see the resulting mapping.")

if __name__ == "__main__":
    asyncio.run(main())
