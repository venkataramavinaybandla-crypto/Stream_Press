# STREAM PRESS — production image (Render, Fly.io, any Docker host)
#
# The three things a serverless platform cannot give us and this image must:
#   1. a real ffmpeg binary  → muxing separate video+audio (no 360p quality cap)
#   2. a long-running process → downloads survive more than a few seconds
#   3. a fresh yt-dlp         → extractor patches for YouTube's shifting ciphers

FROM python:3.11-slim

# ffmpeg is REQUIRED. Without it yt-dlp silently falls back to whatever single
# pre-combined stream exists (usually 360p/720p) instead of muxing bestvideo+bestaudio.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so Docker layer caching survives source-only changes.
COPY server/requirements.txt ./server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt \
    && pip install --no-cache-dir --upgrade yt-dlp

COPY . .

# YTMAX_AUTOUPDATE_YTDLP makes the app run `pip install -U yt-dlp` at boot, so
# extractor fixes land on every deploy without a rebuild. Disable to freeze.
# YTMAX_DOWNLOAD_DIR must point at a persistent mount in production.
ENV YTMAX_AUTOUPDATE_YTDLP=1 \
    YTMAX_DOWNLOAD_DIR=/data/downloads \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data/downloads

EXPOSE 8000

# Render injects $PORT; bind 0.0.0.0 so the proxy can reach us.
CMD ["sh", "-c", "uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
