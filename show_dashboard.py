"""V2 "Show" dashboard — a simplified, touch-only dashboard for the toy-mouse cage.

Designed for a 27cm x 12cm wide-short HDMI touchscreen (1280x480) on a Raspberry Pi 5,
running off the repo's bundled offline clips. It does three things:

  A) connect to the toy mice over BLE,
  B) let the user pick which mice to control and assign each a clip (trajectory + video),
     with a one-tap random "shuffle",
  C) play the trajectories + camera videos while driving the mice along the cage path.

It reuses the engine (MouseFleet, MouseTrack, catalog, SpeedModel, TileVideo) and adds its
own small 30Hz player loop. The big mouse_panel.py is left alone.

Driving: in the 0.5m cage (path scaled to ~0.4m via fit_to_cage) the per-frame speeds are
tiny, so MouseTrack duty-cycles into ~33ms pulses. Those are likely too brief for the toy
to latch, so each duty pulse is COALESCED to a minimum width (default 100ms, adjustable
live with the on-screen stepper). Run calibrate_speed.py so the speed model isn't provisional.

    python3 show_dashboard.py            # fullscreen on the touchscreen
    python3 show_dashboard.py --windowed # 1280x480 window, for dev
"""
from __future__ import annotations

import json
import math
import random
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import catalog as C
import mouse_roster as R
import trajectory as T
from catalog import CAMERAS, extract_clip_mp4s, list_bundled_clips
from choreography import MouseTrack, TRACK_COLORS
from mouse_fleet import MouseFleet
from mouse_protocol import build_raw, build_stop
from speed_model import SpeedModel
from video_tile import TileVideo

# ---- config (separate from mouse_config.json so V1/V2 don't fight) ----------
CONFIG_PATH = Path(__file__).with_name("show_config.json")


def load_show_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_show_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---- palette / sizing -------------------------------------------------------
BG = "#11111a"
SURFACE = "#1c1c2b"
SURFACE2 = "#262638"
ACCENT = "#4dabf7"
TEXT = "#e8e8f0"
MUTED = "#8a8aa8"
OK = "#3aa655"
WARN = "#f7b500"

CAGE_W = CAGE_H = 0.5            # metres (fixed real cage)
SRC_FPS = 30.0
MAX_VIDEO_CARDS = 3             # software-decode budget on the Pi 5

TRAJ_PX = 170                  # trajectory canvas (square)
CAM_W, CAM_H = 210, 150        # camera tile box (720x320 source letterboxed)


