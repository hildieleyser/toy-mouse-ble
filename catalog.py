"""Clip catalog + multicam video pipeline — ported from galaxy_rvr_panel.py so
the toy-mouse dashboard loads the exact same XY-trajectory CSVs and per-camera
video the way the rover panel does.

Source data lives on Backblaze B2 (accessed via rclone) and, optionally, in a
local Unity-project folder. A "clip" is one minute of a cage recording:
  * an EKS multicam_3d_results CSV (the 3D keypoint trajectories), and
  * 12 camera H.264 streams packed in a per-minute .tar.

We reuse galaxy-rvr's on-disk cache so anything already downloaded there is not
re-fetched. The toy panel feeds the raw (x, z) points through trajectory.fit_to_cage
rather than galaxy's fixed scale, so this module returns *unscaled* points.
"""
from __future__ import annotations

import re as _re
import shutil
import subprocess
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# ---- rclone / Backblaze B2 -------------------------------------------------
RCLONE_REMOTE = "b2remote"
RCLONE_FALLBACK = Path(
    r"C:\Users\labra\AppData\Local\Microsoft\WinGet\Packages"
    r"\Rclone.Rclone_Microsoft.Winget.Source_8wekyb3d8bbwe"
    r"\rclone-v1.73.5-windows-amd64\rclone.exe"
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

def _rclone_exe() -> str:
    exe = shutil.which("rclone")
    if exe:
        return exe
    if RCLONE_FALLBACK.exists():
        return str(RCLONE_FALLBACK)
    raise FileNotFoundError("rclone not found on PATH and fallback path missing.")

def b2_lsf(remote_path: str, include: str | None = None) -> list[str]:
    cmd = [_rclone_exe(), "lsf", f"{RCLONE_REMOTE}:{remote_path}"]
    if include:
        cmd += ["--include", include]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        return []
    return [ln.rstrip("/") for ln in r.stdout.splitlines() if ln.strip()]

def b2_exists(remote_path: str) -> bool:
    parent, _, name = remote_path.rpartition("/")
    return name in b2_lsf(parent)

def b2_download(remote_path: str, local_dir: Path, log_cb=None) -> Path:
    local_dir.mkdir(parents=True, exist_ok=True)
    cmd = [_rclone_exe(), "copy", "--stats=1s", "--stats-one-line",
           f"{RCLONE_REMOTE}:{remote_path}", str(local_dir)]
    if log_cb:
        log_cb(f"B2: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, creationflags=_NO_WINDOW)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line and log_cb:
            log_cb(f"B2: {line}")
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"rclone exited with code {rc}")
    return local_dir


# ---- locations -------------------------------------------------------------
CAMERAS = tuple(range(1, 13))             # 12 cameras
DATA_PREFIX = "netholabs/virtual-mouse-cage/data"
CAGES_PREFIX = "netholabs/staging/V6_cages"
CLIP_FPS = 30
# Reuse the galaxy-rvr cache so already-downloaded CSVs/tars/mp4s are shared.
CACHE_DIR = Path(r"C:\Users\labra\galaxy-rvr\cache")
CSV_DIR = CACHE_DIR / "csvs"
TAR_DIR = CACHE_DIR / "video_src"
CLIP_DIR = CACHE_DIR / "clips"
LOCAL_DATA_DIR = Path(
    r"C:\Users\labra\Projects\VirtualMouseCageUnity\virtual-mouse-cage\data\cat_40_vids"
)
# Preloaded clips shipped in the repo (transcoded mp4 + CSV per clip), so a
# Raspberry Pi can play them with no Backblaze/rclone access. See clips/README.md
# and build_bundled_clips.py.
BUNDLED_DIR = Path(__file__).resolve().parent / "clips"


@dataclass
class ClipSpec:
    label: str
    cage: str
    start: datetime
    source: str                    # "b2" or "local"
    csv_remote: str = ""
    csv_local: Path | None = None
    side: str = ""

    @property
    def stem(self) -> str:
        side = f"_{self.side}" if self.side else ""
        return f"{self.cage}_{self.start.strftime('%Y_%m_%d_%H_%M_%S')}{side}"


@dataclass
class MinuteSpec:
    spec: ClipSpec
    n_frames: int = 0
    frame_offset: int = 0
    cam_mp4s: dict = field(default_factory=dict)


_B2_NAME_RE = _re.compile(
    r"multicam_3d_results_(?P<cage>[\d.]+)_"
    r"(?P<y>\d{4})_(?P<m>\d{2})_(?P<d>\d{2})_"
    r"(?P<H>\d{2})_(?P<M>\d{2})_(?P<S>\d{2})_(?P<ms>\d{3})_eks_.+\.csv")
_LOCAL_DIR_RE = _re.compile(
    r"(?P<cage>[\d.]+)_all_"
    r"(?P<y>\d{4})_(?P<m>\d{2})_(?P<d>\d{2})_"
    r"(?P<H>\d{2})_(?P<M>\d{2})_(?P<S>\d{2})_(?P<ms>\d{3})_(?P<side>left|right)_side")

def _ts(g: dict) -> datetime:
    return datetime(int(g["y"]), int(g["m"]), int(g["d"]),
                    int(g["H"]), int(g["M"]), int(g["S"]))

def list_bundled_clips() -> list[ClipSpec]:
    """Clips preloaded in the repo (clips/<stem>/ with clip.json + camN.mp4 +
    multicam_3d_results.csv). No network/rclone needed — this is what the Pi uses."""
    import json
    out = []
    if not BUNDLED_DIR.exists():
        return out
    for d in sorted(BUNDLED_DIR.iterdir()):
        manifest = d / "clip.json"
        if not d.is_dir() or not manifest.exists():
            continue
        try:
            m = json.loads(manifest.read_text())
            csv = d / "multicam_3d_results.csv"
            out.append(ClipSpec(
                label=m.get("label", f"[bundled] {d.name}"),
                cage=m["cage"], start=datetime.fromisoformat(m["start"]),
                source="bundled", csv_local=csv if csv.exists() else None,
                side=m.get("side", "")))
        except Exception:
            continue
    return out

def clip_mp4_path(spec: ClipSpec, cam: int) -> Path:
    """Where a clip's per-camera mp4 lives: the repo's bundled dir for preloaded
    clips, otherwise the galaxy-rvr extraction cache."""
    if spec.source == "bundled":
        return BUNDLED_DIR / spec.stem / f"cam{cam}.mp4"
    return CLIP_DIR / f"{spec.stem}_cam{cam}.mp4"

def list_b2_clips() -> list[ClipSpec]:
    out = []
    try:
        names = b2_lsf(DATA_PREFIX, include="*.csv")
    except Exception:
        return out          # no rclone (e.g. on the Pi) -> just no B2 clips
    for name in names:
        m = _B2_NAME_RE.match(name)
        if not m:
            continue
        g = m.groupdict(); start = _ts(g)
        out.append(ClipSpec(
            label=f"[B2]      {start:%Y-%m-%d %H:%M:%S}  cage {g['cage']}",
            cage=g["cage"], start=start, source="b2",
            csv_remote=f"{DATA_PREFIX}/{name}"))
    return out

def list_local_clips() -> list[ClipSpec]:
    out = []
    if not LOCAL_DATA_DIR.exists():
        return out
    for d in sorted(LOCAL_DATA_DIR.iterdir()):
        if not d.is_dir():
            continue
        m = _LOCAL_DIR_RE.match(d.name)
        if not m:
            continue
        g = m.groupdict()
        eks_dir = next(d.glob("*_eks_*"), None)
        if eks_dir is None:
            continue
        csv = eks_dir / "multicam_3d_results.csv"
        if not csv.exists():
            continue
        side = g["side"][0].upper(); start = _ts(g)
        out.append(ClipSpec(
            label=f"[local {side}] {start:%Y-%m-%d %H:%M:%S}  cage {g['cage']}",
            cage=g["cage"], start=start, source="local", csv_local=csv, side=side))
    return out

def csv_frame_count(local_path: Path) -> int:
    import csv as _csv
    with open(local_path, newline="") as f:
        return sum(1 for _ in _csv.reader(f)) - 3  # 3 header rows

def list_consecutive_minutes(start: ClipSpec, n_minutes: int,
                             all_specs: list[ClipSpec]) -> list[ClipSpec]:
    by_key = {(s.cage, s.start, s.side): s for s in all_specs}
    out, cur = [], start
    for _ in range(n_minutes):
        out.append(cur)
        nxt = by_key.get((cur.cage, cur.start + timedelta(minutes=1), cur.side))
        if nxt is None:
            break
        cur = nxt
    return out


def _slice_xz(rows, bodypart):
    bp, co = rows[1], rows[2]
    ix = next(i for i, (b, c) in enumerate(zip(bp, co)) if b == bodypart and c == "x")
    iz = next(i for i, (b, c) in enumerate(zip(bp, co)) if b == bodypart and c == "z")
    return [(float(r[ix]), float(r[iz])) for r in rows[3:]]

def load_concatenated_raw(specs: list[ClipSpec], bodypart: str
                          ) -> tuple[list[tuple[float, float]], list[MinuteSpec]]:
    """Concatenate (x, z) for `bodypart` across consecutive minutes, in one
    world coordinate frame (recentred on the first point). Returns RAW points
    (no cage scaling — the panel runs fit_to_cage afterward) plus MinuteSpecs
    carrying n_frames + frame_offset so a global frame index maps to a minute."""
    import csv as _csv
    frames: list[tuple[float, float]] = []
    minutes: list[MinuteSpec] = []
    origin = None
    for spec in specs:
        if spec.csv_local is None or not spec.csv_local.exists():
            break
        with open(spec.csv_local, newline="") as f:
            rows = list(_csv.reader(f))
        try:
            raw = _slice_xz(rows, bodypart)
        except StopIteration:
            break
        if not raw:
            continue
        if origin is None:
            origin = raw[0]
        off = len(frames)
        frames.extend((x - origin[0], z - origin[1]) for x, z in raw)
        minutes.append(MinuteSpec(spec=spec, n_frames=len(raw), frame_offset=off))
    return frames, minutes


def tar_minutes(start: datetime, n_frames: int) -> list[datetime]:
    end = start + timedelta(seconds=n_frames / CLIP_FPS)
    cur = start.replace(second=0, microsecond=0)
    out = []
    while cur <= end:
        out.append(cur); cur += timedelta(minutes=1)
    return out

def resolve_tar_remote(cage: str, ts: datetime) -> str | None:
    fname = f"{cage}_all_{ts.strftime('%Y_%m_%d_%H_%M_%S')}_000.tar"
    for c in (f"{CAGES_PREFIX}/{cage}/{ts.strftime('%Y_%m_%d')}/{fname}",
              f"{CAGES_PREFIX}/{cage}/{fname}",
              f"{CAGES_PREFIX}/{cage}/2026_03_OLDER/{fname}"):
        if b2_exists(c):
            return c
    return None

def plan_clip(spec: ClipSpec, cams: tuple[int, ...]) -> dict:
    if spec.source == "bundled":
        missing = [c for c in cams if not clip_mp4_path(spec, c).exists()]
        return {"csv_missing": False, "missing_tars": 0, "total_tars": 0,
                "missing_mp4s": missing, "total_mp4s": len(cams), "eta_s": 0}
    csv_missing = not (
        (spec.csv_local and spec.csv_local.exists())
        or (CSV_DIR / spec.csv_remote.rsplit("/", 1)[-1]).exists())
    if spec.csv_local and spec.csv_local.exists():
        n_frames = csv_frame_count(spec.csv_local)
    else:
        n_frames = 30 * 73
    minutes = tar_minutes(spec.start, n_frames)
    missing_tars = [ts for ts in minutes if not
                    (TAR_DIR / f"{spec.cage}_all_{ts.strftime('%Y_%m_%d_%H_%M_%S')}_000.tar").exists()]
    missing_mp4s = [c for c in cams if not (CLIP_DIR / f"{spec.stem}_cam{c}.mp4").exists()]
    eta_s = (30 if csv_missing else 0) + 30 * len(missing_tars) + 5 * len(missing_mp4s)
    return {"csv_missing": csv_missing, "missing_tars": len(missing_tars),
            "total_tars": len(minutes), "missing_mp4s": missing_mp4s,
            "total_mp4s": len(cams), "eta_s": eta_s}

def extract_clip_mp4s(spec: ClipSpec, cams: tuple[int, ...],
                      log_cb=None, status_cb=None) -> tuple[Path, dict[int, Path]]:
    def _status(msg):
        if status_cb:
            status_cb(msg)
    if spec.source == "bundled":
        # already on disk in the repo — nothing to download or transcode.
        out = {c: clip_mp4_path(spec, c) for c in cams if clip_mp4_path(spec, c).exists()}
        return spec.csv_local, out
    # 1) CSV
    if spec.csv_local and spec.csv_local.exists():
        csv_local = spec.csv_local
    else:
        name = spec.csv_remote.rsplit("/", 1)[-1]
        csv_local = CSV_DIR / name
        if not csv_local.exists():
            _status("downloading CSV...")
            b2_download(spec.csv_remote, CSV_DIR, log_cb=log_cb)
    n_frames = csv_frame_count(csv_local)
    duration_s = n_frames / CLIP_FPS
    minutes = tar_minutes(spec.start, n_frames)
    # 2) tars
    local_tars = []
    for i, ts in enumerate(minutes, 1):
        fname = f"{spec.cage}_all_{ts.strftime('%Y_%m_%d_%H_%M_%S')}_000.tar"
        local = TAR_DIR / fname
        if not local.exists():
            remote = resolve_tar_remote(spec.cage, ts)
            if remote is None:
                raise FileNotFoundError(f"tar not found on B2 for {ts}")
            _status(f"downloading tar {i}/{len(minutes)} (~1.2 GB)...")
            b2_download(remote, TAR_DIR, log_cb=log_cb)
        local_tars.append(local)
    # 3) extract per-cam mp4
    import imageio_ffmpeg, tarfile
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    out: dict[int, Path] = {}
    for ci, cam in enumerate(cams, 1):
        mp4 = CLIP_DIR / f"{spec.stem}_cam{cam}.mp4"
        if mp4.exists():
            out[cam] = mp4
            continue
        _status(f"extracting cam {cam} ({ci}/{len(cams)})...")
        concat = CLIP_DIR / f"{spec.stem}_cam{cam}.h264"
        with concat.open("wb") as outf:
            for tar_path, ts in zip(local_tars, minutes):
                member = f"{spec.cage}_{cam}_{ts.strftime('%Y_%m_%d_%H_%M_%S')}_000.h264"
                with tarfile.open(tar_path) as t:
                    src = t.extractfile(member)
                    if src is None:
                        raise RuntimeError(f"{member} not in {tar_path.name}")
                    while True:
                        chunk = src.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        outf.write(chunk)
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-framerate", str(CLIP_FPS),
               "-i", str(concat), "-t", f"{duration_s:.3f}", "-c:v", "copy", str(mp4)]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW)
        concat.unlink(missing_ok=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()}")
        out[cam] = mp4
    return csv_local, out


