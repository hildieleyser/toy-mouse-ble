"""TileVideo — paint one clip camera onto a Tk Canvas.

Shared by the full dashboard (mouse_panel.py) and the simplified touch dashboard
(show_dashboard.py). Decodes a clip's per-camera mp4s, keeping one cv2 capture per
opened camera, and paints the requested frame of the selected camera as the canvas
background so a trajectory overlay can be drawn on top. Self-contained: it touches
only its canvas + cv2 + PIL (both imported lazily so importing this module is cheap
and works even where OpenCV/Pillow aren't installed, as long as no tile is shown).
"""
from __future__ import annotations

import tkinter as tk


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
