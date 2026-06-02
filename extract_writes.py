"""Pull every ATT write to a specific handle from a btsnoop log, in time order.

Usage:  python extract_writes.py path\\to\\btsnoop_hci.log <HANDLE_HEX>

Example: python extract_writes.py btsnoop_hci.log 0x0015

Run parse_snoop.py first to find which handle the app is writing to. Then call
this with that handle to see the raw bytes of every command in order, with the
gap (ms) between consecutive writes. Match the order of frames against the
order you pressed buttons during the recording session.
"""
import struct
import sys
from pathlib import Path

if len(sys.argv) < 3:
    print(__doc__)
    sys.exit(1)
LOG = Path(sys.argv[1])
TARGET_HANDLE = int(sys.argv[2], 16) if sys.argv[2].lower().startswith("0x") else int(sys.argv[2])

data = LOG.read_bytes()
assert data[:8] == b"btsnoop\0", "not a btsnoop file"
off = 16
writes = []
while off < len(data):
    if off + 24 > len(data):
        break
    orig_len, incl_len, flags, drops = struct.unpack(">IIII", data[off:off+16])
    ts_us = struct.unpack(">q", data[off+16:off+24])[0]
    off += 24
    pkt = data[off:off+incl_len]
    off += incl_len
    if not pkt or pkt[0] != 0x02:  # not ACL
        continue
    body = pkt[1:]
    if len(body) < 4: continue
    handle_word, dlen = struct.unpack("<HH", body[:4])
    l2 = body[4:4+dlen]
    if len(l2) < 4: continue
    l2_len, cid = struct.unpack("<HH", l2[:4])
    if cid != 0x0004: continue
    payload = l2[4:4+l2_len]
    if not payload: continue
    op = payload[0]
    if op not in (0x12, 0x52): continue
    if len(payload) < 3: continue
    t_handle = struct.unpack("<H", payload[1:3])[0]
    if t_handle != TARGET_HANDLE: continue
    val = payload[3:]
    writes.append((ts_us, val))

t0 = writes[0][0] if writes else 0
print(f"{len(writes)} writes to handle 0x{TARGET_HANDLE:04X}\n")
print(f"{'#':>3}  {'dt(ms)':>7}  bytes")
prev = t0
for i, (ts, v) in enumerate(writes, 1):
    dt = (ts - prev) / 1000.0
    print(f"{i:>3}  {dt:>7.1f}   {v.hex()}")
    prev = ts
