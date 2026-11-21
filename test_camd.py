import base64
import contextlib
import io
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import camd

TEST_FRAMES = [
    bytes([0xFF, 0xD8, 0x00, 0x01, 0x02, 0xFF, 0xD9]),
    bytes([0xFF, 0xD8, 0x03, 0x04, 0xFF, 0xD9]),
    bytes([0xFF, 0xD8, 0x05, 0xFF, 0xD9]),
]


def mpjpeg_bytes(frames):
    parts = []
    for f in frames:
        parts.append(
            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(f)
            + f
            + b"\r\n"
        )
    return b"".join(parts)


@contextlib.contextmanager
def patched_env(**values):
    saved = {}
    for key, value in values.items():
        saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def base_env(**overrides):
    env = {
        "AUTH_USER": None,
        "AUTH_PASS": None,
        "INPUT": None,
        "INPUT_FORMAT": None,
        "PORT": None,
        "STREAM_PATH": None,
        "FPS": None,
        "WIDTH": None,
        "HEIGHT": None,
        "JPEG_QUALITY": None,
        "FFMPEG_EXTRA": None,
    }
    env.update(overrides)
    return env


def quiet_logger():
    logger = logging.getLogger("camd.test")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def wait_for(condition, timeout=3.0, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    raise AssertionError("timeout waiting for: %s" % message)


def recv_frame(frames, timeout=3.0):
    try:
        return frames.get(timeout=timeout)
    except Exception:
        raise AssertionError("no frame received within %.1fs" % timeout)


def write_file(directory, name, data):
    path = os.path.join(directory, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


class LoadConfigTest(unittest.TestCase):
    def test_defaults(self):
        with patched_env(**base_env(AUTH_USER="u", AUTH_PASS="p")):
            with mock.patch("camd.autodetect_video_device", return_value="/dev/video3"):
                cfg = camd.load_config()
        self.assertEqual(cfg.input, "/dev/video3")
        self.assertEqual(cfg.input_format, "v4l2")
        self.assertEqual(cfg.port, 8080)
        self.assertEqual(cfg.stream_path, "/stream")
        self.assertEqual(cfg.fps, 5)
        self.assertEqual(cfg.jpeg_quality, 5)
        self.assertEqual(cfg.ffmpeg_extra, [])

    def test_explicit_input_skips_autodetect(self):
        with patched_env(**base_env(AUTH_USER="u", AUTH_PASS="p", INPUT="/dev/video1")):
            with mock.patch("camd.autodetect_video_device") as autodetect:
                cfg = camd.load_config()
        autodetect.assert_not_called()
        self.assertEqual(cfg.input, "/dev/video1")
        self.assertEqual(cfg.input_format, "v4l2")

    def test_autodetect_failure_requires_input(self):
        with patched_env(**base_env(AUTH_USER="u", AUTH_PASS="p")):
            with mock.patch("camd.autodetect_video_device", return_value=None):
                self.assertRaises(camd.ConfigError, camd.load_config)

    def test_requires_auth(self):
        with patched_env(**base_env(AUTH_USER="u")):
            self.assertRaises(camd.ConfigError, camd.load_config)

    def test_validations(self):
        cases = [
            {"PORT": "70000"},
            {"FPS": "0"},
            {"JPEG_QUALITY": "99"},
            {"WIDTH": "640"},
            {"HEIGHT": "480"},
            {"STREAM_PATH": "nope"},
            {"STREAM_PATH": "/"},
            {"STREAM_PATH": "/healthz"},
            {"STREAM_PATH": "/snapshot.jpg"},
        ]
        for overrides in cases:
            with self.subTest(overrides):
                with patched_env(**base_env(AUTH_USER="u", AUTH_PASS="p", **overrides)):
                    self.assertRaises(camd.ConfigError, camd.load_config)

    def test_overrides(self):
        env = base_env(
            AUTH_USER="u",
            AUTH_PASS="p",
            INPUT="testsrc=size=320x240:rate=2",
            INPUT_FORMAT="lavfi",
            PORT="9000",
            STREAM_PATH="/cam/",
            FPS="2",
            WIDTH="320",
            HEIGHT="240",
            JPEG_QUALITY="3",
            FFMPEG_EXTRA="-bf 0",
        )
        with patched_env(**env):
            cfg = camd.load_config()
        self.assertEqual(cfg.input_format, "lavfi")
        self.assertEqual(cfg.port, 9000)
        self.assertEqual(cfg.stream_path, "/cam")
        self.assertEqual(cfg.fps, 2)
        self.assertEqual((cfg.width, cfg.height), (320, 240))
        self.assertEqual(cfg.jpeg_quality, 3)
        self.assertEqual(cfg.ffmpeg_extra, ["-bf", "0"])


class AutodetectTest(unittest.TestCase):
    def test_list_sorted_numerically(self):
        nodes = ["/dev/video10", "/dev/video2", "/dev/video0"]
        with mock.patch("camd.glob.glob", return_value=nodes):
            self.assertEqual(
                camd.list_video_devices(),
                ["/dev/video0", "/dev/video2", "/dev/video10"],
            )

    def test_first_working_node_wins(self):
        with mock.patch(
            "camd.list_video_devices",
            return_value=["/dev/video0", "/dev/video1", "/dev/video2"],
        ), mock.patch("camd.probe_video_device", side_effect=[False, True]):
            self.assertEqual(camd.autodetect_video_device(), "/dev/video1")

    def test_no_working_node(self):
        with mock.patch(
            "camd.list_video_devices", return_value=["/dev/video0", "/dev/video1"]
        ), mock.patch("camd.probe_video_device", return_value=False):
            self.assertIsNone(camd.autodetect_video_device())

    def test_probe_grabs_one_frame(self):
        with mock.patch("camd.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertTrue(camd.probe_video_device("/dev/video0"))
        args = run.call_args[0][0]
        self.assertEqual(args[0], "ffmpeg")
        self.assertEqual(args[args.index("-f") + 1], "v4l2")
        self.assertEqual(args[args.index("-i") + 1], "/dev/video0")
        self.assertIn("-frames:v", args)

    def test_probe_failure_and_timeout(self):
        with mock.patch("camd.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1)
            self.assertFalse(camd.probe_video_device("/dev/video0"))
        with mock.patch(
            "camd.subprocess.run",
            side_effect=subprocess.TimeoutExpired("ffmpeg", 10),
        ):
            self.assertFalse(camd.probe_video_device("/dev/video0"))


class FFmpegArgsTest(unittest.TestCase):
    def test_basic_device_input(self):
        cfg = camd.Config(input="/dev/video0", input_format="v4l2", fps=5, jpeg_quality=5)
        self.assertEqual(
            cfg.ffmpeg_args(),
            [
                "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "v4l2", "-i", "/dev/video0",
                "-an", "-r", "5",
                "-c:v", "mjpeg", "-q:v", "5",
                "-f", "mpjpeg", "-boundary_tag", "frame",
                "pipe:1",
            ],
        )

    def test_realtime_pacing(self):
        # Live inputs are already realtime: no pacing flag.
        for live in ("/dev/video0", "rtsp://cam/stream", "http://cam/mjpeg"):
            cfg = camd.Config(input=live)
            self.assertFalse(cfg.realtime_pacing, live)
            self.assertNotIn("-re", cfg.ffmpeg_args())
        # Synthetic and file inputs would flood at encode speed: pace them.
        for paced in ("testsrc=size=640x480:rate=5", "/videos/cam.mp4"):
            cfg = camd.Config(input=paced)
            self.assertTrue(cfg.realtime_pacing, paced)
            args = cfg.ffmpeg_args()
            self.assertIn("-re", args)
            self.assertLess(args.index("-re"), args.index("-i"))

    def test_scale_and_extra(self):
        cfg = camd.Config(
            input="testsrc=size=320x240:rate=2", input_format="lavfi", fps=2,
            jpeg_quality=3, width=1280, height=720, ffmpeg_extra=["-x", "y"],
        )
        args = cfg.ffmpeg_args()
        self.assertIn("-re", args)
        self.assertIn("-vf", args)
        self.assertEqual(args[args.index("-vf") + 1], "scale=1280:720")
        self.assertEqual(args[-3:], ["-x", "y", "pipe:1"])

    def test_size(self):
        self.assertEqual(camd.Config().size, "native")
        self.assertEqual(camd.Config(width=640, height=480).size, "640x480")


class CheckAuthTest(unittest.TestCase):
    def check(self, header):
        return camd.check_auth(header, "alice", "s3cr:t")

    def basic(self, user, password):
        raw = ("%s:%s" % (user, password)).encode()
        return "Basic " + base64.b64encode(raw).decode()

    def test_cases(self):
        self.assertFalse(self.check(None))
        self.assertFalse(self.check(""))
        self.assertFalse(self.check("Bearer " + base64.b64encode(b"alice:s3cr:t").decode()))
        self.assertFalse(self.check("Basic !!!not-base64!!!"))
        self.assertFalse(self.check(self.basic("bob", "s3cr:t")))
        self.assertFalse(self.check(self.basic("alice", "nope")))
        self.assertTrue(self.check(self.basic("alice", "s3cr:t")))
        self.assertTrue(self.check("Basic  " + base64.b64encode(b"alice:s3cr:t").decode()))


class ReadFrameTest(unittest.TestCase):
    def test_parses_frames(self):
        stream = io.BytesIO(mpjpeg_bytes(TEST_FRAMES[:2]))
        self.assertEqual(camd.read_frame(stream), TEST_FRAMES[0])
        self.assertEqual(camd.read_frame(stream), TEST_FRAMES[1])
        self.assertIsNone(camd.read_frame(stream))

    def test_missing_content_length(self):
        stream = io.BytesIO(b"--frame\r\nContent-Type: image/jpeg\r\n\r\njunk")
        self.assertRaises(camd.FrameParseError, camd.read_frame, stream)

    def test_truncated_frame(self):
        stream = io.BytesIO(b"--frame\r\nContent-Length: 10\r\n\r\nshort")
        self.assertIsNone(camd.read_frame(stream))

    def test_bad_framing_after_frame(self):
        stream = io.BytesIO(b"--frame\r\nContent-Length: 3\r\n\r\nabcXX")
        self.assertRaises(camd.FrameParseError, camd.read_frame, stream)


class ManagerTestBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def make_manager(self, script):
        cfg = camd.Config(auth_user="u", auth_pass="p", input="test")
        manager = camd.CaptureManager(
            cfg, log=quiet_logger(), spawn=lambda: ["sh", "-c", script], backoff=0.05
        )
        self.addCleanup(manager.stop)
        return manager

    def feeder_manager(self):
        parts = []
        for i, frame in enumerate(TEST_FRAMES):
            path = write_file(self.directory, "f%d.mpjpeg" % i, mpjpeg_bytes([frame]))
            parts.append('cat "%s"; sleep 0.2' % path)
        return self.make_manager("; ".join(parts) + "; sleep 30")

    def crash_manager(self):
        path = write_file(self.directory, "source.mpjpeg", mpjpeg_bytes(TEST_FRAMES))
        return self.make_manager('cat "$1"; exit 7'.replace("$1", '"%s"' % path))


class CaptureManagerTest(ManagerTestBase):
    def test_broadcasts_to_all_clients(self):
        manager = self.feeder_manager()
        sub1 = manager.subscribe()
        sub2 = manager.subscribe()
        for expected in TEST_FRAMES:
            self.assertEqual(recv_frame(sub1.frames), expected)
            self.assertEqual(recv_frame(sub2.frames), expected)
        manager.unsubscribe(sub1)
        manager.unsubscribe(sub2)
        wait_for(
            lambda: not manager.status()["ffmpeg_running"],
            message="ffmpeg to stop after last client",
        )

    def test_restarts_after_crash(self):
        manager = self.crash_manager()
        sub = manager.subscribe()
        recv_frame(sub.frames)
        wait_for(
            lambda: manager.status()["ffmpeg_starts"] >= 2, message="ffmpeg restart"
        )
        recv_frame(sub.frames)
        manager.unsubscribe(sub)

    def test_snapshot_cold(self):
        manager = self.feeder_manager()
        frame, ok = manager.snapshot(5.0)
        self.assertTrue(ok)
        self.assertEqual(frame, TEST_FRAMES[0])
        wait_for(
            lambda: not manager.status()["ffmpeg_running"],
            message="ffmpeg to stop after snapshot",
        )

    def test_snapshot_warm_returns_latest(self):
        manager = self.feeder_manager()
        sub = manager.subscribe()
        try:
            for expected in TEST_FRAMES:
                self.assertEqual(recv_frame(sub.frames), expected)
            frame, ok = manager.snapshot(5.0)
            self.assertTrue(ok)
            self.assertEqual(frame, TEST_FRAMES[2])
        finally:
            manager.unsubscribe(sub)

    def test_stop_clears_state(self):
        manager = self.feeder_manager()
        sub = manager.subscribe()
        recv_frame(sub.frames)
        manager.stop()
        self.assertFalse(manager.thread.is_alive())
        self.assertEqual(manager.status()["clients"], 0)
        self.assertFalse(manager.status()["ffmpeg_running"])


class ServerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp()
        parts = []
        for i, frame in enumerate(TEST_FRAMES):
            path = write_file(directory, "f%d.mpjpeg" % i, mpjpeg_bytes([frame]))
            parts.append('cat "%s"; sleep 0.2' % path)
        script = "; ".join(parts) + "; while true; do cat %s; sleep 0.5; done" % (
            '"%s"' % os.path.join(directory, "f0.mpjpeg")
        )
        cfg = camd.Config(auth_user="u", auth_pass="p", input="test",
                          stream_path="/stream", port=0)
        self.manager = camd.CaptureManager(
            cfg, log=quiet_logger(), spawn=lambda: ["sh", "-c", script], backoff=0.05
        )
        self.addCleanup(self.manager.stop)
        self.server = camd.build_server(cfg, self.manager)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def request(self, path, auth=None, timeout=10):
        req = urllib.request.Request(self.url + path)
        if auth is not None:
            req.add_header(
                "Authorization",
                "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode(),
            )
        return urllib.request.urlopen(req, timeout=timeout)

    def test_index_requires_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/")
        self.assertEqual(ctx.exception.code, 401)
        self.assertIn('Basic realm="camera"', ctx.exception.headers["WWW-Authenticate"])

    def test_index_with_auth(self):
        with self.request("/", auth=("u", "p")) as resp:
            body = resp.read().decode()
        self.assertIn('id="fallback" src="/stream"', body)
        self.assertIn('id="viewer"', body)
        self.assertIn("createImageBitmap", body)
        self.assertIn("bitmap.close", body)
        self.assertIn("MJPEGParser", body)

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/nope", auth=("u", "p"))
        self.assertEqual(ctx.exception.code, 404)

    def test_stream_requires_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("/stream")
        self.assertEqual(ctx.exception.code, 401)

    def test_stream_delivers_frames(self):
        with self.request("/stream", auth=("u", "p")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(
                resp.headers["Content-Type"].startswith("multipart/x-mixed-replace")
            )
            data = resp.read(len(mpjpeg_bytes(TEST_FRAMES)) + 128)
        self.assertIn(b"--frame", data)
        self.assertIn(TEST_FRAMES[0], data)

    def test_snapshot_is_jpeg(self):
        with self.request("/snapshot.jpg", auth=("u", "p")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], "image/jpeg")
            body = resp.read()
        self.assertEqual(body[:2], b"\xff\xd8")

    def test_healthz(self):
        with self.request("/healthz") as resp:
            self.assertEqual(resp.status, 200)
            status = json.loads(resp.read())
        self.assertEqual(status["status"], "ok")


if __name__ == "__main__":
    unittest.main()
