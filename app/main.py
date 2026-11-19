"""clip-api — YouTube fetch + advanced clipping microservice for Render.

Auth: `x-api-key` header must match env CLIP_API_TOKEN (except /health).

Endpoints:
  GET  /health                        public liveness (Render health check)
  POST /api/probe   {url}             metadata: title/author/duration (oEmbed
                                      + Invidious/Piped, no download)
  POST /api/fetch   {url, max_height?}
                                      job: download source via public-downloader
                                      ladder (Invidious -> Cobalt -> Piped) to
                                      /tmp store; stream back from
                                      GET /api/jobs/<id>/file
  POST /api/clip    {url?|job_id?, segments:[{start,end,cues?}], height?,
                     face_track?, font_scale?, fades?}
                                      job: fetch if needed, then per segment:
                                      face-tracked 9:16 crop (OpenCV Haar),
                                      ffmpeg cut, styled ASS caption burn,
                                      loudnorm, fades
  GET  /api/jobs/<id>                 {status, progress, detail, log_tail}
  GET  /api/jobs/<id>/files           [{name, size, url}]
  GET  /api/jobs/<id>/file?name=      stream (Range supported)

Free-tier notes: one heavy job at a time (semaphore); jobs expire after 6h;
clients must poll at least every 10 min to keep the free instance awake.
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .fetcher import probe_metadata, fetch_source
from .clipping import clip_segments

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("clipapi")

TOKEN = os.getenv("CLIP_API_TOKEN", "")
DATA_ROOT = Path(os.getenv("DATA_ROOT", "/tmp/clipapi"))
JOB_TTL_S = int(os.getenv("JOB_TTL_S", "21600"))  # 6h
MAX_SEGMENTS = 8

app = FastAPI(title="clip-api", docs_url=None, redoc_url=None)
_slot = threading.BoundedSemaphore(1)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _auth(x_api_key: str | None) -> None:
    if not TOKEN:
        raise HTTPException(503, "service not configured (CLIP_API_TOKEN missing)")
    if not x_api_key or not secrets.compare_digest(TOKEN, x_api_key):
        raise HTTPException(401, "bad x-api-key")


def _new_job(kind: str) -> dict:
    jid = time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
    job = {"id": jid, "kind": kind, "status": "queued", "progress": 0.0,
           "detail": "", "log_tail": [], "meta": {}, "files": [],
           "dir": str(DATA_ROOT / jid), "created": time.time()}
    Path(job["dir"]).mkdir(parents=True, exist_ok=True)
    with _jobs_lock:
        _jobs[jid] = job
    return job


def _logline(job: dict, msg: str) -> None:
    log.info("[%s] %s", job["id"], msg)
    job["log_tail"] = (job["log_tail"] + [msg])[-25:]


def _run_job(job: dict, fn, *args) -> None:
    def runner():
        with _slot:
            job["status"] = "running"
            try:
                fn(job, *args)
                job["status"] = "done"
                job["progress"] = 1.0
            except Exception as exc:  # noqa: BLE001
                job["status"] = "error"
                job["detail"] = f"{type(exc).__name__}: {exc}"
                log.exception("job %s failed", job["id"])
    threading.Thread(target=runner, daemon=True).start()


def _cleanup_loop() -> None:
    while True:
        time.sleep(600)
        with _jobs_lock:
            stale = [jid for jid, j in _jobs.items()
                     if j["status"] in ("done", "error")
                     and time.time() - j["created"] > JOB_TTL_S]
            for jid in stale:
                _jobs.pop(jid, None)
        for jid in stale:
            shutil.rmtree(DATA_ROOT / jid, ignore_errors=True)


threading.Thread(target=_cleanup_loop, daemon=True).start()


# ------------------------------------------------------------------ health
@app.get("/health")
def health():
    return {"ok": True, "jobs_active": sum(
        1 for j in _jobs.values() if j["status"] in ("queued", "running"))}


@app.get("/")
def root():
    return JSONResponse({"service": "clip-api", "auth": "x-api-key"})


# ------------------------------------------------------------------ probe
@app.post("/api/probe")
def probe(body: dict, x_api_key: str | None = Header(None)):
    _auth(x_api_key)
    url = (body or {}).get("url", "")
    if "youtu" not in url:
        raise HTTPException(422, "only youtube.com / youtu.be URLs accepted")
    meta = probe_metadata(url)
    if not meta.get("ok"):
        raise HTTPException(502, "probe failed on all metadata services")
    return meta


# ------------------------------------------------------------------ fetch
@app.post("/api/fetch")
def fetch(body: dict, x_api_key: str | None = Header(None)):
    _auth(x_api_key)
    url = (body or {}).get("url", "")
    max_h = int((body or {}).get("max_height") or 720)
    if "youtu" not in url:
        raise HTTPException(422, "only youtube.com / youtu.be URLs accepted")
    job = _new_job("fetch")
    job["meta"] = {"url": url, "max_height": max_h}
    _run_job(job, fetch_source, url, max_h)
    return {"job_id": job["id"], "status": job["status"]}


# ------------------------------------------------------------------ clip
@app.post("/api/clip")
def clip(body: dict, x_api_key: str | None = Header(None)):
    _auth(x_api_key)
    body = body or {}
    segs = body.get("segments") or []
    if not segs or len(segs) > MAX_SEGMENTS:
        raise HTTPException(422, f"segments must be 1..{MAX_SEGMENTS} "
                                 "items: {start, end, cues?}")
    for s in segs:
        if not (0 <= s.get("start", -1) < s.get("end", -1)):
            raise HTTPException(422, "bad segment times")
    opts = dict(
        height=min(max(int(body.get("height") or 1280), 480), 1920),
        face_track=bool(body.get("face_track", True)),
        font_scale=float(body.get("font_scale") or 1.0),
        fades=bool(body.get("fades", True)),
        title=(body.get("title") or "")[:80],
    )
    job = _new_job("clip")
    job["meta"] = {"segments": segs, **opts}
    if body.get("url") and not body.get("job_id"):
        if "youtu" not in body["url"]:
            raise HTTPException(422, "only youtube.com / youtu.be URLs accepted")
        job["meta"]["url"] = body["url"]
    if body.get("job_id"):
        src = DATA_ROOT / str(body["job_id"]) / "source.mp4"
        if not src.exists():
            raise HTTPException(404, f"no fetched media for job {body['job_id']}")
        job["meta"]["src_path"] = str(src)
    _run_job(job, clip_segments, opts)
    return {"job_id": job["id"], "status": job["status"]}


# ------------------------------------------------------------------ jobs
@app.get("/api/jobs/{jid}")
def job_status(jid: str, x_api_key: str | None = Header(None)):
    _auth(x_api_key)
    job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, "unknown job (may have expired)")
    return {k: job[k] for k in
            ("id", "kind", "status", "progress", "detail", "log_tail", "meta")}


@app.get("/api/jobs/{jid}/files")
def job_files(jid: str, x_api_key: str | None = Header(None)):
    _auth(x_api_key)
    job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, "unknown job")
    out = []
    for name in job["files"]:
        p = Path(job["dir"]) / name
        if p.exists():
            out.append({"name": name, "size": p.stat().st_size,
                        "url": f"/api/jobs/{jid}/file?name={name}"})
    return out


@app.get("/api/jobs/{jid}/file")
def job_file(jid: str, name: str = "source.mp4",
             x_api_key: str | None = Header(None)):
    _auth(x_api_key)
    job = _jobs.get(jid)
    if not job:
        raise HTTPException(404, "unknown job")
    if name not in job["files"] and name != "source.mp4":
        raise HTTPException(404, "unknown file")
    p = Path(job["dir"]) / name
    if not p.exists():
        raise HTTPException(404, "file missing (or still processing)")
    return FileResponse(p, media_type="video/mp4", filename=name)
