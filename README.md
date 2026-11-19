# clip-api

Render-hosted YouTube fetch + advanced clipping microservice (no LLM keys,
fits a 512 MB free instance). Endpoints are auth'd with `x-api-key`
(`CLIP_API_TOKEN` env); `/health` is public.

## Why it exists

YouTube bot-walls every datacenter IP (the sandbox and all Render regions),
so the service never talks to googlevideo directly. It fetches via public
deployed downloaders — Invidious (`?local=true`), Cobalt tunnel, Piped —
each candidate smoke-tested with a ranged GET before full download, then
streams the file back to the caller. Clipping re-encodes with ffmpeg.

## Endpoints

| Method & path | Body / params | Purpose |
| --- | --- | --- |
| GET  `/health` | — | liveness (public) |
| POST `/api/probe` | `{"url"}` | title/author/duration via oEmbed + Invidious/Piped |
| POST `/api/fetch` | `{"url", "max_height"?}` | job: download source, stream from `/api/jobs/<id>/file` |
| POST `/api/clip` | `{"url"\|"job_id", "segments":[{start,end,cues?}], "height"?, "face_track"?, "font_scale"?, "fades"?}` | job: vertical clips |
| GET  `/api/jobs/<id>` | — | `{status, progress, detail, log_tail}` |
| GET  `/api/jobs/<id>/files` | — | output list |
| GET  `/api/jobs/<id>/file?name=` | — | stream (Range OK) |

`cues` are `{"start","end","text"}` in source-absolute seconds; the service
offsets them into the segment and burns a styled ASS subtitle (Hinglish-safe
Latin script, DejaVu Sans bold, heavy outline, Shorts-UI-safe margins).

Advanced features:
- **Face-tracked 9:16 reframing** — OpenCV Haar sampling every 2 s,
  rolling-median smoothing, piecewise ffmpeg `crop` expression; falls back
  to center crop when no face is found.
- **Accurate segment cuts** — input seek + re-encode (libx264 veryfast
  crf 23, 2 threads), loudnorm to -16 LUFS, 0.25 s audio/video fades,
  faststart.

## Free-tier notes

- One heavy job at a time (`BoundedSemaphore(1)`); extra jobs queue.
- Jobs expire after 6 h (`JOB_TTL_S`); media lives under `/tmp/clipapi`.
- A ~60 s clip encodes in ~3–8 min at 720x1280 on a 0.1-CPU free instance.
- Clients should poll at least every 10 min to keep the instance awake.
- Memory budget: opencv sampling and ffmpeg encoding run sequentially,
  never concurrently.

## Deploy

1. Push this repo to GitHub (private).
2. Render -> New Web Service -> pick the repo (Docker runtime) — or apply
   `render.yaml` (Blueprint). Set env `CLIP_API_TOKEN` to a long random
   string.
3. Health check path is `/health`; the free instance cold-starts in ~50 s.

Source layout: `app/main.py` (FastAPI, jobs), `app/fetcher.py` (ladder),
`app/faces.py` (tracking), `app/captions.py` (ASS), `app/clipping.py`
(ffmpeg).
