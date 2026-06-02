"""Map a desired cage-space speed (m/s) onto a toy SPEED byte + duty cycle.

Two regimes:
  * target >= v_min  : the toy can move continuously; pick the byte whose
                       calibrated speed is closest (interpolated).
  * 0 < target < v_min: the toy can't crawl that slowly, so we DUTY-CYCLE —
                       run at v_min for a fraction `duty = target / v_min`
                       of the time and pause the rest. The caller turns that
                       fraction into move/stop pulses.

Calibration lives in speed_calibration.json (written by calibrate_speed.py):
    {"points": [[byte, m_per_s], ...], "measured_at": "..."}

Until that file has >=2 points we fall back to a provisional linear model
anchored on the single field measurement (byte 0x0B -> 1.33 m/s) and a guessed
deadband. Everything downstream keeps working; it just gets more accurate once
you calibrate.
"""
from __future__ import annotations

import json
from pathlib import Path

CALIB_PATH = Path(__file__).with_name("speed_calibration.json")

# Field measurement from 2026-05-29: 60% slider (byte 0x0B=11) -> ~1.33 m/s.
_KNOWN_BYTE = 11
_KNOWN_MPS = 1.33
# Guessed PWM/stiction deadband: below this byte the wheels don't turn.
# Calibration will replace this with the real floor.
_GUESS_DEADBAND_BYTE = 3

SPEED_BYTE_MAX = 0x13  # 19; highest byte the app ever sent ("boost")


class SpeedModel:
    def __init__(self, points: list[tuple[int, float]] | None = None,
                 deadband_byte: int | None = None):
        if points and len(points) >= 2:
            self.points = sorted(points)
            self.provisional = False
        else:
            # provisional linear model through the one known point
            k = _KNOWN_MPS / (_KNOWN_BYTE - _GUESS_DEADBAND_BYTE)
            self.points = [(_GUESS_DEADBAND_BYTE, 0.0),
                           (_KNOWN_BYTE, _KNOWN_MPS),
                           (SPEED_BYTE_MAX, k * (SPEED_BYTE_MAX - _GUESS_DEADBAND_BYTE))]
            self.provisional = True
        self.deadband_byte = (deadband_byte if deadband_byte is not None
                              else self._infer_deadband())

    @classmethod
    def load(cls) -> "SpeedModel":
        if CALIB_PATH.exists():
            try:
                d = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
                pts = [(int(b), float(v)) for b, v in d.get("points", [])]
                return cls(points=pts, deadband_byte=d.get("deadband_byte"))
            except Exception:
                pass
        return cls()

    def _infer_deadband(self) -> int:
        """Lowest integer byte whose interpolated speed is > 0 (first moving byte)."""
        lo = int(self.points[0][0])
        for b in range(lo, SPEED_BYTE_MAX + 1):
            if self.byte_to_speed(b) > 0:
                return b
        return _GUESS_DEADBAND_BYTE

    @property
    def v_min(self) -> float:
        """Slowest *continuous* speed the toy can sustain (at deadband byte)."""
        return self.byte_to_speed(self.deadband_byte) or self._smallest_positive()

    @property
    def v_max(self) -> float:
        return self.byte_to_speed(SPEED_BYTE_MAX)

    def _smallest_positive(self) -> float:
        vs = [v for _, v in self.points if v > 0]
        return min(vs) if vs else _KNOWN_MPS

    def byte_to_speed(self, b: int) -> float:
        """Piecewise-linear interpolation of the calibration curve."""
        pts = self.points
        if b <= pts[0][0]:
            return max(0.0, pts[0][1])
        if b >= pts[-1][0]:
            return pts[-1][1]
        for (b0, v0), (b1, v1) in zip(pts, pts[1:]):
            if b0 <= b <= b1:
                t = (b - b0) / (b1 - b0) if b1 != b0 else 0.0
                return v0 + t * (v1 - v0)
        return pts[-1][1]

    def speed_to_byte(self, target_mps: float) -> int:
        """Invert the curve: smallest byte whose speed >= target (clamped)."""
        target = max(0.0, target_mps)
        if target <= 0:
            return self.deadband_byte
        for b in range(self.deadband_byte, SPEED_BYTE_MAX + 1):
            if self.byte_to_speed(b) >= target:
                return b
        return SPEED_BYTE_MAX

    def plan(self, target_mps: float) -> tuple[int, float]:
        """Return (speed_byte, duty) for a desired speed.

        duty == 1.0 means drive continuously at that byte.
        duty  < 1.0 means pulse at v_min (byte=deadband) for that fraction.
        """
        target = max(0.0, target_mps)
        if target <= 0:
            return self.deadband_byte, 0.0
        if target >= self.v_min:
            return self.speed_to_byte(target), 1.0
        # too slow for continuous motion -> duty-cycle at the floor speed
        duty = max(0.0, min(1.0, target / self.v_min))
        return self.deadband_byte, duty


if __name__ == "__main__":
    m = SpeedModel.load()
    tag = "PROVISIONAL (run calibrate_speed.py)" if m.provisional else "calibrated"
    print(f"Speed model [{tag}]")
    print(f"  deadband byte = 0x{m.deadband_byte:02X}  v_min={m.v_min:.2f} m/s  v_max={m.v_max:.2f} m/s")
    print("  target  -> (byte, duty)")
    for t in (0.05, 0.1, 0.2, 0.4, 0.8, 1.3, 2.0, 3.0):
        b, duty = m.plan(t)
        print(f"   {t:>4.2f} m/s -> byte 0x{b:02X} ({b:2d}), duty {duty:.2f}")
