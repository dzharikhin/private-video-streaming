# private-video-streaming

Turnkey container that exposes a local video device as an MJPEG HTTP stream behind Basic Auth.
Built for episodic monitoring (low fps, latency tolerated) — no config files, one `docker run` command.

- Capture/encode: [ffmpeg](https://ffmpeg.org)
- HTTP server: single-file Python app (stdlib only, no pip dependencies)
- Auth: HTTP Basic via native browser prompt; credentials only via env
- Viewer: built-in HTML page with the live stream
- Image: `ghcr.io/dzharikhin/private-video-streaming` (public, `linux/amd64` + `linux/arm64`)

## Quick start

```bash
docker run --rm -p 8080:8080 --device /dev/video0 \
  -e AUTH_USER=me -e AUTH_PASS=secret \
  ghcr.io/dzharikhin/private-video-streaming
```

Then open `http://localhost:8080/` in a browser and enter the credentials in the native prompt
(or embed them in the URL: `http://me:secret@localhost:8080/`).

## Configuration (env only)

| Env | Default | Description |
|---|---|---|
| `AUTH_USER` | — | **required**; Basic auth username (server refuses to start without it) |
| `AUTH_PASS` | — | **required**; Basic auth password |
| `INPUT` | `/dev/video0` | any ffmpeg input: device path, `rtsp://…`, `testsrc=…`, etc. |
| `INPUT_FORMAT` | auto (`v4l2` for `/dev/*`) | ffmpeg input format (`-f`), e.g. `lavfi` for test sources |
| `PORT` | `8080` | HTTP port to listen on |
| `STREAM_PATH` | `/stream` | path of the MJPEG stream |
| `FPS` | `5` | frames per second |
| `WIDTH` / `HEIGHT` | native | both set → input is scaled (e.g. `1280` / `720`) |
| `JPEG_QUALITY` | `5` | ffmpeg `-q:v` scale: 2 = best, 31 = worst |
| `FFMPEG_EXTRA` | — | raw extra args appended to ffmpeg output options (escape hatch) |

## Endpoints

| Path | Auth | Description |
|---|---|---|
| `/` | yes | viewer page: live stream via `<img>` |
| `$STREAM_PATH` (default `/stream`) | yes | MJPEG stream (`multipart/x-mixed-replace`) |
| `/snapshot.jpg` | yes | latest frame as JPEG |
| `/healthz` | no | JSON status: clients, ffmpeg state, restart count |

Capture runs on demand: ffmpeg starts when the first client connects and stops when the last one disconnects.

## Examples

Raspberry Pi camera / webcam with quality settings:

```bash
docker run --rm -p 8080:8080 --device /dev/video0 \
  -e AUTH_USER=me -e AUTH_PASS=secret \
  -e WIDTH=1280 -e HEIGHT=720 -e FPS=5 -e JPEG_QUALITY=4 \
  ghcr.io/dzharikhin/private-video-streaming
```

Bridge an RTSP camera instead of a local device:

```bash
docker run --rm -p 8080:8080 \
  -e AUTH_USER=me -e AUTH_PASS=secret \
  -e INPUT=rtsp://camera.local/stream \
  ghcr.io/dzharikhin/private-video-streaming
```

Smoke test without any hardware (ffmpeg test source):

```bash
docker run --rm -p 8080:8080 \
  -e AUTH_USER=me -e AUTH_PASS=secret \
  -e INPUT=testsrc=size=640x480:rate=5 -e INPUT_FORMAT=lavfi \
  ghcr.io/dzharikhin/private-video-streaming
```

### Hardening notes

By default the container runs as root so `--device` works out of the box. To avoid root, run as a
host user with access to the device and pass the device group explicitly:

```bash
docker run --rm --user "$(id -u):$(id -g)" --group-add "$(getent group video | cut -d: -f3)" \
  --device /dev/video0 -p 8080:8080 \
  -e AUTH_USER=me -e AUTH_PASS=secret \
  ghcr.io/dzharikhin/private-video-streaming
```

Also recommended in production: TLS-terminating reverse proxy in front, since Basic Auth over
plain HTTP sends base64 (not encrypted) credentials.

## Publishing the image

### Via CI (default path)

The GitHub Actions workflow (`.github/workflows/docker-publish.yml`) builds and pushes to GHCR on:

- push to `main` → tag `edge` + short commit SHA
- tag `vX.Y.Z` → tags `X.Y.Z`, `X.Y`, `latest`
- manual `workflow_dispatch`

It runs unit tests, builds `linux/amd64` + `linux/arm64`, runs a smoke test
(test source → auth → JPEG bytes → healthz), then pushes.

**One-time step:** after the first successful publish, make the package public —
GitHub → your profile → Packages → `private-video-streaming` → Package settings →
Danger Zone → Change visibility → Public.

### Manually

```bash
docker build -t ghcr.io/dzharikhin/private-video-streaming:local .
docker run --rm -p 8080:8080 -e AUTH_USER=me -e AUTH_PASS=secret \
  -e INPUT=testsrc=size=640x480:rate=5 -e INPUT_FORMAT=lavfi \
  ghcr.io/dzharikhin/private-video-streaming:local
# verify http://localhost:8080/ then:
docker login ghcr.io
docker push ghcr.io/dzharikhin/private-video-streaming:local
```

## Development

```bash
make test        # go vet + go test
make image       # docker build
make smoke       # image + run test source + curl assertions
make run         # run locally against /dev/video0
```
