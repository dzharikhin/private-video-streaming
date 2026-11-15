#!/usr/bin/env python3
"""camd - expose a local video device as an MJPEG HTTP stream behind Basic Auth.

Configuration comes exclusively from environment variables (see load_config),
so the container needs no config files. ffmpeg captures and encodes; this
script serves HTTP and fans frames out to clients.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BACKOFF = 30.0
RESERVED_PATHS = {"/", "/healthz", "/snapshot.jpg"}


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- config

@dataclass
class Config:
    input: str = "/dev/video0"
    input_format: str = ""
    port: int = 8080
    stream_path: str = "/stream"
    auth_user: str = ""
    auth_pass: str = ""
    fps: int = 5
    width: int = 0
    height: int = 0
    jpeg_quality: int = 5
    ffmpeg_extra: list = field(default_factory=list)

    def ffmpeg_args(self):
        args = ["-hide_banner", "-loglevel", "warning", "-nostdin"]
        if self.input_format:
            args += ["-f", self.input_format]
        args += ["-i", self.input, "-an", "-r", str(self.fps)]
        if self.width and self.height:
            args += ["-vf", "scale=%d:%d" % (self.width, self.height)]
        args += [
            "-c:v", "mjpeg",
            "-q:v", str(self.jpeg_quality),
            "-f", "mpjpeg",
            "-boundary_tag", "frame",
        ]
        args += self.ffmpeg_extra
        args.append("pipe:1")
        return args

    @property
    def size(self):
        return "%dx%d" % (self.width, self.height) if self.width else "native"


def load_config(env=None):
    env = os.environ if env is None else env

    def get(name, default=""):
        return env.get(name) or default

    def env_int(name, default, lo, hi):
        raw = env.get(name, "")
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            value = None
        if value is None or not lo <= value <= hi:
            raise ConfigError(
                "%s must be an integer between %d and %d, got %r" % (name, lo, hi, raw)
            )
        return value

    cfg = Config(
        input=get("INPUT", "/dev/video0"),
        input_format=env.get("INPUT_FORMAT", ""),
        port=env_int("PORT", 8080, 1, 65535),
        stream_path=get("STREAM_PATH", "/stream"),
        auth_user=env.get("AUTH_USER", ""),
        auth_pass=env.get("AUTH_PASS", ""),
        fps=env_int("FPS", 5, 1, 120),
        width=env_int("WIDTH", 0, 1, 65535),
        height=env_int("HEIGHT", 0, 1, 65535),
        jpeg_quality=env_int("JPEG_QUALITY", 5, 2, 31),
    )
    if not cfg.auth_user or not cfg.auth_pass:
        raise ConfigError("AUTH_USER and AUTH_PASS are required")
    if (cfg.width == 0) != (cfg.height == 0):
        raise ConfigError("WIDTH and HEIGHT must be set together")

    cfg.stream_path = cfg.stream_path.rstrip("/")
    if (
        not cfg.stream_path.startswith("/")
        or not cfg.stream_path
        or cfg.stream_path in RESERVED_PATHS
        or any(c in cfg.stream_path for c in " \t\r\n?*")
    ):
        raise ConfigError(
            "STREAM_PATH must be an absolute path like /stream, got %r"
            % env.get("STREAM_PATH")
        )

    if not cfg.input_format and cfg.input.startswith("/dev/"):
        cfg.input_format = "v4l2"
    cfg.ffmpeg_extra = shlex.split(env.get("FFMPEG_EXTRA", ""))
    return cfg


# ---------------------------------------------------------------- auth

def check_auth(header_value, user, password):
    """Constant-time Basic credentials check."""
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header_value[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    req_user, sep, req_pass = decoded.partition(":")
    if not sep:
        return False
    got = hashlib.sha256(("%s:%s" % (req_user, req_pass)).encode()).digest()
    want = hashlib.sha256(("%s:%s" % (user, password)).encode()).digest()
    return hmac.compare_digest(got, want)


# ---------------------------------------------------------------- mpjpeg parsing

class FrameParseError(Exception):
    pass


def read_frame(stream):
    """Read one JPEG part from ffmpeg's mpjpeg output.

    Parts look like:
      --frame\r\nContent-Type: image/jpeg\r\nContent-Length: N\r\n\r\n<N bytes>\r\n
    Returns None on clean EOF.
    """
    content_length = -1
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.rstrip(b"\r\n")
        if not line:
            break
        lower = line.lower()
        if lower.startswith(b"content-length:"):
            raw = lower.split(b":", 1)[1].strip()
            try:
                content_length = int(raw.decode("ascii"))
            except ValueError:
                raise FrameParseError("invalid content-length %r" % line)
    if content_length <= 0:
        raise FrameParseError("mpjpeg part without content-length")
    frame = stream.read(content_length)
    if len(frame) < content_length:
        return None
    if stream.read(2) != b"\r\n":
        raise FrameParseError("malformed mpjpeg framing after frame")
    return frame


# ---------------------------------------------------------------- capture

class Subscription:
    def __init__(self, sub_id, frames):
        self.id = sub_id
        self.frames = frames


class CaptureManager:
    """Owns the ffmpeg process and fans JPEG frames out to subscribers.

    ffmpeg runs on demand: it starts with the first subscriber and stops when
    the last one leaves. Crashes are retried with exponential backoff.
    """

    def __init__(self, cfg, log=None, spawn=None, backoff=1.0):
        self.cfg = cfg
        self.log = log or logging.getLogger("camd.capture")
        self.backoff = backoff
        self.spawn = spawn or (lambda: ["ffmpeg"] + cfg.ffmpeg_args())
        self.cond = threading.Condition()
        self.clients = {}
        self._next_id = 0
        self._wake_seq = 0
        self.latest = None
        self.proc = None
        self.starts = 0
        self.shutdown = False
        self.thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self.thread.start()

    def subscribe(self):
        with self.cond:
            self._next_id += 1
            sub = Subscription(self._next_id, queue.Queue(maxsize=2))
            self.clients[sub.id] = sub
            self._wake_seq += 1
            self.cond.notify_all()
            return sub

    def unsubscribe(self, sub):
        with self.cond:
            self.clients.pop(sub.id, None)
            if not self.clients:
                self._terminate_locked()
            self._wake_seq += 1
            self.cond.notify_all()

    def snapshot(self, timeout):
        """Most recent frame, waiting for one when the stream is idle."""
        with self.cond:
            if self.latest is not None:
                return self.latest, True
        sub = self.subscribe()
        try:
            return sub.frames.get(timeout=timeout), True
        except queue.Empty:
            return None, False
        finally:
            self.unsubscribe(sub)

    def status(self):
        with self.cond:
            return {
                "clients": len(self.clients),
                "ffmpeg_running": self.proc is not None,
                "ffmpeg_starts": self.starts,
            }

    def stop(self):
        with self.cond:
            self.shutdown = True
            self.clients.clear()
            self._terminate_locked()
            self.cond.notify_all()
        self.thread.join(timeout=10)

    def _terminate_locked(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass

    def _run(self):
        while True:
            with self.cond:
                while not self.clients and not self.shutdown:
                    self.cond.wait()
                if self.shutdown:
                    return
            self._session()

    def _session(self):
        backoff = self.backoff
        while True:
            with self.cond:
                if self.shutdown or not self.clients:
                    break
            error = self._stream_once()
            with self.cond:
                if self.shutdown or not self.clients:
                    break
                seq = self._wake_seq
            if error:
                self.log.warning(
                    "ffmpeg exited unexpectedly, restarting in %.1fs: %s", backoff, error
                )
            else:
                self.log.info("ffmpeg exited, restarting in %.1fs", backoff)
            with self.cond:
                # Wake early on shutdown, idle, or a new client arriving.
                if self.cond.wait_for(
                    lambda: self.shutdown or not self.clients or self._wake_seq != seq,
                    timeout=backoff,
                ):
                    break
            backoff = min(backoff * 2, MAX_BACKOFF)
        with self.cond:
            if not self.clients:
                self.latest = None

    def _stream_once(self):
        """Run one ffmpeg attempt until it exits; return an error string or None."""
        cmd = self.spawn()
        with tempfile.TemporaryFile() as errfile:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=errfile,
                    stdin=subprocess.DEVNULL,
                )
            except OSError as exc:
                return "start ffmpeg: %s" % exc
            with self.cond:
                self.starts += 1
                self.proc = proc
            self.log.info("ffmpeg started (pid %d, input %s)", proc.pid, self.cfg.input)

            error = None
            try:
                while True:
                    frame = read_frame(proc.stdout)
                    if frame is None:
                        break
                    with self.cond:
                        self.latest = frame
                        for sub in list(self.clients.values()):
                            try:
                                sub.frames.put_nowait(frame)
                            except queue.Full:
                                pass  # slow client: drop the frame, the next one catches up
            except FrameParseError as exc:
                error = str(exc)
            finally:
                proc.stdout.close()
            returncode = proc.wait()

            with self.cond:
                if self.proc is proc:
                    self.proc = None
            if error is None and returncode != 0:
                errfile.seek(0)
                tail = errfile.read().decode("utf-8", "replace").strip()[-2048:]
                error = "exit code %d: %s" % (returncode, tail) if tail else "exit code %d" % returncode
            return error


# ---------------------------------------------------------------- http

class CamRequestHandler(BaseHTTPRequestHandler):
    app = None  # assigned by build_server
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.app.handle(self)

    def log_message(self, fmt, *args):
        self.app.log.info("%s %s", self.address_string(), fmt % args)

    def log_request(self, code="-", size="-"):
        if self.path.startswith("/healthz"):
            return
        super().log_request(code, size)


class App:
    def __init__(self, cfg, manager):
        self.cfg = cfg
        self.manager = manager
        self.log = logging.getLogger("camd.http")
        index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        with open(index_path, encoding="utf-8") as fh:
            self.index_html = fh.read()

    def handle(self, h):
        path = h.path.split("?", 1)[0]
        if path == "/healthz":
            self.health(h)
        elif path == "/snapshot.jpg":
            if self.require_auth(h):
                self.snapshot(h)
        elif path == self.cfg.stream_path:
            if self.require_auth(h):
                self.stream(h)
        elif path == "/":
            if self.require_auth(h):
                self.index(h)
        else:
            h.send_error(404)

    def require_auth(self, h):
        if check_auth(h.headers.get("Authorization"), self.cfg.auth_user, self.cfg.auth_pass):
            return True
        body = b"unauthorized\n"
        h.send_response(401)
        h.send_header("WWW-Authenticate", 'Basic realm="camera", charset="UTF-8"')
        h.send_header("Content-Type", "text/plain; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Connection", "close")
        h.close_connection = True
        h.end_headers()
        h.wfile.write(body)
        return False

    def index(self, h):
        body = self.index_html.replace("{{STREAM_PATH}}", self.cfg.stream_path).encode()
        h.send_response(200)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def stream(self, h):
        sub = self.manager.subscribe()
        self.log.info("stream client connected (%s)", h.address_string())
        try:
            h.send_response(200)
            h.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            h.send_header("Cache-Control", "no-store")
            h.send_header("Pragma", "no-cache")
            h.send_header("X-Accel-Buffering", "no")
            h.send_header("Connection", "close")
            h.close_connection = True
            h.end_headers()
            while not self.manager.shutdown:
                try:
                    frame = sub.frames.get(timeout=1.0)
                except queue.Empty:
                    continue
                chunk = (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
                try:
                    h.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
        finally:
            self.manager.unsubscribe(sub)
            self.log.info("stream client disconnected (%s)", h.address_string())

    def snapshot(self, h):
        frame, ok = self.manager.snapshot(15.0)
        if not ok:
            h.send_error(503, "no frame available")
            return
        h.send_response(200)
        h.send_header("Content-Type", "image/jpeg")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Length", str(len(frame)))
        h.end_headers()
        h.wfile.write(frame)

    def health(self, h):
        status = self.manager.status()
        status["status"] = "ok"
        body = json.dumps(status).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)


def build_server(cfg, manager):
    handler = type("Handler", (CamRequestHandler,), {"app": App(cfg, manager)})
    server = ThreadingHTTPServer(("0.0.0.0", cfg.port), handler)
    server.daemon_threads = True
    return server


# ---------------------------------------------------------------- main

def healthcheck():
    port = os.environ.get("PORT") or "8080"
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:%s/healthz" % port, timeout=3
        ) as resp:
            if resp.status != 200:
                raise SystemExit("healthcheck: unexpected status %d" % resp.status)
    except urllib.error.URLError as exc:
        raise SystemExit("healthcheck: %s" % exc)


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="time=%(asctime)s level=%(levelname)s msg=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stdout,
    )
    log = logging.getLogger("camd")
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "healthcheck":
        healthcheck()
        return

    try:
        cfg = load_config()
    except ConfigError as exc:
        log.error("invalid configuration: %s", exc)
        sys.exit(2)

    manager = CaptureManager(cfg)
    server = build_server(cfg, manager)

    def _shutdown(signum, frame):
        log.info("shutting down")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info(
        "listening addr=:%d input=%s input_format=%s stream_path=%s fps=%d size=%s "
        "jpeg_quality=%d auth_user=%s",
        cfg.port, cfg.input, cfg.input_format or "auto", cfg.stream_path,
        cfg.fps, cfg.size, cfg.jpeg_quality, cfg.auth_user,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        manager.stop()


if __name__ == "__main__":
    main()
