# TWITCH-Generator: VOD -> Whisper -> LLM -> clips -> upload
# CPU image. For GPU Whisper swap base to nvidia/cuda + ctranslate2-cuda and run with --gpus all.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg = clip cutting, downloads. git for some yt-dlp extractors.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config.yaml ./config.yaml

# Pipeline artifacts (VODs, clips, transcripts) live here; mount a volume.
RUN mkdir -p /data
VOLUME ["/data"]
ENV TG_DATA_DIR=/data \
    WEB_PORT=9443

EXPOSE 9443

ENTRYPOINT ["python", "-m", "src.main"]
# Default: launch the web interface. Override with: ... run --vod <url>
CMD ["web"]
