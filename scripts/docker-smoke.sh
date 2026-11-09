#!/usr/bin/env bash
# Docker smoke test: build and start the compose service, wait until it is healthy, then
# check the API basics and the image hardening.
#
#   scripts/docker-smoke.sh
#
# The container is left running (for a manual or browser walkthrough); stop it with
# `docker compose down`. Needs docker (with compose v2) and curl on the host.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

SERVICE=saveme
BASE=http://127.0.0.1:8000
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

echo "==> GET /health"
health="$(curl -sS --noproxy '*' --max-time 10 "$BASE/health")" || fail "GET /health: request failed"
[ "$health" = '{"status":"ok"}' ] || fail "GET /health returned: $health"

echo "==> inspect 'not a url' -> 400"
inspect '{"url":"not a url"}'
[ "$CODE" = 400 ] || fail "expected HTTP 400, got $CODE: $BODY"

echo "==> inspect http://169.254.169.254/ -> 403"
inspect '{"url":"http://169.254.169.254/"}'
[ "$CODE" = 403 ] || fail "expected HTTP 403, got $CODE: $BODY"

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
