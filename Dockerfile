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
# ~410 MB of libraries (LLVM, Mesa, X11, every codec) that the app never touches.
# What the app needs from ffmpeg:
#   - stream copy for downloads and Clip Studio cuts: merging, remuxing to MP4, yt-dlp's
#     fixups. That needs demuxers, muxers, parsers and bitstream filters.
#   - for MP3: common web audio decoders, plus libmp3lame.
#   - for AI Highlights transcription: the same audio decoders, resampled (aresample) to
#     16 kHz mono 16-bit PCM (pcm_s16le encoder) in a WAV file (wav muxer).
#   - for Auto Shorts rendering (docs/AUTO_SHORTS_DESIGN.md §3):
#       decoders h264, hevc, vp8, vp9, AV1 (libdav1d) and png (the logo);
#       encoders libx264 (GPL: this makes the binary GPL-2.0+; SaveMe only runs it, so the app
#       stays MIT; publishing the image means offering this source), aac, mjpeg (preview stills)
#       and rawvideo (frames for face tracking); muxers image2 (one still) and rawvideo;
#       the png_pipe demuxer (one image, no file patterns); swscale; libass (burned-in
#       captions, shaped with HarfBuzz) and the filters the render graphs use. transpose,
#       hflip and vflip let FFmpeg apply a phone video's rotation.
#   - local files only: no network protocols, and no HLS/DASH/concat demuxers that open
#     other files. This also shrinks the attack surface for hostile media.
# After changing these flags, run scripts/docker-ffmpeg-check.sh.
# libav* is linked statically into ffmpeg/ffprobe; libmp3lame, libx264, libass (with freetype,
# fribidi, harfbuzz, fontconfig) and libdav1d stay Debian shared libraries.
FROM debian:trixie-slim AS ffmpeg
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils gcc libc6-dev make nasm pkg-config \
        libmp3lame-dev zlib1g-dev libx264-dev libass-dev libdav1d-dev \
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
        --enable-gpl \
        --enable-pthreads \
        --enable-zlib \
        --enable-libmp3lame \
        --enable-libx264 \
        --enable-libass \
        --enable-libdav1d \
        --enable-protocol=file,pipe \
        --enable-demuxer=mov,matroska,mpegts,flv,avi,ogg,mp3,aac,flac,wav,ffmetadata,image_png_pipe \
        --enable-muxer=mp4,ipod,mov,matroska,webm,mp3,wav,image2,rawvideo \
        --enable-parsers \
        --enable-bsfs \
        --enable-decoder=h264,hevc,vp8,vp9,libdav1d,png,aac,aac_latm,mp3,mp3float,mp2,mp2float,opus,vorbis,flac,alac,ac3,eac3,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s32le,pcm_f32le,pcm_u8,pcm_alaw,pcm_mulaw \
        --enable-encoder=libmp3lame,pcm_s16le,libx264,aac,mjpeg,rawvideo \
        --enable-filter=aresample,aformat,anull,null,format,crop,scale,pad,setsar,fps,split,overlay,vstack,boxblur,eq,colorchannelmixer,ass,trim,atrim,setpts,asetpts,concat,anullsrc,apad,transpose,hflip,vflip \
    && make -j"$(nproc)" \
    && make install

# --- runtime
FROM python:3.12-slim-trixie
# The shared libraries the ffmpeg build links against: libmp3lame0 (MP3), libx264-164 (H.264),
# libass9 (captions; it brings freetype, fribidi, harfbuzz and fontconfig) and libdav1d7 (AV1).
# Caption fonts, all free licences, none committed to this repository: Roboto (Apache-2.0),
# DejaVu (Bitstream Vera licence) and Manjari for Malayalam (SIL OFL 1.1).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmp3lame0 libx264-164 libass9 libdav1d7 \
        fontconfig fonts-roboto-unhinted fonts-dejavu-core fonts-smc-manjari \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f
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

# AI Highlights: faster-whisper models, fetched at build time into a root-owned Hugging Face
# cache the app can read but not change. At runtime the transcriber can't open any network
# connection, so a model must be here: rebuild with --build-arg WHISPER_MODELS="base small"
# to add one (WHISPER_MODEL picks which is used).
ARG WHISPER_MODELS=base
ENV HF_HOME=/opt/huggingface HF_HUB_DISABLE_TELEMETRY=1
COPY app/transcript.py app/whisper_models.py app/errors.py app/__init__.py /tmp/models/app/
RUN cd /tmp/models && python -m app.whisper_models ${WHISPER_MODELS} \
    && rm -rf /tmp/models "${HF_HOME}/xet" \
    && chmod -R a+rX "${HF_HOME}"
# Nothing reaches Hugging Face at runtime.
ENV HF_HUB_OFFLINE=1

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
# `python -m app` alone listens on loopback; the container needs every interface so the
# compose port mapping (127.0.0.1 on the host) can reach it.
CMD ["python", "-m", "app", "--host", "0.0.0.0"]
