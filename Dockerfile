# syntax=docker/dockerfile:1

# Deno runs yt-dlp's YouTube JS challenges (via the pinned yt-dlp-ejs package).
ARG DENO_VERSION=2.9.8

# --- css: compile static/dist/styles.css with the pinned, checksum-verified Tailwind CLI.
# build-css.sh picks the CLI from `uname -m`, so this follows TARGETARCH.
FROM debian:trixie-slim AS css
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY scripts/build-css.sh scripts/build-css.sh
COPY static/ static/
RUN scripts/build-css.sh /out/styles.css

# --- deno: the official single-binary image, multi-arch (amd64, arm64).
FROM denoland/deno:bin-${DENO_VERSION} AS deno

# --- runtime
FROM python:3.12-slim-trixie
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# No-op unless overridden: docker compose build --build-arg YTDLP_VERSION=<new>
ARG YTDLP_VERSION=2026.10.26
RUN pip install --no-cache-dir "yt-dlp[default]==${YTDLP_VERSION}"

# App code stays root-owned, so the app user can read but not modify it.
COPY app/ app/
COPY static/ static/
COPY --from=css /out/styles.css static/dist/styles.css

RUN useradd --uid 10001 --create-home app \
    && mkdir /tmp/media-jobs \
    && chown app /tmp/media-jobs

USER app
# PYTHONPATH: workers run `python -m app.worker` with their job directory as cwd.
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
EXPOSE 8000
# The default ALLOWED_HOSTS accepts Host 127.0.0.1; the empty ProxyHandler ignores *_proxy vars.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8000/health', timeout=4)"]
CMD ["python", "-m", "app"]
