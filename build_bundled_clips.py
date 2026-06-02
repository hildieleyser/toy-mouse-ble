"""Build the repo's preloaded clip set (`clips/`) so the Raspberry Pi can play
clips without any Backblaze / rclone access.

For each chosen clip it:
  1. makes sure all 12 source mp4s exist in the galaxy-rvr cache (extracting any
     missing cameras from B2 here, on the dev box that *does* have rclone), then
  2. transcodes every camera down to a small, Pi-friendly mp4
     (720x320, H.264 main profile, 1 s GOP) into  clips/<stem>/camN.mp4 , and
  3. copies the trajectory CSV + writes a clip.json manifest beside it.

The transcoded set is ~1.5-2 MB per camera, so 5 clips x 12 cams fits in a
normal git repo (no LFS). Re-running is resumable: anything already transcoded
is skipped. This script needs B2/ffmpeg and is meant to run on the Windows dev
box only; the Pi just consumes clips/ (see catalog.list_bundled_clips).

    python build_bundled_clips.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import catalog as C

# Transcode target — matches the panel's display tiles (bento 360 wide, grid
# 168 wide, fullscreen-expand) with headroom, while staying tiny + easy for the
# Pi 5 to *software*-decode (it has no hardware H.264 block).
OUT_W, OUT_H = 720, 320          # source is 2016x896 (== 2.25:1), so this is exact
CRF = "28"
GOP = "30"                       # keyframe every ~1 s -> cheap seeks when duty-cycling
PRESET = "veryfast"

BUNDLED_DIR = Path(__file__).resolve().parent / "clips"

# The 5 clips to preload, by stem. The first is a B2 clip; the rest are local
# (Unity) right-side clips. 14_00 has only 8 cams cached -> cams 9-12 come from B2.
TARGET_STEMS = [
    "6.01.001_2026_03_11_08_26_00",
    "6.01.001_2026_04_14_13_51_00_R",
    "6.01.001_2026_04_14_13_55_00_R",
    "6.01.001_2026_04_14_14_00_00_R",
    "6.01.001_2026_04_14_14_02_00_R",
]


def find_specs() -> dict[str, C.ClipSpec]:
    by_stem = {}
    for spec in C.list_b2_clips() + C.list_local_clips():
        by_stem[spec.stem] = spec
    out = {}
    for stem in TARGET_STEMS:
        if stem not in by_stem:
            raise SystemExit(f"Spec not found for {stem} — is the Unity/B2 source available?")
        out[stem] = by_stem[stem]
    return out


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def transcode(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
           "-vf", f"scale={OUT_W}:{OUT_H}",
           "-c:v", "libx264", "-profile:v", "main", "-preset", PRESET, "-crf", CRF,
           "-g", GOP, "-keyint_min", GOP, "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", "-an", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {src.name}: {r.stderr.strip()}")


def main():
    specs = find_specs()
    BUNDLED_DIR.mkdir(parents=True, exist_ok=True)
    cams = C.CAMERAS
    for stem, spec in specs.items():
        print(f"\n=== {stem} ({spec.source}) ===")
        # 1) ensure all 12 source mp4s exist in the cache (pulls missing from B2)
        missing = [c for c in cams if not (C.CLIP_DIR / f"{stem}_cam{c}.mp4").exists()]
        if missing:
            print(f"  fetching {len(missing)} missing cam(s) from B2: {missing}")
            C.extract_clip_mp4s(spec, tuple(missing),
                                log_cb=lambda m: print("   ", m),
                                status_cb=lambda m: print("   *", m))
        # 2) transcode each cam into the repo
        out_dir = BUNDLED_DIR / stem
        present = []
        for c in cams:
            src = C.CLIP_DIR / f"{stem}_cam{c}.mp4"
            dst = out_dir / f"cam{c}.mp4"
            if not src.exists():
                print(f"  cam{c}: SOURCE MISSING, skipped")
                continue
            if dst.exists() and dst.stat().st_size > 0:
                present.append(c); continue
            print(f"  cam{c}: transcoding -> {dst.relative_to(BUNDLED_DIR.parent)}")
            transcode(src, dst)
            present.append(c)
        # 3) trajectory CSV + manifest
        csv_src = (spec.csv_local if spec.csv_local
                   else C.CSV_DIR / spec.csv_remote.rsplit("/", 1)[-1])
        csv_dst = out_dir / "multicam_3d_results.csv"
        if csv_src and Path(csv_src).exists():
            shutil.copyfile(csv_src, csv_dst)
        else:
            print(f"  WARNING: trajectory CSV not found for {stem}")
        manifest = {
            "stem": stem,
            "cage": spec.cage,
            "start": spec.start.isoformat(),
            "side": spec.side,
            "label": f"[bundled] {spec.start:%Y-%m-%d %H:%M:%S}  cage {spec.cage}"
                     + (f" ({spec.side})" if spec.side else ""),
            "cams": present,
            "fps": C.CLIP_FPS,
            "width": OUT_W, "height": OUT_H,
        }
        (out_dir / "clip.json").write_text(json.dumps(manifest, indent=2))
        total = sum((out_dir / f"cam{c}.mp4").stat().st_size for c in present)
        print(f"  done: {len(present)} cams, {total/1e6:.1f} MB")
    print("\nAll clips built under", BUNDLED_DIR)


if __name__ == "__main__":
    main()
