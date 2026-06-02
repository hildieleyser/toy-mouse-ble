"""Slow down a mouse trajectory CSV and make it brake for corners.

Problem this solves
-------------------
The raw `mouse_kart_*` trajectories run at a near-constant speed straight
through their corners, so the toy mouse (a) goes too fast overall and (b)
"accelerates forwards" right as it changes direction — it never brakes for a
turn, it just powers through it.

What this does
--------------
Reads the planar path (the x / z columns of a DeepLabCut-style CSV), throws away
the source *timing*, and re-times the SAME geometric path with a physically
sensible speed profile:

  1. cruise cap       — a low top speed (overall "go slower").
  2. corner cap       — where the path curves, cap speed by a max lateral
                        acceleration:  v_corner = sqrt(a_lat / curvature).
                        Tight turns => low speed.
  3. accel limit      — forward+backward passes bound |dv/dt| by a_long, so the
                        profile must RAMP DOWN before a corner and only ramps
                        back up gradually after it. This is what stops the
                        "accelerate forwards while turning" behaviour.

It then resamples the path in time (advancing arc-length by v*dt each frame at
the chosen fps) and writes a new CSV in the exact same format, so it drops
straight into mouse_panel.py ("Open CSV...").

Slower playback => the path covers less distance per frame => the output simply
has MORE frames than the input. That's expected and fine.

Usage:
  python slow_trajectory.py INPUT.csv [-o OUTPUT.csv]
         [--cruise 0.25] [--lat-accel 0.30] [--accel 0.40]
         [--min-speed 0.08] [--fps 30]
"""
from __future__ import annotations

import argparse
import csv as _csv
import math
from pathlib import Path

import trajectory as T


def _planar_indices(rows: list[list[str]], bodypart: str | None):
    """Find, for every bodypart, the column pair (ix, iy) used as the planar
    (x, z|y) floor coordinate — matching trajectory.read_csv_points()."""
    bp, co = rows[1], rows[2]
    pairs = {}
    for i, (b, c) in enumerate(zip(bp, co)):
        if c == "x":
            jz = next((j for j, (b2, c2) in enumerate(zip(bp, co))
                       if b2 == b and c2 == "z"), None)
            jy = next((j for j, (b2, c2) in enumerate(zip(bp, co))
                       if b2 == b and c2 == "y"), None)
            j = jz if jz is not None else jy
            if j is not None:
                pairs[b] = (i, j)
    return pairs


def curvature_profile(pts: list[tuple[float, float]]) -> tuple[list[float], list[float]]:
    """Return (cumulative arc length s, curvature kappa) per vertex.

    kappa_i = |turn angle at i| / (mean of the two adjacent segment lengths).
    Endpoints have zero curvature.
    """
    n = len(pts)
    s = [0.0] * n
    kappa = [0.0] * n
    seg = [0.0] * n            # seg[i] = length from pts[i-1] to pts[i]
    for i in range(1, n):
        dx = pts[i][0] - pts[i - 1][0]
        dy = pts[i][1] - pts[i - 1][1]
        seg[i] = math.hypot(dx, dy)
        s[i] = s[i - 1] + seg[i]
    for i in range(1, n - 1):
        ax, ay = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
        bx, by = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        if la < 1e-9 or lb < 1e-9:
            continue
        # signed turn angle between consecutive segments
        ang = math.atan2(ax * by - ay * bx, ax * bx + ay * by)
        mean_len = 0.5 * (la + lb)
        kappa[i] = abs(ang) / mean_len if mean_len > 1e-9 else 0.0
    return s, kappa


def velocity_profile(s: list[float], kappa: list[float], *,
                     cruise: float, lat_accel: float, accel: float,
                     min_speed: float) -> list[float]:
    """Speed (m/s) per vertex: corner-capped then accel-limited both ways."""
    n = len(s)
    v = [0.0] * n
    for i in range(n):
        if kappa[i] > 1e-9:
            v_corner = math.sqrt(lat_accel / kappa[i])
        else:
            v_corner = cruise
        v[i] = max(min_speed, min(cruise, v_corner))
    # start and end from (near) rest so it eases in and out
    v[0] = min(v[0], min_speed)
    v[-1] = min(v[-1], min_speed)
    # forward pass: limit acceleration (v can't rise faster than a_long)
    for i in range(1, n):
        ds = s[i] - s[i - 1]
        v[i] = min(v[i], math.sqrt(v[i - 1] ** 2 + 2 * accel * ds))
    # backward pass: limit deceleration => brake BEFORE the corner
    for i in range(n - 2, -1, -1):
        ds = s[i + 1] - s[i]
        v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * accel * ds))
    return v


def _interp_point(pts, s, target_s):
    """Linear-interpolate the (x, y) at cumulative arc length target_s."""
    if target_s <= 0:
        return pts[0]
    if target_s >= s[-1]:
        return pts[-1]
    # binary search the segment containing target_s
    lo, hi = 0, len(s) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if s[mid] <= target_s:
            lo = mid
        else:
            hi = mid
    seg_len = s[hi] - s[lo]
    t = (target_s - s[lo]) / seg_len if seg_len > 1e-12 else 0.0
    return (pts[lo][0] + t * (pts[hi][0] - pts[lo][0]),
            pts[lo][1] + t * (pts[hi][1] - pts[lo][1]))


