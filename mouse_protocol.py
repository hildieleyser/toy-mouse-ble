"""Toy mouse BLE protocol.

Decoded from an Android btsnoop session on 2026-05-29.

Frame:   5A [DIR] [SPEED] 00 [CHK] A5    (6 bytes)
  DIR:   0x01 forward, 0x02 back, 0x04 left, 0x08 right, 0x00 stop
         (bitmask; diagonals like 0x05=fwd+left are likely valid, untested)
  SPEED: 0x00..0x13 — observed 0x08 ("normal") and 0x13 ("100% / boost").
  CHK  = (DIR + SPEED) & 0xFF
Handshake on connect: 5A A5 6B BD D0 30 4A 17 A5  (sent once before any drive).
Write handle is 0x0009 (Write Cmd, no response); notify handle is 0x000B
(toy emits 5-byte 4B..B4 frames periodically — heartbeat or battery counter).

TODO before driving: run `python probe.py <MAC>` to fill in WRITE_UUID and
NOTIFY_UUID below. MAC will come out of `python find_mouse.py`.
"""

# ---- BLE identity ---------------------------------------------------------

DEFAULT_MOUSE_MAC: str = "74:93:6A:5B:AF:C7"
MOUSE_NAME_HINTS: tuple[str, ...] = ("pets",)
MOUSE_FRIENDLY = "Toy Mouse (pets)"

# Service 0000ae00-... with write-without-response at ae01 and notify at ae02
# (handles 0x0009 / 0x000B respectively, matching the snoop log).
WRITE_UUID:  str = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID: str = "0000ae02-0000-1000-8000-00805f9b34fb"

# Toy never sent a fixed ack — the notifications are a periodic heartbeat, not
# a per-command response. So no EXPECTED_ACK_HEX.
EXPECTED_ACK_HEX: str | None = None


# ---- Mode --------------------------------------------------------------
# This toy encodes speed inside the command frame, so SPEED_AWARE is the right
# mode. FIXED_FRAME builders below are kept (with the directly observed bytes
# at default speed 0x08) so the CLI / panel still work if someone flips the
# switch — but SPEED_AWARE is the source of truth.

USE_MODE: str = "SPEED_AWARE"


# ---- Frame builders --------------------------------------------------------

_PREFIX = 0x5A
_SUFFIX = 0xA5
_PAD    = 0x00          # 4th byte, always 0 in captures

# DIR codes as observed.
DIR_STOP    = 0x00
DIR_FORWARD = 0x01
DIR_BACK    = 0x02
DIR_LEFT    = 0x04
DIR_RIGHT   = 0x08

# Observed speed values. 0x13 (19) was the on-screen "100% / boost" preset;
# 0x08 (8) was the normal preset. The range almost certainly extends to 0x14
# (20) — but we've only confirmed up to 0x13, so clamp there.
SPEED_NORMAL = 0x08
SPEED_MAX    = 0x13     # = 19; max observed

def _speed_byte(speed_percent: int) -> int:
    """Map a UI 0..100 percentage onto the toy's 0..0x13 speed byte.

    100 -> 0x13 ('100% / boost' the app exposes)
     ~42 -> 0x08 (matches the 'normal' preset the app sends without boost)
    """
    p = max(0, min(100, int(speed_percent)))
    return int(round(p * SPEED_MAX / 100)) & 0xFF

def _frame(dir_code: int, speed_byte: int) -> bytes:
    chk = (dir_code + speed_byte) & 0xFF
    return bytes([_PREFIX, dir_code, speed_byte, _PAD, chk, _SUFFIX])

# Speed-aware builders (used in SPEED_AWARE mode).
def forward_frame(speed: int) -> bytes: return _frame(DIR_FORWARD, _speed_byte(speed))
def back_frame   (speed: int) -> bytes: return _frame(DIR_BACK,    _speed_byte(speed))
def left_frame   (speed: int) -> bytes: return _frame(DIR_LEFT,    _speed_byte(speed))
def right_frame  (speed: int) -> bytes: return _frame(DIR_RIGHT,   _speed_byte(speed))
def stop_frame   () -> bytes:
    # Carry the 'normal' speed in the stop frame (matches what the app sends
    # when releasing a non-boost button).
    return _frame(DIR_STOP, SPEED_NORMAL)

# Fixed-frame fallback (used if USE_MODE = "FIXED_FRAME").
FIXED_FRAMES = {
    "forward":  _frame(DIR_FORWARD, SPEED_NORMAL),  # 5a 01 08 00 09 a5
    "back":     _frame(DIR_BACK,    SPEED_NORMAL),  # 5a 02 08 00 0a a5
    "left":     _frame(DIR_LEFT,    SPEED_NORMAL),  # 5a 04 08 00 0c a5
    "right":    _frame(DIR_RIGHT,   SPEED_NORMAL),  # 5a 08 08 00 10 a5
    "stop":     _frame(DIR_STOP,    SPEED_NORMAL),  # 5a 00 08 00 08 a5
}

# Persistent-speed setter — this toy has no such concept; speed is per-frame.
FIXED_SPEED_FRAMES: dict[int, bytes] = {}

# Handshake the official app sends on first GATT write after connect. Some
# toys gate motion behind it; we replay it once on connect just in case.
CONNECT_HANDSHAKE: bytes = bytes.fromhex("5aa56bbdd0304a17a5")


# ---- Unified API used by the rest of the codebase --------------------------

DIRECTIONS = ("forward", "back", "left", "right")

_DIR_BY_NAME = {
    "forward": DIR_FORWARD,
    "back":    DIR_BACK,
    "left":    DIR_LEFT,
    "right":   DIR_RIGHT,
    "stop":    DIR_STOP,
}

def build_raw(direction: str, speed_byte: int) -> bytes:
    """Build a frame with an explicit SPEED *byte* (0..0x13), bypassing the
    percent mapping. Used by the trajectory engine, which works in calibrated
    speed bytes from speed_model rather than UI percentages."""
    return _frame(_DIR_BY_NAME[direction], speed_byte & 0xFF)

def build(direction: str, speed: int = 100) -> bytes:
    if USE_MODE == "SPEED_AWARE":
        return {
            "forward": forward_frame,
            "back":    back_frame,
            "left":    left_frame,
            "right":   right_frame,
        }[direction](speed)
    if USE_MODE == "FIXED_FRAME":
        return FIXED_FRAMES[direction]
    raise ValueError(f"bad USE_MODE {USE_MODE!r}")

def build_stop() -> bytes:
    return stop_frame() if USE_MODE == "SPEED_AWARE" else FIXED_FRAMES["stop"]

def build_set_speed(level: int) -> bytes | None:
    return None  # speed is per-command in this protocol

def protocol_ready() -> tuple[bool, str]:
    if not WRITE_UUID:
        return False, "WRITE_UUID empty — run probe.py and set it in mouse_protocol.py"
    return True, "ok"
