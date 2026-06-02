"""Connect and just listen to the notify channel for a while, decoding the
5-byte heartbeat frames (4B [N] 00 [N] B4). Helps work out what N means."""
import asyncio
from collections import Counter
from bleak import BleakClient
from mouse_protocol import DEFAULT_MOUSE_MAC, WRITE_UUID, NOTIFY_UUID, CONNECT_HANDSHAKE

SECONDS = 12.0

async def main():
    seen = Counter()
    async with BleakClient(DEFAULT_MOUSE_MAC) as client:
        print(f"Connected. MTU={client.mtu_size}. Listening {SECONDS:.0f}s...\n")

        def on_notify(_h, data: bytearray):
            b = bytes(data)
            seen[b.hex()] += 1
            # decode 4B [N] 00 [N] B4
            if len(b) == 5 and b[0] == 0x4B and b[4] == 0xB4:
                print(f"  notify {b.hex()}   N=0x{b[1]:02X} ({b[1]})")
            else:
                print(f"  notify {b.hex()}")

        await client.start_notify(NOTIFY_UUID, on_notify)
        if WRITE_UUID and CONNECT_HANDSHAKE:
            await client.write_gatt_char(WRITE_UUID, CONNECT_HANDSHAKE, response=False)

        await asyncio.sleep(SECONDS)

        print("\n=== unique frames seen ===")
        for hexstr, cnt in seen.most_common():
            print(f"  {hexstr}  x{cnt}")

asyncio.run(main())
