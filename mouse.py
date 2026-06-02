"""Toy mouse BLE controller — CLI.

Usage:
    python mouse.py scan                  # find mouse MAC(s)
    python mouse.py drive <MAC>           # interactive driving session (one mouse)
    python mouse.py fleet <MAC1> <MAC2>.. # drive several mice at once (broadcast)

Type any of the direction names (forward, back, left, right, stop), or `q` to
disconnect. If the protocol is in SPEED_AWARE mode, prefix with a number
(`50 forward` to drive forward at 50% throttle).
"""
import asyncio
import sys

from bleak import BleakClient, BleakScanner

from mouse_protocol import (
    DEFAULT_MOUSE_MAC, WRITE_UUID, NOTIFY_UUID, MOUSE_NAME_HINTS,
    CONNECT_HANDSHAKE,
    DIRECTIONS, USE_MODE, build, build_stop, build_set_speed, protocol_ready,
)

def _is_mouse_name(name: str) -> bool:
    n = (name or "").lower()
    return any(h.lower() in n for h in MOUSE_NAME_HINTS)

async def scan(seconds: float = 8.0):
    print(f"Scanning {seconds}s — power the mouse on, phone Bluetooth OFF.")
    devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = [(adv.rssi or -999, addr, dev.name or adv.local_name or "")
            for addr, (dev, adv) in devices.items()]
    rows.sort(reverse=True)
    if not rows:
        print("Nothing found.")
        return
    print(f"\n{len(rows)} devices (strongest first):")
    print(f"  {'RSSI':>5}  {'ADDRESS':<19}  NAME")
    for rssi, addr, name in rows:
        tag = " *" if _is_mouse_name(name) else "  "
        print(f"{tag}{rssi:>5}  {addr:<19}  {name!r}")
    print("\nThen: python mouse.py drive <MAC>")

async def drive(mac: str):
    ok, msg = protocol_ready()
    if not ok:
        print(f"NOTE: {msg}\n")
    print(f"Connecting to {mac}...  (mode={USE_MODE})")
    async with BleakClient(mac) as client:
        print(f"Connected. MTU={client.mtu_size}")
        if NOTIFY_UUID:
            try:
                await client.start_notify(NOTIFY_UUID,
                    lambda h, d: print(f"  <- notify h=0x{h:04X} {d.hex()}"))
            except Exception as e:
                print(f"(notify subscribe failed: {e})")
        if WRITE_UUID and CONNECT_HANDSHAKE:
            try:
                await client.write_gatt_char(WRITE_UUID, CONNECT_HANDSHAKE, response=False)
                print(f"  -> handshake   {CONNECT_HANDSHAKE.hex()}")
            except Exception as e:
                print(f"(handshake failed: {e})")

        print(f"\nCommands: {', '.join(DIRECTIONS)}, stop, q")
        if USE_MODE == "SPEED_AWARE":
            print("  Prefix with a number 0..100 to set speed, e.g. '60 forward'.")
        else:
            print("  (FIXED_FRAME mode; speed is set persistently with 'speed <N>')")

        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    line = await loop.run_in_executor(None, input, "> ")
                except (EOFError, KeyboardInterrupt):
                    break
                line = line.strip().lower()
                if line in ("q", "quit", "exit"):
                    break
                if not line:
                    continue
                # parse `<speed> <cmd>` or `<cmd>` or `speed <N>`
                parts = line.split()
                speed = 100
                cmd = parts[0]
                if parts[0] == "speed" and len(parts) >= 2 and parts[1].isdigit():
                    payload = build_set_speed(int(parts[1]))
                    if payload is None:
                        print("(no persistent speed command in this protocol)")
                        continue
                    await client.write_gatt_char(WRITE_UUID, payload, response=False)
                    print(f"  -> speed={parts[1]}  {payload.hex()}")
                    continue
                if parts[0].lstrip("-").isdigit() and len(parts) >= 2:
                    speed = int(parts[0])
                    cmd = parts[1]
                if cmd == "stop":
                    payload = build_stop()
                elif cmd in DIRECTIONS:
                    payload = build(cmd, speed)
                else:
                    print(f"unknown; try: {', '.join(DIRECTIONS)}, stop, q")
                    continue
                await client.write_gatt_char(WRITE_UUID, payload, response=False)
                print(f"  -> {cmd:<8} speed={speed:>3}  {payload.hex()}")
        finally:
            try:
                await client.write_gatt_char(WRITE_UUID, build_stop(), response=True)
                print("Sent Stop. Disconnecting.")
            except Exception:
                pass

async def fleet(macs: list[str]):
    """Connect several mice and broadcast each command to all of them."""
    from mouse_fleet import MouseFleet
    done = asyncio.Event()
    f = MouseFleet(log_cb=lambda m: print(m))
    for mac in macs:
        await f.connect(mac)
    slugs = f.connected_slugs
    if not slugs:
        print("No mice connected.")
        return
    print(f"\nFleet of {len(slugs)} connected: {', '.join(slugs)}")
    print(f"Commands broadcast to ALL. {', '.join(DIRECTIONS)}, stop, q")
    if USE_MODE == "SPEED_AWARE":
        print("  Prefix with 0..100 to set speed, e.g. '60 forward'.")
    loop = asyncio.get_event_loop()
    try:
        while True:
            line = (await loop.run_in_executor(None, input, "fleet> ")).strip().lower()
            if line in ("q", "quit", "exit"):
                break
            if not line:
                continue
            parts = line.split()
            speed, cmd = 100, parts[0]
            if parts[0].lstrip("-").isdigit() and len(parts) >= 2:
                speed, cmd = int(parts[0]), parts[1]
            if cmd == "stop":
                await f.broadcast(build_stop(), "stop")
            elif cmd in DIRECTIONS:
                await f.broadcast(build(cmd, speed), f"{cmd}@{speed}")
            else:
                print(f"unknown; try: {', '.join(DIRECTIONS)}, stop, q")
    finally:
        await f.stop_all()
        await f.disconnect_all()
        print("Fleet stopped + disconnected.")

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "scan":
        asyncio.run(scan())
    elif cmd == "drive":
        mac = sys.argv[2] if len(sys.argv) >= 3 else DEFAULT_MOUSE_MAC
        if not mac:
            print("usage: python mouse.py drive <MAC>   (or set DEFAULT_MOUSE_MAC)")
            return
        asyncio.run(drive(mac))
    elif cmd == "fleet":
        macs = sys.argv[2:]
        if not macs:
            print("usage: python mouse.py fleet <MAC1> <MAC2> ...")
            return
        asyncio.run(fleet(macs))
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
