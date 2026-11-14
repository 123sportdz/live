from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer
from pathlib import Path

import requests

from live_relay import dashboard as dashboard_module
from live_relay.dashboard import PROJECT_ROOT, DashboardController, build_handler_class

FFMPEG = shutil.which("ffmpeg")


def make_clip(path: Path, seconds: float = 2.0) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x1b4332:s=320x180:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=330:duration={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


@unittest.skipUnless(FFMPEG, "ffmpeg is required")
class EpisodeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-episode-", dir=PROJECT_ROOT
        )
        self.root = Path(self._tmp.name)
        (self.root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.state = self.root / "state"
        self.state.mkdir()
        self.recordings = self.state / "recordings"
        self.recordings.mkdir()
        make_clip(self.recordings / "session.mp4", 2)
        old = time.time() - 600
        os.utime(self.recordings / "session.mp4", (old, old))
        self.env = self.root / ".env"
        self.env.write_text(
            f"LIVE_RELAY_STATE_DIR={self.state}\n"
            "LIVE_PLATFORMS=youtube\n"
            "YOUTUBE_STREAM_KEY=dummy-key\n"
            "BRAND_NAME=Oussama News\n",
            encoding="utf-8",
        )
        self.manifest = self.root / "episodes" / "test.json"
        self.manifest.parent.mkdir()
        self.manifest.write_text(
            json.dumps(
                {
                    "title": "حلقة اختبار",
                    "date": "2026-11-14",
                    "brand": "Oussama News",
                    "intro_seconds": 1,
                    "card_seconds": 1,
                    "outro_seconds": 1,
                    "segments": [
                        {
                            "heading": "المقطع الأول",
                            "clip": str(self.recordings / "session.mp4"),
                            "duration": 2,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.controller = DashboardController(self.root)
        handler = build_handler_class(
            self.controller, Path(dashboard_module.__file__).with_name("dashboard.html")
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}{path}"

    def post(self, path: str, payload: dict) -> requests.Response:
        return requests.post(
            self.url(path),
            json=payload,
            headers={
                "X-Relay-Token": self.controller.control_token,
                "Content-Type": "application/json",
            },
            timeout=10,
        )

    def test_plan_returns_timeline(self):
        response = self.post(
            "/api/episodes/plan",
            {"manifest": "episodes/test.json", "allow_short": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total_seconds"], 4)
        self.assertEqual(len(body["chapters"]), 2)

    def test_short_plan_needs_explicit_permission(self):
        response = self.post(
            "/api/episodes/plan", {"manifest": "episodes/test.json"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("قصيرة", response.json()["error"])

    def test_manifest_outside_project_is_rejected(self):
        response = self.post(
            "/api/episodes/plan",
            {"manifest": "../outside.json", "allow_short": True},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("داخل مجلد المشروع", response.json()["error"])

    def test_sample_uses_recordings(self):
        response = self.post("/api/episodes/sample", {})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["segments"], 1)
        self.assertTrue((self.root / "episodes" / "today.json").is_file())

    def test_status_is_idle_before_any_build(self):
        response = requests.get(
            self.url("/api/episodes/status"),
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=5,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["running"])

    def test_build_runs_and_writes_files(self):
        response = self.post(
            "/api/episodes/build",
            {"manifest": "episodes/test.json", "allow_short": True},
        )
        self.assertEqual(response.status_code, 202, response.text)
        deadline = time.time() + 180
        status = {}
        while time.time() < deadline:
            status = requests.get(
                self.url("/api/episodes/status"),
                headers={"X-Relay-Token": self.controller.control_token},
                timeout=10,
            ).json()
            if not status.get("running"):
                break
            time.sleep(1)
        self.assertFalse(status.get("running"), status)
        self.assertEqual(status.get("error"), "", status)
        output = Path(status["output"])
        self.assertTrue((output / "episode.mp4").is_file(), status)
        listing = requests.get(
            self.url("/api/episodes"),
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=10,
        ).json()
        self.assertEqual(len(listing["episodes"]), 1)


if __name__ == "__main__":
    unittest.main()
