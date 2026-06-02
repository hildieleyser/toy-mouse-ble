"""One-off: collapse extract_writes.py output into run-length groups so we can
match each block of repeats to a single button press."""
import re, subprocess, sys, itertools

out = subprocess.check_output(
    [sys.executable,
     r"C:\Users\labra\toy-mouse-ble\extract_writes.py",
     r"C:\Users\labra\toy-mouse-ble\btsnoop_hci.log",
     "0x0009"],
    text=True,
)

rx = re.compile(r"\s*\d+\s+(\S+)\s+([0-9a-f]+)")
recs = []
for line in out.splitlines():
    m = rx.match(line)
    if m:
        recs.append(m.groups())  # (dt_ms_str, bytes_hex)

print(f"{len(recs)} writes to 0x0009 (run-length collapsed):\n")
print(f"{'#':>3}  {'count':>5}  {'gap_ms':>6}  bytes")
for i, (key, grp) in enumerate(itertools.groupby(recs, key=lambda x: x[1]), 1):
    items = list(grp)
    # the first dt in each group is the gap *to that group's first frame* — useful
    print(f"{i:>3}  {len(items):>5}  {items[0][0]:>6}  {key}")
