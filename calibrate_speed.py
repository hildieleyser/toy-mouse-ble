"""Measure the toy's speed at several SPEED bytes so speed_model.py can map
real-mouse velocities onto byte+duty accurately.

How it works: it scans for a mouse (any toy in range, not a fixed MAC), connects,
and for each byte you choose drives FORWARD for a fixed number of seconds then
stops. You measure how far it travelled (a tape measure on the floor, or count
floor tiles) and type the distance in metres. The result is appended to
speed_calibration.json.

Tips:
  * Give it a long straight runway and mark the start line.
  * Do the lowest bytes first to find the 'deadband' — the byte below which the
    wheels don't turn at all. Enter 0 distance for those; the model uses the
    lowest *moving* byte as the floor.
  * 3-5 points across the range (e.g. 4, 6, 8, 11, 19) is plenty.

Usage:
    python calibrate_speed.py                 # scan, pick a mouse, 2.0 s runs
    python calibrate_speed.py 1.5             # 1.5 s runs
    python calibrate_speed.py --mac AA:BB:..  # skip the scan, use this MAC
"""
import argparse
import asyncio
import json
import time
from datetime import datetime

from bleak import BleakClient, BleakScanner

from mouse_protocol import (
    WRITE_UUID, CONNECT_HANDSHAKE,
    _frame, DIR_FORWARD, DIR_STOP, SPEED_NORMAL,
)
from mouse_fleet import is_mouse_name
from mouse_roster import describe_mac
from speed_model import CALIB_PATH

PULSE_HZ = 20  # resend the drive frame at 20 Hz so the toy keeps moving


def parse_args():
    p = argparse.ArgumentParser(description="Calibrate toy-mouse speed bytes.")
    p.add_argument("seconds", nargs="?", type=float, default=2.0,
                   help="forward run duration per byte (default 2.0)")
    p.add_argument("--mac", default=None,
                   help="connect to this MAC directly instead of scanning")
    p.add_argument("--scan", type=float, default=8.0,
                   help="scan duration in seconds (default 8)")
    return p.parse_args()


def load_existing() -> dict:
    if CALIB_PATH.exists():
        try:
            return json.loads(CALIB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"points": []}


async def pick_mouse(forced_mac: str | None, scan_s: float):
    """Return (mac, target) for a mouse to calibrate, or (None, None) if none.

    `target` is the BLEDevice from the scan when available (more reliable to
    connect to than a bare address), else the MAC string."""
    if forced_mac:
        print(f"Using MAC {forced_mac} (skipping scan).")
        return forced_mac, forced_mac
    print(f"Scanning {scan_s:.0f}s for mice — power one on "
          "(and turn the phone's Bluetooth off so the toy isn't paired)...")
    devices = await BleakScanner.discover(timeout=scan_s, return_adv=True)
    cands = []
    for addr, (dev, adv) in devices.items():
        name = dev.name or adv.local_name or ""
        if is_mouse_name(name):
            rssi = adv.rssi if adv.rssi is not None else -999
            cands.append((rssi, addr, name, dev))
    cands.sort(reverse=True)               # strongest signal first
    if not cands:
        print("No mice found. Power one on and retry, or pass --mac AA:BB:CC:DD:EE:FF.")
        return None, None
    if len(cands) == 1:
        rssi, addr, name, dev = cands[0]
        print(f"Found {describe_mac(addr)}  {addr}  ({rssi} dBm, {name!r}).")
        return addr, dev
    print(f"Found {len(cands)} mice:")
    for i, (rssi, addr, name, dev) in enumerate(cands, 1):
        print(f"  {i}. {describe_mac(addr):<12} {addr}  {rssi:>4} dBm  {name!r}")
    while True:
        sel = input(f"Pick one [1-{len(cands)}, Enter = 1 (strongest)]: ").strip()
        if sel == "":
            idx = 0
            break
        if sel.isdigit() and 1 <= int(sel) <= len(cands):
            idx = int(sel) - 1
            break
        print("  invalid choice")
    rssi, addr, name, dev = cands[idx]
    return addr, dev


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
    args = parse_args()
    run_seconds = args.seconds
    data = load_existing()
    pts = {int(b): float(v) for b, v in data.get("points", [])}

    mac, target = await pick_mouse(args.mac, args.scan)
    if target is None:
        return
    print(f"Connecting to {mac}...")
    async with BleakClient(target) as client:
        if CONNECT_HANDSHAKE:
            await client.write_gatt_char(WRITE_UUID, CONNECT_HANDSHAKE, response=False)
        print(f"Connected. Each run drives FORWARD for {run_seconds:.1f}s.\n"
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
            await drive_for(client, DIR_FORWARD, b, run_seconds)
            dist = (await loop.run_in_executor(None, input,
                    "  distance travelled (metres, 0 if it didn't move): ")).strip()
            try:
                d = float(dist)
            except ValueError:
                print("  not a number, skipping")
                continue
            mps = d / run_seconds
            pts[b] = mps
            print(f"  byte 0x{b:02X} -> {mps:.3f} m/s\n")

    out = {
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "run_seconds": run_seconds,
        "mac": mac,
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
