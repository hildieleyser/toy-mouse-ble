"""Open-loop trajectory mapping for the toy mice.

Pipeline (all pure functions, no BLE, no GUI — unit-testable):

  raw CSV points (arbitrary units)
    -> fit_to_cage()      : scale + translate the bounding box to sit inside
                            the cage minus a safety margin. This is the PRIMARY
                            edge-safety mechanism in open-loop mode: the planned
                            path is guaranteed in-bounds.
    -> segment_speeds()   : per-frame desired speed (m/s) in cage space, from
                            the source frame rate.
    -> choose_timewarp()  : a single global playback-rate factor so the fastest
                            segment is within the toy's top speed (preserves
                            timing where physically possible).

A predicted-position GEOFENCE (see in_safe_zone / nearest_edge_dist) lets the
player halt if dead-reckoning says the toy is drifting out — the best an
open-loop system can do without a camera.
"""
from __future__ import annotations

import csv as _csv
from dataclasses import dataclass, field


@dataclass
class CageFit:
    points: list[tuple[float, float]]   # cage-space metres, origin at cage centre
    scale: float                        # metres per source-unit
    cage_w: float
    cage_h: float
    margin: float

    def in_safe_zone(self, x: float, y: float) -> bool:
        half_w = self.cage_w / 2 - self.margin
        half_h = self.cage_h / 2 - self.margin
        return -half_w <= x <= half_w and -half_h <= y <= half_h

    def nearest_edge_dist(self, x: float, y: float) -> float:
        """Distance (m) to the nearest *safe-zone* boundary; negative if outside."""
        half_w = self.cage_w / 2 - self.margin
        half_h = self.cage_h / 2 - self.margin
        return min(half_w - abs(x), half_h - abs(y))


def read_csv_points(path: str, bodypart: str | None = None
                    ) -> tuple[list[tuple[float, float]], str]:
    """Load (x, y_or_z) from an EKS multi-cam CSV or a plain x/y|x/z CSV."""
    with open(path, newline="") as f:
        rows = list(_csv.reader(f))
    if not rows:
        raise ValueError("empty CSV")

    if len(rows) >= 4:
        bp, co = rows[1], rows[2]
        candidates = []
        for i, (b, c) in enumerate(zip(bp, co)):
            if c == "x":
                # Cage floor plane is x-z (y is vertical/height, often ~0), so
                # pair x with z; only fall back to y if this bodypart has no z.
                jz = next((j for j, (b2, c2) in enumerate(zip(bp, co))
                           if b2 == b and c2 == "z"), None)
                jy = next((j for j, (b2, c2) in enumerate(zip(bp, co))
                           if b2 == b and c2 == "y"), None)
                j = jz if jz is not None else jy
                if j is not None:
                    candidates.append((b, co[j], i, j))
        if candidates:
            if bodypart:
                pick = next((t for t in candidates if t[0] == bodypart), candidates[0])
            else:
                pick = next((t for t in candidates if t[0].lower() in
                             ("tailbase", "spine_m", "center", "nose")), candidates[0])
            name, other, ix, iy = pick
            data = []
            for r in rows[3:]:
                try:
                    data.append((float(r[ix]), float(r[iy])))
                except (ValueError, IndexError):
                    continue
            return data, f"EKS bodypart={name} ({other})"

    header = [h.strip().lower() for h in rows[0]]
    def col(n): return header.index(n) if n in header else -1
    ix, iy = col("x"), (col("y") if col("y") >= 0 else col("z"))
    if ix < 0 or iy < 0:
        raise ValueError("CSV has no recognised (x,y) or (x,z) columns")
    data = []
    for r in rows[1:]:
        try:
            data.append((float(r[ix]), float(r[iy])))
        except (ValueError, IndexError):
            continue
    return data, f"plain csv ix={ix} iy={iy}"


def fit_to_cage(points: list[tuple[float, float]],
                cage_w: float, cage_h: float, margin: float,
                preserve_aspect: bool = True) -> CageFit:
    """Scale + centre the trajectory's bounding box into the usable cage area.

    With preserve_aspect=True (default) the path's shape is kept and it's scaled
    by a single factor to fit the tighter of the two axes — so it never exceeds
    the margin on either side. Returns points re-centred on the cage origin.
    """
    if not points:
        return CageFit([], 1.0, cage_w, cage_h, margin)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    bw = (xmax - xmin) or 1e-9
    bh = (ymax - ymin) or 1e-9
    usable_w = max(1e-6, cage_w - 2 * margin)
    usable_h = max(1e-6, cage_h - 2 * margin)
    if preserve_aspect:
        scale = min(usable_w / bw, usable_h / bh)
        sx = sy = scale
    else:
        sx, sy = usable_w / bw, usable_h / bh
        scale = (sx + sy) / 2
    cx_src = (xmin + xmax) / 2
    cy_src = (ymin + ymax) / 2
    fitted = [((x - cx_src) * sx, (y - cy_src) * sy) for x, y in points]
    return CageFit(fitted, scale, cage_w, cage_h, margin)


def segment_speeds(points: list[tuple[float, float]], fps: float) -> list[float]:
    """Per-frame desired speed (m/s): distance to next point * fps.
    Last frame repeats the previous speed."""
    if len(points) < 2:
        return [0.0] * len(points)
    speeds = []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        d = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        speeds.append(d * fps)
    speeds.append(speeds[-1])
    return speeds


def smooth(points: list[tuple[float, float]], window: int
           ) -> list[tuple[float, float]]:
    """Centred moving-average low-pass over (x, y) to damp keypoint jitter.

    `window` is the number of frames averaged (odd is natural; 1/0 = no-op).
    Reduces the frame-to-frame speed spikes that otherwise inflate the time-warp,
    so playback is smoother and faster. Endpoints use a shrinking window."""
    n = len(points)
    if window <= 1 or n < 3:
        return points
    half = window // 2
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        k = hi - lo
        out.append((sum(xs[lo:hi]) / k, sum(ys[lo:hi]) / k))
    return out


def choose_timewarp(speeds: list[float], v_max: float, pct: float = 1.0) -> float:
    """Global playback-rate divisor so the fastest segment fits under v_max.

    Returns T >= 1.0. Replay frames at fps/T and divide every desired speed by
    T. T=1 means the toy can keep up with the real timing; T>1 means we had to
    slow the whole clip down because the mouse out-ran the toy somewhere.

    `pct` (0..1] picks which quantile of the per-frame speeds counts as the
    "peak". pct=1.0 uses the strict maximum (original behaviour). pct<1.0 uses a
    high percentile instead, so a single jittery keypoint frame — which produces
    a huge bogus speed — can't slow the whole clip to a crawl. The genuinely
    fastest darts above that percentile just get clipped (the toy can't match
    them anyway).
    """
    if not speeds or v_max <= 0:
        return 1.0
    if pct >= 1.0:
        peak = max(speeds)
    else:
        s = sorted(speeds)
        k = min(len(s) - 1, max(0, int(round(pct * (len(s) - 1)))))
        peak = s[k]
    if peak <= v_max:
        return 1.0
    return peak / v_max


def heading_of(p0: tuple[float, float], p1: tuple[float, float]) -> float:
    import math
    return math.atan2(p1[1] - p0[1], p1[0] - p0[0])
