#!/usr/bin/env bash
# Build the page CSS with the pinned Tailwind v4 standalone CLI.
#
#   scripts/build-css.sh [output]      (default: static/dist/styles.css)
#
# The CLI for this OS/arch (linux|macos x x64|arm64) is downloaded once into .cache/ and
# checked against the release's sha256sums.txt, whose own SHA-256 is pinned below. To bump
# Tailwind, change TAILWIND_VERSION and set TAILWIND_SUMS_SHA256 to the output of
#   curl -fsSL https://github.com/tailwindlabs/tailwindcss/releases/download/<version>/sha256sums.txt | shasum -a 256
# Needs only bash, curl and sha256sum (Linux) or shasum (macOS).
set -euo pipefail

TAILWIND_VERSION="v4.3.5"
TAILWIND_SUMS_SHA256="60afad4b087abdde5b493a2a30fabee333661a0c63f0e05693763e915e8b4a8c"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/static/dist/styles.css}"
CACHE="$ROOT/.cache"
BASE_URL="https://github.com/tailwindlabs/tailwindcss/releases/download/$TAILWIND_VERSION"

die() { echo "build-css: $*" >&2; exit 1; }

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  *) die "unsupported OS: $(uname -s)" ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=x64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) die "unsupported architecture: $(uname -m)" ;;
esac
asset="tailwindcss-$os-$arch"

if [ "$os" = macos ]; then
  hasher=(shasum -a 256)
elif command -v sha256sum >/dev/null 2>&1; then
  hasher=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
  hasher=(shasum -a 256)
else
  die "sha256sum or shasum is required"
fi
command -v curl >/dev/null 2>&1 || die "curl is required"

sha256() {
  local out
  out="$("${hasher[@]}" "$1")"
  printf '%s' "${out%% *}"
}

fetch() {
  curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$2" "$1"
}

mkdir -p "$CACHE"
sums="$CACHE/tailwindcss-$TAILWIND_VERSION-sha256sums.txt"
bin="$CACHE/$asset-$TAILWIND_VERSION"

if [ ! -f "$sums" ] || [ "$(sha256 "$sums")" != "$TAILWIND_SUMS_SHA256" ]; then
  fetch "$BASE_URL/sha256sums.txt" "$sums.tmp"
  if [ "$(sha256 "$sums.tmp")" != "$TAILWIND_SUMS_SHA256" ]; then
    rm -f "$sums.tmp"
    die "sha256sums.txt for $TAILWIND_VERSION does not match the pinned TAILWIND_SUMS_SHA256"
  fi
  mv "$sums.tmp" "$sums"
fi

expected=""
while read -r hash name; do
  if [ "$name" = "./$asset" ] || [ "$name" = "$asset" ]; then expected="$hash"; fi
done <"$sums"
[ -n "$expected" ] || die "no checksum for $asset in sha256sums.txt"

if [ ! -x "$bin" ] || [ "$(sha256 "$bin")" != "$expected" ]; then
  echo "build-css: downloading $asset $TAILWIND_VERSION" >&2
  fetch "$BASE_URL/$asset" "$bin.tmp"
  actual="$(sha256 "$bin.tmp")"
  if [ "$actual" != "$expected" ]; then
    rm -f "$bin.tmp"
    die "checksum mismatch for $asset: expected $expected, got $actual"
  fi
  chmod 755 "$bin.tmp"
  mv "$bin.tmp" "$bin"
fi

mkdir -p "$(dirname "$OUT")"
"$bin" --input "$ROOT/static/src/input.css" --output "$OUT" --minify
echo "build-css: wrote $OUT" >&2
