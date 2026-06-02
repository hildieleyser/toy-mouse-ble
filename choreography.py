"""Per-mouse trajectory tracks for multi-mouse choreography.

A MouseTrack is one mouse following one clip: it owns the cage-fitted path, the
speed/time-warp plan, a dead-reckoned pose, and the duty-cycle accumulator. It
does NOT own a timer — the Choreographer (in mouse_panel.py) ticks every track
on one shared real-time clock, so 4-6 clips of different lengths/speeds all play
together, each driving its own mouse.

`advance(dt)` returns the drive decision for this instant:
    (direction | None, speed_byte, drive_now: bool)
The caller turns that into a BLE frame for this track's mouse.
"""
from __future__ import annotations

import math

import trajectory as T

# Distinct colours for up to 6 mice on the shared canvas.
TRACK_COLORS = ["#ff5555", "#4dabf7", "#3aa655", "#f7b500", "#b14dff", "#ff7ac0"]


class MouseTrack:
    LOOKAHEAD = 8
    DEAD_BAND = math.radians(18)
    TURN_STEP = math.radians(10)
    GEOFENCE_M = 0.04            # hold only if predicted this far OUTSIDE safe zone

    def __init__(self, slug: str, label: str, raw, cage: tuple[float, float, float],
                 src_fps: float, speed_model, color: str):
        self.slug = slug
        self.label = label
        self.color = color
        self.model = speed_model
        self.src_fps = src_fps if src_fps > 0 else 30.0
        self.fit = T.fit_to_cage(raw, cage[0], cage[1], cage[2])
        self.frames = self.fit.points
        self.speeds = T.segment_speeds(self.frames, self.src_fps)
        self.timewarp = T.choose_timewarp(self.speeds, speed_model.v_max)
        self.eff_fps = self.src_fps / self.timewarp
        # runtime state
        self.pos = 0.0           # fractional frame index
        self.duty_acc = 0.0
        self.done = False
        self.geofenced = False
        self.last_dir: str | None = None
        self.rx = self.ry = self.rtheta = 0.0
        self.reset()

    @property
    def idx(self) -> int:
        return int(self.pos)

    def reset(self):
        self.pos = 0.0
        self.duty_acc = 0.0
        self.done = False
        self.geofenced = False
        self.last_dir = None
        if self.frames:
            self.rx, self.ry = self.frames[0]
            if len(self.frames) > 1:
                self.rtheta = T.heading_of(self.frames[0], self.frames[1])

    def advance(self, dt: float) -> tuple[str | None, int, bool]:
        """Step this track by `dt` seconds of real time and return its decision."""
        n = len(self.frames)
        if n < 2 or self.idx >= n - 1:
            self.done = True
            self.last_dir = None
            return None, self.model.deadband_byte, False

        i = self.idx
        look = min(i + self.LOOKAHEAD, n - 1)
        tx, ty = self.frames[look]
        dx, dy = tx - self.rx, ty - self.ry

        v_target = self.speeds[i] / self.timewarp
        byte, duty = self.model.plan(v_target)

        direction = None
        if abs(dx) > 1e-9 or abs(dy) > 1e-9:
            target_theta = math.atan2(dy, dx)
            err = (target_theta - self.rtheta + math.pi) % (2 * math.pi) - math.pi
            if abs(err) <= self.DEAD_BAND:
                direction = "forward"
                self.rtheta += 0.3 * err
            elif err > 0:
                direction = "left"
                self.rtheta += self.TURN_STEP
            else:
                direction = "right"
                self.rtheta -= self.TURN_STEP

        self.duty_acc += duty
        drive_now = direction is not None and self.duty_acc >= 1.0
        if drive_now:
            self.duty_acc -= 1.0

        self.geofenced = False
        v_actual = self.model.byte_to_speed(byte)
        if drive_now and direction == "forward" and self.fit:
            nx = self.rx + v_actual * math.cos(self.rtheta) * dt
            ny = self.ry + v_actual * math.sin(self.rtheta) * dt
            if self.fit.nearest_edge_dist(nx, ny) < -self.GEOFENCE_M:
                drive_now = False
                self.geofenced = True

        if drive_now and direction == "forward":
            self.rx += v_actual * math.cos(self.rtheta) * dt
            self.ry += v_actual * math.sin(self.rtheta) * dt

        self.last_dir = direction
        self.pos += dt * self.eff_fps
        return direction, byte, drive_now
