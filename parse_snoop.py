"""Parse Android btsnoop_hci.log and summarise BLE GATT writes / notifies.

Usage:  python parse_snoop.py path\\to\\btsnoop_hci.log

What to look for in the output:
  * The "GATT writes / notifies" section ranks (op, handle) pairs by frequency.
    The handle the app spammed during driving is your WRITE handle.
    The handle that came back acks is your NOTIFY handle.
  * "First 40 writes/notifies in order" lets you align frames to the buttons
    you pressed in time order.
"""
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)
LOG = Path(sys.argv[1])

HCI_CMD = 0x01
HCI_ACL = 0x02
HCI_EVT = 0x04

ATT_OPCODES = {
    0x01: "Error Response",
    0x02: "Exchange MTU Req",
    0x03: "Exchange MTU Rsp",
    0x08: "Read By Type Req",
    0x09: "Read By Type Rsp",
    0x0A: "Read Req",
    0x0B: "Read Rsp",
    0x10: "Read By Group Type Req",
    0x11: "Read By Group Type Rsp",
    0x12: "Write Req",
    0x13: "Write Rsp",
    0x1B: "Handle Value Notify",
    0x1D: "Handle Value Indicate",
    0x52: "Write Cmd",
}

def parse_btsnoop(path: Path):
    data = path.read_bytes()
    if data[:8] != b"btsnoop\0":
        raise SystemExit(f"Not a btsnoop file (magic={data[:8]!r})")
    version, dlt = struct.unpack(">II", data[8:16])
    print(f"btsnoop v{version}, datalink type {dlt}, {len(data)} bytes total")
    off = 16
    records = []
    while off < len(data):
        if off + 24 > len(data):
            break
        orig_len, incl_len, flags, drops = struct.unpack(">IIII", data[off:off+16])
        ts_us = struct.unpack(">q", data[off+16:off+24])[0]
        off += 24
        pkt = data[off:off+incl_len]
        off += incl_len
        records.append((ts_us, flags, pkt))
    return records

def parse_acl(pkt: bytes):
    if len(pkt) < 4:
        return None
    handle_word, dlen = struct.unpack("<HH", pkt[:4])
    handle = handle_word & 0x0FFF
    body = pkt[4:4+dlen]
    if len(body) < 4:
        return None
    l2_len, cid = struct.unpack("<HH", body[:4])
    l2_payload = body[4:4+l2_len]
    return handle, l2_len, cid, l2_payload

def parse_le_meta_event(pkt: bytes):
    if len(pkt) < 3:
        return None
    evt_code, plen = pkt[0], pkt[1]
    if evt_code != 0x3E:
        return None
    sub = pkt[2]
    return sub, pkt[3:3+plen-1]

records = parse_btsnoop(LOG)
print(f"Parsed {len(records)} records\n")

att_writes = []
conn_handles = {}
adv_seen = Counter()
adv_names = {}

for ts, flags, pkt in records:
    if not pkt:
        continue
    hci_type = pkt[0]
    body = pkt[1:]
    direction = "TX" if (flags & 1) == 0 else "RX"

    if hci_type == HCI_EVT:
        meta = parse_le_meta_event(body)
        if meta is None:
            continue
        sub, sub_body = meta
        if sub == 0x01 and len(sub_body) >= 11:
            status = sub_body[0]
            conn_handle = struct.unpack("<H", sub_body[1:3])[0]
            peer_addr = sub_body[5:11][::-1]
            addr_str = ":".join(f"{b:02X}" for b in peer_addr)
            conn_handles[conn_handle] = addr_str
            print(f"[{ts}] LE Conn Complete handle=0x{conn_handle:04X} peer={addr_str} status={status}")
        elif sub == 0x02 and len(sub_body) >= 1:
            n = sub_body[0]
            off = 1
            for _ in range(n):
                if off + 9 > len(sub_body):
                    break
                evt_type = sub_body[off]; off += 1
                addr_type = sub_body[off]; off += 1
                addr = sub_body[off:off+6][::-1]; off += 6
                dlen = sub_body[off]; off += 1
                adv_data = sub_body[off:off+dlen]; off += dlen
                if off >= len(sub_body): break
                rssi = sub_body[off]; off += 1
                addr_str = ":".join(f"{b:02X}" for b in addr)
                adv_seen[addr_str] += 1
                i = 0
                while i < len(adv_data):
                    l = adv_data[i]
                    if l == 0 or i + 1 + l > len(adv_data):
                        break
                    t = adv_data[i+1]
                    v = adv_data[i+2:i+1+l]
                    if t in (0x08, 0x09):
                        try:
                            adv_names[addr_str] = v.decode("utf-8", errors="replace")
                        except Exception:
                            pass
                    i += 1 + l
    elif hci_type == HCI_ACL:
        parsed = parse_acl(body)
        if parsed is None:
            continue
        handle, l2_len, cid, payload = parsed
        if cid != 0x0004 or not payload:
            continue
        op = payload[0]
        opname = ATT_OPCODES.get(op, f"0x{op:02X}")
        if op in (0x12, 0x52, 0x1B, 0x1D) and len(payload) >= 3:
            t_handle = struct.unpack("<H", payload[1:3])[0]
            value = payload[3:]
            att_writes.append((ts, direction, handle, opname, t_handle, value))

print(f"\n=== Devices seen in advertising ({len(adv_seen)}) ===")
for addr, cnt in adv_seen.most_common(15):
    nm = adv_names.get(addr, "")
    print(f"  {addr}  count={cnt}  name={nm!r}")

print(f"\n=== Connected peers ===")
for h, addr in conn_handles.items():
    print(f"  conn_handle=0x{h:04X}  peer={addr}  name={adv_names.get(addr, '?')!r}")

print(f"\n=== GATT writes / notifies ({len(att_writes)} total) ===")
by_handle = defaultdict(list)
for ts, d, ch, op, th, val in att_writes:
    by_handle[(op, th)].append(val)

for (op, th), vals in sorted(by_handle.items(), key=lambda kv: -len(kv[1])):
    print(f"  {op:25s}  handle=0x{th:04X}  count={len(vals)}")
    uniq, seen = [], set()
    for v in vals:
        h = bytes(v).hex()
        if h not in seen:
            seen.add(h); uniq.append(v)
        if len(uniq) >= 12:
            break
    for v in uniq:
        print(f"      {v.hex()}  ({len(v)} bytes)")

print("\n=== First 40 writes/notifies in order ===")
for ts, d, ch, op, th, val in att_writes[:40]:
    print(f"  t={ts}  {d}  {op:22s} handle=0x{th:04X}  val={val.hex()}")
