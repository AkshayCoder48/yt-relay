"""Segment clipping: face-tracked 9:16 crop + captions + loudnorm via ffmpeg.

Pipeline per segment (memory-sequential for 512 MB instances):
  1. ffprobe source dimensions/duration
  2. OpenCV face tracking -> piecewise crop x-expression (released after)
  3. ASS caption file (if cues provided)
  4. ffmpeg re-encode: accurate seek cut, crop, scale, subtitles, fades,
     loudnorm, faststart  ->  clip_XX.mp4
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .captions import build_ass
from .faces import crop_expression, track_crop_centers


def _ffprobe(path: Path) -> dict:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=width,height,codec_type", "-of", "json",
         str(path)], capture_output=True, text=True)
    d = json.loads(p.stdout or "{}")
    v = next((s for s in d.get("streams", [])
              if s.get("codec_type") == "video"), {})
    return {"w": int(v.get("width") or 0), "h": int(v.get("height") or 0),
            "dur": float((d.get("format") or {}).get("duration") or 0)}


def clip_segments(job: dict, opts: dict) -> None:
    src = Path(job["meta"].get("src_path")
               or Path(job["dir"]) / "source.mp4")
    if not src.exists():
        # clip() with a bare URL: fetch inside the clip job
        from .fetcher import fetch_source
        url = job["meta"].get("url")
        if not url:
            raise RuntimeError("clip job needs url or job_id")
        fetch_source(job, url, int(job["meta"].get("max_height") or 720))
        if not src.exists():
            raise RuntimeError("media fetch failed for clip job")
    info = _ffprobe(src)
    if not info["w"]:
        raise RuntimeError("ffprobe failed on source")
    segs = job["meta"]["segments"]
    out_w = round(opts["height"] * 9 / 16)
    files: list[str] = []
    for i, seg in enumerate(segs):
        start, end = float(seg["start"]), float(seg["end"])
        dur = min(end - start, max(0.0, info["dur"] - start))
        if dur < 3:
            raise RuntimeError(f"segment {i+1}: duration {dur:.1f}s < 3s")
        job["progress"] = round(i / max(len(segs), 1), 2)
        job["detail"] = f"segment {i+1}/{len(segs)}: preparing"

        vf_parts: list[str] = []
        if info["w"] / max(info["h"], 1) >= 1.2:      # landscape source
            crop_w = min(round(info["h"] * 9 / 16), info["w"])
            if opts["face_track"] and info["w"] - crop_w > 40:
                samples = track_crop_centers(str(src), start, end)
                x_expr = crop_expression(samples, info["w"], crop_w)
                job["log_tail"] = job["log_tail"] + [
                    f"seg {i+1}: {len(samples)} face samples"]
            else:
                x_expr = str((info["w"] - crop_w) // 2)
            vf_parts.append(
                f"crop={crop_w}:{info['h']}:x='{x_expr}':y=0")
            vf_parts.append(f"scale={out_w}:{opts['height']}"
                            ":flags=bicubic")
        else:                                          # portrait source
            vf_parts.append(f"scale={out_w}:{opts['height']}"
                            ":force_original_aspect_ratio=decrease")
            vf_parts.append(f"pad={out_w}:{opts['height']}:"
                            f"(ow-iw)/2:(oh-ih)/2:black")
        vf_parts.append("setsar=1")

        cues = seg.get("cues") or []
        if cues:
            ass = Path(job["dir"]) / f"cap_{i+1:02d}.ass"
            ass.write_text(build_ass(cues, start, end, out_w,
                                     opts["height"],
                                     opts["font_scale"]), encoding="utf-8")
            vf_parts.append(f"subtitles={ass}")
        if opts["fades"]:
            vf_parts += [f"fade=t=in:st=0:d=0.25",
                         f"fade=t=out:st={dur-0.25:.2f}:d=0.25"]
        vf_parts.append("format=yuv420p")

        out = Path(job["dir"]) / f"clip_{i+1:02d}.mp4"
        job["detail"] = f"segment {i+1}/{len(segs)}: encoding {dur:.0f}s"
        af = "loudnorm=I=-16:TP=-1.5:LRA=11"
        if opts["fades"]:
            af += f",afade=t=in:st=0:d=0.25," \
                  f"afade=t=out:st={dur-0.25:.2f}:d=0.25"
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}",
               "-i", str(src), "-t", f"{dur:.3f}",
               "-vf", ",".join(vf_parts), "-af", af,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-threads", "2", "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart", str(out)]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0 or not out.exists():
            tail = (p.stderr or "")[-400:]
            raise RuntimeError(f"ffmpeg failed on segment {i+1}: {tail}")
        files.append(out.name)
    job["files"] = files
    job["meta"]["outputs"] = files
