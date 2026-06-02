"""Drive ONE mouse through a simple timed command sequence (open-loop).

Unlike the trajectory player (trajectory.py + choreography.py, which fits a CSV
path and uses a pursuit controller), this sends a fixed script of
(direction, seconds) steps at a single reduced speed -- a brief turn, go
straight, a brief turn the other way, go straight. Easy to eyeball and tune.

Default "weave" block (one repeat):
    left  TURN_S    ->  forward STRAIGHT_S  ->  right TURN_S  ->  forward STRAIGHT_S

Each step's frame is re-sent every RESEND_S so the toy doesn't time out
mid-step (mirrors the panel's hold-to-drive). A Stop is always sent at the end
and on Ctrl-C.

Usage:
    python drive_sequence.py                 # mouse from mouse_config.json, defaults
    python drive_sequence.py 4               # mouse #4 from the roster
    python drive_sequence.py 4 --speed 35 --straight 2.0 --turn 0.1 --repeat 3
    python drive_sequence.py 98:3A:F2:CA:5C:5C   # explicit MAC
    python drive_sequence.py 2 --dry-run     # print the plan, don't connect

NOTE: only one BLE central can hold a mouse at a time. If the panel is connected
to this mouse, disconnect it there first (or target a different mouse), or the
connect here will fail.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner

import mouse_roster as R
from mouse_protocol import (
    WRITE_UUID, NOTIFY_UUID, CONNECT_HANDSHAKE, DEFAULT_MOUSE_MAC,
    build, build_stop,
)

RESEND_S = 0.1          # re-send the current frame this often within a step
DEADBAND_PCT = 25       # below ~this %, wheels likely won't turn (see speed_model)


def resolve_mac(token: str | None) -> tuple[str, str]:
    """Return (mac, label). token may be a number 1-6, a MAC, or None."""
    if token and ":" in token:                     # explicit MAC
        return token, R.describe_mac(token)
    if token and token.isdigit():                  # roster number
        for m in R.load_roster():
            if m["number"] == int(token):
                if not m["mac"]:
                    sys.exit(f"Mouse #{token} has no MAC recorded yet "
                             f"(fill it in mouse_config.json).")
                return m["mac"], R.describe_mac(m["mac"])
        sys.exit(f"No mouse #{token} in the roster.")
    # default: the single mouse in config (falls back to protocol default)
    import json
    from pathlib import Path
    cfg = {}
    p = Path(__file__).with_name("mouse_config.json")
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    mac = cfg.get("mouse_mac") or DEFAULT_MOUSE_MAC
    return mac, R.describe_mac(mac)


def build_steps(turn_s: float, straight_s: float, repeat: int,
                settle_s: float = 0.0) -> list[tuple[str, float]]:
    block = [("left", turn_s), ("forward", straight_s),
             ("right", turn_s), ("forward", straight_s)]
    steps = block * max(1, repeat)
    if settle_s <= 0:
        return steps
    # Insert a brief Stop between every motion step so forward momentum dies
    # before a pivot -- the turn is then a clean in-place rotation, not an arc.
    woven: list[tuple[str, float]] = []
    for i, st in enumerate(steps):
        if i > 0:
            woven.append(("stop", settle_s))
        woven.append(st)
    return woven


async def run(mac: str, label: str, speed: int, steps: list[tuple[str, float]]):
    print(f"Connecting to {label} {mac} ...")
    async with BleakClient(await _target(mac)) as client:
        print(f"Connected (MTU={client.mtu_size}).")
        if NOTIFY_UUID:
            try:
                await client.start_notify(NOTIFY_UUID, lambda *_: None)
            except Exception:
                pass
        if WRITE_UUID and CONNECT_HANDSHAKE:
            try:
                await client.write_gatt_char(WRITE_UUID, CONNECT_HANDSHAKE, response=False)
            except Exception as e:
                print(f"(handshake failed: {e})")
        try:
            for i, (direction, secs) in enumerate(steps, 1):
                frame = build_stop() if direction == "stop" else build(direction, speed)
                spd = "" if direction == "stop" else f" @ {speed}%"
                print(f"  [{i:>2}/{len(steps)}] {direction:<7}{spd:<7} {secs:>4.2f}s")
                # send + hold (re-send) for the step duration
                elapsed = 0.0
                while elapsed < secs:
                    await client.write_gatt_char(WRITE_UUID, frame, response=False)
                    dt = min(RESEND_S, secs - elapsed)
                    await asyncio.sleep(dt)
                    elapsed += dt
        finally:
            try:
                await client.write_gatt_char(WRITE_UUID, build_stop(), response=True)
                print("Stop sent. Disconnecting.")
            except Exception:
                pass


async def _target(mac: str):
    """These toys stop advertising when idle; re-discover so connect succeeds."""
    try:
        dev = await BleakScanner.find_device_by_address(mac, timeout=8.0)
        return dev if dev is not None else mac
    except Exception:
        return mac


def main():
    ap = argparse.ArgumentParser(description="Drive one mouse through a timed weave.")
    ap.add_argument("mouse", nargs="?", default=None,
                    help="roster number 1-6, a MAC, or omit for config default")
    ap.add_argument("--speed", type=int, default=30, help="0-100%% (default 30)")
    ap.add_argument("--turn", type=float, default=0.1, help="seconds per turn (default 0.1)")
    ap.add_argument("--straight", type=float, default=1.5,
                    help="seconds going straight between turns (default 1.5)")
    ap.add_argument("--repeat", type=int, default=1, help="weave blocks (default 1)")
    ap.add_argument("--settle", type=float, default=0.1,
                    help="seconds stopped between steps so pivots don't arc "
                         "(default 0.1; 0 = no settle)")
    ap.add_argument("--dry-run", action="store_true", help="print plan, don't connect")
    a = ap.parse_args()

    mac, label = resolve_mac(a.mouse)
    steps = build_steps(a.turn, a.straight, a.repeat, a.settle)
    total = sum(s for _, s in steps)
    print(f"Target : {label}  {mac}")
    settle_txt = f", settle {a.settle}s between steps" if a.settle > 0 else ""
    print(f"Plan   : {a.repeat}x [left {a.turn}s, straight {a.straight}s, "
          f"right {a.turn}s, straight {a.straight}s] @ {a.speed}%{settle_txt}  "
          f"(~{total:.1f}s total)")
    if a.speed < DEADBAND_PCT:
        print(f"WARNING: {a.speed}% is below the ~{DEADBAND_PCT}% stiction floor — "
              f"the wheels may not turn. Bump --speed up if it doesn't move.")
    if a.dry_run:
        for i, (d, s) in enumerate(steps, 1):
            print(f"  [{i:>2}] {d:<7} {s:.2f}s")
        return
    try:
        asyncio.run(run(mac, label, a.speed, steps))
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()