class ShowApp:
    def __init__(self, root: tk.Tk, fleet=None, fullscreen: bool = True):
        self.root = root
        self.cfg = load_show_config()
        self.fleet = fleet if fleet is not None else MouseFleet(self._log)
        self.speed_model = SpeedModel.load()
        self.clips = list_bundled_clips()
        self.clips_by_label = {c.label: c for c in self.clips}
        self.clip_labels = list(self.clips_by_label.keys())

        # persisted, live-tunable settings
        self.min_pulse_ms = int(self.cfg.get("min_pulse_ms", 100))
        self.clearance_m = float(self.cfg.get("clearance_m", 0.05))
        self.drive_var = tk.BooleanVar(value=bool(self.cfg.get("drive", True)))
        self.video_var = tk.BooleanVar(value=bool(self.cfg.get("video", True)))

        # setup state
        self.found: list[tuple[str, str, int]] = []     # (mac, name, rssi)
        self.assign: dict[str, str] = {}                # slug -> clip label
        self._known_slugs: set[str] = set()

        # show state
        self.tracks: dict[str, MouseTrack] = {}
        self.cam_mp4s: dict[str, dict[int, Path]] = {}
        self.cards: dict[str, dict] = {}
        self.pulse_state: dict[str, dict] = {}
        self.after_id: str | None = None

        root.title("Toy Mice — Show")
        root.configure(background=BG)
        self._init_style()
        if fullscreen:
            root.attributes("-fullscreen", True)
        else:
            root.geometry("1280x480")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<space>", lambda e: self.on_play_pause())
        root.bind("<Escape>", lambda e: root.attributes("-fullscreen", False))
        root.bind("<Key-r>", lambda e: self.on_reset())

        self.setup_frame = ttk.Frame(root, style="App.TFrame")
        self.show_frame = ttk.Frame(root, style="App.TFrame")
        self._build_setup()
        self._build_show()
        self._goto_setup()
        self._refresh_setup_loop()

    # ---- styling ----
    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("App.TFrame", background=BG)
        st.configure("Surface.TFrame", background=SURFACE)
        st.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 13))
        st.configure("Title.TLabel", background=BG, foreground=TEXT,
                     font=("Segoe UI", 20, "bold"))
        st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 12))
        st.configure("Status.TLabel", background=SURFACE, foreground=MUTED,
                     font=("Segoe UI", 12))
        st.configure("Big.TButton", font=("Segoe UI", 16, "bold"), padding=(18, 16))
        st.configure("Go.TButton", font=("Segoe UI", 17, "bold"), padding=(22, 16))
        st.map("Big.TButton",
               background=[("active", SURFACE2), ("!active", SURFACE)],
               foreground=[("!active", TEXT)])
        st.map("Go.TButton",
               background=[("active", "#3b8fd6"), ("!active", ACCENT)],
               foreground=[("!active", "#06121f")])

    def _log(self, msg: str):
        # console only; the UI surfaces state via status lines
        print(msg)

    # ============================ SETUP VIEW ============================
    def _build_setup(self):
        f = self.setup_frame
        bar = ttk.Frame(f, style="App.TFrame"); bar.pack(fill="x", padx=14, pady=(12, 6))
        ttk.Label(bar, text="TOY MICE — SETUP", style="Title.TLabel").pack(side="left")
        ttk.Button(bar, text="START  ▶", style="Go.TButton",
                   command=self.on_start).pack(side="right")
        ttk.Button(bar, text="SCAN", style="Big.TButton",
                   command=self.on_scan).pack(side="right", padx=8)

        body = ttk.Frame(f, style="App.TFrame"); body.pack(fill="both", expand=True, padx=14)
        body.columnconfigure(0, weight=11)
        body.columnconfigure(1, weight=9)
        body.rowconfigure(0, weight=1)

        # left: mouse chips
        left = ttk.Frame(body, style="App.TFrame"); left.grid(row=0, column=0, sticky="nsew")
        ttk.Label(left, text="MICE  (tap to connect)", style="Muted.TLabel").pack(anchor="w")
        self.chips_f = ttk.Frame(left, style="App.TFrame")
        self.chips_f.pack(fill="both", expand=True, pady=4)

        opts = ttk.Frame(left, style="App.TFrame"); opts.pack(fill="x", pady=(2, 6))
        self.drive_btn = tk.Button(opts, command=lambda: self._toggle(self.drive_var, self.drive_btn, "Drive"),
                                   relief="flat", bd=0, width=10, height=2, font=("Segoe UI", 12, "bold"))
        self.drive_btn.pack(side="left", padx=(0, 8))
        self.video_btn = tk.Button(opts, command=lambda: self._toggle(self.video_var, self.video_btn, "Video"),
                                   relief="flat", bd=0, width=10, height=2, font=("Segoe UI", 12, "bold"))
        self.video_btn.pack(side="left")
        self._paint_toggle(self.drive_var, self.drive_btn, "Drive")
        self._paint_toggle(self.video_var, self.video_btn, "Video")

        # right: assignments + steppers
        right = ttk.Frame(body, style="App.TFrame"); right.grid(row=0, column=1, sticky="nsew")
        ttk.Label(right, text="ASSIGN CLIPS  (tap ◀ ▶ to choose)",
                  style="Muted.TLabel").pack(anchor="w")
        self.assign_f = ttk.Frame(right, style="App.TFrame")
        self.assign_f.pack(fill="both", expand=True, pady=4)
        ttk.Button(right, text="🎲  SHUFFLE ALL", style="Big.TButton",
                   command=self.on_shuffle_all).pack(anchor="w", pady=2)

        steppers = ttk.Frame(right, style="App.TFrame"); steppers.pack(fill="x", pady=4)
        self.pulse_lbl = self._stepper(steppers, "min pulse", self._fmt_pulse,
                                       lambda d: self.on_step_pulse(d))
        self.clear_lbl = self._stepper(steppers, "clearance", self._fmt_clear,
                                       lambda d: self.on_step_clear(d))

        self.setup_status = tk.StringVar(value="")
        sb = ttk.Frame(f, style="Surface.TFrame"); sb.pack(fill="x", side="bottom")
        ttk.Label(sb, textvariable=self.setup_status, style="Status.TLabel"
                  ).pack(anchor="w", padx=12, pady=6)
        self._update_setup_status()

    def _stepper(self, parent, name, fmt, cb):
        row = ttk.Frame(parent, style="App.TFrame"); row.pack(fill="x", pady=3)
        ttk.Label(row, text=name, style="TLabel", width=9).pack(side="left")
        tk.Button(row, text="−", command=lambda: cb(-1), width=3, height=1,
                  font=("Segoe UI", 15, "bold"), relief="flat", bd=0,
                  bg=SURFACE, fg=TEXT).pack(side="left", padx=3)
        val = ttk.Label(row, text=fmt(), style="TLabel", width=8, anchor="center")
        val.pack(side="left")
        tk.Button(row, text="＋", command=lambda: cb(+1), width=3, height=1,
                  font=("Segoe UI", 15, "bold"), relief="flat", bd=0,
                  bg=SURFACE, fg=TEXT).pack(side="left", padx=3)
        return val

    def _fmt_pulse(self): return f"{self.min_pulse_ms} ms"
    def _fmt_clear(self): return f"{self.clearance_m:.2f} m"

    def _toggle(self, var, btn, name):
        var.set(not var.get()); self._paint_toggle(var, btn, name); self._persist()

    def _paint_toggle(self, var, btn, name):
        on = var.get()
        btn.config(text=f"{name}: {'ON' if on else 'off'}",
                   bg=OK if on else SURFACE, fg="#06121f" if on else MUTED,
                   activebackground=OK if on else SURFACE2)

    def on_step_pulse(self, d):
        self.min_pulse_ms = max(40, min(400, self.min_pulse_ms + 20 * d))
        self.pulse_lbl.config(text=self._fmt_pulse()); self._persist()

    def on_step_clear(self, d):
        self.clearance_m = max(0.0, min(0.20, round(self.clearance_m + 0.01 * d, 2)))
        self.clear_lbl.config(text=self._fmt_clear()); self._persist()

    def _persist(self):
        self.cfg.update(min_pulse_ms=self.min_pulse_ms, clearance_m=self.clearance_m,
                        drive=self.drive_var.get(), video=self.video_var.get())
        save_show_config(self.cfg)

    # ---- scanning / connecting ----
    def on_scan(self):
        self.setup_status.set("scanning 6s...")
        fut = self.fleet.submit(self.fleet.find_all_mice(6.0))

        def done(_f):
            try:
                self.found = _f.result()
            except Exception as e:
                self.setup_status.set(f"scan failed: {e}"); return
            self._render_chips(); self._update_setup_status()
        fut.add_done_callback(lambda _f: self.root.after(0, done, _f))

    def _slug_for_mac(self, mac: str) -> str | None:
        for s, h in self.fleet.mice.items():
            if h.mac == mac and h.connected:
                return s
        return None

    def on_chip_tap(self, mac: str):
        slug = self._slug_for_mac(mac)
        if slug is not None:
            self.fleet.submit(self.fleet.disconnect(slug))
            self.setup_status.set(f"disconnecting {slug}...")
        else:
            self.fleet.submit(self.fleet.connect(mac, slug=R.slug_for_mac(mac)))
            self.setup_status.set(f"connecting {R.describe_mac(mac)}...")

    def _render_chips(self):
        for w in self.chips_f.winfo_children():
            w.destroy()
        macs = [m for m, _, _ in self.found]
        for h in self.fleet.mice.values():        # include connected-but-not-scanned
            if h.connected and h.mac not in macs:
                macs.append(h.mac)
        rssi = {m: r for m, _, r in self.found}
        cols = 2
        for i, mac in enumerate(macs):
            slug = self._slug_for_mac(mac)
            color = self._slug_color(slug) if slug else MUTED
            r, c = divmod(i, cols)
            txt = f"{'●' if slug else '○'} {R.describe_mac(mac)}"
            sub = slug if slug else (f"{rssi.get(mac, '?')} dBm")
            chip = tk.Button(self.chips_f, text=f"{txt}\n{sub}", width=14, height=2,
                             relief="flat", bd=0, justify="left",
                             font=("Segoe UI", 12, "bold"),
                             bg=SURFACE if slug else SURFACE2,
                             fg=color if slug else TEXT,
                             activebackground=SURFACE2,
                             highlightthickness=3,
                             highlightbackground=color if slug else SURFACE2,
                             command=lambda m=mac: self.on_chip_tap(m))
            chip.grid(row=r, column=c, padx=4, pady=4, sticky="ew")
            self.chips_f.columnconfigure(c, weight=1)

    def _slug_color(self, slug: str | None) -> str:
        if not slug:
            return MUTED
        n = "".join(ch for ch in slug if ch.isdigit())
        idx = (int(n) - 1) if n else 0
        return TRACK_COLORS[idx % len(TRACK_COLORS)]

    # ---- clip assignment ----
    def _render_assign_rows(self):
        for w in self.assign_f.winfo_children():
            w.destroy()
        slugs = self.fleet.connected_slugs
        if not slugs:
            ttk.Label(self.assign_f, text="(connect mice first)", style="Muted.TLabel"
                      ).pack(anchor="w", pady=8)
            return
        if not self.clip_labels:
            ttk.Label(self.assign_f, text="(no bundled clips found)", style="Muted.TLabel"
                      ).pack(anchor="w", pady=8)
            return
        for slug in slugs:
            row = ttk.Frame(self.assign_f, style="App.TFrame"); row.pack(fill="x", pady=3)
            tk.Label(row, text="●", fg=self._slug_color(slug), bg=BG,
                     font=("Segoe UI", 14)).pack(side="left")
            tk.Button(row, text="◀", width=2, relief="flat", bd=0, bg=SURFACE, fg=TEXT,
                      font=("Segoe UI", 13, "bold"),
                      command=lambda s=slug: self.on_clip_cycle(s, -1)).pack(side="left", padx=2)
            name = tk.Label(row, text=self._clip_display(self.assign.get(slug)),
                            bg=SURFACE, fg=TEXT, width=18, height=2,
                            font=("Segoe UI", 12, "bold"))
            name.pack(side="left", padx=2)
            tk.Button(row, text="▶", width=2, relief="flat", bd=0, bg=SURFACE, fg=TEXT,
                      font=("Segoe UI", 13, "bold"),
                      command=lambda s=slug: self.on_clip_cycle(s, +1)).pack(side="left", padx=2)
            tk.Button(row, text="🎲", width=3, relief="flat", bd=0, bg=SURFACE, fg=TEXT,
                      font=("Segoe UI", 13),
                      command=lambda s=slug: self.on_clip_random(s)).pack(side="left", padx=4)

    def _clip_display(self, label: str | None) -> str:
        if not label or label not in self.clips_by_label:
            return "—"
        spec = self.clips_by_label[label]
        side = f" {spec.side}" if spec.side else ""
        return f"{spec.start:%m-%d %H:%M}{side}"

    def on_clip_cycle(self, slug: str, delta: int):
        cur = self.assign.get(slug)
        i = self.clip_labels.index(cur) if cur in self.clip_labels else -delta % len(self.clip_labels)
        self.assign[slug] = self.clip_labels[(i + delta) % len(self.clip_labels)]
        self._render_assign_rows()

    def on_clip_random(self, slug: str):
        self.assign[slug] = random.choice(self.clip_labels)
        self._render_assign_rows()

    def on_shuffle_all(self):
        slugs = self.fleet.connected_slugs
        if not slugs or not self.clip_labels:
            return
        pool = self.clip_labels[:]
        random.shuffle(pool)
        used: set[str] = set()
        ptr = 0
        for s in slugs:
            pick = next((pool[(ptr + k) % len(pool)] for k in range(len(pool))
                         if pool[(ptr + k) % len(pool)] not in used), pool[ptr % len(pool)])
            self.assign[s] = pick
            used.add(pick)
            ptr += 1
        self._render_assign_rows()
        self._update_setup_status()

    def _refresh_setup_loop(self):
        """Poll connection state so chips/assignments update after async connects."""
        slugs = set(self.fleet.connected_slugs)
        if slugs != self._known_slugs:
            for s in slugs - self._known_slugs:           # newly connected -> auto pick
                if s not in self.assign and self.clip_labels:
                    used = set(self.assign.values())
                    self.assign[s] = next((l for l in self.clip_labels if l not in used),
                                          random.choice(self.clip_labels))
            for s in self._known_slugs - slugs:           # disconnected -> drop
                self.assign.pop(s, None)
            self._known_slugs = slugs
            self._render_chips(); self._render_assign_rows(); self._update_setup_status()
        self.root.after(500, self._refresh_setup_loop)

    def _update_setup_status(self):
        prov = "PROVISIONAL (run calibrate_speed.py)" if getattr(
            self.speed_model, "provisional", False) else "calibrated"
        self.setup_status.set(
            f"connected {len(self.fleet.connected_slugs)} / found {len(self.found)}  ·  "
            f"{len(self.clip_labels)} bundled clips  ·  cage {CAGE_W}×{CAGE_H} m  ·  "
            f"speed model: {prov}")

    # ============================ SHOW VIEW ============================
    def _build_show(self):
        f = self.show_frame
        bar = ttk.Frame(f, style="App.TFrame"); bar.pack(fill="x", padx=14, pady=(12, 6))
        ttk.Button(bar, text="◀ Setup", style="Big.TButton",
                   command=self._goto_setup).pack(side="left")
        ttk.Label(bar, text="SHOW", style="Title.TLabel").pack(side="left", padx=16)
        self.play_btn = ttk.Button(bar, text="▶  PLAY", style="Go.TButton",
                                   command=self.on_play_pause)
        self.play_btn.pack(side="right")
        ttk.Button(bar, text="Full", style="Big.TButton",
                   command=self.on_fullscreen).pack(side="right", padx=8)
        ttk.Button(bar, text="⟲ Reset", style="Big.TButton",
                   command=self.on_reset).pack(side="right", padx=8)

        self.cards_f = ttk.Frame(f, style="App.TFrame")
        self.cards_f.pack(fill="both", expand=True, padx=10, pady=4)

        self.show_status = tk.StringVar(value="")
        sb = ttk.Frame(f, style="Surface.TFrame"); sb.pack(fill="x", side="bottom")
        ttk.Label(sb, textvariable=self.show_status, style="Status.TLabel"
                  ).pack(anchor="w", padx=12, pady=6)

    def _build_cards(self):
        for w in self.cards_f.winfo_children():
            w.destroy()
        for d in self.cards.values():
            d["video"].release()
        self.cards = {}
        for i, (slug, tr) in enumerate(self.tracks.items()):
            color = tr.color
            cell = tk.Frame(self.cards_f, bg=BG, highlightthickness=2,
                            highlightbackground=color)
            cell.grid(row=0, column=i, padx=6, sticky="nsew")
            self.cards_f.columnconfigure(i, weight=1)
            self.cards_f.rowconfigure(0, weight=1)
            tk.Label(cell, text=f"●  {slug}   {self._clip_display(self.assign.get(slug))}",
                     bg=color, fg="#06121f", anchor="w",
                     font=("Segoe UI", 12, "bold")).pack(fill="x")
            pair = tk.Frame(cell, bg=BG); pair.pack(fill="both", expand=True)
            tcv = tk.Canvas(pair, width=TRAJ_PX, height=TRAJ_PX, bg="#0c0c12",
                            highlightthickness=0)
            tcv.pack(side="left", padx=4, pady=4)
            ccv = tk.Canvas(pair, width=CAM_W, height=CAM_H, bg="#0c0c12",
                            highlightthickness=0)
            ccv.pack(side="left", padx=4, pady=4)
            counter = tk.Label(cell, text="", bg=BG, fg=MUTED, anchor="w",
                               font=("Segoe UI", 11))
            counter.pack(fill="x")
            video = TileVideo(ccv)
            cams = sorted(self.cam_mp4s.get(slug, {}).keys())
            if cams:
                video.set_sources({c: str(p) for c, p in self.cam_mp4s[slug].items()})
                ccv.bind("<Button-1>", lambda e, s=slug: self._cycle_cam(s))
            else:
                tcv_msg = "trajectory only" if not self.video_var.get() else "no video"
                ccv.create_text(CAM_W // 2, CAM_H // 2, fill=MUTED, text=tcv_msg)
            self.cards[slug] = {"traj": tcv, "cam": ccv, "video": video,
                                "counter": counter, "cams": cams, "ci": 0}

    def _cycle_cam(self, slug: str):
        d = self.cards.get(slug)
        if not d or not d["cams"]:
            return
        d["ci"] = (d["ci"] + 1) % len(d["cams"])
        self._render_frame()

    # ---- transport ----
    def on_play_pause(self):
        if self.after_id is not None:
            self._pause()
        else:
            self._play()

    def _play(self):
        if not self.tracks or self.after_id is not None:
            return
        self.play_btn.config(text="❚❚  PAUSE")
        self._tick()

    def _pause(self):
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        if self.drive_var.get():
            self.fleet.submit(self.fleet.stop_all())
        self.play_btn.config(text="▶  PLAY")

    def on_reset(self):
        self._pause()
        for tr in self.tracks.values():
            tr.reset()
        self.pulse_state = {s: {"dir": None, "until": 0.0, "idle_sent": True}
                            for s in self.tracks}
        self._render_frame()

    def on_fullscreen(self):
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    # ---- the 30Hz player loop ----
    MASTER_DT = 1.0 / 30.0

    def _tick(self):
        if not self.root.winfo_exists():
            return
        drive = self.drive_var.get()
        now = time.monotonic()
        all_done = True
        for slug, tr in self.tracks.items():
            direction, byte, drive_now = tr.advance(self.MASTER_DT)
            if not tr.done:
                all_done = False
            if drive:
                payload, label = self._resolve_pulse(slug, direction, byte, drive_now, now)
                if payload is not None:
                    self.fleet.submit(self.fleet.send_to([slug], payload, label))
        self._render_frame()
        if all_done:
            self._pause()
            self.show_status.set("done — ⟲ Reset to replay")
            return
        self.after_id = self.root.after(int(1000 * self.MASTER_DT), self._tick)

    def _resolve_pulse(self, slug, direction, byte, drive_now, now):
        """Coalesce duty pulses to a minimum width so the toy actually latches them.
        Returns (payload|None, label); None means 'send nothing this tick'."""
        st = self.pulse_state.setdefault(slug, {"dir": None, "until": 0.0, "idle_sent": True})
        min_s = self.min_pulse_ms / 1000.0
        if drive_now and direction is not None:
            st["dir"], st["byte"], st["until"], st["idle_sent"] = direction, byte, now + min_s, False
            return build_raw(direction, byte), direction
        if st["dir"] is not None and now < st["until"]:
            return build_raw(st["dir"], st["byte"]), ""        # hold; no log spam
        st["dir"] = None
        if not st["idle_sent"]:
            st["idle_sent"] = True
            return build_stop(), "stop"
        return None, None

    def _render_frame(self):
        playing = self.after_id is not None
        notes = []
        for slug, d in self.cards.items():
            tr = self.tracks.get(slug)
            if tr is None:
                continue
            self._draw_traj(d["traj"], tr)
            if d["cams"]:
                d["video"].show(d["cams"][d["ci"]], tr.idx, CAM_W, CAM_H)
            cam_txt = f"cam {d['cams'][d['ci']]}" if d["cams"] else "no video"
            geo = "  ⚠GEOFENCED" if getattr(tr, "geofenced", False) else ""
            d["counter"].config(text=f"{cam_txt}   {tr.idx}/{len(tr.frames)}{geo}")
            if getattr(tr, "geofenced", False):
                notes.append(f"{slug} geofenced")
        drive = "ON" if self.drive_var.get() else "off (preview)"
        head = "playing" if playing else "paused"
        extra = ("  ·  " + ", ".join(notes)) if notes else ""
        self.show_status.set(f"{head}  ·  driving {drive}  ·  min pulse {self.min_pulse_ms}ms{extra}")

    def _draw_traj(self, cv: tk.Canvas, tr: MouseTrack):
        cv.delete("all")
        w = TRAJ_PX
        half = CAGE_W / 2
        m = self.clearance_m
        span = CAGE_W * 1.08 or 1.0

        def px(x, y):
            return (w / 2 + x / span * w, w / 2 - y / span * w)
        cv.create_rectangle(*px(-half, -half), *px(half, half), outline="#8a8aa8", width=1)
        cv.create_rectangle(*px(-half + m, -half + m), *px(half - m, half - m),
                            outline=OK, dash=(4, 3))
        flat = []
        for x, y in tr.frames:
            a, b = px(x, y); flat += [a, b]
        if len(flat) >= 4:
            cv.create_line(*flat, fill=tr.color, width=1)
        if tr.frames:
            mx, my = tr.frames[min(tr.idx, len(tr.frames) - 1)]
            a, b = px(mx, my)
            cv.create_oval(a - 3, b - 3, a + 3, b + 3, fill=tr.color, outline="")
        ra, rb = px(tr.rx, tr.ry)
        cv.create_oval(ra - 5, rb - 5, ra + 5, rb + 5, fill=tr.color, outline="#fff", width=2)
        hx = tr.rx + 0.05 * math.cos(tr.rtheta)
        hy = tr.ry + 0.05 * math.sin(tr.rtheta)
        cv.create_line(ra, rb, *px(hx, hy), fill="#fff", width=2)

    # ---- view switching ----
    def _goto_setup(self):
        self._pause()
        self.show_frame.pack_forget()
        self.setup_frame.pack(fill="both", expand=True)
        self._render_chips(); self._render_assign_rows()

    def _goto_show(self):
        self.setup_frame.pack_forget()
        self.show_frame.pack(fill="both", expand=True)

    # ---- START: build tracks ----
    def on_start(self):
        slugs = [s for s in self.fleet.connected_slugs if self.assign.get(s)]
        if not slugs:
            self.setup_status.set("connect at least one mouse and assign a clip first.")
            return
        self.setup_status.set("preparing tracks...")
        want_video = self.video_var.get()
        clearance = self.clearance_m

        def worker():
            tracks: dict[str, MouseTrack] = {}
            cam_mp4s: dict[str, dict[int, Path]] = {}
            for i, slug in enumerate(slugs):
                label = self.assign[slug]
                spec = self.clips_by_label.get(label)
                if spec is None or not spec.csv_local:
                    continue
                try:
                    pts, _ = T.read_csv_points(str(spec.csv_local), bodypart="Tailbase")
                except Exception as e:
                    self._log(f"{slug}: CSV read failed: {e}"); continue
                if not pts:
                    continue
                color = self._slug_color(slug)
                tracks[slug] = MouseTrack(slug, self._clip_display(label), pts,
                                          (CAGE_W, CAGE_H, clearance), SRC_FPS,
                                          self.speed_model, color)
                if want_video and len(cam_mp4s) < MAX_VIDEO_CARDS:
                    try:
                        _, mp4s = extract_clip_mp4s(spec, CAMERAS)
                        if mp4s:
                            cam_mp4s[slug] = mp4s
                    except Exception as e:
                        self._log(f"{slug}: video prep failed: {e}")
            self.root.after(0, lambda: self._finish_start(tracks, cam_mp4s))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_start(self, tracks, cam_mp4s):
        if not tracks:
            self.setup_status.set("no usable trajectories — check the clips."); return
        self.tracks = tracks
        self.cam_mp4s = cam_mp4s
        self.pulse_state = {s: {"dir": None, "until": 0.0, "idle_sent": True} for s in tracks}
        self._build_cards()
        self._goto_show()
        self.on_reset()
        n_vid = len(cam_mp4s)
        self.show_status.set(f"{len(tracks)} mouse(mice) ready, {n_vid} with video — ▶ PLAY")

    # ---- teardown ----
    def on_close(self):
        self._pause()
        try:
            self.fleet.submit(self.fleet.stop_all())
            self.fleet.submit(self.fleet.disconnect_all())
        except Exception:
            pass
        for d in self.cards.values():
            d["video"].release()
        self.root.after(150, self.root.destroy)


def main():
    windowed = "--windowed" in sys.argv
    root = tk.Tk()
    ShowApp(root, fullscreen=not windowed)
    root.mainloop()


if __name__ == "__main__":
    main()
