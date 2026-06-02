"""Canonical roster: which physical mouse is which.

Every mouse advertises the same BLE name ("pets"), so the only stable identity
is the MAC. This module maps each MAC to a human number (1-6), a stable slug
used as the fleet dict key / log prefix (so the same toy is always e.g.
"mouse3", not whatever connection-order slot it happened to grab), and a faulty
flag.

The data lives in mouse_config.json under "mice" so it's user-editable; this
module loads + indexes it, with a built-in default so labels work even before
the config has been written. MAC #6 ("the remaining one") is not yet known —
fill it in in mouse_config.json once it's been scanned.
"""
from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("mouse_config.json")

# Built-in default roster (labelled 2026-05-29). number -> MAC.
DEFAULT_MICE = [
    {"number": 1, "mac": "8E:0C:A4:98:D7:94", "faulty": False},
    {"number": 2, "mac": "98:3A:F2:CA:5C:5C", "faulty": False},
    {"number": 3, "mac": "90:DD:61:6F:5F:FB", "faulty": True},   # faulty
    {"number": 4, "mac": "74:93:6A:5B:AF:C7", "faulty": False},
    {"number": 5, "mac": "99:E6:D1:FA:F9:9B", "faulty": False},
    {"number": 6, "mac": None,                "faulty": False},  # MAC TBD
]


def _norm(mac: str | None) -> str:
    return (mac or "").strip().upper()


def slug_for_number(n: int) -> str:
    return f"mouse{n}"


def load_roster() -> list[dict]:
    """Roster from mouse_config.json["mice"], or the built-in default.

    Each entry is normalised to {number, slug, mac (upper or None), faulty}.
    """
    mice = None
    if CONFIG_PATH.exists():
        try:
            mice = json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("mice")
        except Exception:
            mice = None
    if not mice:
        mice = [dict(m) for m in DEFAULT_MICE]
    out = []
    for m in mice:
        n = int(m["number"])
        out.append({
            "number": n,
            "slug": m.get("slug") or slug_for_number(n),
            "mac": _norm(m.get("mac")) or None,
            "faulty": bool(m.get("faulty", False)),
        })
    out.sort(key=lambda m: m["number"])
    return out


def by_mac(roster: list[dict] | None = None) -> dict[str, dict]:
    roster = roster if roster is not None else load_roster()
    return {m["mac"]: m for m in roster if m["mac"]}


def entry_for_mac(mac: str, roster: list[dict] | None = None) -> dict | None:
    return by_mac(roster).get(_norm(mac))


def slug_for_mac(mac: str, roster: list[dict] | None = None) -> str | None:
    """Stable slug for a known MAC, or None if it isn't in the roster."""
    e = entry_for_mac(mac, roster)
    return e["slug"] if e else None


def describe_mac(mac: str, roster: list[dict] | None = None) -> str:
    """Short human label for a MAC: '#3 (FAULTY)', '#1', or '(unlabeled)'."""
    e = entry_for_mac(mac, roster)
    if not e:
        return "(unlabeled)"
    return f"#{e['number']}" + (" (FAULTY)" if e["faulty"] else "")


if __name__ == "__main__":
    print(f"{'NUM':>3}  {'SLUG':<7}  {'MAC':<19}  STATUS")
    for m in load_roster():
        status = "FAULTY" if m["faulty"] else ("(MAC unknown)" if not m["mac"] else "ok")
        print(f"{m['number']:>3}  {m['slug']:<7}  {m['mac'] or '(unknown)':<19}  {status}")
