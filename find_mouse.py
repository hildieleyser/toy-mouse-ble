"""Scan for BLE devices and highlight likely toy-mouse candidates.

Usage:  python find_mouse.py
"""
import asyncio
from bleak import BleakScanner

from mouse_protocol import MOUSE_NAME_HINTS

# Catch-all hints — anything that smells like a toy. Augments MOUSE_NAME_HINTS
# from mouse_protocol so the first-ever scan still highlights the right device
# even before you've set DEFAULT_MOUSE_MAC.
GENERIC_HINTS = ("mouse", "rat", "toy", "ble", "pet", "rc", "smart")
HINTS = tuple(set(h.lower() for h in (*MOUSE_NAME_HINTS, *GENERIC_HINTS)))

async def main():
    print("Scanning 8s — power the mouse on, and turn the phone's Bluetooth OFF")
    print("so the toy is not already paired to the app.\n")
    devices = await BleakScanner.discover(timeout=8.0, return_adv=True)

    rows = []
    for addr, (dev, adv) in devices.items():
        name = (dev.name or adv.local_name or "").strip()
        svcs = adv.service_uuids or []
        rssi = adv.rssi if adv.rssi is not None else -999
        rows.append((rssi, addr, name, svcs))
    rows.sort(reverse=True)

    print("=== Likely toy-mouse candidates (named, strongest first) ===")
    named = [r for r in rows if r[2]]
    likely = [r for r in named if any(k in r[2].lower() for k in HINTS)]
    if likely:
        for n, (rssi, addr, name, svcs) in enumerate(likely, 1):
            svc_str = ",".join(svcs) if svcs else ""
            print(f"  {n}. {rssi:>4} dBm  {addr}  {name!r}   {svc_str}")
        if len(likely) > 1:
            print(f"\n  {len(likely)} mice in range — connect to several at once in")
            print("  the panel, or with: python mouse.py fleet <MAC1> <MAC2> ...")
    else:
        print("  (no name matches — see full list below)")

    print(f"\n=== All NAMED devices ({len(named)}) ===")
    for rssi, addr, name, svcs in named:
        svc_str = ",".join(svcs) if svcs else ""
        print(f"  {rssi:>4} dBm  {addr}  {name!r}   {svc_str}")

    print("\n=== Strongest UNNAMED (top 8) ===")
    unnamed = [r for r in rows if not r[2]]
    for rssi, addr, name, svcs in unnamed[:8]:
        svc_str = ",".join(svcs) if svcs else ""
        print(f"  {rssi:>4} dBm  {addr}   {svc_str}")

    print("\nNext: python probe.py <MAC>")

asyncio.run(main())
