FROM python:3.12-slim

# ffmpeg + fonts (libass subtitle burning needs fontconfig fonts)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg fonts-liberation fontconfig curl \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

# auto-editor (Public Domain) — pinned prebuilt binary from GitHub releases
ARG AE_VERSION=29.3.1
RUN curl -fsSL -o /usr/local/bin/auto-editor \
      "https://github.com/WyattBlue/auto-editor/releases/download/${AE_VERSION}/auto-editor-linux-x86_64" \
    && chmod +x /usr/local/bin/auto-editor \
    && auto-editor --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV MAX_UPLOAD_MB=400 PROCESS_TIMEOUT_S=1800
EXPOSE 10000
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --timeout-keep-alive 75
