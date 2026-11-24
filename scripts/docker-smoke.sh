#!/usr/bin/env bash
# Docker smoke test: build and start the compose service, wait until it is healthy, then
# check the API basics (AI Highlights included) and the image hardening.
#
#   scripts/docker-smoke.sh
#
# The container is left running (for a manual or browser walkthrough); stop it with
# `docker compose down`. Needs docker (with compose v2) and curl on the host.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

SERVICE=saveme
NL=$'\n'

fail() {
  echo "SMOKE FAIL: $*" >&2
  echo "--- last container log lines:" >&2
  docker compose logs --tail 40 "$SERVICE" >&2 || true
  exit 1
}

# Runs a command in the container; prints its output.
in_container() {
  docker compose exec -T "$SERVICE" "$@"
}

# POST /api/inspect with the given JSON body; sets CODE and BODY.
inspect() {
  local resp
  resp="$(curl -sS --noproxy '*' --max-time 30 -w "${NL}%{http_code}" \
    -H 'Content-Type: application/json' -d "$1" "$BASE/api/inspect")" \
    || fail "POST /api/inspect $1: request failed"
  CODE="${resp##*"$NL"}"
  BODY="${resp%"$NL"*}"
}

echo "==> docker compose up -d --build"
docker compose up -d --build

cid="$(docker compose ps -q "$SERVICE")" || fail "docker compose ps failed"
[ -n "$cid" ] || fail "no running container for service $SERVICE"

echo "==> waiting up to 90 s for the container to be healthy"
deadline=$((SECONDS + 90))
first_restarts=""
while :; do
  # A crash shows as status exited/restarting, or as a higher restart count once
  # `restart: unless-stopped` has brought it back up; either way, stop waiting.
  state="$(docker inspect -f '{{.State.Status}} {{.RestartCount}} {{.State.ExitCode}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid")" \
    || fail "docker inspect $cid failed"
  read -r run_status restarts exit_code health <<<"$state"
  [ -n "$first_restarts" ] || first_restarts="$restarts"
  if [ "$run_status" != running ] || [ "$restarts" != "$first_restarts" ]; then
    fail "container stopped or restarted (status: $run_status, exit code: $exit_code, restarts: $restarts)"
  fi
  [ "$health" = healthy ] && break
  [ "$health" = unhealthy ] && fail "container reported unhealthy"
  [ "$SECONDS" -lt "$deadline" ] || fail "not healthy after 90 s (status: ${health:-unknown})"
  sleep 2
done
echo "    healthy"

# The host port comes from SAVEME_PORT in .env, so ask compose where it published the app.
published="$(docker compose port "$SERVICE" 8000)" || fail "docker compose port failed"
case "$published" in
  127.0.0.1:[0-9]*) ;;
  *) fail "expected the app published on 127.0.0.1, got: ${published:-nothing}" ;;
esac
BASE="http://$published"
echo "    published on $BASE"

echo "==> GET /health"
health="$(curl -sS --noproxy '*' --max-time 10 "$BASE/health")" || fail "GET /health: request failed"
[ "$health" = '{"status":"ok"}' ] || fail "GET /health returned: $health"

echo "==> inspect 'not a url' -> 400"
inspect '{"url":"not a url"}'
[ "$CODE" = 400 ] || fail "expected HTTP 400, got $CODE: $BODY"

echo "==> inspect http://169.254.169.254/ -> 403"
inspect '{"url":"http://169.254.169.254/"}'
[ "$CODE" = 403 ] || fail "expected HTTP 403, got $CODE: $BODY"

echo "==> Clip Studio: limits, and an upload that isn't a video -> 422 after the probe"
studio="$(curl -sS --noproxy '*' --max-time 10 "$BASE/api/studio")" || fail "GET /api/studio: request failed"
case "$studio" in
  *'"max_upload_bytes":'*'"min_clip_seconds":0.1'*) ;;
  *) fail "GET /api/studio returned: $studio" ;;
esac
resp="$(head -c 4096 /dev/urandom | curl -sS --noproxy '*' --max-time 30 -w "${NL}%{http_code}" \
  -H 'Content-Type: application/octet-stream' --data-binary @- "$BASE/api/sources/upload")" \
  || fail "POST /api/sources/upload: request failed"
[ "${resp##*"$NL"}" = 202 ] || fail "expected HTTP 202 for the upload, got: $resp"
source_id="$(printf '%s' "${resp%"$NL"*}" | sed -n 's/.*"source_id":"\([A-Za-z0-9_-]*\)".*/\1/p')"
[ -n "$source_id" ] || fail "no source_id in: $resp"
for _ in $(seq 1 30); do
  view="$(curl -sS --noproxy '*' --max-time 10 "$BASE/api/sources/$source_id")" || fail "GET source failed"
  case "$view" in *'"status":"failed"'*) break ;; esac
  sleep 1
done
case "$view" in
  *'"code":"unreadable_media"'*) ;;
  *) fail "expected the probe to refuse random bytes as unreadable_media, got: $view" ;;
esac

echo "==> AI Highlights: status answers, and the configured Whisper model is in the image, offline"
ai="$(curl -sS --noproxy '*' --max-time 10 "$BASE/api/ai/status")" || fail "GET /api/ai/status: request failed"
case "$ai" in
  *'"available":true'*) ;;
  *) fail "GET /api/ai/status returned: ${ai:0:300}" ;;
esac
case "$ai" in *sk-or-*) fail "GET /api/ai/status shows an API key" ;; esac
in_container sh -c 'python -m app.whisper_models --list | grep -x "${WHISPER_MODEL:-base}  installed" >/dev/null' \
  || fail "the Whisper model ${WHISPER_MODEL:-base} isn't installed in the image"
in_container sh -c 'test "$HF_HUB_OFFLINE" = 1' || fail "HF_HUB_OFFLINE isn't 1 in the container"

echo "==> Auto Shorts: options, then a captioned 9:16 short rendered in the container"
shorts="$(curl -sS --noproxy '*' --max-time 10 "$BASE/api/shorts/options")" || fail "GET /api/shorts/options failed"
case "$shorts" in
  *'"available":true'*'"captions_available":true'*) ;;
  *) fail "GET /api/shorts/options returned: ${shorts:0:300}" ;;
esac
in_container python - < scripts/smoke_shorts.py || fail "the Auto Shorts render in the container failed"
peak="$(in_container cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo unknown)"
echo "    container memory peak so far: $peak bytes"

echo "==> runs as uid 10001"
uid="$(in_container id -u)" || fail "id -u failed in the container"
[ "$uid" = 10001 ] || fail "container runs as uid $uid, expected 10001"

echo "==> curl_cffi is not importable"
in_container python -c "import importlib.util,sys; sys.exit(importlib.util.find_spec('curl_cffi') is not None)" \
  || fail "curl_cffi is importable in the image"

echo "==> deno and ffmpeg run"
deno_out="$(in_container deno --version)" || fail "deno --version failed in the container"
ffmpeg_out="$(in_container ffmpeg -version)" || fail "ffmpeg -version failed in the container"
echo "    ${deno_out%%"$NL"*}"
echo "    ${ffmpeg_out%%"$NL"*}"

echo "SMOKE OK (container left running; stop it with: docker compose down)"
