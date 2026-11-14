from __future__ import annotations

import os
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


class AutomationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-automation-", dir=PROJECT_ROOT
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
        self.env = self.root / ".env"
        self.env.write_text(
            f"LIVE_RELAY_STATE_DIR={self.state}\n"
            "LIVE_PLATFORMS=youtube\n"
            "YOUTUBE_STREAM_KEY=dummy-key\n"
            "AUTOPILOT_ENABLED=false\n"
            "AUTO_CLIPS=false\n"
            "AUTO_EPISODE=false\n"
            "AUTO_UPLOAD=false\n",
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
        self.controller.stop_automation()
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

    def wait_done(self, timeout: float = 30) -> dict:
        deadline = time.time() + timeout
        status: dict = {}
        while time.time() < deadline:
            status = self.get("/api/automation").json()
            if (status.get("queue") or {}).get("done", 0) >= 1:
                return status
            time.sleep(0.2)
        self.fail(f"pipeline did not finish: {status}")

    def test_status_exposes_config_and_warnings(self):
        body = self.get("/api/automation").json()
        self.assertIn("config", body)
        self.assertFalse(body["config"]["enabled"])
        self.assertTrue(any("متوقفة" in warning for warning in body["warnings"]))

    def test_save_then_run_a_manual_job(self):
        response = self.post(
            "/api/automation/config",
            {
                "enabled": True,
                "auto_clips": False,
                "auto_episode": False,
                "auto_upload": False,
                "upload_privacy": "private",
                "min_free_gb": 1,
                "upload_max": 0,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["config"]["enabled"])
        self.assertIn("AUTOPILOT_ENABLED=true", self.env.read_text(encoding="utf-8"))

        response = self.post(
            "/api/automation/run", {"recording": str(self.recording)}
        )
        self.assertEqual(response.status_code, 202, response.text)
        status = self.wait_done()
        self.assertEqual(status["queue"]["done"], 1)
        job = status["jobs"][0]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["stages"]["clips"], "disabled")

    def test_invalid_privacy_is_rejected(self):
        response = self.post(
            "/api/automation/config",
            {"enabled": True, "upload_privacy": "friends"},
        )
        self.assertEqual(response.status_code, 400)

    def test_retry_unknown_job(self):
        response = self.post("/api/automation/retry", {"job_id": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_write_endpoints_require_the_token(self):
        for path in ("/api/automation/config", "/api/automation/run"):
            response = requests.post(self.url(path), json={}, timeout=5)
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
