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

## Finding your camera

Webcams usually appear as `/dev/video0`, `/dev/video1`, … (a single camera may create several
nodes — one per function). If `INPUT` is not set, the container auto-detects at startup: it
probes every `/dev/video*` node it was given via `--device` and uses the first one that
actually delivers a frame (metadata nodes are skipped automatically) — so pointing
`--device` at the camera is usually all you need. To pick a specific node instead:

```bash
# best: shows device names next to each node (requires v4l-utils on the host)
v4l2-ctl --list-devices

# no tools needed: stable symlinks include the camera model
ls -l /dev/v4l/by-id/
```

Sanity-check that a device produces video, using this image's ffmpeg (no host installs):

```bash
docker run --rm --device /dev/video0 \
  --entrypoint ffmpeg ghcr.io/dzharikhin/private-video-streaming \
  -hide_banner -f v4l2 -i /dev/video0 -t 1 -f null -
```

If it works, you will see the device's supported formats and a short encode run without errors.

## Configuration (env only)

| Env | Default | Description |
|---|---|---|
| `AUTH_USER` | — | **required**; Basic auth username (server refuses to start without it) |
| `AUTH_PASS` | — | **required**; Basic auth password |
| `INPUT` | auto | first capture-capable `/dev/video*` node (probed at startup); or any ffmpeg input: device path, `rtsp://…`, `testsrc=…`, etc. |
| `INPUT_FORMAT` | auto (`v4l2` for `/dev/*`) | ffmpeg input format (`-f`), e.g. `lavfi` for test sources |
| `PORT` | `8080` | HTTP port to listen on |
| `STREAM_PATH` | `/stream` | path of the MJPEG stream |
| `FPS` | `5` | frames per second |
| `WIDTH` / `HEIGHT` | native | both set → input is scaled (e.g. `1280` / `720`) |
| `JPEG_QUALITY` | `5` | ffmpeg `-q:v` scale: 2 = best, 31 = worst |
| `FFMPEG_EXTRA` | — | raw extra args appended to ffmpeg output options (escape hatch) |

Inputs that are not inherently live (files, lavfi test sources like `testsrc`)
are automatically paced to realtime via ffmpeg's `-re` flag; device and
network (`rtsp://`, `http://`) inputs are left as-is.

### Browser notes

The built-in viewer page renders the stream itself: it fetches the multipart
stream, draws each frame to a `<canvas>`, and immediately releases the decoded
frame — memory stays flat, so viewing can last indefinitely. The connection is
also rotated every 5 minutes and paused while the tab is hidden. This requires
a modern browser (fetch streaming + `createImageBitmap`); older ones fall back
to a plain `<img>`, where the browser manages memory and long streams can grow
(Firefox is the most affected).

Avoid opening `/stream` directly or embedding it in your own `<img>` for long
viewing sessions — browsers accumulate multipart frames on such connections.
For lower bandwidth on slow links, reduce `WIDTH`/`HEIGHT` or `FPS`.

## Endpoints

| Path | Auth | Description |
|---|---|---|
| `/` | yes | viewer page: canvas-rendered live stream (memory-safe for indefinite viewing) |
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
