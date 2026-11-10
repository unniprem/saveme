#!/usr/bin/env bash
# Checks that the image's minimal ffmpeg build covers what the app and yt-dlp ask of it.
# It runs inside a copy of the image, with the app's ffmpeg/ffprobe wrappers pointed at
# that build:
#   1. a codec/container matrix: the commands the app and yt-dlp run (remux, merge,
#      HLS/M4A/timestamp fixups, MP3 conversion) on formats the test fixtures lack
#      (VP9, AV1, HEVC, Opus, Vorbis, FLAC, AC-3, FLV, AVI). Each output must open and
#      hold the expected streams; a missing component can exit 0 and still write a
#      broken file.
#   2. the offline integration tests (real worker: direct, HLS merge, MP3, probing).
#
#   scripts/docker-ffmpeg-check.sh [image]     (default: saveme:local, as built by compose)
#
# Run it after changing the ffmpeg configure flags in the Dockerfile. The test copy adds
# Debian's full ffmpeg only to make the inputs and probe the results. The image's own
# build moves to /opt/minimal, where the wrappers find it through SAVEME_FFMPEG_DIR.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

IMAGE="${1:-saveme:local}"
TEST_IMAGE=saveme-ffmpeg-check

docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || { echo "FFMPEG CHECK FAIL: image $IMAGE not found; build it first" >&2; exit 1; }

echo "==> building $TEST_IMAGE from $IMAGE"
docker build -q -t "$TEST_IMAGE" --build-arg "BASE=$IMAGE" -f - . >/dev/null <<'EOF'
ARG BASE
FROM ${BASE}
USER root
RUN mkdir /opt/minimal \
    && mv /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /opt/minimal/ \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
USER app
ENV SAVEME_FFMPEG_DIR=/opt/minimal SAVEME_TEST_FFMPEG_DIR=/opt/minimal PYTHONDONTWRITEBYTECODE=1
EOF

docker run --rm -i \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/pyproject.toml:/app/pyproject.toml:ro" \
  "$TEST_IMAGE" sh -s <<'EOF' || { echo "FFMPEG CHECK FAIL" >&2; exit 1; }
set -eu
# The wrappers must resolve to the minimal build, not the Debian ffmpeg on PATH.
app/bin/ffmpeg -hide_banner -version | grep -q -- "--disable-everything"
app/bin/ffprobe -hide_banner -version | grep -q -- "--disable-everything"

echo "==> codec/container matrix (minimal ffmpeg through the app wrapper)"
cd "$(mktemp -d)"
gen() { ffmpeg -hide_banner -loglevel error -nostdin -y "$@" </dev/null; }
V="-f lavfi -i testsrc=size=320x240:rate=25:d=2"
A="-f lavfi -i sine=d=2"
VA="-map 0:v -map 1:a -pix_fmt yuv420p"
gen $V $A $VA -c:v libvpx-vp9 -b:v 200k -c:a libopus vp9.webm
gen $V -pix_fmt yuv420p -c:v libvpx-vp9 -b:v 200k vonly.webm
gen $A -c:a libopus aonly.webm
gen $V $A $VA -c:v libsvtav1 -preset 12 -c:a libopus av1.webm
gen $V $A $VA -c:v libx265 -x265-params log-level=error -c:a aac -f mpegts hevc.ts
gen $V $A $VA -c:v libx264 -c:a aac -f mpegts h264.ts
# Keyframes every 5 frames: the timestamp fixup's `-ss` (stream copy) starts video at one.
gen $V $A $VA -c:v libx264 -g 5 -c:a aac s.mp4
gen $A -c:a aac s.m4a
gen $A -c:a libvorbis s.ogg
gen $A -c:a flac s.flac
gen $V $A $VA -c:v libx264 -c:a ac3 ac3.mkv
gen $V $A $VA -c:v libx264 -c:a aac s.flv
gen $V $A $VA -c:v mpeg4 -c:a libmp3lame s.avi
printf '1\n00:00:00,000 --> 00:00:01,000\nhi\n' > s.srt
gen $V $A -i s.srt -map 0:v -map 1:a -map 2 -pix_fmt yuv420p -c:v libx264 -c:a aac -c:s mov_text subs.mp4

