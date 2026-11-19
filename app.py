"""clip-api — media clipping service on Render (auto-editor + ffmpeg).

Wraps the open-source auto-editor (github.com/WyattBlue/auto-editor,
Public Domain) and plain ffmpeg behind a small token-authed REST API so
the OOM-limited agency sandbox can offload heavy media work.

Routes (header `x-relay-token: <CLIP_API_TOKEN>`):
  GET  /render-healthz          no auth, for Render health checks
  GET  /                        service banner (no auth)
  GET  /clip/status             tool versions, busy flag
  POST /clip/probe              multipart file         -> ffprobe json
  POST /clip/extract            file+start_s+end_s     -> exact cut mp4
                                (mode=encode frame-accurate | mode=copy fast)
  POST /clip/tighten            file+margin+edit       -> auto-editor
                                silence/motion tightened mp4
  POST /clip/verticalize        file(+srt)+badge        -> 1080x1920 vertical
                                with blurred bg + burned captions

One job at a time (429 otherwise): Render free is 512 MB — the lock is
the OOM guard. Every job runs in its own temp dir which is removed when
the response finishes streaming.
"""
import asyncio
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask


def _jobdir(prefix: str) -> Path:
    """Temp dir that outlives the handler: cleaned by a BackgroundTask
    after the response finished streaming (never a context manager —
    that would delete the file before FileResponse can serve it)."""
    return Path(tempfile.mkdtemp(prefix=prefix))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clip-api")

app = FastAPI(title="clip-api")
_lock = asyncio.Lock()

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "400"))
PROCESS_TIMEOUT_S = int(os.getenv("PROCESS_TIMEOUT_S", "1800"))
AE_BIN = os.getenv("AE_BIN", "/usr/local/bin/auto-editor")
FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
DEFAULT_STYLE = ("FontName=Liberation Sans,Fontsize=13,Bold=1,"
                 "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                 "BorderStyle=1,Outline=1,Shadow=1,Alignment=2,MarginV=56")


def _authorized(request: Request) -> bool:
    token = os.getenv("CLIP_API_TOKEN", "")
    supplied = request.headers.get("x-relay-token", "")
    return bool(token) and secrets.compare_digest(token, supplied)


def _guard(request: Request) -> None:
    if not _authorized(request):
        raise HTTPException(403, "unauthorized")
    if _lock.locked():
        raise HTTPException(429, "another job is running; retry shortly")
    declared = request.headers.get("content-length")
    if declared and int(declared) > (MAX_UPLOAD_MB + 8) * 1024 * 1024:
        raise HTTPException(413, f"upload larger than {MAX_UPLOAD_MB} MB cap")


async def _save_upload(dest: Path, upload: UploadFile) -> Path:
    written = 0
    cap = MAX_UPLOAD_MB * 1024 * 1024
    with open(dest, "wb") as f:
        while True:
            chunk = await upload.read(1 << 20)
            if not chunk:
                break
            written += len(chunk)
            if written > cap:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"upload larger than {MAX_UPLOAD_MB} MB cap")
            f.write(chunk)
    if written < 1024:
        raise HTTPException(422, "uploaded file is empty")
    return dest


def _run(cmd: list, timeout: int, cwd: str | None = None) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, cwd=cwd)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise HTTPException(502, f"command failed: {tail}")
    return proc.stdout + proc.stderr


def _ffprobe(path: Path) -> dict:
    out = _run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration,size:stream=codec_type,codec_name,width,height",
                "-of", "json", str(path)], timeout=120)
    return json.loads(out)


def _duration(path: Path) -> float:
    try:
        return float(_ffprobe(path)["format"]["duration"])
    except (KeyError, ValueError, HTTPException):
        return -1.0


def _ae_version() -> str:
    try:
        return subprocess.run([AE_BIN, "--version"], capture_output=True,
                              text=True, timeout=60).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unavailable"


# --------------------------------------------------------------- routes

@app.get("/render-healthz")
def healthz():
    return "ok"


@app.get("/")
def index():
    return {"service": "clip-api",
            "tools": "auto-editor (Public Domain) + ffmpeg",
            "auth": "x-relay-token header on /clip/* routes"}


