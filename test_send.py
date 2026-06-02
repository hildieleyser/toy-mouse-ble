"""Send the Stop command to the mouse and wait for an ack.

Usage:  python test_send.py [MAC]   (MAC defaults to DEFAULT_MOUSE_MAC)

This is the lowest-risk first contact — Stop will not run the motors. If you
see the notify line print and (optionally) the EXPECTED_ACK_HEX match, the
protocol is correct.
"""
import asyncio
import sys

from bleak import BleakClient

from mouse_protocol import (
    DEFAULT_MOUSE_MAC, WRITE_UUID, NOTIFY_UUID,
    EXPECTED_ACK_HEX, build_stop, protocol_ready,
)

MAC = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MOUSE_MAC

async def main():
    ok, msg = protocol_ready()
    if not ok:
        print(f"NOTE: {msg}")
    if not MAC:
        print("No MAC provided and DEFAULT_MOUSE_MAC is empty — pass one or edit mouse_protocol.py.")
        return

    got_ack = asyncio.Event()
    got_ack_bytes = b""
    async with BleakClient(MAC) as client:
        print(f"Connected. MTU={client.mtu_size}")

        def on_notify(handle, data: bytearray):
            nonlocal got_ack_bytes
            print(f"  <- notify: {data.hex()}")
            got_ack_bytes = bytes(data)
            got_ack.set()

        if NOTIFY_UUID:
            try:
                await client.start_notify(NOTIFY_UUID, on_notify)
            except Exception as e:
                print(f"(notify subscribe failed: {e})")
        else:
            print("(no NOTIFY_UUID set — proceeding without subscribing)")

        payload = build_stop()
        print(f"  -> sending Stop: {payload.hex()}")
        await client.write_gatt_char(WRITE_UUID, payload, response=False)

        try:
            await asyncio.wait_for(got_ack.wait(), timeout=3.0)
            if EXPECTED_ACK_HEX and got_ack_bytes.hex() != EXPECTED_ACK_HEX:
                print(f"\n*** got an ack, but bytes differ from EXPECTED_ACK_HEX={EXPECTED_ACK_HEX} ***")
            else:
                print("\n*** SUCCESS — toy acked. ***")
        except asyncio.TimeoutError:
            if NOTIFY_UUID:
                print("\n!!! Wrote command, but no ack within 3s. !!!")
            else:
                print("\n(write succeeded; no notify channel set up to listen on)")

asyncio.run(main())
