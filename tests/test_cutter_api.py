from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import requests

from live_relay import dashboard as dashboard_module
from live_relay.dashboard import PROJECT_ROOT, DashboardController, build_handler_class

FAKE = '''import argparse, json, os, pathlib, sys
parser = argparse.ArgumentParser()
parser.add_argument("--local-video", required=True)
args, _ = parser.parse_known_args()
recording = pathlib.Path(args.local_video)
project = pathlib.Path("VIRALS") / recording.stem
(project / "final").mkdir(parents=True, exist_ok=True)
payload = {"segments": [{
    "title": "raw",
    "recommended_title": "مقطع تجريبي",
    "start_time": 0,
    "end_time": 30,
    "duration": 30,
    "selection_score": 91,
    "reasoning": "خطّاف قوي"
}]}
(project / "viral_segments.txt").write_text(
    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
(project / "final" / "000_clip.mp4").write_bytes(b"")
print("fake cutter finished")
sys.exit(int(os.environ.get("FAKE_CUTTER_EXIT", "0")))
'''


class CutterApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-cutter-", dir=PROJECT_ROOT
        )
        self.root = Path(self._tmp.name)
        (self.root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.state = self.root / "state"
        recordings = self.state / "recordings"
        recordings.mkdir(parents=True)
        self.recording = recordings / "session.mp4"
        self.recording.write_bytes(b"")
        old = time.time() - 600
        os.utime(self.recording, (old, old))
        self.home = self.root / "cutter"
        self.home.mkdir()
        (self.home / "main_improved.py").write_text(FAKE, encoding="utf-8")
        self.env = self.root / ".env"
        self.env.write_text(
            f"LIVE_RELAY_STATE_DIR={self.state}\n"
            "LIVE_PLATFORMS=youtube\n"
            "YOUTUBE_STREAM_KEY=dummy-key\n"
            "CUTTER_ENABLED=true\n"
            f"CUTTER_HOME={self.home}\n"
            f"CUTTER_PYTHON={sys.executable}\n"
            "CUTTER_AI_BACKEND=manual\n"
            "CUTTER_SEGMENTS=2\n"
            "CUTTER_PLATFORM=tiktok\n",
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

    def get(self, path: str) -> requests.Response:
        return requests.get(
            self.url(path),
            headers={"X-Relay-Token": self.controller.control_token},
            timeout=10,
        )

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

    def wait(self, timeout: float = 60) -> dict:
        deadline = time.time() + timeout
        status: dict = {}
        while time.time() < deadline:
            status = self.get("/api/clips/status").json()
            if not status.get("running"):
                return status
            time.sleep(0.2)
        self.fail(f"cutter job did not finish: {status}")

    def test_check_reports_ready(self):
        body = self.get("/api/clips/check").json()
        self.assertTrue(body["available"], body)
        self.assertEqual(body["python"], sys.executable)
        self.assertEqual(len(body["recordings"]), 1)

    def test_analyze_runs_and_returns_ranked_clips(self):
        response = self.post(
            "/api/clips/analyze", {"recording": str(self.recording), "workflow": 1}
        )
        self.assertEqual(response.status_code, 202, response.text)
        status = self.wait()
        self.assertEqual(status["error"], "", status)
        self.assertEqual(status["clips"], 1)
        self.assertEqual(status["phase"], "انتهى")
        project = status["project"]
        listing = self.get(
            "/api/clips?project=" + quote(project, safe="")
        ).json()
        self.assertEqual(len(listing["clips"]), 1)
        clip = listing["clips"][0]
        self.assertEqual(clip["title"], "مقطع تجريبي")
        self.assertEqual(clip["score"], 91)
        self.assertTrue(clip["file"].endswith("000_clip.mp4"))

    def test_analyze_rejects_a_path_outside_the_project(self):
        outside = Path(tempfile.gettempdir()) / "not-a-recording.mp4"
        outside.write_bytes(b"")
        try:
            response = self.post("/api/clips/analyze", {"recording": str(outside)})
            self.assertEqual(response.status_code, 400)
            self.assertIn("مجلد التسجيلات", response.json()["error"])
        finally:
            outside.unlink(missing_ok=True)

    def test_failure_is_reported(self):
        with unittest.mock.patch.dict(os.environ, {"FAKE_CUTTER_EXIT": "3"}):
            response = self.post(
                "/api/clips/analyze", {"recording": str(self.recording)}
            )
            self.assertEqual(response.status_code, 202)
            status = self.wait()
        self.assertEqual(status["phase"], "فشل")
        self.assertIn("فشل CQT1", status["error"])

    def test_analyze_requires_the_token(self):
        response = requests.post(
            self.url("/api/clips/analyze"),
            json={"recording": str(self.recording)},
            timeout=5,
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
