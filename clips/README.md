# Preloaded clips

Self-contained clips shipped in the repo so a Raspberry Pi (or any machine) can
play them **with no Backblaze / rclone access**. Each clip is one minute of cage
recording, all 12 cameras.

## Layout

```
clips/<stem>/
  clip.json                 # manifest: cage, start, side, label, cams, fps, size
  multicam_3d_results.csv   # EKS 3D keypoint trajectory (drives the toy mouse)
  cam1.mp4 … cam12.mp4       # per-camera video, transcoded small for the Pi
```

`<stem>` is `<cage>_<YYYY_MM_DD_HH_MM_SS>[_R|_L]` (the `_R`/`_L` side suffix is
present for the local right/left-side clips).

## Why transcoded

The source camera video is 2016×896 @ 30 fps H.264 (~25–96 MB per camera per
minute). The **Raspberry Pi 5 has no hardware H.264 decoder** (only HEVC), so it
software-decodes on the CPU. These mp4s are re-encoded to **720×320, H.264 main
profile, 1 s GOP, CRF 28** (~1–1.5 MB/cam) — matched to the dashboard's display
tiles (bento 360 px, multicam grid 168 px) with headroom for the fullscreen
expand. At this size a Pi 5 comfortably software-decodes the 3 simultaneous
streams the per-mouse choreography view uses; seeks stay cheap thanks to the
frequent keyframes. The trajectory CSV is untouched, so driving is identical to
the full-resolution source.

## How they're loaded

`catalog.list_bundled_clips()` scans this folder and returns `ClipSpec`s with
`source="bundled"`. `plan_clip` / `extract_clip_mp4s` short-circuit for bundled
clips (files are already here — no download, no ffmpeg), and the `RollingFetcher`
never garbage-collects them. They appear first in the dashboard's **Clip catalog**
refresh, labelled `[bundled] …`.

## Regenerating

Run on a machine that *does* have rclone/B2 + the Unity source:

```
python build_bundled_clips.py
```

It's resumable (skips cameras already transcoded). Edit `TARGET_STEMS` in that
script to change which clips are bundled.
