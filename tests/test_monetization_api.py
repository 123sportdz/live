from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests

from live_relay import dashboard as dashboard_module
from live_relay.dashboard import PROJECT_ROOT, DashboardController, build_handler_class


class MonetizationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-ads-", dir=PROJECT_ROOT
        )
        root = Path(self._tmp.name)
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.state_dir = root / "state"
        (root / ".env").write_text(
            f"LIVE_RELAY_STATE_DIR={self.state_dir}\n"
            "LIVE_PLATFORMS=youtube\n"
            "YOUTUBE_STREAM_KEY=dummy-key\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            self.controller = DashboardController(root)
        html_path = Path(dashboard_module.__file__).with_name("dashboard.html")
        handler = build_handler_class(self.controller, html_path)
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

    def headers(self) -> dict[str, str]:
        return {
            "X-Relay-Token": self.controller.control_token,
            "Content-Type": "application/json",
        }

    def test_config_exposes_the_new_policy_fields(self):
        config = requests.get(self.url("/api/config"), timeout=5).json()
        self.assertEqual(config["source_rights"], "unknown")
        self.assertEqual(config["source_rights_note"], "")
        self.assertEqual(config["transform_level"], "branded")
        self.assertFalse(config["youtube_made_for_kids"])
        self.assertEqual(config["youtube_default_language"], "ar")
        self.assertEqual(config["youtube_category_id"], "25")
        self.assertTrue(config["seo_rewrite"])

    def test_summary_is_available_without_a_token(self):
        response = requests.get(self.url("/api/monetization"), timeout=5)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["sessions"], 0)
        self.assertIn("current", body)

    def test_audit_requires_the_local_control_token(self):
        response = requests.post(
            self.url("/api/monetization/audit"), json={"rights": "third_party"}, timeout=5
        )
        self.assertEqual(response.status_code, 403)

    def test_audit_returns_a_plan_and_stores_it_locally(self):
        payload = {
            "title": "تغطية مباشرة لاجتماع مجلس الأمن مع تحليل ميداني",
            "description": "تغطية مباشرة مع تعليق وتحليل. المصدر: قناة الحدث.",
            "source_channel": "قناة الحدث",
            "rights": "third_party",
            "transform": "branded",
            "duration_seconds": 3600,
        }
        response = requests.post(
            self.url("/api/monetization/audit"),
            json=payload,
            headers=self.headers(),
            timeout=5,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["audit"]["band"], "blocked")
        self.assertTrue(body["markdown"])
        self.assertTrue(any(item["code"] == "reused_content" for item in body["audit"]["risks"]))
        log = self.state_dir / "ads-audit.jsonl"
        self.assertTrue(log.is_file())
        if os.name != "nt":
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_editorial_plan_reaches_a_ready_band(self):
        payload = {
            "title": "تغطية مباشرة لاجتماع مجلس الأمن مع تحليل ميداني",
            "description": "تغطية مباشرة مع تعليق وتحليل. المصدر: قناة الحدث.",
            "source_channel": "قناة الحدث",
            "rights": "licensed",
            "transform": "editorial",
            "duration_seconds": 3600,
            "original_segments": 4,
        }
        body = requests.post(
            self.url("/api/monetization/audit"),
            json=payload,
            headers=self.headers(),
            timeout=5,
        ).json()
        self.assertEqual(body["audit"]["band"], "ready")
        self.assertIn("نشر بموجب ترخيص", body["plan"]["description"])

    def test_video_status_without_a_session_is_a_clear_error(self):
        response = requests.post(
            self.url("/api/youtube/video-status"), json={}, headers=self.headers(), timeout=5
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("YouTube", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
