FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno from GitHub releases (the deno.land install script can silently
# no-op inside some builders, leaving the relay without a JS runtime).
RUN curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip" \
        -o /tmp/deno.zip \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version

# yt-dlp + FastAPI + bgutil POT provider plugin (script mode, deno).
RUN pip install --no-cache-dir "yt-dlp[default]" fastapi "uvicorn[standard]" \
        bgutil-ytdlp-pot-provider==2.0.2 \
    && git clone --depth 1 --branch 2.0.2 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && deno install --allow-scripts=npm:canvas --frozen

COPY app.py /app/app.py
WORKDIR /app
ENV TEMP_DIR=/tmp
ENV BGUTIL_SERVER_HOME=/opt/bgutil/server

CMD ["sh", "-c", "python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
