# Toy Mouse BLE control

Sibling project to `dingdongwu-ble` and `galaxy-rvr`. Reverse-engineer the BLE
protocol of a toy mouse, then drive it from a Python dashboard that can also
play back local video files and movement CSVs in sync.

## Quick recipe (Samsung Android phone)

1. **Dev settings on the phone**
   - Settings → About phone → Software information → tap *Build number* 7×.
   - Settings → Developer options:
     - Enable *USB debugging*
     - Enable *Bluetooth HCI snoop log*
   - Settings → Security and privacy → **disable Auto Blocker** (otherwise USB
     debugging keeps getting revoked).
   - **Reboot** the phone. The snoop log only kicks in after a full restart.
2. Open the toy's official app, drive forward, back, left, right at a few
   different speed settings. Stop. Disconnect.
3. `adb bugreport` → unzip → extract `FS/data/log/bt/btsnoop_hci.log`.
4. `python parse_snoop.py btsnoop_hci.log` — note the handle the app kept
   writing to, and any incoming notify handle.
5. `python extract_writes.py btsnoop_hci.log 0xWRITE_HANDLE` — read every
   frame in chronological order and match against the buttons you pressed.
6. `python find_mouse.py` — note the MAC + advertised name.
7. `python probe.py <MAC>` — confirm the write & notify characteristic UUIDs.
8. **Edit `mouse_protocol.py`** — fill in WRITE_UUID, NOTIFY_UUID,
   DEFAULT_MOUSE_MAC, MOUSE_NAME_HINTS, and either the FIXED_FRAMES dict or
   the SPEED_AWARE builders (and set `USE_MODE` accordingly).
9. `python test_send.py` — sends one Stop command. Should ack.
10. `python mouse_panel.py` — full dashboard.

## What to share back with Claude

When you're ready to fill in the protocol, paste back:

- The advertised name + MAC (from step 6).
- The write & notify UUIDs (from step 7).
- For each of **forward, back, left, right, stop** — the bytes you observed.
- Whether speed is encoded *in* the command bytes or set separately
  (and if separately, the bytes for each speed level).

That's enough to wire `mouse_protocol.py` end-to-end.

## Protocol (decoded 2026-05-29)

Mouse advertises as `pets`, MAC `74:93:6A:5B:AF:C7`. Write char `0000ae01-…`
(write-without-response), notify char `0000ae02-…`.

    Frame:  5A [DIR] [SPEED] 00 [CHK] A5
      DIR   = 0x01 fwd, 0x02 back, 0x04 left, 0x08 right, 0x00 stop (bitmask)
      SPEED = 0x00..0x13   (0x08 "normal", 0x13 "boost")
      CHK   = (DIR + SPEED) & 0xFF
    Connect handshake (once):  5A A5 6B BD D0 30 4A 17 A5
    Notify heartbeat:          4B [N] 00 [N] B4   (N counts DOWN = sleep timer)

## Driving multiple mice

All mice advertise the same name, so they're told apart by MAC.

    python find_mouse.py                       # lists every mouse in range
    python mouse.py fleet <MAC1> <MAC2> ...     # broadcast-drive several at once

In `mouse_panel.py`: **Find mice → Connect ALL found**. The "Connected" list is
the drive target — select a subset to drive just those, or leave it unselected
to broadcast to all. The BLE central handles ~7 simultaneous links comfortably,
so 4 mice is fine on the one TP-Link adapter.

## Emulating real-mouse trajectories in a cage (open-loop)

The goal: replay a tracked mouse's X/Y path on a toy in a wall-less cage,
matching its movement without driving off the edge. Workflow:

1. **Calibrate speed** (once): `python calibrate_speed.py`. Drives forward at
   bytes you pick for a fixed time; you type the distance travelled. Builds
   `speed_calibration.json` (byte→m/s curve + deadband). Until you do this the
   panel uses a *provisional* model anchored on the one field measurement
   (byte 0x0B → 1.33 m/s) — it works, just less accurately.
2. **Set the cage size** in the panel (W×H + safety margin, in metres) and
   *Apply cage + refit*.
3. **Open CSV** — the trajectory's bounding box is scaled to fit the cage minus
   the margin, so the *planned* path can't leave the cage.
4. Tick **Drive mice from CSV** and press **Play**.

## Loading clips like the galaxy-rvr dashboard

The **Clip catalog** card pulls the same data as `galaxy_rvr_panel.py`:
- **↻ Refresh** lists EKS `multicam_3d_results` CSVs from Backblaze B2 (via
  rclone) plus local clips from the Unity project folder.
- Tick the **cameras** you want, pick a clip, **Get clip & load** — it downloads
  the CSV + per-minute tar(s) and remuxes one mp4 per camera, then loads the
  XY trajectory + multicam grid, synced to the playback frame.
- **▶ Stream** plays N consecutive minutes as one continuous timeline, with a
  `RollingFetcher` pre-extracting upcoming minutes and GC-ing old ones.
- It reuses the **galaxy-rvr cache** (`C:\Users\labra\galaxy-rvr\cache`), so
  anything already downloaded there isn't fetched again.

The catalog CSVs feed the same cage-fit + speed-matching pipeline below, so a
real recorded mouse trajectory can be replayed straight onto the toy.

### Preloaded (offline) clips — no Backblaze needed

