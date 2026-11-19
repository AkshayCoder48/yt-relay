"""Public-downloader fetch ladder: Invidious -> Cobalt -> Piped.

Ported from the agency's proven scripts/fetch_via_services.py. YouTube
bot-walls datacenter IPs (sandbox AND Render), so we never hit googlevideo
directly: public deployed downloaders fetch the media and we stream it back.

Each candidate media URL is smoke-tested with a ranged GET before the full
download; the file is staged to source.mp4.part and verified with ffprobe.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

INVIDIOUS = [
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
    "https://iv.melmac.space",
    "https://invidious.jing.rocks",
    "https://iv.ggtyler.dev",
    "https://invidious.f5.si",
    "https://invidious.privacyredirect.com",
]
COBALT = [
    "https://api.cobalt.tools",
    "https://cobalt-api.kwiatekmiki.com",
    "https://cobalt-backend.canine.tools",
    "https://capi.3kh0.net",
    "https://cobalt-api.ayo.tf",
]
PIPED = [
    "https://pipedapi.kavin.rocks",
    "https://api.piped.private.coffee",
    "https://pipedapi.adminforge.de",
    "https://pipedapi.drgns.space",
    "https://pipedapi.reallyaweso.me",
]

VIDEO_ID_RE = re.compile(r"(?:v=|shorts/|embed/|live/|youtu\.be/)"
                         r"([A-Za-z0-9_-]{6,16})")


def video_id(url: str) -> str:
    m = VIDEO_ID_RE.search(url)
    if not m:
        raise ValueError(f"cannot parse video id from {url}")
    return m.group(1)


# ---------------------------------------------------------------- helpers
def _smoke(url: str) -> int:
    try:
        with requests.get(url, headers={**HDRS, "Range": "bytes=0-1048575"},
                          stream=True, timeout=(15, 60)) as r:
            if r.status_code not in (200, 206):
                return 0
            ct = r.headers.get("Content-Type", "")
            if "text/html" in ct or "json" in ct:
                return 0
            n = 0
            for chunk in r.iter_content(1 << 16):
                n += len(chunk)
                if n >= 262144:  # 256 KB is proof enough
                    return n
            return n if n > 100_000 else 0
    except requests.RequestException:
        return 0


def _full_download(url: str, dest: Path, on_bytes=None) -> bool:
    part = dest.with_suffix(".part")
    try:
        with requests.get(url, headers=HDRS, stream=True,
                          timeout=(30, 180)) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    if on_bytes:
                        on_bytes(len(chunk))
    except requests.RequestException:
        part.unlink(missing_ok=True)
        return False
    if part.stat().st_size < 2_000_000:
        part.unlink(missing_ok=True)
        return False
    part.replace(dest)
    return True


def _ffprobe_ok(path: Path) -> bool:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True)
    try:
        d = json.loads(p.stdout)
        dur = float(d["format"]["duration"])
        types = {s.get("codec_type") for s in d.get("streams", [])}
        return dur > 10 and "audio" in types and "video" in types
    except Exception:
        return False


def _merge_av(video: Path, audio: Path, dest: Path) -> bool:
    p = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(audio),
         "-c", "copy", "-movflags", "+faststart", str(dest)],
        capture_output=True, text=True)
    return p.returncode == 0 and dest.exists()


# ---------------------------------------------------------------- ladder
def _invidious(vid: str, dest: Path, max_h: int, job=None) -> bool:
    for base in INVIDIOUS:
        try:
            r = requests.get(f"{base}/api/v1/videos/{vid}",
                             params={"local": "true"}, headers=HDRS,
                             timeout=15)
            if r.status_code != 200:
                continue
            best = None
            for s in r.json().get("formatStreams") or []:
                if s.get("container") not in ("mp4", None):
                    continue
                label = (s.get("qualityLabel") or "0").split("p")[0]
                if int(label or 0) <= max_h and (
                        best is None or int(label) > best[0]):
                    best = (int(label), s["url"])
            if not best:
                continue
            if job:
                job["detail"] = f"invidious {base} {best[0]}p: downloading"
            if _smoke(best[1]) and _full_download(best[1], dest) \
                    and _ffprobe_ok(dest):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _cobalt(url: str, dest: Path, job=None) -> bool:
    for base in COBALT:
        try:
            r = requests.post(base, headers={**HDRS, "Accept":
                                             "application/json"},
                              json={"url": url, "videoQuality": "720",
                                    "filenameStyle": "basic"},
                              timeout=25)
            if r.status_code != 200:
                continue
            d = r.json()
            if d.get("status") not in ("tunnel", "redirect"):
                continue
            if job:
                job["detail"] = f"cobalt {base}: downloading"
            if _smoke(d["url"]) and _full_download(d["url"], dest) \
                    and _ffprobe_ok(dest):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _piped(vid: str, dest: Path, max_h: int, job=None) -> bool:
    for base in PIPED:
        try:
            r = requests.get(f"{base}/streams/{vid}", headers=HDRS, timeout=15)
            if r.status_code != 200:
                continue
            data = r.json()
            vs = [s for s in data.get("videoStreams", [])
                  if s.get("videoOnly") and "vp9" not in (s.get("codec") or "")
                  and (s.get("mimeType") or "").startswith("video/mp4")
                  and int(s.get("quality", 99999)) <= max_h]
            auds = [a for a in data.get("audioStreams", [])
                    if (a.get("mimeType") or "").startswith("audio/mp4")]
            if not vs or not auds:
                continue
            v = max(vs, key=lambda s: int(s.get("quality", 0)))
            a = max(auds, key=lambda s: s.get("bitrate", 0))
            tmp = dest.parent / ".tmp_av"
            tmp.mkdir(exist_ok=True)
            if job:
                job["detail"] = f"piped {base} {v.get('quality')}p: downloading"
            vf, af = tmp / "v.mp4", tmp / "a.m4a"
            if _smoke(v["url"]) and _full_download(v["url"], vf) \
                    and _full_download(a["url"], af) \
                    and _merge_av(vf, af, dest) and _ffprobe_ok(dest):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# ---------------------------------------------------------------- entry
def fetch_source(job: dict, url: str, max_h: int) -> None:
    vid = video_id(url)
    dest = Path(job["dir"]) / "source.mp4"
    t0 = time.time()
    ladder = (("invidious", lambda: _invidious(vid, dest, max_h, job)),
              ("cobalt", lambda: _cobalt(url, dest, job)),
              ("piped", lambda: _piped(vid, dest, max_h, job)))
    for name, fn in ladder:
        job["detail"] = f"trying {name}"
        if fn():
            size_mb = dest.stat().st_size / 1e6
            job["log_tail"] = job["log_tail"] + [
                f"OK via {name} in {time.time()-t0:.0f}s ({size_mb:.0f} MB)"]
            job["files"] = ["source.mp4"]
            job["meta"]["via"] = name
            job["meta"]["size_mb"] = round(size_mb, 1)
            return
    raise RuntimeError("all public downloader services failed")


def probe_metadata(url: str) -> dict:
    """Title/author/duration without downloading: oEmbed + Invidious/Piped."""
    vid = video_id(url)
    meta = {"ok": False, "video_id": vid, "title": "", "author": "",
            "duration_s": None}
    try:
        r = requests.get("https://www.youtube.com/oEmbed",
                         params={"url": f"https://youtu.be/{vid}",
                                 "format": "json"}, headers=HDRS, timeout=15)
        if r.status_code == 200:
            d = r.json()
            meta["title"] = d.get("title", "")
            meta["author"] = d.get("author_name", "")
            meta["ok"] = True
    except requests.RequestException:
        pass
    for base in INVIDIOUS[:4]:
        try:
            r = requests.get(f"{base}/api/v1/videos/{vid}", headers=HDRS,
                             timeout=12)
            if r.status_code == 200:
                d = r.json()
                meta["duration_s"] = d.get("lengthSeconds")
                meta["title"] = meta["title"] or d.get("title", "")
                meta["author"] = meta["author"] or d.get("author", "")
                return meta
        except requests.RequestException:
            continue
    for base in PIPED[:3]:
        try:
            r = requests.get(f"{base}/streams/{vid}", headers=HDRS, timeout=12)
            if r.status_code == 200:
                meta["duration_s"] = r.json().get("duration")
                return meta
        except requests.RequestException:
            continue
    return meta