@app.get("/clip/status")
def status(request: Request):
    _guard(request)
    ff = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                        text=True, timeout=60).stdout.split("\n")[0]
    return {"busy": _lock.locked(),
            "ffmpeg": ff, "auto_editor": _ae_version(),
            "max_upload_mb": MAX_UPLOAD_MB,
            "process_timeout_s": PROCESS_TIMEOUT_S}


@app.post("/clip/probe")
async def probe(request: Request, file: UploadFile = File(...)):
    _guard(request)
    async with _lock:
        td = _jobdir("probe-")
        try:
            src = await _save_upload(td / "in.bin", file)
            return JSONResponse(_ffprobe(src))
        finally:
            shutil.rmtree(td, ignore_errors=True)


@app.post("/clip/extract")
async def extract(request: Request,
                  file: UploadFile = File(...),
                  start_s: float = Form(0.0),
                  end_s: float = Form(-1.0),
                  mode: str = Form("encode"),
                  crf: int = Form(20),
                  height: int = Form(0)):
    """Exact cut: re-encode (frame-accurate) or stream-copy (keyframe)."""
    _guard(request)
    if mode not in ("encode", "copy"):
        raise HTTPException(422, "mode must be encode|copy")
    if end_s > 0 and end_s <= start_s:
        raise HTTPException(422, "end_s must be greater than start_s")
    if not (18 <= crf <= 28):
        raise HTTPException(422, "crf must be 18-28")
    async with _lock:
        td = _jobdir("extract-")
        try:
            src = await _save_upload(td / "in.bin", file)
            dur = _duration(src)
            if end_s <= 0:
                end_s = dur if dur > 0 else 0.0
            if dur > 0 and (start_s >= dur or end_s > dur + 1.0):
                raise HTTPException(422, f"range {start_s:.2f}-{end_s:.2f} "
                                          f"outside media duration {dur:.2f}")
            length = max(0.1, end_s - start_s)
            out = td / "clip.mp4"
            cmd = ["ffmpeg", "-y", "-loglevel", "error",
                   "-ss", f"{max(0.0, start_s):.3f}", "-i", str(src),
                   "-t", f"{length:.3f}"]
            if mode == "copy":
                cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
            else:
                cmd += ["-threads", "2", "-c:v", "libx264", "-preset",
                        "veryfast", "-crf", str(crf),
                        "-c:a", "aac", "-b:a", "160k"]
                if 240 <= height <= 2160:
                    cmd += ["-vf", f"scale=-2:'min({height},ih)'"]
            cmd += ["-movflags", "+faststart", str(out)]
            await asyncio.to_thread(_run, cmd, PROCESS_TIMEOUT_S)
            if not out.exists() or out.stat().st_size < 10_000:
                raise HTTPException(502, "extract produced no output")
            return FileResponse(str(out), media_type="video/mp4",
                                filename="clip.mp4",
                                background=BackgroundTask(
                                    shutil.rmtree, str(td), True))
        except BaseException:
            shutil.rmtree(td, ignore_errors=True)
            raise


@app.post("/clip/tighten")
async def tighten(request: Request,
                  file: UploadFile = File(...),
                  edit: str = Form("audio"),
                  margin: str = Form("0.25s"),
                  when_silent: str = Form("cut")):
    """auto-editor pass: cut silence (edit=audio) or stillness (edit=motion).

    Returns the tightened mp4; X-Input-Duration / X-Output-Duration
    response headers carry the before/after lengths. If nothing was
    tightenable the input is returned unchanged (headers show it).
    """
    _guard(request)
    if edit not in ("audio", "motion", "audio:threshold=2%", "none"):
        raise HTTPException(422, "edit must be audio | motion | none")
    if when_silent not in ("cut", "nil"):
        raise HTTPException(422, "when_silent must be cut | nil")
    async with _lock:
        td = _jobdir("tighten-")
        try:
            src = await _save_upload(td / "in.mp4", file)
            in_dur = _duration(src)
            out = td / "tight.mp4"
            cmd = [AE_BIN, str(src), "--edit", edit, "--margin", margin,
                   "--when-silent", when_silent, "--quiet", "--no-open",
                   "-o", str(out)]
            await asyncio.to_thread(_run, cmd, PROCESS_TIMEOUT_S)
            if not out.exists() or out.stat().st_size < 10_000:
                raise HTTPException(502, "auto-editor produced no output")
            out_dur = _duration(out)
            headers = {"X-Input-Duration": f"{in_dur:.3f}",
                       "X-Output-Duration": f"{out_dur:.3f}"}
            src_name = Path("input.mp4")
            serve = out if out_dur < in_dur - 0.05 else src
            return FileResponse(str(serve), media_type="video/mp4",
                                filename=src_name.name, headers=headers,
                                background=BackgroundTask(
                                    shutil.rmtree, str(td), True))
        except BaseException:
            shutil.rmtree(td, ignore_errors=True)
            raise


