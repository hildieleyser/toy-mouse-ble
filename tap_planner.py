"""Turn-by-turn TAP planner: trajectory -> discrete mouse instructions.

The toy is a tap-driven RC car, not a continuously-steered robot. The mobile app
controls it by *tapping* direction buttons: a tap = "send direction, then stop".
Turns are in-place rotations (left/right taps); straights are forward taps.

The old TrajectoryPlayer fought this model: it emitted a fresh continuous
heading-correction every video frame (30x/s), blending tiny turns into forward
duty-cycle pulses. That never yields a clean "forward, left, forward, right,
forward x3" sequence.

This module instead does what a human does reading a route:

  1. simplify()  — Ramer-Douglas-Peucker collapses the dense path into a few
                   straight legs joined at real corners (drops GPS-style wiggle).
  2. plan()      — walk the legs: each corner becomes a number of LEFT or RIGHT
                   taps (turn angle / degrees-per-tap); each leg becomes a number
                   of FORWARD taps (leg length / metres-per-tap).

Output is a flat list of TapGroup(direction, count) — exactly the instruction
stream you'd type into the app. The two physical constants (m per forward tap,
degrees per turn tap) are the calibration knobs; defaults are placeholders until
calibrate_speed.py measures them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class TapGroup:
    direction: str          # "forward" | "left" | "right"
    count: int              # number of taps
    # diagnostics (cage units / degrees) for display + sanity-checking
    meters: float = 0.0     # leg length (forward groups)
    degrees: float = 0.0    # signed turn angle (turn groups, +left / -right)
    seconds: float = 0.0    # intended traversal time of this leg (forward groups)

    def __repr__(self):
        if self.direction == "forward":
            t = f", {self.seconds:.1f}s" if self.seconds else ""
            return f"forward x{self.count}  ({self.meters:.2f} m{t})"
        return f"{self.direction} x{self.count}  ({self.degrees:+.0f} deg)"


def _perp_dist(p, a, b) -> float:
    """Perpendicular distance from point p to the line segment a-b."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 < 1e-18:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def simplify_indices(points: list[tuple[float, float]], tol: float) -> list[int]:
    """Ramer-Douglas-Peucker, returning the KEPT original indices (sorted).
    Keeping indices lets callers map each leg back to its source frames (timing).
    """
    if len(points) < 3:
        return list(range(len(points)))
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        dmax, idx = 0.0, -1
        for i in range(lo + 1, hi):
            d = _perp_dist(points[i], points[lo], points[hi])
            if d > dmax:
                dmax, idx = d, i
        if idx != -1 and dmax > tol:
            keep[idx] = True
            stack.append((lo, idx))
            stack.append((idx, hi))
    return [i for i, k in enumerate(keep) if k]


def simplify(points: list[tuple[float, float]], tol: float
             ) -> list[tuple[float, float]]:
    """Ramer-Douglas-Peucker. `tol` is in the same units as the points (metres).
    Larger tol => fewer, longer legs => fewer turns."""
    return [points[i] for i in simplify_indices(points, tol)]


def _wrap(a: float) -> float:
    """Wrap angle to (-pi, pi]."""
    return (a + math.pi) % (2 * math.pi) - math.pi


def plan(points: list[tuple[float, float]], *,
         m_per_tap: float = 0.05,
         deg_per_tap: float = 15.0,
         simplify_tol: float = 0.04,
         min_turn_deg: float = 8.0,
         min_leg_m: float = 0.0,
         fps: float | None = None) -> list[TapGroup]:
    """Convert a dense path into a turn-by-turn tap sequence.

    m_per_tap     : metres the toy travels per forward tap (calibration).
    deg_per_tap   : degrees the toy rotates per left/right tap (calibration).
    simplify_tol  : RDP tolerance (m); bigger => coarser route, fewer turns.
    min_turn_deg  : corners shallower than this are treated as straight.
    min_leg_m     : legs shorter than this are dropped (merged into the turn).
    fps           : if given, each forward leg is tagged with its intended
                    traversal time (source frames spanned / fps) so the driver
                    can PACE taps to the clip's real duration.
    """
    idx = simplify_indices(points, simplify_tol)
    pts = [points[i] for i in idx]
    if len(pts) < 2:
        return []
    # legs: (length, heading, intended_seconds) — seconds from source frame span
    legs = []
    for k in range(1, len(pts)):
        a, b = pts[k - 1], pts[k]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L < min_leg_m:
            continue
        secs = (idx[k] - idx[k - 1]) / fps if fps and fps > 0 else 0.0
        legs.append((L, math.atan2(b[1] - a[1], b[0] - a[0]), secs))
    if not legs:
        return []

    groups: list[TapGroup] = []

    def add_forward(L, secs):
        n = int(round(L / m_per_tap)) if m_per_tap > 0 else 0
        if n > 0:
            groups.append(TapGroup("forward", n, meters=L, seconds=secs))

    # first leg: assume the toy starts aligned to it (no opening turn)
    add_forward(legs[0][0], legs[0][2])
    prev_h = legs[0][1]
    for L, h, secs in legs[1:]:
        turn = math.degrees(_wrap(h - prev_h))
        if abs(turn) >= min_turn_deg:
            n = int(round(abs(turn) / deg_per_tap)) if deg_per_tap > 0 else 0
            if n > 0:
                groups.append(TapGroup("left" if turn > 0 else "right", n,
                                       degrees=turn))
        prev_h = h
        add_forward(L, secs)
    return groups


def summarize(groups: list[TapGroup]) -> str:
    fwd = sum(g.count for g in groups if g.direction == "forward")
    turns = sum(g.count for g in groups if g.direction in ("left", "right"))
    corners = sum(1 for g in groups if g.direction in ("left", "right"))
    return (f"{len(groups)} instructions  |  {fwd} forward taps, "
            f"{turns} turn taps over {corners} corners")


if __name__ == "__main__":
    import argparse
    import trajectory as T
    ap = argparse.ArgumentParser(description="Print the turn-by-turn tap plan "
                                             "for a trajectory CSV.")
    ap.add_argument("csv")
    ap.add_argument("--m-per-tap", type=float, default=0.05)
    ap.add_argument("--deg-per-tap", type=float, default=15.0)
    ap.add_argument("--tol", type=float, default=0.04)
    ap.add_argument("--min-turn", type=float, default=8.0)
    ap.add_argument("--bodypart", default=None)
    args = ap.parse_args()

    raw, desc = T.read_csv_points(args.csv, bodypart=args.bodypart)
    pts = simplify(raw, args.tol)
    groups = plan(raw, m_per_tap=args.m_per_tap, deg_per_tap=args.deg_per_tap,
                  simplify_tol=args.tol, min_turn_deg=args.min_turn)
    print(f"path: {desc}  ({len(raw)} pts -> {len(pts)} legs after simplify)")
    print(summarize(groups))
    print("-" * 56)
    for i, g in enumerate(groups):
        print(f"{i + 1:3d}.  {g}")
