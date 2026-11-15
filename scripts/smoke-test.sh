#!/usr/bin/env bash
# End-to-end smoke test: runs the image with ffmpeg's lavfi test source and
# asserts auth, streaming, snapshot, and health behavior.
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/dzharikhin/private-video-streaming:local}"
NAME="${NAME:-pvs-smoke}"
PORT="${PORT:-18080}"
AUTH_USER="${AUTH_USER:-me}"
AUTH_PASS="${AUTH_PASS:-secret}"
URL="http://localhost:${PORT}"
TMP="$(mktemp -d)"
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

command -v curl >/dev/null || fail "curl is required"
command -v docker >/dev/null || fail "docker is required"

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p "${PORT}:8080" \
  -e AUTH_USER="$AUTH_USER" -e AUTH_PASS="$AUTH_PASS" \
  -e INPUT="testsrc=size=640x480:rate=5" -e INPUT_FORMAT=lavfi \
  "$IMAGE" >/dev/null

# wait for the server to come up
code=""
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$URL/healthz" || true)
  [ "$code" = "200" ] && break
  sleep 1
done
[ "$code" = "200" ] || { docker logs "$NAME"; fail "server did not become healthy"; }

curl -sf "$URL/healthz" | grep -q '"status"' || fail "healthz is not JSON with status"

code=$(curl -s -o /dev/null -w '%{http_code}' "$URL/")
[ "$code" = "401" ] || fail "expected 401 without credentials, got $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -u "$AUTH_USER:wrong" "$URL/")
[ "$code" = "401" ] || fail "expected 401 with wrong credentials, got $code"

code=$(curl -s -o "$TMP/index.html" -w '%{http_code}' -u "$AUTH_USER:$AUTH_PASS" "$URL/")
[ "$code" = "200" ] || fail "expected 200 for index page, got $code"
grep -q 'src="/stream"' "$TMP/index.html" || fail "index page has no stream img"

stream_code=$(curl -s -o "$TMP/stream.bin" -w '%{http_code}' --max-time 3 \
  -u "$AUTH_USER:$AUTH_PASS" "$URL/stream" || true)
[ "$stream_code" = "200" ] || fail "expected 200 for stream, got $stream_code"
head -c 8192 "$TMP/stream.bin" | od -An -tx1 | tr -d ' \n' | grep -q 'ffd8' \
  || fail "stream contains no JPEG SOI marker"

curl -sf --max-time 20 -u "$AUTH_USER:$AUTH_PASS" "$URL/snapshot.jpg" -o "$TMP/snap.jpg" \
  || fail "snapshot request failed"
[ "$(head -c 2 "$TMP/snap.jpg" | od -An -tx1 | tr -d ' \n')" = "ffd8" ] \
  || fail "snapshot is not a JPEG"

docker exec "$NAME" python3 /app/camd.py healthcheck >/dev/null \
  || fail "container healthcheck subcommand failed"

echo "SMOKE OK"
