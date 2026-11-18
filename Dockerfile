FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp needs a JS runtime for YouTube's player challenge.
RUN curl -fsSL https://deno.land/install.sh | sh
ENV DENO_DIR=/root/.deno
ENV PATH="/root/.deno/bin:${PATH}"

RUN pip install --no-cache-dir "yt-dlp[default]" fastapi "uvicorn[standard]"

COPY app.py /app/app.py
WORKDIR /app
ENV TEMP_DIR=/tmp

CMD ["sh", "-c", "python -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
