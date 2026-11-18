"""Minimal YouTube fetch relay (standalone, no supoclip dependencies).

Same contract as the supoclip fetch-relay route:
- GET  /fetch-relay/status
- POST /fetch-relay/probe     {"url", "video_id"}
- POST /fetch-relay/download  {"url", "video_id"}   -> video/mp4 stream
Header: x-relay-token: <FETCH_RELAY_TOKEN>. One download at a time.

Includes the bgutil-ytdlp-pot-provider plugin (script mode, deno) so the
web player client can present a PO token from datacenter IPs.
Env: BGUTIL_SERVER_HOME (default /opt/bgutil/server), YT_COOKIES
(Netscape cookie jar contents), YOUTUBE_MAX_HEIGHT.
"""
import asyncio
import logging
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("relay")

app = FastAPI()
_lock = asyncio.Lock()

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,16}$")
_ALLOWED_HOSTS = ("youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com")
_MAX_HEIGHT = max(144, min(int(os.getenv("YOUTUBE_MAX_HEIGHT", "720") or 720), 1080))
# web first: the bgutil POT plugin (script mode) unlocks it from
# datacenter IPs; embedded clients next; native apps last.
_CLIENTS = ["web", "tv", "android_vr", "ios", "mweb",
            "web_embedded", "tv_embedded", "default"]

_COOKIES_PATH = Path("/tmp/relay_cookies.txt")
_POT_HOME = Path(os.getenv("BGUTIL_SERVER_HOME", "/opt/bgutil/server"))


def _cookies_file() -> str | None:
    """Materialize the operator's exported cookies (Netscape format) if the
    YT_COOKIES env var is set. Never logged, never returned to clients."""
    content = os.getenv("YT_COOKIES", "").strip()
    if not content:
        return None
    if not _COOKIES_PATH.exists() or _COOKIES_PATH.read_text() != content:
        _COOKIES_PATH.write_text(content)
        _COOKIES_PATH.chmod(0o600)
    return str(_COOKIES_PATH)


def _authorized(request: Request) -> bool:
    token = os.getenv("FETCH_RELAY_TOKEN", "")
    supplied = request.headers.get("x-relay-token", "")
    return bool(token) and secrets.compare_digest(token, supplied)


def _validate(url: str, video_id: str) -> None:
    if not _VIDEO_ID_RE.match(video_id or ""):
        raise HTTPException(422, "video_id must be 6-16 [A-Za-z0-9_-]")
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if host not in _ALLOWED_HOSTS:
        raise HTTPException(422, "only youtube.com / youtu.be URLs accepted")


def _opts(client: str, outtmpl: str | None = None, download: bool = False) -> dict:
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 20, "retries": 2, "fragment_retries": 3,
        "http_headers": {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if download:
        opts["format"] = (f"bv*[height<={_MAX_HEIGHT}]+ba/"
                          f"b[height<={_MAX_HEIGHT}]/b")
        opts["merge_output_format"] = "mp4"
        opts["outtmpl"] = outtmpl
        opts["overwrites"] = True
        opts["concurrent_fragment_downloads"] = 4
    else:
        opts["skip_download"] = True
    ext_args: dict = {}
    if client != "default":
        ext_args["youtube"] = {"player_client": [client]}
    if _POT_HOME.is_dir():
        ext_args["youtubepot-bgutilscript"] = {"server_home": [str(_POT_HOME)]}
    if ext_args:
        opts["extractor_args"] = ext_args
    cookies = _cookies_file()
    if cookies:
        opts["cookiefile"] = cookies
    if shutil.which("deno"):
        opts["js_runtimes"] = {"deno": {}}
    return opts


def _probe_one(url: str, client: str) -> dict:
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL(_opts(client)) as ydl:
            info = ydl.extract_info(url, download=False)
        return {"client": client, "ok": True,
                "title": (info or {}).get("title"),
                "duration": (info or {}).get("duration")}
    except Exception as exc:  # noqa: BLE001
        return {"client": client, "ok": False, "error": str(exc)[:200]}


def _download(url: str, dest: Path, video_id: str) -> Path:
    import yt_dlp
    errors: list[str] = []
    for client in _CLIENTS:
        try:
            with yt_dlp.YoutubeDL(
                    _opts(client, outtmpl=str(dest / f"{video_id}.%(ext)s"),
                          download=True)) as ydl:
                ydl.download([url])
            for cand in sorted(dest.glob(f"{video_id}.*")):
                if cand.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
                    return cand
            raise RuntimeError("yt-dlp success but no media file")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{client}: {str(exc)[:160]}")
            for stale in dest.glob(f"{video_id}.*"):
                try:
                    stale.unlink()
                except OSError:
                    pass
    raise RuntimeError(" | ".join(errors) or "all clients failed")


@app.get("/render-healthz")
def healthz():
    return "ok"


@app.get("/fetch-relay/status")
def status(request: Request):
    if not _authorized(request):
        raise HTTPException(403, "unauthorized")
    return {"busy": _lock.locked(), "max_height": _MAX_HEIGHT,
            "cookies": bool(os.getenv("YT_COOKIES", "").strip()),
            "deno": bool(shutil.which("deno")),
            "pot": bool(shutil.which("deno") and _POT_HOME.is_dir())}


@app.post("/fetch-relay/probe")
async def probe(request: Request):
    if not _authorized(request):
        raise HTTPException(403, "unauthorized")
    body = await request.json()
    url = str(body.get("url") or "")
    _validate(url, str(body.get("video_id") or "x0x0x0x0x0x"))
    import yt_dlp
    results = [await asyncio.to_thread(_probe_one, url, c)
               for c in ["web", "android_vr", "tv", "ios", "mweb", "default"]]
    return {"yt_dlp_version": yt_dlp.version.__version__,
            "deno": bool(shutil.which("deno")), "results": results}


@app.post("/fetch-relay/download")
async def download(request: Request):
    if not _authorized(request):
        raise HTTPException(403, "unauthorized")
    if _lock.locked():
        raise HTTPException(429, "another download is running; retry shortly")
    body = await request.json()
    url = str(body.get("url") or "")
    video_id = str(body.get("video_id") or "")
    _validate(url, video_id)
    async with _lock:
        dest = Path(tempfile.mkdtemp(prefix="relay-"))
        try:
            path = await asyncio.wait_for(
                asyncio.to_thread(_download, url, dest, video_id),
                timeout=1500)
            if not path.exists() or path.stat().st_size == 0:
                raise HTTPException(502, "download produced no file")
            return FileResponse(
                str(path), media_type="video/mp4",
                filename=f"{video_id}.mp4",
                background=BackgroundTask(shutil.rmtree, str(dest), True))
        except HTTPException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        except BaseException as exc:  # noqa: BLE001
            shutil.rmtree(dest, ignore_errors=True)
            raise HTTPException(
                502, f"download failed: {type(exc).__name__}: {exc}"[:400]
            ) from exc