def _v_at(s, v, target_s):
    if target_s <= 0:
        return v[0]
    if target_s >= s[-1]:
        return v[-1]
    lo, hi = 0, len(s) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if s[mid] <= target_s:
            lo = mid
        else:
            hi = mid
    seg_len = s[hi] - s[lo]
    t = (target_s - s[lo]) / seg_len if seg_len > 1e-12 else 0.0
    return v[lo] + t * (v[hi] - v[lo])


def resample_by_time(pts, s, v, fps: float) -> list[tuple[float, float]]:
    """Walk the path at speed v(s), emitting one point per 1/fps second."""
    dt = 1.0 / fps
    total = s[-1]
    out = [pts[0]]
    cur = 0.0
    guard = 0
    max_iter = int(total / max(1e-6, min(x for x in v if x > 0)) * fps) + 100000
    while cur < total and guard < max_iter:
        guard += 1
        # midpoint speed for a slightly better step
        v0 = _v_at(s, v, cur)
        step = v0 * dt
        v_mid = _v_at(s, v, cur + step / 2)
        cur += max(1e-6, v_mid * dt)
        out.append(_interp_point(pts, s, min(cur, total)))
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def dedupe(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > 1e-9:
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="source trajectory CSV (DeepLabCut format)")
    ap.add_argument("-o", "--output", help="output CSV (default: *_slow.csv)")
    ap.add_argument("--cruise", type=float, default=0.25,
                    help="top cruising speed, m/s (default 0.25)")
    ap.add_argument("--lat-accel", type=float, default=0.30,
                    help="max lateral accel for corners, m/s^2 (default 0.30); "
                         "lower => slower in turns")
    ap.add_argument("--accel", type=float, default=0.40,
                    help="max longitudinal accel/decel, m/s^2 (default 0.40); "
                         "lower => brakes earlier / eases out more gently")
    ap.add_argument("--min-speed", type=float, default=0.08,
                    help="floor speed so it never fully stalls, m/s (default 0.08)")
    ap.add_argument("--fps", type=float, default=30.0,
                    help="output frame rate (default 30)")
    ap.add_argument("--bodypart", default=None,
                    help="which bodypart defines the path (default: auto)")
    args = ap.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output) if args.output else \
        in_path.with_name(in_path.stem + "_slow.csv")

    with open(in_path, newline="") as f:
        rows = list(_csv.reader(f))
    if len(rows) < 4:
        raise SystemExit("CSV too short / not a DeepLabCut-format file")

    pairs = _planar_indices(rows, args.bodypart)
    if not pairs:
        raise SystemExit("could not locate x/z columns in the CSV header")

    raw, desc = T.read_csv_points(str(in_path), bodypart=args.bodypart)
    pts = dedupe(raw)
    if len(pts) < 3:
        raise SystemExit("path has fewer than 3 distinct points")

    s, kappa = curvature_profile(pts)
    v = velocity_profile(s, kappa, cruise=args.cruise, lat_accel=args.lat_accel,
                         accel=args.accel, min_speed=args.min_speed)
    new_pts = resample_by_time(pts, s, v, args.fps)

    # ---- report before/after ----
    src_fps = args.fps
    sp_in = T.segment_speeds(raw, src_fps)
    sp_out = T.segment_speeds(new_pts, args.fps)
    print(f"path: {desc}")
    print(f"  length            {s[-1]:.2f} m")
    print(f"  input            {len(raw):4d} frames   "
          f"mean {sum(sp_in)/len(sp_in):.2f}  peak {max(sp_in):.2f} m/s")
    print(f"  output           {len(new_pts):4d} frames   "
          f"mean {sum(sp_out)/len(sp_out):.2f}  peak {max(sp_out):.2f} m/s")
    print(f"  duration  {len(raw)/src_fps:.1f}s -> {len(new_pts)/args.fps:.1f}s "
          f"@ {args.fps:g} fps")
    print(f"  knobs: cruise={args.cruise}  lat_accel={args.lat_accel}  "
          f"accel={args.accel}  min_speed={args.min_speed}")

    # ---- write new CSV in the SAME DeepLabCut format ----
    # Every bodypart column-pair gets the resampled (x, planar) value; any third
    # (height) coordinate is held at the source's first-frame value (usually 0).
    ncols = len(rows[0])
    # height value per bodypart's 3rd coord, taken from row 3 (first data frame)
    first = rows[3]
    with open(out_path, "w", newline="") as f:
        w = _csv.writer(f)
        for hdr in rows[:3]:
            w.writerow(hdr)
        for fi, (x, z) in enumerate(new_pts):
            line = [""] * ncols
            line[0] = fi
            for b, (ix, iy) in pairs.items():
                line[ix] = f"{x:.4f}"
                line[iy] = f"{z:.4f}"
                # fill the remaining (height) coord of this bodypart, if any
                for k in (ix - 1, ix + 1, ix + 2, iy - 1, iy + 1):
                    if 0 <= k < ncols and k not in (ix, iy) and line[k] == "":
                        # only the coord columns belonging to this bodypart
                        pass
            # fill any still-empty coord cells from the source first frame
            for k in range(1, ncols):
                if line[k] == "":
                    line[k] = first[k] if k < len(first) else "0.0"
            w.writerow(line)

    print(f"\nwrote {out_path}")
    print("Load it in mouse_panel.py via 'Open CSV...'.")


if __name__ == "__main__":
    main()