The repo ships 5 ready-to-play clips (all 12 cameras) under `clips/`, so a
Raspberry Pi with no B2/rclone access can still load and play them. They appear
first in the catalog refresh labelled `[bundled] …`; selecting one needs no
download (CSV + per-camera mp4s are already on disk). The video is transcoded to
720×320 — small enough for the Pi 5 to software-decode (it has no hardware H.264
decoder) and to fit a normal git repo. To drive a mouse only the bundled
trajectory CSV is used, so motion is identical to the full-res source. See
`clips/README.md`; regenerate with `python build_bundled_clips.py`.

## Simplified touch dashboard (V2 — `show_dashboard.py`)

A stripped-down, touch-only dashboard for the field rig (a 27×12 cm / 1280×480 HDMI
touchscreen on the Pi 5). It does only three things: connect mice, pick which mice +
which bundled clip each one plays, then play the trajectories + camera videos while
driving the mice.

    python3 show_dashboard.py              # fullscreen on the touchscreen
    python3 show_dashboard.py --windowed   # 1280×480 window, for desktop dev

- **Setup screen:** tap a mouse chip to connect/disconnect; per-mouse `◀ ▶` to choose a
  clip (or 🎲 / **Shuffle all** for random); big **Drive** / **Video** toggles; touch
  steppers for **min pulse** and **clearance**.
- **Show screen:** one card per mouse — trajectory + a camera tile (tap the camera to
  cycle angle) — with big Play/Pause/Reset/Fullscreen.
- **Cage** defaults to 0.5×0.5 m with 0.05 m clearance, so the path spans ~0.4 m.
- **Driving:** reuses the same cage-fit + speed/duty-cycle engine as the choreographer,
  but **coalesces each duty pulse to a minimum width** (default 100 ms, adjustable live)
  so the toy actually latches the short commands a 0.4 m cage produces. Run
  `calibrate_speed.py` first so the speed model isn't provisional. Settings persist to
  `show_config.json`. It reuses the shared `video_tile.TileVideo` decoder.

## Per-mouse choreography (a different clip per mouse)

The **Per-mouse choreography** card runs 4–6 mice, each following its **own**
clip, all on one shared real-time clock:
1. Connect the mice, then **⟳ Sync mice** — one assignment row appears per
   connected mouse (colour-coded), each gets a **random** catalog clip
   (distinct clips first, repeating only if there are more mice than clips,
   and preferring clips already cached on disk), and the picks are then loaded
   automatically. Press it again to reshuffle.
2. To override a pick, choose a clip from any mouse's dropdown (catalog labels,
   or *— browse CSV… —* for any local file), then **Prepare tracks** again.
3. **Prepare tracks** (downloads B2 CSVs if needed, cage-fits each) runs on its
   own too, then **▶ Play all**.

Every track keeps its own cage-fit, speed/time-warp plan, duty-cycle and
dead-reckoned pose, so clips of different lengths and speeds stay in step and
each mouse is driven independently. The shared cage canvas shows every mouse +
path in its colour. It reuses the same *Drive mice from CSV*, bodypart, cage and
FPS settings as the single-clip player (the two won't drive at the same time).

How speed is matched (`speed_model.py` + `trajectory.py`):
- Per frame, the desired cage-space speed is computed from the source FPS.
- A global **time-warp** slows the whole clip only if the mouse's fastest dart
  exceeds the toy's top speed (otherwise timing is 1:1).
- Speeds the toy can do continuously → mapped to the nearest calibrated byte.
- Speeds **below the toy's minimum continuous speed** → the toy can't crawl
  that slowly, so it's **duty-cycled**: short bursts at the floor speed with
  pauses, so the *average* matches the mouse.
- A dead-reckoned **geofence** halts driving if the estimated position drifts
  toward the safe-zone edge. (Open-loop has no real feedback — the cage-fit
  scaling is the primary guarantee; the geofence is a backstop. A future
  overhead-camera mode would make this closed-loop and exact.)

## Files

- `catalog.py` — B2/rclone clip catalog + per-camera mp4 extraction + RollingFetcher (ported from galaxy-rvr; reuses its cache).
- `choreography.py` — `MouseTrack`: one mouse following one clip (cage-fit + speed plan + pose), ticked by the panel's `Choreographer` on a shared clock.
- `mouse_protocol.py` — UUIDs, MAC, frame builders (`build`, `build_raw`, `build_stop`).
- `mouse_fleet.py` — `MouseFleet`: manage N mice (connect/broadcast/subset/stop-all).
- `speed_model.py` — byte↔m/s calibration model + `plan(target)` → (byte, duty).
- `trajectory.py` — CSV load, cage-fit scaling, per-segment speeds, time-warp, geofence.
- `calibrate_speed.py` — interactive speed calibration → `speed_calibration.json`.
- `find_mouse.py` — BLE scan, lists all `pets` matches.
- `probe.py` — connect + dump GATT table.
- `parse_snoop.py` / `extract_writes.py` — Android btsnoop decoders.
- `test_send.py` — first-contact ping (sends Stop only).
- `listen.py` — subscribe to the notify channel and decode the heartbeat.
- `mouse.py` — CLI: `scan`, `drive <MAC>`, `fleet <MAC...>`.
- `mouse_panel.py` — Tkinter dashboard (fleet drive + video + cage trajectory playback).
- `mouse_config.json` / `speed_calibration.json` — auto-generated.
