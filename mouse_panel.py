"""Toy-mouse fleet control panel.

A Tkinter dashboard that:
  * scans + connects to one or MORE toy mice over BLE (a fleet)
  * drives the selected mice with arrow keys (hold-to-drive) and a speed slider
  * loads a local mp4 and a movement CSV, fits the trajectory into a cage of
    known size, and replays it OPEN-LOOP onto the mice — matching the real
    mouse's speed where the toy physically can, duty-cycling (pulsing) the slow
    crawls the toy is too fast to do continuously, and time-warping only where
    the mouse out-runs the toy.

Edge safety (open-loop, no camera): the trajectory is scaled to sit inside the
cage minus a margin, so the *planned* path can't leave the cage. A dead-reckoned
geofence halts driving if the estimated position drifts toward the boundary.
This is the best guarantee possible without position feedback.

Run:  python mouse_panel.py
Requires: bleak, opencv-python, pillow.
"""
import json
import math
import queue
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from mouse_protocol import (
    DEFAULT_MOUSE_MAC, MOUSE_FRIENDLY,
    USE_MODE, build, build_raw, build_stop, build_set_speed, protocol_ready,
)
from mouse_fleet import MouseFleet, is_mouse_name
import mouse_roster as R
from speed_model import SpeedModel
import trajectory as T
import tap_planner as TP
import catalog as C
from catalog import CAMERAS, ClipSpec, MinuteSpec, RollingFetcher
from choreography import MouseTrack, TRACK_COLORS

CONFIG_PATH = Path(__file__).with_name("mouse_config.json")

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---- Video sidecar ---------------------------------------------------------

class VideoSidecar:
    """Decode an mp4 with cv2 and render frame N into a Tk Label."""
    DISPLAY_W = 420

    def __init__(self, label: tk.Label):
        self.label = label
        self.cap = None
        self.path: str | None = None
        self._photo = None
        self._next_idx = 0

    def open(self, path: str) -> bool:
        import cv2
        if self.cap is not None:
            self.cap.release()
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            self.cap = None
            return False
        self.path = path
        self._next_idx = 0
        return True

    @property
    def n_frames(self) -> int:
        if self.cap is None:
            return 0
        import cv2
        return int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))

    @property
    def fps(self) -> float:
        if self.cap is None:
            return 30.0
        import cv2
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        return fps if fps and fps > 0 else 30.0

    def show_frame_at(self, path: str, idx: int):
        """Switch source file first if it changed, then show the frame."""
        if self.path != path:
            if not self.open(path):
                return
        self.show_frame(idx)

    def show_frame(self, idx: int):
        if self.cap is None:
            return
        import cv2
        from PIL import Image, ImageTk
        if idx != self._next_idx:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            self._next_idx = idx
        ok, frame = self.cap.read()
        if not ok:
            return
        self._next_idx += 1
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame.shape[:2]
        new_w = self.DISPLAY_W
        new_h = int(h * new_w / w) if w else h
        frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
        img = Image.fromarray(frame)
        self._photo = ImageTk.PhotoImage(img)
        self.label.config(image=self._photo)