failed=0
# check NAME OUTPUT "WANTED CODECS" ARGS...: run the wrapped ffmpeg with ARGS then OUTPUT,
# and require the full ffprobe to read OUTPUT back with exactly the wanted codecs.
check() {
  name=$1 out=$2 want=$3
  shift 3
  if ! /app/app/bin/ffmpeg -hide_banner -loglevel error -nostdin -y "$@" "$out" 2>err.txt </dev/null; then
    echo "FAIL  $name: ffmpeg failed: $(tail -n 2 err.txt | tr '\n' ' ')"; failed=1; return 0
  fi
  got=$(ffprobe -v error -show_entries stream=codec_name -of csv=p=0 "$out" 2>&1 | sort | xargs)
  if [ "$got" = "$want" ]; then echo "PASS  $name"; else echo "FAIL  $name: got '$got', want '$want'"; failed=1; fi
}
COPY_ALL="-map 0 -dn -ignore_unknown -c copy"
MP3="-vn -acodec libmp3lame -q:a 2"
check "remux VP9+Opus WebM -> MP4"     r_vp9.mp4  "opus vp9"   -i vp9.webm -c copy -sn -dn
check "remux AV1+Opus WebM -> MP4"     r_av1.mp4  "av1 opus"   -i av1.webm -c copy -sn -dn
check "remux FLV -> MP4"               r_flv.mp4  "aac h264"   -i s.flv -c copy -sn -dn
check "remux AVI -> MP4"               r_avi.mp4  "mp3 mpeg4"  -i s.avi -c copy -sn -dn
check "merge VP9 + Opus -> MP4"        m.mp4      "opus vp9"   -i vonly.webm -i aonly.webm -c copy -map 1:a:0 -map 0:v:0 -movflags +faststart
check "merge VP9 + Opus -> MKV"        m.mkv      "opus vp9"   -i vonly.webm -i aonly.webm -c copy -map 1:a:0 -map 0:v:0
check "HLS fixup: HEVC+AAC TS -> MP4"  f_hevc.mp4 "aac hevc"   -i hevc.ts $COPY_ALL -f mp4 -bsf:a aac_adtstoasc
check "HLS fixup: H.264+AAC TS -> MP4" f_h264.mp4 "aac h264"   -i h264.ts $COPY_ALL -f mp4 -bsf:a aac_adtstoasc
check "M4A fixup"                      f.m4a      "aac"        -i s.m4a $COPY_ALL -f mp4
check "timestamp fixup (setts)"        t.mp4      "aac h264"   -i s.mp4 -c copy -bsf setts=ts=TS-STARTPTS $COPY_ALL -ss 0.001
check "fixup keeps MP4 subtitles"      sub.mp4    "aac h264 mov_text" -i subs.mp4 $COPY_ALL -f mp4
check "MP3 from Opus"                  o1.mp3     "mp3"        -i aonly.webm $MP3
check "MP3 from AAC"                   o2.mp3     "mp3"        -i s.m4a $MP3
check "MP3 from Vorbis"                o3.mp3     "mp3"        -i s.ogg $MP3
check "MP3 from FLAC"                  o4.mp3     "mp3"        -i s.flac $MP3
check "MP3 from AC-3 in MKV"           o5.mp3     "mp3"        -i ac3.mkv $MP3
got=$(/app/app/bin/ffprobe -v error -show_entries stream=codec_name -of csv=p=0 vp9.webm | sort | xargs)
if [ "$got" = "opus vp9" ]; then echo "PASS  ffprobe reads WebM"; else echo "FAIL  ffprobe reads WebM: got '$got'"; failed=1; fi
[ "$failed" = 0 ] || exit 1

echo "==> integration tests against the minimal ffmpeg"
cd /app
python -m pytest -q -p no:cacheprovider tests/integration
EOF

echo "FFMPEG CHECK OK"
