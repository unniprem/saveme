# syntax=docker/dockerfile:1

# Deno runs yt-dlp's YouTube JS challenges (via the pinned yt-dlp-ejs package).
# Pinned by the multi-arch index digest, which is what gets pulled; change both lines together.
# New digest: docker buildx imagetools inspect denoland/deno:bin-<version>  (top "Digest:")
ARG DENO_VERSION=2.9.8
ARG DENO_DIGEST=sha256:b507583d3c68ba69f3ed2ce5969ae5b8b70f43421c781279c7aea06435263543

# FFmpeg source release, built below. Change both lines together. New release: download
# https://ffmpeg.org/releases/ffmpeg-<version>.tar.xz and its .asc, check the signature
# (key FCF9 86EA 15E6 E293 A564 4F10 B432 2F04 D676 58D8), then: sha256sum ffmpeg-<version>.tar.xz
ARG FFMPEG_VERSION=9.0.2
ARG FFMPEG_SHA256=8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e

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
FROM denoland/deno:bin-${DENO_VERSION}@${DENO_DIGEST} AS deno

# --- ffmpeg: a minimal build with only what the app uses. Debian's ffmpeg package added
# ~410 MB of libraries (LLVM, Mesa, X11, video encoders) that the app never touches.
# What the app needs from ffmpeg:
#   - stream copy only for video: merging, remuxing to MP4, yt-dlp's fixups. That needs
#     demuxers, muxers, parsers and bitstream filters, and no video encoders.
#   - one video decoder, vp9, used only while probing. The MP4 muxer needs the VP9 pixel
#     format for its vpcC box, and only the decoder can derive it. Without it, ffmpeg
#     exits 0 but writes an MP4 that won't open.
#   - for MP3: common web audio decoders, plus libmp3lame.
#   - local files only: no network protocols, and no HLS/DASH/concat demuxers that open
#     other files. This also shrinks the attack surface for hostile media.
# After changing these flags, run scripts/docker-ffmpeg-check.sh.
# libav* is linked statically into ffmpeg/ffprobe; libmp3lame stays a Debian shared library.
FROM debian:trixie-slim AS ffmpeg
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils gcc libc6-dev make nasm pkg-config \
        libmp3lame-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
ARG FFMPEG_VERSION
ARG FFMPEG_SHA256
RUN curl -fsSL --retry 5 --retry-all-errors -o ffmpeg.tar.xz \
        "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
    && echo "${FFMPEG_SHA256}  ffmpeg.tar.xz" | sha256sum -c - \
    && tar -xJf ffmpeg.tar.xz --strip-components=1 \
    && rm ffmpeg.tar.xz
RUN ./configure \
        --prefix=/opt/ffmpeg \
        --disable-everything \
        --disable-autodetect \
        --disable-network \
        --disable-debug \
        --disable-doc \
        --disable-ffplay \
        --disable-avdevice \
        --disable-swscale \
        --enable-pthreads \
        --enable-zlib \
        --enable-libmp3lame \
        --enable-protocol=file,pipe \
        --enable-demuxer=mov,matroska,mpegts,flv,avi,ogg,mp3,aac,flac,wav \
        --enable-muxer=mp4,ipod,mov,matroska,webm,mp3 \
        --enable-parsers \
        --enable-bsfs \
        --enable-decoder=vp9,aac,aac_latm,mp3,mp3float,mp2,mp2float,opus,vorbis,flac,alac,ac3,eac3,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s32le,pcm_f32le,pcm_u8,pcm_alaw,pcm_mulaw \
        --enable-encoder=libmp3lame \
        --enable-filter=aresample,aformat,anull,null,format \
    && make -j"$(nproc)" \
    && make install

# --- runtime
FROM python:3.12-slim-trixie
# libmp3lame0: the MP3 encoder library the ffmpeg build links against.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmp3lame0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe /usr/local/bin/
# Fail the build if a shared library is missing or the binary isn't FFMPEG_VERSION.
ARG FFMPEG_VERSION
RUN ffmpeg -hide_banner -version | grep -q "^ffmpeg version ${FFMPEG_VERSION} " \
    && ffprobe -hide_banner -version >/dev/null
COPY --from=deno /deno /usr/local/bin/deno
# The digest overrides the tag: fail the build if the binary isn't DENO_VERSION.
ARG DENO_VERSION
RUN deno --version | grep -q "^deno ${DENO_VERSION} "

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
# No PYTHONPATH needed: `python -m app` runs from WORKDIR /app, and the runner sets
# PYTHONPATH for the workers it starts (`python -P -m app.worker`).
ENV PYTHONUNBUFFERED=1
# Deno must not phone home to check for a newer version.
ENV DENO_NO_UPDATE_CHECK=1
EXPOSE 8000
# GET /health skips the ALLOWED_HOSTS check; the empty ProxyHandler ignores *_proxy vars.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8000/health', timeout=4)"]
CMD ["python", "-m", "app"]