class TileVideo:
    """Decode one clip's mp4s (several camera angles) and paint a frame as the
    background image of a Tk Canvas, so a trajectory overlay can be drawn on top.
    Keeps one cv2 capture per opened camera and switches between them cheaply."""

    def __init__(self, canvas: tk.Canvas):
        self.canvas = canvas
        self.caps: dict[int, object] = {}      # cam -> cv2.VideoCapture
        self.paths: dict[int, str] = {}        # cam -> mp4 path
        self._next: dict[int, int] = {}        # cam -> next sequential frame
        self._photo = None
        self._img_id = None

    def set_sources(self, cam_paths: dict[int, str]):
        for cap in self.caps.values():
            cap.release()
        self.caps = {}
        self._next = {}
        self.paths = dict(cam_paths)

    def has(self, cam: int) -> bool:
        return cam in self.paths

    def _cap(self, cam: int):
        import cv2
        if cam not in self.caps and cam in self.paths:
            cap = cv2.VideoCapture(self.paths[cam])
            if not cap.isOpened():
                return None
            self.caps[cam] = cap
            self._next[cam] = 0
        return self.caps.get(cam)

    def show(self, cam: int, idx: int, box_w: int, box_h: int) -> bool:
        """Render frame `idx` of `cam` centred in a box_w×box_h canvas.
        Returns True if a frame was painted."""
        import cv2
        from PIL import Image, ImageTk
        cap = self._cap(cam)
        if cap is None:
            return False
        if idx != self._next.get(cam):
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, idx))
            self._next[cam] = idx
        ok, frame = cap.read()
        if not ok:
            return False
        self._next[cam] += 1
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame.shape[:2]
        scale = min(box_w / w, box_h / h) if w and h else 1.0
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
        self._photo = ImageTk.PhotoImage(Image.fromarray(frame))
        if self._img_id is None:
            self._img_id = self.canvas.create_image(
                box_w // 2, box_h // 2, image=self._photo)
        else:
            self.canvas.itemconfigure(self._img_id, image=self._photo)
            self.canvas.coords(self._img_id, box_w // 2, box_h // 2)
        self.canvas.tag_lower(self._img_id)
        return True

    def release(self):
        for cap in self.caps.values():
            cap.release()
        self.caps = {}


# ---- Trajectory player (cage-fit, speed-matched, geofenced) ----------------

class TrajectoryPlayer:
    """Replays a cage-fitted trajectory, driving the selected mice open-loop.

    Per frame it computes the desired cage-space speed, asks the SpeedModel for
    a (byte, duty) plan, picks a direction from the heading error, duty-cycles
    slow segments into move/stop pulses, and dead-reckons a pose for display +
    geofencing.
    """
    LOOKAHEAD = 8                  # frames ahead to aim at
    DEAD_BAND = math.radians(18)   # heading error within which we go straight
    TURN_STEP = math.radians(10)   # pose rotation per turning tick
    GEOFENCE_STOP_M = 0.04         # stop if predicted within this of safe edge

    def __init__(self, app):
        self.app = app
        self.raw: list[tuple[float, float]] = []
        self.fit: T.CageFit | None = None
        self.frames: list[tuple[float, float]] = []
        self.speeds: list[float] = []
        self.timewarp = 1.0
        self.src_fps = 30.0
        self.eff_fps = 30.0
        self.idx = 0
        self.after_id: str | None = None
        self.duty_acc = 0.0
        self.geofenced = False
        self._tick_count = 0
        # source CSV (single local clip) + current bodypart for re-slicing
        self.csv_path: str | None = None
        self.bodypart = "Tailbase"
        # continuous-mode state: minute boundaries for multicam video swaps
        self.minutes: list[MinuteSpec] = []
        self.continuous_specs: list[ClipSpec] = []
        # dead-reckoned toy pose (cage metres, rad)
        self.rx = self.ry = self.rtheta = 0.0
        # UI hooks
        self.canvas: tk.Canvas | None = None
        self.status_var: tk.StringVar | None = None
        self.drive_var: tk.BooleanVar | None = None
        self.seek_hook = None          # called whenever idx changes (updates seek bar)

    def _fit_and_prepare(self, raw, src_fps):
        self.raw = raw
        self.src_fps = src_fps if src_fps > 0 else 30.0
        self.fit = T.fit_to_cage(raw, self.app.cage_w, self.app.cage_h, self.app.cage_margin)
        self.frames = self.fit.points
        self.speeds = T.segment_speeds(self.frames, self.src_fps)
        self.timewarp = T.choose_timewarp(self.speeds, self.app.speed_model.v_max)
        self.eff_fps = self.src_fps / self.timewarp
        self.reset()
        peak = max(self.speeds) if self.speeds else 0.0
        self.app._log(
            f"Trajectory fitted: scale={self.fit.scale:.3g} m/unit, "
            f"peak {peak:.2f} m/s, time-warp x{self.timewarp:.2f} "
            f"(toy v_max={self.app.speed_model.v_max:.2f} m/s)")

    def load(self, raw, src_fps: float, csv_path: str | None = None):
        """Single trajectory (local CSV or a single catalog clip)."""
        self.csv_path = csv_path
        self.minutes = []
        self.continuous_specs = []
        self._fit_and_prepare(raw, src_fps)

    def load_continuous(self, raw, minutes: list[MinuteSpec],
                        specs: list[ClipSpec], src_fps: float):
        """Concatenated multi-minute timeline; video swaps mp4 at boundaries."""
        self.csv_path = None
        self.minutes = minutes
        self.continuous_specs = specs
        self._fit_and_prepare(raw, src_fps)

    def current_minute_idx(self) -> int:
        if not self.minutes:
            return -1
        for i, m in enumerate(self.minutes):
            if self.idx < m.frame_offset + m.n_frames:
                return i
        return len(self.minutes) - 1

    def play(self):
        if not self.frames:
            self.app._log("Play: no trajectory loaded (Open CSV or Get clip first).")
            return
        if self.after_id is not None:
            return
        self.geofenced = False
        self._tick_count = 0
        # Diagnostics so 'mice not moving' is never a mystery.
        if not (self.drive_var and self.drive_var.get()):
            self.app._log("Play: previewing on canvas only — tick 'Drive mice from CSV' to move the mice.")
        else:
            targets = self.app._targets()
            if not targets:
                self.app._log("Play: 'Drive mice from CSV' is on but NO mice are connected — connect first.")
            else:
                self.app._log(f"Play: driving {len(targets)} mouse(mice): {', '.join(targets)}")
        self._tick()

    def pause(self):
        if self.after_id is not None:
            self.app.root.after_cancel(self.after_id)
            self.after_id = None
        if self.drive_var and self.drive_var.get():
            self.app.send_stop("traj pause")

    def reset(self):
        self.pause()
        self.idx = 0
        self.duty_acc = 0.0
        self.geofenced = False
        # start the toy pose on the first trajectory point, heading along it
        if self.frames:
            self.rx, self.ry = self.frames[0]
            if len(self.frames) > 1:
                self.rtheta = T.heading_of(self.frames[0], self.frames[1])
        self._redraw()
        self._advance_video()
        self._set_status("ready")

    def seek(self, idx: int):
        """Jump to a frame for scrubbing — find a good part of the clip without
        driving the mice. Pauses playback (sending a stop if we were driving),
        snaps the dead-reckoned pose onto the planned path at that frame, and
        refreshes the cage canvas + every camera tile."""
        if not self.frames:
            return
        self.pause()
        self.idx = max(0, min(int(idx), len(self.frames) - 1))
        self.duty_acc = 0.0
        self.geofenced = False
        # snap pose onto the planned path so the overlay reads sensibly here
        self.rx, self.ry = self.frames[self.idx]
        if self.idx + 1 < len(self.frames):
            self.rtheta = T.heading_of(self.frames[self.idx], self.frames[self.idx + 1])
        elif self.idx > 0:
            self.rtheta = T.heading_of(self.frames[self.idx - 1], self.frames[self.idx])
        self._redraw()
        self._advance_video()
        self._set_status("scrub")

    def _set_status(self, extra=""):
        if self.status_var:
            n = len(self.frames)
            self.status_var.set(f"frame {self.idx}/{n}   {extra}")
        if self.seek_hook:
            self.seek_hook()

    def _tick(self):
        if self.idx >= len(self.frames) - 1:
            self.pause()
            self._set_status("done")
            return
        dt = 1.0 / self.eff_fps
        look = min(self.idx + self.LOOKAHEAD, len(self.frames) - 1)
        tx, ty = self.frames[look]
        dx, dy = tx - self.rx, ty - self.ry

        v_target = self.speeds[self.idx] / self.timewarp
        byte, duty = self.app.speed_model.plan(v_target)

        # direction from heading error
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

        # duty-cycle: only actually drive on a `duty` fraction of ticks
        self.duty_acc += duty
        drive_this_tick = direction is not None and self.duty_acc >= 1.0
        if drive_this_tick:
            self.duty_acc -= 1.0

        # Geofence: the *planned* path already sits inside the safe zone (the
        # cage-fit guarantees that), so being near the safe edge is normal and
        # must NOT block motion. Only hold if dead-reckoning predicts the toy
        # has actually DRIFTED out of the safe zone (nearest_edge_dist < 0).
        self.geofenced = False
        v_actual = self.app.speed_model.byte_to_speed(byte)
        if drive_this_tick and direction == "forward" and self.fit:
            nx = self.rx + v_actual * math.cos(self.rtheta) * dt
            ny = self.ry + v_actual * math.sin(self.rtheta) * dt
            if self.fit.nearest_edge_dist(nx, ny) < -self.GEOFENCE_STOP_M:
                drive_this_tick = False
                self.geofenced = True

        # dead-reckon pose
        if drive_this_tick and direction == "forward":
            self.rx += v_actual * math.cos(self.rtheta) * dt
            self.ry += v_actual * math.sin(self.rtheta) * dt

        # emit to the mice
        if self.drive_var and self.drive_var.get():
            if drive_this_tick:
                self.app.send_raw(direction, byte)
            else:
                self.app.send_stop("")
            # Throttled heartbeat in the log (~once/sec) so the user can see
            # commands actually flowing without flooding it every frame.
            self._tick_count += 1
            if self._tick_count % max(1, int(self.eff_fps)) == 0:
                self.app._log(f"Drive: frame {self.idx} {direction or '-'} "
                              f"byte=0x{byte:02X} duty={duty:.2f}")

        self.idx += 1
        tag = "GEOFENCE-HOLD" if self.geofenced else (
            f"{(direction or '-')[:4]} byte=0x{byte:02X} duty={duty:.2f} v={v_target:.2f}")
        self._set_status(tag)
        self._redraw()
        self._advance_video()
        self.after_id = self.app.root.after(max(10, int(1000 / self.eff_fps)), self._tick)

    def _advance_video(self):
        if not self.frames:
            return
        if self.minutes:
            # continuous multicam: pick the minute, swap each cam's mp4 + frame
            mi = self.current_minute_idx()
            if mi < 0:
                return
            m = self.minutes[mi]
            frame_in_file = self.idx - m.frame_offset
            for cam, sidecar in self.app.videos.items():
                mp4 = m.cam_mp4s.get(cam)
                if mp4 is None or not Path(mp4).exists():
                    continue
                sidecar.show_frame_at(str(mp4), max(0, frame_in_file))
            return
        # single catalog clip: cams already opened, frame index is 1:1
        any_cam = False
        for sidecar in self.app.videos.values():
            if sidecar.cap is not None:
                any_cam = True
                n_v = sidecar.n_frames
                if n_v > 0:
                    sidecar.show_frame(int(self.idx * n_v / len(self.frames)))
        if any_cam:
            return
        # fallback: single local mp4 from "Open mp4..."
        v = self.app.video
        if v.cap is not None:
            n_v = v.n_frames
            if n_v > 0:
                v.show_frame(int(self.idx * n_v / len(self.frames)))

    def _redraw(self):
        c = self.canvas
        if c is None or self.fit is None:
            return
        c.delete("all")
        w = int(c["width"]); h = int(c["height"])
        # view spans the whole cage, centred, with a little padding
        half_w = self.fit.cage_w / 2
        half_h = self.fit.cage_h / 2
        span = max(half_w, half_h) * 2 * 1.08 or 1.0
        def to_px(x, y):
            return (w / 2 + x / span * w, h / 2 + y / span * h)

        # cage walls
        x0, y0 = to_px(-half_w, -half_h)
        x1, y1 = to_px(half_w, half_h)
        c.create_rectangle(x0, y0, x1, y1, outline="#8a8aa8", width=2)
        # safe zone (cage minus margin)
        m = self.fit.margin
        sx0, sy0 = to_px(-half_w + m, -half_h + m)
        sx1, sy1 = to_px(half_w - m, half_h - m)
        c.create_rectangle(sx0, sy0, sx1, sy1, outline="#3aa655", dash=(4, 3))
        c.create_text(sx0 + 4, sy0 + 2, text="safe zone", anchor="nw",
                      fill="#3aa655", font=("Segoe UI", 8))

        if not self.frames:
            return
        # planned path
        flat = []
        for x, y in self.frames:
            px, py = to_px(x, y); flat += [px, py]
        if len(flat) >= 4:
            c.create_line(*flat, fill="#9090b8", width=1)
        # target dot
        mx, my = self.frames[self.idx]
        px, py = to_px(mx, my)
        c.create_oval(px - 4, py - 4, px + 4, py + 4, fill="red", outline="")
        # toy pose
        rpx, rpy = to_px(self.rx, self.ry)
        col = "#ff5555" if self.geofenced else "#4dabf7"
        c.create_oval(rpx - 6, rpy - 6, rpx + 6, rpy + 6, fill=col,
                      outline="#e8e8f0", width=1)
        hx = self.rx + 0.04 * math.cos(self.rtheta)
        hy = self.ry + 0.04 * math.sin(self.rtheta)
        c.create_line(rpx, rpy, *to_px(hx, hy), fill=col, width=2)


# ---- Tap driver (turn-by-turn discrete taps, like the mobile app) ----------

class TapDriver:
    """Drives the toy the way a human drives the app: as a sequence of discrete
    TAPS — "forward, left, forward, right, forward x3" — instead of a continuous
    per-frame correction.

    The loaded (cage-fitted) path is simplified into straight legs joined at real
    corners (tap_planner), each leg -> N forward taps, each corner -> N left/right
    taps. A tap = send the direction for `tap_on_ms`, then stop, then wait
    `tap_gap_ms` so the toy settles before the next move (no blending of turning
    into forward motion). Pose is dead-reckoned from the SAME per-tap constants
    used to plan, so the on-canvas preview matches what the toy is told to do.
    """

    def __init__(self, app):
        self.app = app
        self.groups: list[TP.TapGroup] = []
        self.taps: list[str] = []          # expanded, one direction per tap
        self.legs: list[tuple[float, float]] = []   # simplified route (preview)
        self.i = 0
        self.after_id: str | None = None
        # dead-reckoned pose (cage metres / rad)
        self.rx = self.ry = self.rtheta = 0.0
        # UI hooks (wired by App)
        self.canvas: tk.Canvas | None = None
        self.status_var: tk.StringVar | None = None
        self.drive_var: tk.BooleanVar | None = None
        # calibration / timing knobs (set from the UI before play)
        self.m_per_tap = 0.05
        self.deg_per_tap = 15.0
        self.simplify_tol = 0.04
        self.min_turn_deg = 10.0
        # forward taps are long (cm-scale travel); turn taps are SHORT because
        # the toy spins fast (~764 deg/s) — a long turn tap would over-rotate.
        self.tap_on_fwd_ms = 100
        self.tap_on_turn_ms = 40
        self.tap_gap_ms = 120
        self.speed_pct = 60

    def build_plan(self) -> bool:
        frames = self.app.player.frames
        if not frames:
            self.app._log("Tap drive: no trajectory loaded (Open CSV / Get clip first).")
            return False
        fps = self.app.player.src_fps or 30.0
        self.groups = TP.plan(
            frames, m_per_tap=self.m_per_tap, deg_per_tap=self.deg_per_tap,
            simplify_tol=self.simplify_tol, min_turn_deg=self.min_turn_deg, fps=fps)
        self.legs = TP.simplify(frames, self.simplify_tol)
        # Expand into timed taps: (direction, on_ms, gap_ms). Forward legs are
        # PACED to the clip's real timing — each leg's intended seconds are spread
        # across its taps, so a 40s clip takes ~40s instead of finishing instantly
        # (and slow legs naturally get longer gaps => the toy crawls there).
        self.taps = []
        for g in self.groups:
            if g.direction == "forward":
                if g.seconds and g.count:
                    per_ms = g.seconds * 1000.0 / g.count      # budget per tap
                    gap = max(self.tap_gap_ms, int(per_ms - self.tap_on_fwd_ms))
                else:
                    gap = self.tap_gap_ms
                for _ in range(g.count):
                    self.taps.append(("forward", self.tap_on_fwd_ms, gap))
            else:
                for _ in range(g.count):
                    self.taps.append((g.direction, self.tap_on_turn_ms, self.tap_gap_ms))
        return bool(self.taps)

    def est_seconds(self) -> float:
        return sum((on + gap) / 1000.0 for _, on, gap in self.taps)

    def play(self):
        if self.after_id is not None:
            return
        if not self.build_plan():
            self.app._log("Tap drive: nothing to do — plan is empty "
                          "(try a smaller 'min turn' or 'simplify' value).")
            return
        self.i = 0
        self._reset_pose()
        # show the route + how the run time compares to the clip's real duration
        frames = self.app.player.frames
        fps = self.app.player.src_fps or 30.0
        clip_s = len(frames) / fps
        est_s = self.est_seconds()
        self.app._log(f"Tap plan: {TP.summarize(self.groups)}")
        self.app._log(f"  clip is {clip_s:.0f}s; tap-drive will take ~{est_s:.0f}s")
        n_fwd = sum(1 for d, _, _ in self.taps if d == "forward")
        if n_fwd < 6 and clip_s > 8:
            self.app._log("  ⚠ very few forward taps — taps are too COARSE for this "
                          "cage. Lower 'fwd ms' or set a larger Cage W×H so the "
                          "path isn't scaled down so far.")
        self.app._log("  " + ",  ".join(f"{g.direction}x{g.count}" for g in self.groups))
        if not (self.drive_var and self.drive_var.get()):
            self.app._log("Tap drive: previewing on canvas only — tick "
                          "'Drive mice from CSV' to actually tap the mice.")
        else:
            targets = self.app._targets()
            if not targets:
                self.app._log("Tap drive: 'Drive mice from CSV' is on but NO mice "
                              "connected — connect first.")
            else:
                self.app._log(f"Tap drive: tapping {len(targets)} mouse(mice): "
                              f"{', '.join(targets)}")
        self._step()

    def pause(self):
        if self.after_id is not None:
            self.app.root.after_cancel(self.after_id)
            self.after_id = None
        if self.drive_var and self.drive_var.get():
            self.app.send_stop("tap stop")

    def reset(self):
        self.pause()
        self.i = 0
        self.build_plan()
        self._reset_pose()
        self._redraw()
        self._set_status("ready")

    def _reset_pose(self):
        if self.legs:
            self.rx, self.ry = self.legs[0]
            if len(self.legs) > 1:
                self.rtheta = T.heading_of(self.legs[0], self.legs[1])
        else:
            self.rx = self.ry = self.rtheta = 0.0

    def _set_status(self, extra=""):
        if self.status_var:
            self.status_var.set(f"tap {self.i}/{len(self.taps)}   {extra}")

    def _step(self):
        """ON phase: emit the current tap's direction, then schedule the OFF."""
        if self.i >= len(self.taps):
            self.pause()
            self._set_status("done")
            self.app._log("Tap drive: finished.")
            return
        direction, on_ms, _gap = self.taps[self.i]
        if self.drive_var and self.drive_var.get():
            self.app.send_dir(direction, self.speed_pct)
        self._set_status(f"{direction}")
        self.after_id = self.app.root.after(on_ms, self._tap_off)

    def _tap_off(self):
        """OFF phase: stop the toy, dead-reckon the tap, wait the gap, advance."""
        direction, _on, gap_ms = self.taps[self.i]
        if self.drive_var and self.drive_var.get():
            self.app.send_stop("tap gap")
        if direction == "forward":
            self.rx += self.m_per_tap * math.cos(self.rtheta)
            self.ry += self.m_per_tap * math.sin(self.rtheta)
        elif direction == "left":
            self.rtheta += math.radians(self.deg_per_tap)
        elif direction == "right":
            self.rtheta -= math.radians(self.deg_per_tap)
        self._redraw()
        self.i += 1
        self.after_id = self.app.root.after(gap_ms, self._step)

    def _redraw(self):
        c = self.canvas
        fit = self.app.player.fit
        if c is None or fit is None:
            return
        c.delete("all")
        w = int(c["width"]); h = int(c["height"])
        half_w = fit.cage_w / 2
        half_h = fit.cage_h / 2
        span = max(half_w, half_h) * 2 * 1.08 or 1.0
        def to_px(x, y):
            return (w / 2 + x / span * w, h / 2 + y / span * h)
        # cage + safe zone
        c.create_rectangle(*to_px(-half_w, -half_h), *to_px(half_w, half_h),
                           outline="#8a8aa8", width=2)
        m = fit.margin
        c.create_rectangle(*to_px(-half_w + m, -half_h + m),
                           *to_px(half_w - m, half_h - m),
                           outline="#3aa655", dash=(4, 3))
        # simplified route (the legs the taps actually follow)
        flat = []
        for x, y in self.legs:
            px, py = to_px(x, y); flat += [px, py]
        if len(flat) >= 4:
            c.create_line(*flat, fill="#c9a23a", width=2)
        for x, y in self.legs:
            px, py = to_px(x, y)
            c.create_oval(px - 2, py - 2, px + 2, py + 2, fill="#c9a23a", outline="")
        # dead-reckoned toy pose
        rpx, rpy = to_px(self.rx, self.ry)
        c.create_oval(rpx - 6, rpy - 6, rpx + 6, rpy + 6, fill="#4dabf7",
                      outline="#e8e8f0", width=1)
        hx = self.rx + 0.05 * math.cos(self.rtheta)
        hy = self.ry + 0.05 * math.sin(self.rtheta)
        c.create_line(rpx, rpy, *to_px(hx, hy), fill="#4dabf7", width=2)


# ---- Choreographer (multiple mice, one clip each, shared clock) ------------

class Choreographer:
    """Plays several MouseTracks together on one real-time clock: each track
    drives its own mouse with its own clip, all advancing in lockstep so 4-6
    mice replay different trajectories simultaneously in the shared cage."""
    MASTER_DT = 1.0 / 30.0

    def __init__(self, app, canvas: tk.Canvas, status_var: tk.StringVar):
        self.app = app
        self.canvas = canvas
        self.status_var = status_var
        self.tracks: dict[str, MouseTrack] = {}
        self.after_id: str | None = None

    def set_tracks(self, tracks: dict[str, MouseTrack]):
        self.pause()
        self.tracks = tracks
        self.reset()
        self.app._bento_rebuild()

    def play(self):
        if not self.tracks:
            self.app._log("Choreo: no tracks prepared — assign clips and press Prepare.")
            return
        if self.after_id is not None:
            return
        if not (self.app.drive_from_csv.get()):
            self.app._log("Choreo: previewing on canvas only — tick 'Drive mice from CSV' to move them.")
        else:
            self.app._log(f"Choreo: driving {len(self.tracks)} mice: {', '.join(self.tracks)}")
        self._tick()

    def pause(self):
        if self.after_id is not None:
            self.app.root.after_cancel(self.after_id)
            self.after_id = None
        if self.app.drive_from_csv.get():
            for slug in self.tracks:
                self.app.send_one(slug, build_stop())

    def reset(self):
        self.pause()
        for tr in self.tracks.values():
            tr.reset()
        self._redraw()
        self.app._bento_tick()
        if self.status_var:
            self.status_var.set(f"{len(self.tracks)} track(s) ready")

    def _tick(self):
        drive_on = self.app.drive_from_csv.get()
        all_done = True
        for slug, tr in self.tracks.items():
            direction, byte, drive_now = tr.advance(self.MASTER_DT)
            if not tr.done:
                all_done = False
            if drive_on:
                if drive_now and direction is not None:
                    self.app.send_one(slug, build_raw(direction, byte))
                else:
                    self.app.send_one(slug, build_stop())
        self._redraw()
        self.app._bento_tick()
        if self.status_var:
            parts = [f"{s}:{tr.idx}/{len(tr.frames)}" for s, tr in self.tracks.items()]
            self.status_var.set("  ".join(parts))
        if all_done:
            self.pause()
            if self.status_var:
                self.status_var.set("done")
            return
        self.after_id = self.app.root.after(int(1000 * self.MASTER_DT), self._tick)

    def _redraw(self):
        c = self.canvas
        if c is None:
            return
        c.delete("all")
        w = int(c["width"]); h = int(c["height"])
        half_w = self.app.cage_w / 2
        half_h = self.app.cage_h / 2
        m = self.app.cage_margin
        span = max(half_w, half_h) * 2 * 1.08 or 1.0
        def to_px(x, y):
            return (w / 2 + x / span * w, h / 2 + y / span * h)
        # cage + safe zone
        c.create_rectangle(*to_px(-half_w, -half_h), *to_px(half_w, half_h),
                           outline="#8a8aa8", width=2)
        c.create_rectangle(*to_px(-half_w + m, -half_h + m), *to_px(half_w - m, half_h - m),
                           outline="#3aa655", dash=(4, 3))
        # each track
        for i, (slug, tr) in enumerate(self.tracks.items()):
            col = tr.color
            flat = []
            for x, y in tr.frames:
                px, py = to_px(x, y); flat += [px, py]
            if len(flat) >= 4:
                c.create_line(*flat, fill=col, width=1)
            if tr.frames:
                mx, my = tr.frames[min(tr.idx, len(tr.frames) - 1)]
                px, py = to_px(mx, my)
                c.create_oval(px - 4, py - 4, px + 4, py + 4, fill=col, outline="")
            rpx, rpy = to_px(tr.rx, tr.ry)
            c.create_oval(rpx - 6, rpy - 6, rpx + 6, rpy + 6, fill=col,
                          outline="#e8e8f0", width=1)
            hx = tr.rx + 0.04 * math.cos(tr.rtheta)
            hy = tr.ry + 0.04 * math.sin(tr.rtheta)
            c.create_line(rpx, rpy, *to_px(hx, hy), fill=col, width=2)
            # legend
            c.create_text(8, 8 + i * 14, anchor="nw", fill=col,
                          font=("Segoe UI", 8, "bold"),
                          text=f"■ {slug}: {tr.label}")


# ---- Bento view (one video+trajectory tile per mouse) ----------------------

class BentoView(tk.Toplevel):
    """Per-mouse video + trajectory window with two layouts:

    * "Show view" (default) — one row per rover: its cage trajectory beside one
      or more camera tiles, each with its own angle dropdown and add/remove, so
      you can curate exactly which cameras are on screen. Resizable + F11
      fullscreen with embedded transport, for driving a single HDMI screen.
    * "Overlay grid" — the original compact bento: one tile per rover with the
      trajectory drawn on top of a single shared camera angle.

    Each camera tile is one decode; trajectory tiles are free (vector draws)."""
    DEFAULT_CAMS = (1, 4, 9)             # preferred angle order for new cam tiles
    GRID_TILE_W, GRID_TILE_H = 360, 240
    MAX_DECODES_WARN = 6                 # software-decode budget on the Pi 5

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Per-mouse view — trajectory + cameras")
        self.configure(background="#11111a")
        self.mode = "show"               # "show" | "grid"
        self._fullscreen = False
        self._cfg_after: str | None = None
        self.rows: dict[str, dict] = {}      # show mode: slug -> row dict
        self.tiles: dict[str, dict] = {}     # grid mode: slug -> tile dict
        self.grid_cam_var = tk.IntVar(value=self.DEFAULT_CAMS[0])
        self._build_toolbar()
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, padx=4, pady=4)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.bind("<Escape>", lambda e: self.exit_fullscreen())
        self.bind("<Configure>", self._on_configure)
        # keyboard transport so the view stays controllable in fullscreen
        self.bind("<space>", lambda e: self._toggle_play())
        self.bind("<Key-r>", lambda e: self.app.on_choreo_reset())
        self.rebuild()

    # ---- toolbar / transport ----
    def _build_toolbar(self):
        self.bar = ttk.Frame(self); self.bar.pack(fill="x", padx=6, pady=4)
        self.play_btn = ttk.Button(self.bar, text="▶ Play", width=8,
                                   command=self._toggle_play)
        self.play_btn.pack(side="left", padx=2)
        ttk.Button(self.bar, text="⟲ Reset", width=7,
                   command=self.app.on_choreo_reset).pack(side="left", padx=2)
        ttk.Separator(self.bar, orient="vertical").pack(side="left", fill="y", padx=8)
        self.mode_var = tk.StringVar(value=self.mode)
        ttk.Radiobutton(self.bar, text="Show view", value="show",
                        variable=self.mode_var, command=self._on_mode).pack(side="left")
        ttk.Radiobutton(self.bar, text="Overlay grid", value="grid",
                        variable=self.mode_var, command=self._on_mode).pack(side="left")
        # grid-mode global camera angle (only shown in grid mode)
        self.grid_cam_bar = ttk.Frame(self.bar)
        ttk.Label(self.grid_cam_bar, text="  angle:").pack(side="left")
        for cam in self.DEFAULT_CAMS:
            ttk.Radiobutton(self.grid_cam_bar, text=str(cam), value=cam,
                            variable=self.grid_cam_var,
                            command=self.tick).pack(side="left")
        ttk.Separator(self.bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(self.bar, text="⛶ Fullscreen (F11)",
                   command=self.toggle_fullscreen).pack(side="left", padx=2)
        self.note = ttk.Label(self.bar, text="", foreground="#aaa")
        self.note.pack(side="left", padx=12)

    def _toggle_play(self):
        ch = self.app.choreographer
        ch.pause() if ch.after_id is not None else ch.play()
        self._refresh_play_btn()

    def _refresh_play_btn(self):
        playing = self.app.choreographer.after_id is not None
        self.play_btn.config(text="⏸ Pause" if playing else "▶ Play")

    def toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        self.attributes("-fullscreen", self._fullscreen)
        if self._fullscreen:
            self.bar.pack_forget()           # clean presentation screen
        else:
            self.bar.pack(fill="x", padx=6, pady=4, before=self.body)

    def exit_fullscreen(self):
        if self._fullscreen:
            self.toggle_fullscreen()

    def _on_mode(self):
        self.mode = self.mode_var.get()
        self.rebuild()

    def _on_configure(self, _e):
        # repaint after a resize settles, so tiles refill when paused too
        if self._cfg_after is not None:
            self.after_cancel(self._cfg_after)
        self._cfg_after = self.after(120, self.tick)

    def close(self):
        self._release_videos()
        self.app.bento = None
        self.destroy()

    def _release_videos(self):
        for r in self.rows.values():
            for c in r["cams"]:
                c["video"].release()
        for t in self.tiles.values():
            t["video"].release()

    # ---- (re)build ----
    def rebuild(self):
        """(Re)build the layout for the current tracks. Call after Prepare."""
        for child in self.body.winfo_children():
            child.destroy()
        self._release_videos()
        self.rows = {}; self.tiles = {}
        # grid-mode global angle picker: only relevant in overlay-grid mode
        self.grid_cam_bar.pack_forget()
        if self.mode == "grid":
            self.grid_cam_bar.pack(side="left")
        tracks = self.app.choreographer.tracks
        if not tracks:
            ttk.Label(self.body, foreground="#aaa",
                      text="No tracks yet — assign clips and press 'Prepare tracks'."
                      ).pack(padx=20, pady=20)
            self.note.config(text="")
            return
        if self.mode == "show":
            self._build_show(tracks)
        else:
            self._build_grid(tracks)
        self._update_note()
        self.after(60, self.tick)            # first paint once geometry settles

    def _build_show(self, tracks):
        for i, (slug, tr) in enumerate(tracks.items()):
            row = tk.Frame(self.body, background=tr.color,
                           highlightbackground=tr.color, highlightthickness=2)
            row.grid(row=i, column=0, sticky="nsew", padx=3, pady=3)
            self.body.rowconfigure(i, weight=1)
            self.body.columnconfigure(0, weight=1)
            tk.Label(row, text=f"■\n{slug}", bg=tr.color, fg="white", width=8,
                     font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="ns")
            tcv = tk.Canvas(row, width=220, height=180, bg="#11111a",
                            highlightthickness=0)
            tcv.grid(row=0, column=1, sticky="nsew", padx=2, pady=2)
            holder = tk.Frame(row, bg=tr.color)
            holder.grid(row=0, column=2, sticky="nsew")
            row.rowconfigure(0, weight=1)
            row.columnconfigure(1, weight=1, minsize=160)   # trajectory
            row.columnconfigure(2, weight=2)                # cameras
            addb = tk.Button(row, text="＋\ncam", command=lambda s=slug: self._add_cam_tile(s),
                             bg="#23232e", fg="white", relief="flat", width=4)
            addb.grid(row=0, column=3, sticky="ns", padx=2)
            avail = sorted(self.app.choreo_cam_mp4s.get(slug, {}).keys())
            self.rows[slug] = {"frame": row, "traj": tcv, "holder": holder,
                               "avail": avail, "cams": [], "addb": addb}
            if avail:
                self._add_cam_tile(slug, avail[0])
            else:
                tk.Label(holder, text="(no video for this clip)", bg=tr.color,
                         fg="#eee").pack(padx=10, pady=10)
                addb.config(state="disabled")

    def _add_cam_tile(self, slug: str, cam: int | None = None):
        r = self.rows.get(slug)
        if not r or not r["avail"]:
            return
        avail = r["avail"]
        if cam is None:
            used = [c["var"].get() for c in r["cams"]]
            cam = next((a for a in avail if a not in used), avail[0])
        holder = r["holder"]
        col = len(r["cams"])
        cell = tk.Frame(holder, bg="#11111a")
        cell.grid(row=0, column=col, sticky="nsew", padx=1, pady=1)
        holder.columnconfigure(col, weight=1)
        holder.rowconfigure(0, weight=1)
        top = tk.Frame(cell, bg="#11111a"); top.pack(fill="x")
        tk.Label(top, text="cam", bg="#11111a", fg="#aaa",
                 font=("Segoe UI", 8)).pack(side="left")
        var = tk.IntVar(value=cam)
        om = tk.OptionMenu(top, var, *avail,
                           command=lambda _v: self.tick())
        om.config(bg="#23232e", fg="white", highlightthickness=0, bd=0,
                  font=("Segoe UI", 8), width=3)
        om["menu"].config(bg="#23232e", fg="white")
        om.pack(side="left")
        tk.Button(top, text="✕", bd=0, relief="flat", bg="#11111a", fg="#f88",
                  font=("Segoe UI", 8),
                  command=lambda c=cell: self._remove_cam_tile(slug, c)).pack(side="right")
        cv = tk.Canvas(cell, width=160, height=120, bg="#11111a", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        video = TileVideo(cv)
        video.set_sources({c: str(p)
                           for c, p in self.app.choreo_cam_mp4s.get(slug, {}).items()})
        r["cams"].append({"cell": cell, "canvas": cv, "video": video, "var": var})
        self._update_note()
        self.tick()

    def _remove_cam_tile(self, slug: str, cell):
        r = self.rows.get(slug)
        if not r:
            return
        keep = []
        for c in r["cams"]:
            if c["cell"] is cell:
                c["video"].release(); c["cell"].destroy()
            else:
                keep.append(c)
        for col, c in enumerate(keep):       # reflow columns
            c["cell"].grid_configure(column=col)
        r["cams"] = keep
        self._update_note()

    def _build_grid(self, tracks):
        cols = max(1, int(math.ceil(math.sqrt(len(tracks)))))
        for i, (slug, tr) in enumerate(tracks.items()):
            r, c = divmod(i, cols)
            cell = tk.Frame(self.body, background=tr.color,
                            highlightbackground=tr.color, highlightthickness=2)
            cell.grid(row=r, column=c, padx=4, pady=4, sticky="nsew")
            self.body.rowconfigure(r, weight=1); self.body.columnconfigure(c, weight=1)
            title = tk.Label(cell, anchor="w", fg="white", background=tr.color,
                             font=("Segoe UI", 9, "bold"))
            title.pack(fill="x")
            cv = tk.Canvas(cell, width=self.GRID_TILE_W, height=self.GRID_TILE_H,
                           background="#11111a", highlightthickness=0)
            cv.pack(fill="both", expand=True)
            video = TileVideo(cv)
            video.set_sources({cam: str(p)
                               for cam, p in self.app.choreo_cam_mp4s.get(slug, {}).items()})
            self.tiles[slug] = {"canvas": cv, "video": video, "title": title}

    def _update_note(self):
        total = sum(len(r["cams"]) for r in self.rows.values())
        if self.mode != "show":
            self.note.config(text="")
            return
        msg = f"{total} camera stream(s) → {total} decode(s)"
        if total > self.MAX_DECODES_WARN:
            msg += f"   ⚠ may drop frames past ~{self.MAX_DECODES_WARN} on the Pi"
        self.note.config(text=msg)

    # ---- per-frame repaint ----
    def tick(self):
        if not self.winfo_exists():           # a pending after() fired post-close
            return
        self._refresh_play_btn()
        if self.mode == "show":
            self._tick_show()
        else:
            self._tick_grid()

    def _tick_show(self):
        for slug, r in self.rows.items():
            tr = self.app.choreographer.tracks.get(slug)
            if tr is None:
                continue
            self._draw_traj(r["traj"], tr)
            for c in r["cams"]:
                cv = c["canvas"]
                w = max(1, cv.winfo_width()); h = max(1, cv.winfo_height())
                c["video"].show(c["var"].get(), tr.idx, w, h)

    def _tick_grid(self):
        cam = self.grid_cam_var.get()
        for slug, t in self.tiles.items():
            tr = self.app.choreographer.tracks.get(slug)
            if tr is None:
                continue
            cv = t["canvas"]
            w = max(1, cv.winfo_width()); h = max(1, cv.winfo_height())
            painted = t["video"].show(cam, tr.idx, w, h)
            cam_txt = f"cam {cam}" if t["video"].has(cam) else "no video"
            t["title"].config(text=f"■ {slug}  ·  {tr.label}  ·  {cam_txt}  "
                                   f"({tr.idx}/{len(tr.frames)})")
            self._draw_traj_overlay(cv, tr, w, h)

    # ---- drawing helpers ----
    def _to_px(self, w, h):
        half_w = self.app.cage_w / 2
        half_h = self.app.cage_h / 2
        span = max(half_w, half_h) * 2 * 1.08 or 1.0
        return half_w, half_h, (lambda x, y: (w / 2 + x / span * w, h / 2 + y / span * h))

    def _draw_traj(self, cv: tk.Canvas, tr: "MouseTrack"):
        """Standalone trajectory tile: cage box + safe zone + path + live pose."""
        cv.delete("all")
        w = max(1, cv.winfo_width()); h = max(1, cv.winfo_height())
        half_w, half_h, to_px = self._to_px(w, h)
        m = self.app.cage_margin
        cv.create_rectangle(*to_px(-half_w, -half_h), *to_px(half_w, half_h),
                            outline="#8a8aa8", width=1)
        cv.create_rectangle(*to_px(-half_w + m, -half_h + m), *to_px(half_w - m, half_h - m),
                            outline="#3aa655", dash=(4, 3))
        self._draw_path_pose(cv, tr, to_px, tags=None)
        cv.create_text(6, 6, anchor="nw", fill=tr.color, font=("Segoe UI", 8, "bold"),
                       text=f"{tr.label}  {tr.idx}/{len(tr.frames)}")

    def _draw_traj_overlay(self, cv: tk.Canvas, tr: "MouseTrack", w, h):
        """Grid mode: path + pose drawn over the camera frame (no cage box)."""
        cv.delete("traj")
        _, _, to_px = self._to_px(w, h)
        self._draw_path_pose(cv, tr, to_px, tags="traj")

    def _draw_path_pose(self, cv, tr, to_px, tags):
        kw = {"tags": tags} if tags else {}
        flat = []
        for x, y in tr.frames:
            px, py = to_px(x, y); flat += [px, py]
        if len(flat) >= 4:
            cv.create_line(*flat, fill=tr.color, width=1, **kw)
        if tr.frames:
            mx, my = tr.frames[min(tr.idx, len(tr.frames) - 1)]
            px, py = to_px(mx, my)
            cv.create_oval(px - 3, py - 3, px + 3, py + 3, fill=tr.color, outline="", **kw)
        rpx, rpy = to_px(tr.rx, tr.ry)
        cv.create_oval(rpx - 6, rpy - 6, rpx + 6, rpy + 6,
                       fill=tr.color, outline="#ffffff", width=2, **kw)
        hx = tr.rx + 0.05 * math.cos(tr.rtheta)
        hy = tr.ry + 0.05 * math.sin(tr.rtheta)
        cv.create_line(rpx, rpy, *to_px(hx, hy), fill="#ffffff", width=2, **kw)


# ---- App -------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Toy Mouse Fleet Control")
        root.geometry("1180x860")
        root.minsize(1000, 760)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.config = load_config()
        self.fleet = MouseFleet(self._log, notify_cb=self._on_notify)
        self.speed_model = SpeedModel.load()

        # cage geometry (metres)
        self.cage_w = float(self.config.get("cage_w", 1.0))
        self.cage_h = float(self.config.get("cage_h", 1.0))
        self.cage_margin = float(self.config.get("cage_margin", 0.1))

        self._found: list[tuple[str, str, int]] = []   # (mac, name, rssi)
        self._conn_slugs: list[str] = []               # parallel to conn_list
        self._held: set[str] = set()
        self._held_after: str | None = None
        # manual "tap mode": one press = a single fixed-duration nudge, then
        # auto-stop (mirrors a mobile-app tap) instead of hold-to-drive.
        self._tap_after: str | None = None
        self._tap_dir: str | None = None
        self._tap_end = 0.0
        # preprogrammed tap sequence (hand-authored list of nudges)
        self._seq_taps: list[str] = []
        self._seq_i = 0
        self._seq_after: str | None = None
        self._seq_running = False
        self._seq_end = 0.0

        # catalog / multicam state
        self.videos: dict[int, VideoSidecar] = {}       # cam_idx -> sidecar
        self.cam_tiles: dict[int, ttk.Frame] = {}
        self.cam_holders: dict[int, tk.Frame] = {}      # cam_idx -> fixed-size video holder
        self.expanded_cam: int | None = None            # cam shown large (click to toggle)
        self.cam_vars: dict[int, tk.BooleanVar] = {}
        self.cat_specs: dict[str, ClipSpec] = {}
        self.fetcher: RollingFetcher | None = None

        # per-mouse choreography state
        self.choreo_assign: dict[str, tk.StringVar] = {}   # slug -> chosen clip label
        self.choreo_files: dict[str, str] = {}             # slug -> browsed CSV path
        self.choreo_cam_mp4s: dict[str, dict[int, Path]] = {}  # slug -> {cam: mp4}
        self.BROWSE_SENTINEL = "— browse CSV… —"
        self.bento: "BentoView | None" = None

        self._prev_connected: set[str] = set()
        self._beat_warned = False

        self._build_ui()
        self._bind_keys()
        self._poll_log()
        self._watchdog()

        ok, msg = protocol_ready()
        self._log(f"Speed model: v_min={self.speed_model.v_min:.2f} v_max={self.speed_model.v_max:.2f} m/s"
                  + ("  (PROVISIONAL — run calibrate_speed.py)" if self.speed_model.provisional else ""))
        if not ok:
            self._log(f"NOTE: {msg}")

    # ---- UI ----
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ---- scrollable container so every card is reachable on any screen ----
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        self._scroll_canvas = tk.Canvas(outer, highlightthickness=0)
        vbar = ttk.Scrollbar(outer, orient="vertical",
                             command=self._scroll_canvas.yview)
        self._scroll_canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        self._scroll_canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self._scroll_canvas)
        self._body_win = self._scroll_canvas.create_window(
            (0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self._scroll_canvas.configure(
            scrollregion=self._scroll_canvas.bbox("all")))
        self._scroll_canvas.bind("<Configure>", lambda e: self._scroll_canvas
                                 .itemconfigure(self._body_win, width=e.width))
        self._scroll_canvas.bind_all(
            "<MouseWheel>",
            lambda e: self._scroll_canvas.yview_scroll(int(-e.delta / 120), "units"))

        # ===== Connection / fleet =====
        top = ttk.LabelFrame(self.body, text="Fleet connection")
        top.pack(fill="x", **pad)
        row1 = ttk.Frame(top); row1.pack(fill="x", padx=6, pady=4)
        ttk.Button(row1, text="🔍 Find mice", command=self.on_find).pack(side="left")
        ttk.Button(row1, text="Connect selected", command=self.on_connect_selected).pack(side="left", padx=4)
        ttk.Button(row1, text="Connect ALL found", command=self.on_connect_all).pack(side="left", padx=4)
        ttk.Button(row1, text="Disconnect all", command=self.on_disconnect_all).pack(side="left", padx=4)
        ttk.Label(row1, text="  manual MAC:").pack(side="left")
        self.mac_var = tk.StringVar(value=self.config.get("mouse_mac", DEFAULT_MOUSE_MAC))
        ttk.Entry(row1, textvariable=self.mac_var, width=20).pack(side="left", padx=2)
        ttk.Button(row1, text="+", width=3, command=self.on_connect_manual).pack(side="left")
        # Big always-available panic button (also bound to spacebar).
        tk.Button(row1, text="⛔ STOP ALL", command=lambda: self.emergency_stop("STOP ALL button"),
                  bg="#c0392b", fg="white", activebackground="#e74c3c",
                  font=("Segoe UI", 10, "bold"), relief="raised", bd=3
                  ).pack(side="right", padx=6)
        self.link_var = tk.StringVar(value="link: —")
        self.link_lbl = tk.Label(row1, textvariable=self.link_var, fg="#888")
        self.link_lbl.pack(side="right", padx=6)

        lists = ttk.Frame(top); lists.pack(fill="x", padx=6, pady=(0, 6))
        lcol = ttk.Frame(lists); lcol.pack(side="left", fill="both", expand=True)
        ttk.Label(lcol, text="Found (multi-select):").pack(anchor="w")
        self.found_list = tk.Listbox(lcol, height=4, selectmode="extended")
        self.found_list.pack(fill="both", expand=True)
        rcol = ttk.Frame(lists); rcol.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ttk.Label(rcol, text="Connected = drive target (none selected ⇒ all):").pack(anchor="w")
        self.conn_list = tk.Listbox(rcol, height=4, selectmode="extended")
        self.conn_list.pack(fill="both", expand=True)

        # ===== Middle: drive pad + video =====
        mid = ttk.Frame(self.body); mid.pack(fill="x", **pad)
        mid.columnconfigure(0, weight=0); mid.columnconfigure(1, weight=1)
        drive_box = ttk.LabelFrame(mid, text="Manual drive (targets)")
        drive_box.grid(row=0, column=0, sticky="nsw", padx=4, pady=4)
        self._build_drive_pad(drive_box)
        vid_box = ttk.LabelFrame(mid, text="Video")
        vid_box.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)
        ttk.Button(vid_box, text="Open mp4...", command=self.on_open_video).pack(anchor="w", padx=6, pady=4)
        self.video_label = tk.Label(vid_box, background="#1a1a1f", width=52, height=14)
        self.video_label.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.video = VideoSidecar(self.video_label)

        # ===== Clip catalog (B2 + local, same source as galaxy-rvr) =====
        cat = ttk.LabelFrame(self.body, text="Clip catalog  (Backblaze B2 / local)")
        cat.pack(fill="x", **pad)
        c1 = ttk.Frame(cat); c1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(c1, text="Clip:").pack(side="left")
        self.cat_csv_var = tk.StringVar(value=self.config.get("cat_csv", ""))
        self.cat_combo = ttk.Combobox(c1, textvariable=self.cat_csv_var, state="readonly", width=60)
        self.cat_combo.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(c1, text="↻ Refresh", command=self.on_cat_refresh).pack(side="left")
        c2 = ttk.Frame(cat); c2.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(c2, text="Cameras:").pack(side="left", padx=(0, 6))
        saved = set(self.config.get("cat_cams", [1, 4, 9, 12]))
        for cam in CAMERAS:
            v = tk.BooleanVar(value=(cam in saved))
            ttk.Checkbutton(c2, text=str(cam), variable=v,
                            command=self.on_cam_toggle).pack(side="left")
            self.cam_vars[cam] = v
        ttk.Button(c2, text="Get clip & load", command=self.on_cat_get).pack(side="left", padx=(12, 2))
        ttk.Label(c2, text="Stream").pack(side="left", padx=(12, 2))
        self.stream_minutes_var = tk.IntVar(value=self.config.get("stream_minutes", 5))
        ttk.Spinbox(c2, from_=2, to=20, width=3, textvariable=self.stream_minutes_var).pack(side="left")
        ttk.Label(c2, text="min").pack(side="left", padx=(2, 2))
        ttk.Button(c2, text="▶ Stream", command=self.on_cat_stream).pack(side="left", padx=2)
        self.cat_status = tk.StringVar(value="(click Refresh)")
        ttk.Label(c2, textvariable=self.cat_status, foreground="#888").pack(side="left", padx=10)

        # ===== Trajectory =====
        traj_box = ttk.LabelFrame(self.body, text="Movement CSV → cage playback")
        traj_box.pack(fill="both", expand=True, **pad)
        tr = ttk.Frame(traj_box); tr.pack(fill="x", padx=6, pady=4)
        ttk.Button(tr, text="Open CSV...", command=self.on_open_csv).pack(side="left")
        ttk.Label(tr, text="Bodypart:").pack(side="left", padx=(10, 2))
        self.bodypart_var = tk.StringVar(value=self.config.get("bodypart", "Tailbase"))
        bp = ttk.Combobox(tr, textvariable=self.bodypart_var, width=11, state="readonly",
                          values=("Tailbase", "SpineM", "SpineF", "Snout", "EarL", "EarR",
                                  "ForepawL", "ForepawR", "HindpawL", "HindpawR"))
        bp.pack(side="left")
        bp.bind("<<ComboboxSelected>>", lambda e: self.on_bodypart_change())
        ttk.Label(tr, text="src FPS:").pack(side="left", padx=(10, 2))
        self.fps_var = tk.DoubleVar(value=float(self.config.get("src_fps", 30.0)))
        ttk.Entry(tr, textvariable=self.fps_var, width=5).pack(side="left")
        ttk.Button(tr, text="▶ Play", command=self.on_play).pack(side="left", padx=(10, 2))
        ttk.Button(tr, text="⏸ Pause", command=self.on_pause).pack(side="left", padx=2)
        ttk.Button(tr, text="⟲ Reset", command=self.on_reset).pack(side="left", padx=2)
        self.drive_from_csv = tk.BooleanVar(value=False)
        ttk.Checkbutton(tr, text="Drive mice from CSV", variable=self.drive_from_csv).pack(side="left", padx=(12, 2))

        # seek / scrub bar: drag (or step) to find a good part of the clip.
        # Scrubbing never drives the mice — player.seek() pauses first.
        self._seek_guard = False
        sb = ttk.Frame(traj_box); sb.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(sb, text="Seek:").pack(side="left")
        ttk.Button(sb, text="⏮", width=2, command=lambda: self.on_seek_to(0)).pack(side="left")
        ttk.Button(sb, text="◀1s", width=4, command=lambda: self.on_seek_step(-1.0)).pack(side="left", padx=1)
        ttk.Button(sb, text="◀", width=2, command=lambda: self.on_seek_frames(-1)).pack(side="left")
        self.seek_var = tk.DoubleVar(value=0.0)
        self.seek_scale = ttk.Scale(sb, from_=0, to=1, orient="horizontal",
                                    variable=self.seek_var, command=self.on_seek)
        self.seek_scale.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(sb, text="▶", width=2, command=lambda: self.on_seek_frames(1)).pack(side="left")
        ttk.Button(sb, text="1s▶", width=4, command=lambda: self.on_seek_step(1.0)).pack(side="left", padx=1)
        ttk.Button(sb, text="⏭", width=2, command=lambda: self.on_seek_to(10**9)).pack(side="left")
        self.seek_lbl = ttk.Label(sb, text="–", foreground="#888", width=16)
        self.seek_lbl.pack(side="left", padx=6)

        # cage settings row
        cr = ttk.Frame(traj_box); cr.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(cr, text="Cage W×H (m):").pack(side="left")
        self.cage_w_var = tk.DoubleVar(value=self.cage_w)
        self.cage_h_var = tk.DoubleVar(value=self.cage_h)
        self.cage_m_var = tk.DoubleVar(value=self.cage_margin)
        ttk.Entry(cr, textvariable=self.cage_w_var, width=5).pack(side="left", padx=2)
        ttk.Entry(cr, textvariable=self.cage_h_var, width=5).pack(side="left", padx=2)
        ttk.Label(cr, text="margin (m):").pack(side="left", padx=(8, 2))
        ttk.Entry(cr, textvariable=self.cage_m_var, width=5).pack(side="left", padx=2)
        ttk.Button(cr, text="Apply cage + refit", command=self.on_apply_cage).pack(side="left", padx=8)

        # tap-drive row: discrete turn-by-turn taps (forward/left/right), the way
        # the mobile app drives the toy — an alternative to the continuous Play.
        # Each tap holds the direction for `on ms`. Distance/angle per tap are
        # DERIVED from that hold time via the calibration so the plan, the canvas
        # dead-reckoning, and the real toy all agree:
        #   forward tap distance = (cal_m / cal_s) * on_ms
        #   turn   tap angle     =  turn_rate      * on_ms
        tp = ttk.Frame(traj_box); tp.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(tp, text="Tap drive:", font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Button(tp, text="▶ Tap-drive", command=self.on_tap_play).pack(side="left", padx=(6, 2))
        ttk.Button(tp, text="⏹ Stop", command=self.on_tap_stop).pack(side="left", padx=2)
        ttk.Button(tp, text="⟲ Reset", command=self.on_tap_reset).pack(side="left", padx=2)
        def _tap_field(label, key, default, width=5):
            ttk.Label(tp, text=label).pack(side="left", padx=(8, 1))
            var = tk.DoubleVar(value=float(self.config.get(key, default)))
            e = ttk.Entry(tp, textvariable=var, width=width)
            e.pack(side="left")
            # persist + live-recompute whenever the field is edited & left
            e.bind("<FocusOut>", lambda ev: self._sync_tap_knobs())
            e.bind("<Return>", lambda ev: self._sync_tap_knobs())
            return var
        # hold-speed calibration: "2 m in 1.5 s" => 1.33 m/s at full hold
        self.tap_cal_m_var = _tap_field("hold", "tap_cal_m", 2.0, 4)
        ttk.Label(tp, text="m in").pack(side="left", padx=1)
        self.tap_cal_s_var = _tap_field("", "tap_cal_s", 1.5, 4)
        ttk.Label(tp, text="s").pack(side="left")
        # spin calibration: 7 turns in 3.3 s => ~764 deg/s
        self.tap_turnrate_var = _tap_field("spin", "tap_turn_rate", 763.6, 6)
        ttk.Label(tp, text="°/s").pack(side="left")
        # separate hold times: forward taps are long (cm-scale), turn taps short.
        # 100ms keeps taps fine (~13cm) so small cages don't collapse to 1 tap/leg.
        self.tap_on_var  = _tap_field("fwd ms", "tap_on_ms", 100, 5)
        self.tap_turn_on_var = _tap_field("turn ms", "tap_turn_on_ms", 40, 5)
        self.tap_gap_var = _tap_field("gap ms", "tap_gap_ms", 150, 5)
        self.tap_tol_var = _tap_field("simplify", "tap_tol", 0.04)
        self.tap_turn_var = _tap_field("min turn°", "tap_min_turn", 10.0)
        self.tap_calc_lbl = ttk.Label(tp, text="", foreground="#3aa6c9")
        self.tap_calc_lbl.pack(side="left", padx=8)

        viz = ttk.Frame(traj_box); viz.pack(fill="both", expand=True, padx=6, pady=4)
        self.canvas = tk.Canvas(viz, background="#2a2a3a", width=360, height=300,
                                highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=False)
        # multicam grid (one tile per camera, shown only when loaded + selected)
        self.cam_grid_f = ttk.Frame(viz)
        self.cam_grid_f.pack(side="left", fill="both", expand=True, padx=(10, 0))
        cell_w, cell_h = 168, 96
        for cam in CAMERAS:
            tile = ttk.Frame(self.cam_grid_f)
            caplbl = ttk.Label(tile, text=f"cam {cam}  ⤢", foreground="#888", cursor="hand2")
            caplbl.pack(anchor="w")
            holder = tk.Frame(tile, width=cell_w, height=cell_h, bg="#1a1a1f")
            holder.pack_propagate(False); holder.pack()
            lbl = tk.Label(holder, background="#1a1a1f", borderwidth=0, cursor="hand2")
            lbl.pack(fill="both", expand=True)
            # click the caption or the frame to expand/collapse this camera
            caplbl.bind("<Button-1>", lambda e, c=cam: self.on_cam_expand(c))
            lbl.bind("<Button-1>", lambda e, c=cam: self.on_cam_expand(c))
            sc = VideoSidecar(lbl); sc.DISPLAY_W = cell_w
            self.videos[cam] = sc
            self.cam_tiles[cam] = tile
            self.cam_holders[cam] = holder
        self._relayout_cam_grid()
        self.traj_status = tk.StringVar(value="(no CSV loaded)")
        ttk.Label(traj_box, textvariable=self.traj_status, foreground="#888"
                  ).pack(anchor="w", padx=10, pady=(0, 4))
        self.player = TrajectoryPlayer(self)
        self.player.canvas = self.canvas
        self.player.status_var = self.traj_status
        self.player.drive_var = self.drive_from_csv
        self.player.seek_hook = self._sync_seek_bar
        self._sync_seek_bar()
        # tap driver shares the same canvas + status line + drive toggle
        self.tap_driver = TapDriver(self)
        self.tap_driver.canvas = self.canvas
        self.tap_driver.status_var = self.traj_status
        self.tap_driver.drive_var = self.drive_from_csv

        # ===== Per-mouse choreography (a different clip per mouse) =====
        ch = ttk.LabelFrame(self.body, text="Per-mouse choreography  (a different clip per mouse)")
        ch.pack(fill="both", expand=True, **pad)
        ch1 = ttk.Frame(ch); ch1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(ch1, text="⟳ Sync mice", command=self.on_choreo_sync).pack(side="left")
        ttk.Button(ch1, text="Prepare tracks", command=self.on_choreo_prepare).pack(side="left", padx=4)
        ttk.Button(ch1, text="▶ Play all", command=self.on_choreo_play).pack(side="left", padx=(10, 2))
        ttk.Button(ch1, text="⏸ Pause", command=self.on_choreo_pause).pack(side="left", padx=2)
        ttk.Button(ch1, text="⟲ Reset", command=self.on_choreo_reset).pack(side="left", padx=2)
        ttk.Button(ch1, text="▦ Bento view", command=self.on_choreo_bento).pack(side="left", padx=(10, 2))
        self.choreo_with_video = tk.BooleanVar(value=bool(self.config.get("choreo_video", True)))
        def _save_video_pref():
            self.config["choreo_video"] = self.choreo_with_video.get(); save_config(self.config)
        ttk.Checkbutton(ch1, text="video tiles (cams 1,4,9)", variable=self.choreo_with_video,
                        command=_save_video_pref).pack(side="left", padx=4)
        ttk.Label(ch1, text="(uses the same 'Drive mice from CSV', bodypart, cage + FPS above)",
                  foreground="#888").pack(side="left", padx=10)
        chviz = ttk.Frame(ch); chviz.pack(fill="both", expand=True, padx=6, pady=4)
        # left: per-mouse assignment rows
        self.choreo_rows_f = ttk.Frame(chviz)
        self.choreo_rows_f.pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(self.choreo_rows_f, text="(click 'Sync mice' after connecting)",
                  foreground="#888").pack(anchor="w")
        # right: shared cage canvas
        self.choreo_canvas = tk.Canvas(chviz, background="#2a2a3a", width=360, height=280,
                                       highlightthickness=0)
        self.choreo_canvas.pack(side="left", fill="both", expand=True)
        self.choreo_status = tk.StringVar(value="(no tracks)")
        ttk.Label(ch, textvariable=self.choreo_status, foreground="#888"
                  ).pack(anchor="w", padx=10, pady=(0, 4))
        self.choreographer = Choreographer(self, self.choreo_canvas, self.choreo_status)

        # ===== Log =====
        log_frame = ttk.LabelFrame(self.body, text="Log")
        log_frame.pack(fill="both", expand=False, **pad)
        self.log_box = tk.Text(log_frame, height=7, wrap="word", state="disabled",
                               font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=4, pady=4)

    def _build_drive_pad(self, parent):
        ttk.Label(parent, text=f"Mode: {USE_MODE}", foreground="#888"
                  ).grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(4, 0))
        ttk.Label(parent, text="Speed:").grid(row=1, column=0, sticky="e", padx=4, pady=2)
        self.speed_var = tk.IntVar(value=self.config.get("speed", 60))
        ttk.Scale(parent, from_=0, to=100, orient="horizontal", variable=self.speed_var,
                  length=150, command=self._on_speed_change).grid(row=1, column=1, columnspan=2, sticky="ew", padx=2)
        self.speed_label = ttk.Label(parent, text=str(self.speed_var.get()))
        self.speed_label.grid(row=1, column=3, sticky="w", padx=4)

        def btn(text, cmd, r, c):
            b = ttk.Button(parent, text=text, width=8)
            b.bind("<ButtonPress-1>", lambda e: self._on_press(cmd))
            b.bind("<ButtonRelease-1>", lambda e: self._on_release(cmd))
            b.grid(row=r, column=c, padx=3, pady=3, sticky="ew")
        btn("↑ Fwd", "forward", 2, 1)
        btn("← Left", "left", 3, 0)
        btn("■ Stop", "stop", 3, 1)
        btn("→ Right", "right", 3, 2)
        btn("↓ Back", "back", 4, 1)
        ttk.Label(parent, text="Keys: ↑↓←→ hold to drive, space=stop",
                  foreground="#888").grid(row=5, column=0, columnspan=4, sticky="w", padx=6, pady=(4, 0))

        # Tap mode: one press = a single fixed-duration nudge (like tapping a
        # button in the mobile app), then auto-stop — instead of hold-to-drive.
        self.manual_tap_mode = tk.BooleanVar(
            value=bool(self.config.get("manual_tap_mode", False)))
        self.manual_tap_s_var = tk.DoubleVar(
            value=float(self.config.get("manual_tap_s", 0.2)))
        def _save_manual_tap(*_):
            self.config["manual_tap_mode"] = self.manual_tap_mode.get()
            try:
                self.config["manual_tap_s"] = max(0.05, float(self.manual_tap_s_var.get()))
            except (tk.TclError, ValueError):
                pass
            save_config(self.config)
        tapf = ttk.Frame(parent)
        tapf.grid(row=6, column=0, columnspan=4, sticky="w", padx=6, pady=(2, 6))
        ttk.Checkbutton(tapf, text="Tap mode (press = nudge)",
                        variable=self.manual_tap_mode,
                        command=_save_manual_tap).pack(side="left")
        ttk.Label(tapf, text="each tap").pack(side="left", padx=(8, 1))
        e = ttk.Entry(tapf, textvariable=self.manual_tap_s_var, width=4)
        e.pack(side="left")
        e.bind("<FocusOut>", _save_manual_tap)
        e.bind("<Return>", _save_manual_tap)
        ttk.Label(tapf, text="s").pack(side="left")

        # Preprogrammed tap sequence: a hand-authored list of nudges (e.g.
        # "forward, left, forward x2, right …") played as timed taps — forward
        # taps run for 'fwd s', turns for 'turn s', with a 'gap s' settle
        # between each so pivots don't blend into the next move.
        DEFAULT_SEQ = ("forward, left, forward, right, forward x2, right, "
                       "forward, left, forward x2, right, forward, left, "
                       "forward, forward")
        seqw = ttk.Frame(parent)
        seqw.grid(row=7, column=0, columnspan=4, sticky="ew", padx=6, pady=(0, 6))
        r1 = ttk.Frame(seqw); r1.pack(fill="x")
        ttk.Label(r1, text="Tap sequence:").pack(side="left")
        self.seq_var = tk.StringVar(value=self.config.get("tap_sequence", DEFAULT_SEQ))
        ttk.Entry(r1, textvariable=self.seq_var, width=34).pack(
            side="left", fill="x", expand=True, padx=(4, 0))
        r2 = ttk.Frame(seqw); r2.pack(fill="x", pady=(2, 0))

        def _seq_field(label, key, default):
            ttk.Label(r2, text=label).pack(side="left", padx=(0, 1))
            var = tk.DoubleVar(value=float(self.config.get(key, default)))
            en = ttk.Entry(r2, textvariable=var, width=4)
            en.pack(side="left")
            en.bind("<FocusOut>", lambda ev: self._save_seq_cfg())
            en.bind("<Return>", lambda ev: self._save_seq_cfg())
            ttk.Label(r2, text="s").pack(side="left", padx=(0, 6))
            return var
        self.seq_fwd_s_var = _seq_field("fwd", "tap_seq_fwd_s", 0.2)
        self.seq_turn_s_var = _seq_field("turn", "tap_seq_turn_s", 0.15)
        self.seq_gap_s_var = _seq_field("gap", "tap_seq_gap_s", 0.1)
        ttk.Button(r2, text="▶ Run", command=self.on_seq_run).pack(side="left", padx=(4, 2))
        ttk.Button(r2, text="⏹ Stop", command=self.on_seq_stop).pack(side="left")
        self.seq_status_var = tk.StringVar(value="")
        ttk.Label(seqw, textvariable=self.seq_status_var, foreground="#3aa6c9"
                  ).pack(anchor="w", pady=(1, 0))

        parent.columnconfigure(1, weight=1); parent.columnconfigure(2, weight=1)

    def _on_speed_change(self, _v):
        self.speed_label.config(text=str(int(self.speed_var.get())))
        self.config["speed"] = int(self.speed_var.get())
        save_config(self.config)

    # ---- target resolution ----
    def _targets(self) -> list[str]:
        """Selected connected mice, or ALL connected if nothing is selected."""
        sel = self.conn_list.curselection()
        if sel:
            return [self._conn_slugs[i] for i in sel if i < len(self._conn_slugs)]
        return self.fleet.connected_slugs

    def _refresh_conn_list(self):
        self.conn_list.delete(0, "end")
        self._conn_slugs = []
        for slug in self.fleet.connected_slugs:
            h = self.fleet.mice[slug]
            self.conn_list.insert("end", f"{slug}  {h.mac}")
            self._conn_slugs.append(slug)

    # ---- BLE send ----
    def send_dir(self, direction: str, speed_pct: int | None = None):
        if speed_pct is None:
            speed_pct = int(self.speed_var.get())
        self.fleet.submit(self.fleet.send_to(self._targets(), build(direction, speed_pct), direction))

    def send_raw(self, direction: str, speed_byte: int, label: str = ""):
        self.fleet.submit(self.fleet.send_to(self._targets(), build_raw(direction, speed_byte), label))

    def send_stop(self, why: str = "stop"):
        self.fleet.submit(self.fleet.send_to(self._targets(), build_stop(), why))

    def send_one(self, slug: str, payload: bytes):
        """Send to exactly one mouse (used by the per-mouse choreographer)."""
        self.fleet.submit(self.fleet.send_to([slug], payload))

    # ---- range / link watchdog ----
    WATCHDOG_MS = 700
    STALE_BEAT_S = 4.0      # heartbeat older than this => warn (link going quiet)

    def _driving_active(self) -> bool:
        """Is any automated driver currently looping motion commands?"""
        return (self.player.after_id is not None
                or self.tap_driver.after_id is not None
                or self.choreographer.after_id is not None
                or self._seq_running)

    def _watchdog(self):
        try:
            cur = set(self.fleet.connected_slugs)
            beats = self.fleet.beats()
            driving = self._driving_active() and self.drive_from_csv.get()
            worst = max(beats.values()) if beats else 1e9
            # link indicator
            if not cur:
                self.link_var.set("link: no mice"); self.link_lbl.config(fg="#888")
            else:
                col = "#3aa655" if worst < 2.5 else ("#c9a23a" if worst < self.STALE_BEAT_S else "#e74c3c")
                self.link_var.set(f"link: {len(cur)} ok · beat {worst:.1f}s")
                self.link_lbl.config(fg=col)
            # DEFINITIVE halt: a mouse we had is no longer connected while driving
            dropped = self._prev_connected - cur
            if driving and dropped:
                self.emergency_stop(f"lost connection to {', '.join(sorted(dropped))} "
                                    "(out of range?)")
            # EARLY warning: heartbeat going stale mid-drive (link weakening)
            elif driving and worst >= self.STALE_BEAT_S:
                if not self._beat_warned:
                    self._beat_warned = True
                    self._log(f"⚠ link quiet ({worst:.1f}s since heartbeat) — mouse may "
                              "be nearing range. Stop is NOT guaranteed to reach it.")
            else:
                self._beat_warned = False
            self._prev_connected = cur
        except Exception:
            pass
        self.root.after(self.WATCHDOG_MS, self._watchdog)

    # ---- emergency stop ----
    def emergency_stop(self, why: str = ""):
        """Halt EVERYTHING: cancel every automated driver so nothing re-sends a
        motion frame after the stop, then hammer a stop at all connected mice.

        This is the fix for 'I pressed stop and it kept going': the toy latches
        its last command, so a lone stop is futile if Tap-drive/Play is still
        looping (it re-sends forward ~100 ms later). We must stop the LOOP too."""
        self.player.pause()
        self.tap_driver.pause()
        self.choreographer.pause()
        self._held.clear()
        if self._held_after is not None:
            self.root.after_cancel(self._held_after)
            self._held_after = None
        self._cancel_manual_tap()
        self._cancel_seq()
        self.fleet.submit(self.fleet.emergency_stop())
        self._log(f"⛔ EMERGENCY STOP{(' — ' + why) if why else ''}: all drivers "
                  "halted, stop sent x4.")

    # ---- hold-to-drive ----
    def _on_press(self, direction: str):
        if direction == "stop":
            self.emergency_stop("stop button")
            return
        if self.manual_tap_mode.get():
            self._manual_tap(direction)
            return
        self._held.add(direction)
        self.send_dir(direction)
        if self._held_after is None:
            self._held_after = self.root.after(150, self._tick_held)

    def _on_release(self, direction: str):
        # In tap mode the timed window stops the toy itself; release is a no-op
        # (and key-autorepeat shouldn't matter — see _manual_tap).
        if self.manual_tap_mode.get():
            return
        self._held.discard(direction)
        if not self._held:
            self.send_stop("release")

    # ---- tap mode (manual): one press = a single fixed-duration nudge ----
    def _manual_tap(self, direction: str):
        """Drive `direction` for `manual_tap_s` seconds, then auto-stop.

        The frame is re-sent every 100 ms through the window (like hold-to-
        drive) so the toy doesn't self-time-out mid-tap; a Stop closes it.
        A repeat press of the SAME direction while a tap is live is ignored so
        OS key-autorepeat can't silently extend one tap; a different direction
        cancels the current tap and starts fresh."""
        if self._tap_after is not None and self._tap_dir == direction:
            return
        try:
            secs = max(0.05, float(self.manual_tap_s_var.get()))
        except (tk.TclError, ValueError):
            secs = 0.2
        self._cancel_manual_tap()
        self._tap_dir = direction
        self._tap_end = time.monotonic() + secs
        self._manual_tap_tick()

    def _manual_tap_tick(self):
        if time.monotonic() >= self._tap_end:
            self.send_stop("tap done")
            self._tap_after = None
            self._tap_dir = None
            return
        self.send_dir(self._tap_dir)
        self._tap_after = self.root.after(100, self._manual_tap_tick)

    def _cancel_manual_tap(self):
        if self._tap_after is not None:
            self.root.after_cancel(self._tap_after)
            self._tap_after = None
        self._tap_dir = None

    # ---- preprogrammed tap sequence ----
    ALIASES = {"f": "forward", "fwd": "forward", "forward": "forward",
               "l": "left", "left": "left", "r": "right", "right": "right",
               "b": "back", "back": "back"}

    def _parse_tap_seq(self, text: str) -> list[str]:
        """Parse a human tap list into a flat list of directions.

        Accepts words or abbreviations (forward/f, left/l, right/r, back/b),
        separated by commas/spaces, with optional repeat counts written as
        'forward x2', 'forwardx2', 'fx2', or a bare 'x2' / number after a
        direction. Unrecognised tokens are ignored."""
        dirs: list[str] = []
        for t in re.split(r"[,\s]+", text.strip().lower()):
            if not t:
                continue
            if t in ("x", "*"):                      # multiplier marker; the
                continue                             # following number repeats
            if t.isdigit():                          # 'forward 2' / 'forward x 2'
                if dirs and int(t) >= 1:
                    dirs += [dirs[-1]] * (int(t) - 1)
                continue
            m = re.fullmatch(r"(?:x|\*)(\d+)", t)     # standalone 'x2'
            if m:
                if dirs and int(m.group(1)) >= 1:
                    dirs += [dirs[-1]] * (int(m.group(1)) - 1)
                continue
            m = re.fullmatch(r"([a-z]+)(?:x|\*)(\d+)", t)   # 'forwardx2' / 'fx2'
            if m and m.group(1) in self.ALIASES:
                dirs += [self.ALIASES[m.group(1)]] * int(m.group(2))
                continue
            if t in self.ALIASES:
                dirs.append(self.ALIASES[t])
        return dirs

    def _save_seq_cfg(self):
        self.config["tap_sequence"] = self.seq_var.get()
        for key, var in (("tap_seq_fwd_s", self.seq_fwd_s_var),
                         ("tap_seq_turn_s", self.seq_turn_s_var),
                         ("tap_seq_gap_s", self.seq_gap_s_var)):
            try:
                self.config[key] = max(0.0, float(var.get()))
            except (tk.TclError, ValueError):
                pass
        save_config(self.config)

    def on_seq_run(self):
        dirs = self._parse_tap_seq(self.seq_var.get())
        if not dirs:
            self._log("Tap sequence: nothing recognised — use words like "
                      "'forward, left, forward x2, right'.")
            return
        # one driver at a time: don't let Play/Tap-drive/Choreo fight the mice
        self.player.pause()
        self.tap_driver.pause()
        self.choreographer.pause()
        self._cancel_manual_tap()
        self._cancel_seq()
        self._save_seq_cfg()
        self._seq_taps = dirs
        self._seq_i = 0
        self._seq_running = True
        targets = self._targets()
        short = " ".join(d[0].upper() for d in dirs)
        self._log(f"Tap sequence: {len(dirs)} taps [{short}] — "
                  + (f"driving {', '.join(targets)}" if targets
                     else "NO mice connected (connect first)"))
        self._seq_step()

    def on_seq_stop(self):
        if self._seq_running or self._seq_after is not None:
            self._cancel_seq()
            self.send_stop("seq stop")
            self._log("Tap sequence: stopped.")
            self.seq_status_var.set("stopped")

    def _cancel_seq(self):
        self._seq_running = False
        if self._seq_after is not None:
            self.root.after_cancel(self._seq_after)
            self._seq_after = None

    def _seq_step(self):
        """Start the next tap: hold its direction for the per-tap duration."""
        if not self._seq_running:
            return
        if self._seq_i >= len(self._seq_taps):
            self.send_stop("seq done")
            self._seq_running = False
            self._seq_after = None
            self.seq_status_var.set("done")
            self._log("Tap sequence: finished.")
            return
        direction = self._seq_taps[self._seq_i]
        try:
            var = self.seq_fwd_s_var if direction in ("forward", "back") else self.seq_turn_s_var
            secs = max(0.05, float(var.get()))
        except (tk.TclError, ValueError):
            secs = 0.2
        self._seq_end = time.monotonic() + secs
        self.seq_status_var.set(f"{self._seq_i + 1}/{len(self._seq_taps)}: {direction}")
        self._seq_on(direction)

    def _seq_on(self, direction: str):
        """Re-send the tap's frame every 100 ms until its window ends, then
        Stop, wait the gap, and advance to the next tap."""
        if not self._seq_running:
            return
        if time.monotonic() >= self._seq_end:
            self.send_stop("seq gap")
            self._seq_i += 1
            try:
                gap = max(0.0, float(self.seq_gap_s_var.get()))
            except (tk.TclError, ValueError):
                gap = 0.1
            self._seq_after = self.root.after(max(1, int(gap * 1000)), self._seq_step)
            return
        self.send_dir(direction)
        self._seq_after = self.root.after(100, lambda: self._seq_on(direction))

    def _tick_held(self):
        self._held_after = None
        if not self._held:
            return
        for d in list(self._held):
            self.send_dir(d)
        self._held_after = self.root.after(200, self._tick_held)

    def _bind_keys(self):
        mapping = {"Up": "forward", "Down": "back", "Left": "left", "Right": "right"}
        for keysym, d in mapping.items():
            self.root.bind(f"<KeyPress-{keysym}>", lambda e, dd=d: self._on_press(dd))
            self.root.bind(f"<KeyRelease-{keysym}>", lambda e, dd=d: self._on_release(dd))
        self.root.bind("<KeyPress-space>", lambda e: self._on_press("stop"))

    # ---- connection handlers ----
    def on_find(self):
        self.found_list.delete(0, "end")
        fut = self.fleet.submit(self.fleet.find_all_mice(8.0))
        def done(_):
            try:
                found = fut.result()
            except Exception as e:
                self._log(f"Find failed: {e}"); return
            self._found = found
            for mac, name, rssi in found:
                self.found_list.insert("end",
                    f"{R.describe_mac(mac):<14} {rssi:>4} dBm  {mac}  {name!r}")
            if found:
                # preselect all so 'Connect selected' grabs everything by default
                self.found_list.selection_set(0, "end")
        fut.add_done_callback(lambda f: self.root.after(0, done, f))

    def _connect_macs(self, macs: list[str]):
        if not macs:
            self._log("Nothing to connect — click Find mice first.")
            return
        self._log(f"Connecting to {len(macs)} mouse(mice): {', '.join(macs)}")
        for mac in macs:
            entry = R.entry_for_mac(mac)
            slug = entry["slug"] if entry else None   # None -> fleet auto-slugs
            if entry and entry["faulty"]:
                self._log(f"NOTE: {mac} is mouse #{entry['number']} — flagged FAULTY.")
            fut = self.fleet.submit(self.fleet.connect(mac, slug))
            fut.add_done_callback(lambda f: self.root.after(0, self._after_connect, f))
        # remember the first as the default single mouse
        if macs:
            self.config["mouse_mac"] = macs[0]
            save_config(self.config)

    def _after_connect(self, _f):
        try:
            _f.result()
        except Exception as e:
            self._log(f"Connect failed: {e}")
        self._refresh_conn_list()

    def on_connect_selected(self):
        sel = self.found_list.curselection()
        macs = [self._found[i][0] for i in sel if i < len(self._found)]
        if not macs:
            self._log("No mice selected in the Found list.")
            return
        self._connect_macs(macs)

    def on_connect_all(self):
        self._connect_macs([m for m, _, _ in self._found])

    def on_connect_manual(self):
        mac = self.mac_var.get().strip()
        if mac:
            self._connect_macs([mac])

    def on_disconnect_all(self):
        self.player.pause()
        fut = self.fleet.submit(self.fleet.disconnect_all())
        fut.add_done_callback(lambda f: self.root.after(0, self._refresh_conn_list))

    def _on_notify(self, slug: str, data: bytes):
        # heartbeat/sleep-countdown frames; keep it quiet unless it's unusual
        if not (len(data) == 5 and data[0] == 0x4B):
            self._log(f"[{slug}] <- {data.hex()}")

    # ---- video / csv ----
    def on_open_video(self):
        path = filedialog.askopenfilename(
            title="Open video", filetypes=[("Video", "*.mp4 *.avi *.mov *.mkv"), ("All", "*.*")])
        if not path:
            return
        if self.video.open(path):
            self._log(f"Video: {Path(path).name} ({self.video.n_frames} frames @ {self.video.fps:.1f} fps)")
            self.video.show_frame(0)
        else:
            self._log(f"Failed to open video: {path}")

    def on_open_csv(self):
        path = filedialog.askopenfilename(
            title="Open movement CSV", filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if not path:
            return
        try:
            data, desc = T.read_csv_points(path, bodypart=self.bodypart_var.get() or None)
        except Exception as e:
            self._log(f"CSV load failed: {e}"); return
        if not data:
            self._log("CSV had no usable rows."); return
        self.player.load(data, src_fps=float(self.fps_var.get() or 30.0), csv_path=path)
        self._log(f"CSV: {Path(path).name} ({len(data)} frames; {desc})")
        self.config["bodypart"] = self.bodypart_var.get()
        self.config["src_fps"] = float(self.fps_var.get() or 30.0)
        save_config(self.config)

    def on_apply_cage(self):
        self.cage_w = float(self.cage_w_var.get())
        self.cage_h = float(self.cage_h_var.get())
        self.cage_margin = float(self.cage_m_var.get())
        self.config.update(cage_w=self.cage_w, cage_h=self.cage_h, cage_margin=self.cage_margin)
        save_config(self.config)
        if self.player.raw:
            # _fit_and_prepare keeps minutes/specs intact (continuous stays continuous)
            self.player._fit_and_prepare(self.player.raw, self.player.src_fps)
        self._log(f"Cage set to {self.cage_w}×{self.cage_h} m, margin {self.cage_margin} m.")

    def on_play(self):
        self.choreographer.pause()   # don't let multiple drivers fight the mice
        self.tap_driver.pause()
        self.player.play()
    def on_pause(self): self.player.pause()
    def on_reset(self): self.player.reset()

    # ---- seek / scrub --------------------------------------------------------
    def on_seek(self, _val):
        """Slider drag → jump to that frame (ignores our own programmatic sets)."""
        if self._seek_guard or not self.player.frames:
            return
        self.player.seek(int(round(self.seek_var.get())))

    def on_seek_frames(self, d: int):
        if self.player.frames:
            self.player.seek(self.player.idx + d)

    def on_seek_to(self, idx: int):
        if self.player.frames:
            self.player.seek(idx)

    def on_seek_step(self, secs: float):
        if self.player.frames:
            fps = self.player.src_fps or 30.0
            self.player.seek(self.player.idx + int(round(secs * fps)))

    def _sync_seek_bar(self):
        """Reflect the player's current frame on the scrub bar + label. Guarded
        so updating the slider here doesn't fire on_seek and recurse."""
        n = len(self.player.frames)
        self.seek_scale.config(to=float(max(1, n - 1)))
        self._seek_guard = True
        try:
            self.seek_var.set(float(self.player.idx))
        finally:
            self._seek_guard = False
        if n == 0:
            self.seek_lbl.config(text="–")
        else:
            fps = self.player.src_fps or 30.0
            t = self.player.idx / fps
            self.seek_lbl.config(
                text=f"{self.player.idx}/{n - 1}  ·  {int(t // 60)}:{int(t % 60):02d}")

    # ---- tap drive (turn-by-turn) ----
    def _sync_tap_knobs(self):
        """Pull the calibration/timing fields into the driver and persist them.

        Per-tap distance and angle are DERIVED from the hold-time calibration
        (e.g. 2 m in 1.5 s => 1.33 m/s) and the tap on-time, so a tap can't be
        too long and 'race off': shrink 'on ms' and every tap gets smaller.
        """
        d = self.tap_driver
        try:
            cal_m = float(self.tap_cal_m_var.get())
            cal_s = float(self.tap_cal_s_var.get())
            turn_rate = float(self.tap_turnrate_var.get())   # deg per second
            d.tap_on_fwd_ms = max(20, int(self.tap_on_var.get()))
            d.tap_on_turn_ms = max(10, int(self.tap_turn_on_var.get()))
            d.tap_gap_ms = max(0, int(self.tap_gap_var.get()))
            d.simplify_tol = float(self.tap_tol_var.get())
            d.min_turn_deg = float(self.tap_turn_var.get())
        except (tk.TclError, ValueError):
            self._log("Tap drive: check the numeric fields (hold m/s, spin °/s, ms...).")
            return False
        if cal_s <= 0:
            self._log("Tap drive: hold-time calibration seconds must be > 0.")
            return False
        v_fwd = cal_m / cal_s                          # m/s at a full hold
        d.m_per_tap = v_fwd * d.tap_on_fwd_ms / 1000.0    # one forward tap
        d.deg_per_tap = turn_rate * d.tap_on_turn_ms / 1000.0  # one turn tap
        d.speed_pct = int(self.speed_var.get())
        self.tap_calc_lbl.config(
            text=f"→ fwd {d.m_per_tap*100:.0f} cm/tap, turn {d.deg_per_tap:.0f}°/tap")
        self.config.update(tap_cal_m=cal_m, tap_cal_s=cal_s,
                           tap_turn_rate=turn_rate, tap_on_ms=d.tap_on_fwd_ms,
                           tap_turn_on_ms=d.tap_on_turn_ms, tap_gap_ms=d.tap_gap_ms,
                           tap_tol=d.simplify_tol, tap_min_turn=d.min_turn_deg)
        save_config(self.config)
        return True

    def on_tap_play(self):
        self.player.pause()          # don't let continuous + tap fight the mice
        self.choreographer.pause()
        if self._sync_tap_knobs():
            self.tap_driver.play()

    def on_tap_stop(self): self.tap_driver.pause()

    def on_tap_reset(self):
        if self._sync_tap_knobs():
            self.tap_driver.reset()

    # ---- bodypart / camera selection ----
    def on_bodypart_change(self):
        bp = self.bodypart_var.get()
        self.config["bodypart"] = bp
        save_config(self.config)
        try:
            if self.player.continuous_specs:
                raw, minutes = C.load_concatenated_raw(self.player.continuous_specs, bp)
                self.player.load_continuous(raw, minutes, self.player.continuous_specs,
                                            src_fps=self.player.src_fps)
            elif self.player.csv_path:
                data, _ = T.read_csv_points(self.player.csv_path, bodypart=bp)
                self.player.load(data, src_fps=self.player.src_fps, csv_path=self.player.csv_path)
            self._log(f"Track: switched to {bp}")
        except Exception as e:
            self._log(f"Track: bodypart switch failed: {e}")

    def selected_cams(self) -> tuple[int, ...]:
        return tuple(c for c in CAMERAS if self.cam_vars[c].get())

    def on_cam_toggle(self):
        self.config["cat_cams"] = list(self.selected_cams())
        save_config(self.config)
        self._relayout_cam_grid()

    def on_cam_expand(self, cam: int):
        """Toggle a camera between the small grid and a large single view."""
        if self.videos[cam].cap is None:
            return  # nothing loaded for this cam yet
        self.expanded_cam = None if self.expanded_cam == cam else cam
        self._relayout_cam_grid()

    def _size_cam_tile(self, cam: int, big: bool):
        w, h = (520, 300) if big else (168, 96)
        self.cam_holders[cam].config(width=w, height=h)
        self.videos[cam].DISPLAY_W = w

    def _relayout_cam_grid(self):
        for tile in self.cam_tiles.values():
            tile.grid_forget()
        visible = [c for c in self.selected_cams() if self.videos[c].cap is not None]
        # drop a stale expansion (cam deselected or its clip closed)
        if self.expanded_cam not in visible:
            self.expanded_cam = None
        if self.expanded_cam is not None:
            self._size_cam_tile(self.expanded_cam, big=True)
            self.cam_tiles[self.expanded_cam].grid(row=0, column=0, padx=2, pady=2)
        else:
            for i, cam in enumerate(visible):
                self._size_cam_tile(cam, big=False)
                r, c = divmod(i, 3)  # 3 columns
                self.cam_tiles[cam].grid(row=r, column=c, padx=2, pady=2)
        # repaint the current frame at the new tile size (player may not exist
        # yet on the first layout during UI build)
        player = getattr(self, "player", None)
        if player is not None and player.frames:
            player._advance_video()

    # ---- clip catalog ----
    def on_cat_refresh(self):
        self.cat_status.set("listing clips...")
        def worker():
            try:
                specs = C.list_bundled_clips() + C.list_b2_clips() + C.list_local_clips()
            except Exception as e:
                self.root.after(0, lambda err=e: self.cat_status.set(f"refresh failed: {err}"))
                return
            self.root.after(0, lambda: self._cat_set_specs(specs))
        threading.Thread(target=worker, daemon=True).start()

    def _cat_set_specs(self, specs):
        self.cat_specs = {s.label: s for s in specs}
        labels = list(self.cat_specs.keys())
        self.cat_combo["values"] = labels
        prev = self.config.get("cat_csv", "")
        if prev in self.cat_specs:
            self.cat_csv_var.set(prev)
        elif labels:
            self.cat_csv_var.set(labels[0])
        n_b2 = sum(1 for s in specs if s.source == "b2")
        n_local = sum(1 for s in specs if s.source == "local")
        self.cat_status.set(f"{n_b2} B2 + {n_local} local clips")

    def on_cat_get(self):
        label = self.cat_csv_var.get().strip()
        spec = self.cat_specs.get(label)
        if spec is None:
            self._log("Catalog: pick a clip first (Refresh, then choose)."); return
        cams = self.selected_cams()
        if not cams:
            self._log("Catalog: select at least one camera."); return
        self.config["cat_csv"] = label; save_config(self.config)
        plan = C.plan_clip(spec, cams)
        eta = plan["eta_s"]
        announce = "all cached — loading..." if eta == 0 else f"~{eta//60}m {eta%60:02d}s of fetch/extract..."
        self.cat_status.set(announce)
        self._log(f"Catalog: {label} cams={list(cams)} — {announce}")
        def set_status(msg):
            self.root.after(0, lambda m=msg: self.cat_status.set(m))
        def worker():
            try:
                csv_local, mp4s = C.extract_clip_mp4s(spec, cams, log_cb=self._log, status_cb=set_status)
                self.root.after(0, lambda: self._cat_load_into_player(csv_local, mp4s))
            except Exception as e:
                self.root.after(0, lambda err=e: self.cat_status.set(f"failed: {err}"))
                self._log(f"Catalog: failed: {e}")
        threading.Thread(target=worker, daemon=True).start()

    def _cat_load_into_player(self, csv_path, mp4s):
        if self.fetcher is not None:
            self.fetcher.stop(); self.fetcher = None
        try:
            data, desc = T.read_csv_points(str(csv_path), bodypart=self.bodypart_var.get() or None)
        except Exception as e:
            self._log(f"Catalog: CSV load failed: {e}"); return
        self.player.load(data, src_fps=float(self.fps_var.get() or 30.0), csv_path=str(csv_path))
        for cam in CAMERAS:
            sc = self.videos[cam]
            if cam in mp4s:
                if sc.open(str(mp4s[cam])):
                    sc.show_frame(0)
            elif sc.cap is not None:
                sc.cap.release(); sc.cap = None; sc.label.config(image="")
        self._relayout_cam_grid()
        self.cat_status.set(f"loaded {len(self.player.frames)} frames + {len(mp4s)} cam(s)")

    def on_cat_stream(self):
        label = self.cat_csv_var.get().strip()
        if label not in self.cat_specs:
            self._log("Stream: pick a clip first (Refresh, then choose)."); return
        cams = self.selected_cams()
        if not cams:
            self._log("Stream: select at least one camera."); return
        n = max(2, min(20, int(self.stream_minutes_var.get())))
        self.config["stream_minutes"] = n; save_config(self.config)
        all_specs = list(self.cat_specs.values())
        chain = C.list_consecutive_minutes(self.cat_specs[label], n, all_specs)
        if not chain:
            self._log("Stream: no minutes resolved."); return
        if self.fetcher is not None:
            self.fetcher.stop()
        # load the concatenated trajectory immediately (canvas runs without video)
        raw, minutes = C.load_concatenated_raw(chain, self.bodypart_var.get() or "Tailbase")
        if not raw:
            self._log("Stream: trajectory needs the CSVs on disk (local clips, or Get clip first)."); return
        self.player.load_continuous(raw, minutes, chain, src_fps=float(self.fps_var.get() or 30.0))
        self.cat_status.set(f"streaming {len(chain)} min ({len(self.player.frames)} frames)")
        self._log(f"Stream: {chain[0].label.strip()} +{len(chain)} min, cams={list(cams)}")
        self.fetcher = RollingFetcher(self, self.player.minutes, cams, do_gc=True)
        self.fetcher.start()
        for cam in CAMERAS:
            sc = self.videos[cam]
            if sc.cap is not None:
                sc.cap.release(); sc.cap = None; sc.label.config(image="")
        self._relayout_cam_grid()

    def _on_stream_minute_ready(self):
        mi = self.player.current_minute_idx()
        if 0 <= mi < len(self.player.minutes):
            m = self.player.minutes[mi]
            frame_in_file = self.player.idx - m.frame_offset
            for cam in self.selected_cams():
                mp4 = m.cam_mp4s.get(cam)
                sc = self.videos[cam]
                if mp4 and Path(mp4).exists() and sc.path != str(mp4):
                    sc.show_frame_at(str(mp4), max(0, frame_in_file))
        self._relayout_cam_grid()

    # ---- per-mouse choreography ----
    def on_choreo_sync(self):
        """Build one assignment row per connected mouse."""
        self.choreographer.pause()
        for child in self.choreo_rows_f.winfo_children():
            child.destroy()
        slugs = self.fleet.connected_slugs
        if not slugs:
            ttk.Label(self.choreo_rows_f, text="(no mice connected)",
                      foreground="#888").pack(anchor="w")
            return
        clip_values = list(self.cat_specs.keys()) + [self.BROWSE_SENTINEL]
        ttk.Label(self.choreo_rows_f, text="Mouse → clip:",
                  foreground="#888").grid(row=0, column=0, columnspan=2, sticky="w")
        for i, slug in enumerate(slugs, start=1):
            col = TRACK_COLORS[(i - 1) % len(TRACK_COLORS)]
            tk.Label(self.choreo_rows_f, text="■", fg=col).grid(row=i, column=0, sticky="w")
            ttk.Label(self.choreo_rows_f, text=slug, width=8).grid(row=i, column=1, sticky="w")
            var = self.choreo_assign.get(slug) or tk.StringVar()
            self.choreo_assign[slug] = var
            combo = ttk.Combobox(self.choreo_rows_f, textvariable=var, width=42,
                                 state="readonly", values=clip_values)
            combo.grid(row=i, column=2, padx=4, pady=2, sticky="w")
            combo.bind("<<ComboboxSelected>>",
                       lambda e, s=slug: self._choreo_pick(s))
        # drop assignments for mice no longer connected
        for s in list(self.choreo_assign):
            if s not in slugs:
                self.choreo_assign.pop(s, None)
                self.choreo_files.pop(s, None)
        n_clips = len(self.cat_specs)
        if n_clips == 0:
            self.choreo_status.set(
                f"{len(slugs)} mouse(mice) — no catalog clips yet; ↻ Refresh, "
                "or pick '— browse CSV… —' per mouse, then Prepare.")
            return
        n_auto = self._choreo_autoassign(slugs)
        self._log(f"Choreo: synced {len(slugs)} mouse(mice) — {n_auto} random "
                  f"clip(s) assigned, loading...")
        # auto-load the freshly-picked clips. Distinct-first/repeat means any
        # shared clip is intentional, so skip the duplicate-clip confirmation.
        self.on_choreo_prepare(confirm_dups=False)

    def _clip_cached(self, spec: ClipSpec, want_video: bool,
                     cams: tuple[int, ...]) -> bool:
        """True if this clip's CSV (and, when video is wanted, every cam mp4) is
        already on disk, so picking it needs no B2 download."""
        csv_ok = bool(
            (spec.csv_local and spec.csv_local.exists())
            or (spec.csv_remote
                and (C.CSV_DIR / spec.csv_remote.rsplit("/", 1)[-1]).exists()))
        if not csv_ok:
            return False
        if not want_video:
            return True
        return all(C.clip_mp4_path(spec, c).exists() for c in cams)

    def _choreo_autoassign(self, slugs) -> int:
        """Randomly assign a catalog clip to each mouse: distinct clips first,
        repeating only once the catalog is exhausted (the user opted into that).
        Clips already cached on disk are preferred, so a re-sync reshuffles the
        picks without triggering fresh downloads where it can avoid them. A
        deliberate browsed-CSV pick (tracked in choreo_files) is left alone;
        every other slug is re-randomised on each sync."""
        clips = list(self.cat_specs.keys())
        if not clips:
            return 0
        want_video = self.choreo_with_video.get()
        cams = BentoView.CAMS
        cached, fresh = [], []
        for lbl in clips:
            (cached if self._clip_cached(self.cat_specs[lbl], want_video, cams)
             else fresh).append(lbl)
        random.shuffle(cached); random.shuffle(fresh)
        pool = cached + fresh             # random within each group, cached first
        used: set[str] = set()
        ptr = 0
        n_auto = 0
        for s in slugs:
            if s in self.choreo_files and self.choreo_assign[s].get():
                used.add(self.choreo_assign[s].get())   # keep deliberate file pick
                continue
            pick = None
            for k in range(len(pool)):    # prefer a clip not yet used (distinct)
                cand = pool[(ptr + k) % len(pool)]
                if cand not in used:
                    pick = cand
                    ptr += k + 1
                    break
            if pick is None:              # fewer clips than mice: allow a repeat
                pick = pool[ptr % len(pool)]
                ptr += 1
            self.choreo_assign[s].set(pick)
            self.choreo_files.pop(s, None)
            used.add(pick)
            n_auto += 1
        return n_auto

    def _choreo_pick(self, slug: str):
        if self.choreo_assign[slug].get() == self.BROWSE_SENTINEL:
            path = filedialog.askopenfilename(
                title=f"CSV for {slug}", filetypes=[("CSV", "*.csv"), ("All", "*.*")])
            if path:
                self.choreo_files[slug] = path
                self.choreo_assign[slug].set(f"file: {Path(path).name}")
            else:
                self.choreo_assign[slug].set("")
        else:
            self.choreo_files.pop(slug, None)

    def _resolve_clip_csv(self, slug: str, label: str) -> str | None:
        """Return a local CSV path for an assignment (downloading from B2 if needed)."""
        if slug in self.choreo_files:
            return self.choreo_files[slug]
        spec = self.cat_specs.get(label)
        if spec is None:
            return None
        if spec.csv_local and spec.csv_local.exists():
            return str(spec.csv_local)
        name = spec.csv_remote.rsplit("/", 1)[-1]
        local = C.CSV_DIR / name
        if not local.exists():
            C.b2_download(spec.csv_remote, C.CSV_DIR, log_cb=self._log)
        return str(local)

    def on_choreo_prepare(self, confirm_dups: bool = True):
        assigns = {s: v.get() for s, v in self.choreo_assign.items() if v.get()}
        if not assigns:
            self._log("Choreo: assign a clip to at least one mouse first."); return
        # warn if two mice share a clip — they'd trace the identical path
        seen: dict[str, list[str]] = {}
        for s, label in assigns.items():
            seen.setdefault(label, []).append(s)
        dups = {lbl: ss for lbl, ss in seen.items() if len(ss) > 1}
        if dups and confirm_dups:
            lines = "\n".join(f"  • {', '.join(ss)}  →  {lbl}"
                              for lbl, ss in dups.items())
            if not messagebox.askyesno(
                    "Duplicate clips",
                    "These mice share the SAME clip and will trace identical "
                    f"paths:\n\n{lines}\n\nPrepare them anyway?"):
                self._log("Choreo: prepare cancelled — reassign duplicate clips.")
                return
        bodypart = self.bodypart_var.get() or "Tailbase"
        src_fps = float(self.fps_var.get() or 30.0)
        cage = (self.cage_w, self.cage_h, self.cage_margin)
        want_video = self.choreo_with_video.get()
        cams = BentoView.CAMS
        self.choreo_cam_mp4s = {}
        self.choreo_status.set("preparing tracks"
                               + (" + video (may download GBs of tars)..." if want_video
                                  else " (may download CSVs)..."))
        def mk_status(s):
            return lambda m: self.root.after(
                0, lambda mm=m: self.choreo_status.set(f"{s}: {mm}"))
        def worker():
            tracks: dict[str, MouseTrack] = {}
            for i, (slug, label) in enumerate(assigns.items()):
                try:
                    spec = self.cat_specs.get(label)   # None for a browsed CSV
                    mp4s: dict[int, Path] = {}
                    if spec is not None and want_video:
                        csv_local, mp4s = C.extract_clip_mp4s(
                            spec, cams, log_cb=self._log, status_cb=mk_status(slug))
                        csv_path = str(csv_local)
                    else:
                        csv_path = self._resolve_clip_csv(slug, label)
                    if not csv_path:
                        self._log(f"Choreo: no CSV for {slug} ({label})"); continue
                    pts, _ = T.read_csv_points(csv_path, bodypart=bodypart)
                    if not pts:
                        self._log(f"Choreo: {slug} CSV had no usable rows"); continue
                    color = TRACK_COLORS[i % len(TRACK_COLORS)]
                    tracks[slug] = MouseTrack(slug, Path(csv_path).stem[:24], pts,
                                              cage, src_fps, self.speed_model, color)
                    if mp4s:
                        self.choreo_cam_mp4s[slug] = mp4s
                    self._log(f"Choreo: {slug} ← {label} ({len(pts)} frames, "
                              f"x{tracks[slug].timewarp:.2f} time-warp"
                              + (f", {len(mp4s)} cam(s))" if mp4s else ")"))
                except Exception as e:
                    self._log(f"Choreo: {slug} prepare failed: {e}")
            def finish():
                self.choreographer.set_tracks(tracks)
                n_vid = sum(1 for s in tracks if s in self.choreo_cam_mp4s)
                self.choreo_status.set(
                    f"{len(tracks)} track(s) ready"
                    + (f", {n_vid} with video — ▦ Bento view + Play all."
                       if n_vid else " — press Play all."))
            self.root.after(0, finish)
        threading.Thread(target=worker, daemon=True).start()

    def on_choreo_play(self):
        self.player.pause()          # stop single-clip playback first
        self.choreographer.play()
    def on_choreo_pause(self): self.choreographer.pause()
    def on_choreo_reset(self): self.choreographer.reset()

    def on_choreo_bento(self):
        """Open (or focus) the per-mouse video+trajectory bento grid."""
        if self.bento is not None:
            try:
                self.bento.lift(); self.bento.focus_force(); return
            except tk.TclError:
                self.bento = None
        self.bento = BentoView(self)

    def _bento_tick(self):
        if self.bento is not None:
            try:
                self.bento.tick()
            except tk.TclError:
                self.bento = None

    def _bento_rebuild(self):
        if self.bento is not None:
            try:
                self.bento.rebuild()
            except tk.TclError:
                self.bento = None

    # ---- log plumbing ----
    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{ts}] {msg}")

    def _poll_log(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", line + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()
