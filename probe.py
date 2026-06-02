"""Connect to a BLE device and dump its GATT services / characteristics.

Usage:  python probe.py <MAC>

The bits you want from the output:
  * a characteristic with 'write' or 'write-without-response' in props
    -> set WRITE_UUID  in mouse_protocol.py to that characteristic's uuid
  * a characteristic with 'notify' or 'indicate' in props
    -> set NOTIFY_UUID in mouse_protocol.py to that characteristic's uuid

Cross-check those UUIDs against the write handle you saw in parse_snoop.py;
they should be the same characteristic.
"""
import asyncio
import sys
from bleak import BleakClient

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)
MAC = sys.argv[1]

async def main():
    print(f"Connecting to {MAC}...")
    try:
        async with BleakClient(MAC, timeout=15.0) as client:
            print(f"Connected. MTU={client.mtu_size}\n")
            print("=== GATT services / characteristics ===")
            for svc in client.services:
                print(f"  Service {svc.uuid}")
                for ch in svc.characteristics:
                    props = ",".join(ch.properties)
                    print(f"    handle 0x{ch.handle:04X}  uuid={ch.uuid}  props={props}")
                    for d in ch.descriptors:
                        print(f"      descr handle 0x{d.handle:04X}  uuid={d.uuid}")
            print()
            print("Pick the characteristic the app keeps writing to (from parse_snoop.py),")
            print("set its UUID as WRITE_UUID in mouse_protocol.py, and likewise for NOTIFY_UUID.")
    except Exception as e:
        print(f"Failed: {e}")

asyncio.run(main())