class RollingFetcher:
    """Keep per-cam mp4s ready a couple minutes ahead of the playback head, and
    GC minutes that have fallen well behind so disk stays bounded. Polls
    app.player.current_minute_idx() on a background thread."""
    LOOKAHEAD = 2
    GC_KEEP_BEHIND = 1

    def __init__(self, app, minutes: list[MinuteSpec], cams: tuple[int, ...],
                 do_gc: bool = True):
        self.app = app
        self.minutes = minutes
        self.cams = cams
        self.do_gc = do_gc
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                cur = max(0, self.app.player.current_minute_idx())
                end = min(cur + self.LOOKAHEAD, len(self.minutes) - 1)
                for i in range(cur, end + 1):
                    if self._stop.is_set():
                        return
                    m = self.minutes[i]
                    needed = [c for c in self.cams
                              if c not in m.cam_mp4s or not m.cam_mp4s[c].exists()]
                    if not needed:
                        continue
                    try:
                        _, mp4s = extract_clip_mp4s(m.spec, tuple(needed), log_cb=self.app._log)
                        m.cam_mp4s.update(mp4s)
                        self.app._log(f"Stream: minute {i} ready ({len(mp4s)} cams)")
                        self.app.root.after(0, self.app._on_stream_minute_ready)
                    except Exception as e:
                        self.app._log(f"Stream: minute {i} fetch failed: {e}")
                if self.do_gc:
                    for i in range(0, max(0, cur - self.GC_KEEP_BEHIND)):
                        self._gc_minute(i)
            except Exception as e:
                self.app._log(f"Stream: fetcher loop error: {e}")
            _time.sleep(0.5)

    def _gc_minute(self, i: int):
        m = self.minutes[i]
        if m.spec.source == "bundled":
            return                      # never delete repo-shipped clips
        for cam, mp4 in list(m.cam_mp4s.items()):
            try:
                Path(mp4).unlink(missing_ok=True)
                del m.cam_mp4s[cam]
            except Exception:
                pass
        fname = f"{m.spec.cage}_all_{m.spec.start.strftime('%Y_%m_%d_%H_%M_%S')}_000.tar"
        tar = TAR_DIR / fname
        if tar.exists():
            try:
                tar.unlink()
            except Exception:
                pass