@app.post("/clip/verticalize")
async def verticalize(request: Request,
                      file: UploadFile = File(...),
                      srt: UploadFile | None = File(None),
                      badge: str = Form(""),
                      style: str = Form(DEFAULT_STYLE),
                      target_h: int = Form(1920)):
    """9:16 vertical render: blurred cover background + contained foreground
    + optional burned subtitles (SRT upload) + optional top badge text.
    This is the HyperFrames-free fallback renderer for OOM situations."""
    _guard(request)
    if not (720 <= target_h <= 2160):
        raise HTTPException(422, "target_h must be 720-2160")
    if len(badge) > 40:
        raise HTTPException(422, "badge too long (max 40 chars)")
    if len(style) > 400:
        raise HTTPException(422, "style too long (max 400 chars)")
    async with _lock:
        td = _jobdir("vert-")
        try:
            src = await _save_upload(td / "in.mp4", file)
            srt_text = None
            if srt is not None and srt.filename and srt.filename.endswith(".srt"):
                raw = await srt.read()
                if len(raw) > 512_000:
                    raise HTTPException(422, "srt too large")
                srt_text = raw.decode("utf-8", errors="replace").strip()
            w = target_h * 9 // 16
            parts = [
                f"[0:v]split=2[v0][v1]",
                f"[v0]scale={w}:{target_h}:force_original_aspect_ratio=increase"
                f",crop={w}:{target_h},gblur=sigma=24,eq=brightness=-0.15[bgb]",
                f"[v1]scale={w}:{target_h}:force_original_aspect_ratio=decrease[fg]",
                f"[bgb][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2[comp]",
            ]
            last = "comp"
            if srt_text:
                (td / "subs.srt").write_text(srt_text + "\n",
                                               encoding="utf-8")
                parts.append(f"[{last}]subtitles=subs.srt:force_style='{style}'[vsub]")
                last = "vsub"
            if badge:
                esc = (badge.replace("\\", "\\\\").replace(":", "\\:")
                        .replace("'", "\\'"))
                parts.append(
                    f"[{last}]drawtext=fontfile={FONT_BOLD}:text='{esc}'"
                    f":fontcolor=black:box=1:boxcolor=white@0.92:boxborderw=22"
                    f":fontsize={target_h // 44}:x=(w-text_w)/2:y={target_h // 22}"
                    f"[vfin]")
                last = "vfin"
            vf = ";".join(parts)
            out = td / "vertical.mp4"
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                   "-filter_complex", vf, "-map", f"[{last}]",
                   "-map", "0:a?", "-threads", "2", "-c:v", "libx264",
                   "-preset", "veryfast", "-crf", "20", "-c:a", "aac",
                   "-b:a", "160k", "-movflags", "+faststart", str(out)]
            await asyncio.to_thread(
                _run, cmd, PROCESS_TIMEOUT_S, str(td))
            if not out.exists() or out.stat().st_size < 10_000:
                raise HTTPException(502, "verticalize produced no output")
            return FileResponse(str(out), media_type="video/mp4",
                                filename="vertical.mp4",
                                background=BackgroundTask(
                                    shutil.rmtree, str(td), True))
        except BaseException:
            shutil.rmtree(td, ignore_errors=True)
            raise
