# clip-api

Media clipping API hosted on Render — wraps
[auto-editor](https://github.com/WyattBlue/auto-editor) (Public Domain,
5.9k★) and ffmpeg. Deployed as the agency's off-box clipping service:
the OOM-limited sandbox uploads media, Render does the heavy
ffmpeg/auto-editor work, and streams the result back.

## Endpoints

All `/clip/*` routes require header `x-relay-token: <CLIP_API_TOKEN>`.
One job at a time (busy → `429`); uploads capped at `MAX_UPLOAD_MB` (400).

| Route | Method | Body | Returns |
| --- | --- | --- | --- |
| `/render-healthz` | GET | — | `ok` (no auth) |
| `/clip/status` | GET | — | ffmpeg/auto-editor versions, busy flag |
| `/clip/probe` | POST | multipart `file` | ffprobe JSON |
| `/clip/extract` | POST | `file`, `start_s`, `end_s`, `mode=encode\|copy`, `crf`, `height` | exact cut mp4 |
| `/clip/tighten` | POST | `file`, `edit=audio\|motion`, `margin`, `when_silent=cut\|nil` | auto-editor tightened mp4 (+`X-Input/Output-Duration` headers) |
| `/clip/verticalize` | POST | `file`, `srt` (optional), `badge`, `style`, `target_h` | 1080×1920 vertical mp4: blurred bg + contained fg + burned captions |

## Example

```bash
curl -H "x-relay-token: $TOKEN" \
     -F file=@clip.mp4 -F start_s=12.5 -F end_s=71.0 -F mode=encode \
     -o out.mp4 https://<host>/clip/extract
```

## Deploy (Render)

Docker service, free plan (512 MB — the one-job lock is the OOM guard),
health check path `/render-healthz`, env `CLIP_API_TOKEN` (required;
every other env has a default).

## Credits

- auto-editor — github.com/WyattBlue/auto-editor (Public Domain)
- ffmpeg, FastAPI
