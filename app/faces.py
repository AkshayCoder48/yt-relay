"""Face-tracked 9:16 crop window computation (OpenCV Haar).

Pattern popularized by the open-source clippers (Autocrop-vertical /
OpenShorts): sample frames across the segment, detect faces, pick the
dominant face center per sample, smooth with a rolling median and emit a
piecewise ffmpeg `crop` x-expression with 2s granularity.

Memory-friendly for Render free tier: frames are decoded at ~2 fps and
downscaled to 320 px wide; VideoCapture is released before ffmpeg runs.
"""
from __future__ import annotations

import cv2
import numpy as np

_CASCADE = None


def _cascade():
    global _CASCADE
    if _CASCADE is None:
        _CASCADE = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return _CASCADE


def track_crop_centers(source: str, start: float, end: float,
                       step_s: float = 2.0) -> list[tuple[float, float]]:
    """Return [(t_rel, x_center_src_px)] sampled every step_s seconds."""
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError("cannot open source for face tracking")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        w = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 640.0
        scale = 320.0 / max(w, 1)
        cascade = _cascade()
        out: list[tuple[float, float]] = []
        t = start
        while t < end - 0.05:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok:
                break
            small = cv2.resize(frame, None, fx=scale, fy=scale)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.15, 5, minSize=(24, 24))
            if len(faces):
                # dominant face = largest box; center mapped to source px
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                out.append((t - start, (x + fw / 2) / scale))
            t += step_s
        return out
    finally:
        cap.release()


def crop_expression(samples: list[tuple[float, float]], src_w: float,
                    crop_w: int, step_s: float = 2.0) -> str:
    """Piecewise-linear ffmpeg crop x expression, clamped, quantized 2s."""
    if not samples:
        return str(int(max(0.0, (src_w - crop_w) / 2)))
    # bucket to step_s grid -> median of centers in each bucket
    grid: dict[int, list[float]] = {}
    for t, xc in samples:
        grid.setdefault(int(t // step_s), []).append(xc)
    keys = sorted(grid)
    centers = [float(np.median(grid[k])) for k in keys]
    # rolling median smoothing (window 3)
    sm = []
    for i in range(len(centers)):
        lo, hi = max(0, i - 1), min(len(centers), i + 2)
        sm.append(float(np.median(centers[lo:hi])))
    # clamp crop window
    xs = [int(round(min(max(c - crop_w / 2, 0.0), src_w - crop_w)))
          for c in sm]
    # de-duplicate consecutive equal values (shorter expression)
    kept: list[tuple[float, int]] = []
    for k, x in zip(keys, xs):
        if not kept or kept[-1][1] != x:
            kept.append((k * step_s, x))
    expr = ""
    for t, x in reversed(kept):
        expr = f"if(lt(t\\,{t:.1f})\\,{x}\\,{expr or x})"
    return expr
