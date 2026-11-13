from __future__ import annotations

import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from live_relay import dashboard as dashboard_module
from live_relay.dashboard import PROJECT_ROOT, DashboardController, build_handler_class
from live_relay.destinations import DestinationSession


class ThumbnailApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-thumb-", dir=PROJECT_ROOT
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
        self.root = root
        with patch.dict(os.environ, {}, clear=True):
            self.controller = DashboardController(root)
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

    def headers(self) -> dict[str, str]:
        return {
            "X-Relay-Token": self.controller.control_token,
            "Content-Type": "application/json",
        }

    def test_config_exposes_thumbnail_mode_and_brand(self):
        config = requests.get(self.url("/api/config"), timeout=5).json()
        self.assertEqual(config["thumbnail_mode"], "auto")
        self.assertEqual(config["brand_name"], "Oussama News")

    def test_generate_requires_a_title(self):
        response = requests.post(
            self.url("/api/youtube/thumbnail/generate"),
            json={},
            headers=self.headers(),
            timeout=5,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("عنوان", response.json()["error"])

    def test_generate_writes_a_png_and_reports_dimensions(self):
        response = requests.post(
            self.url("/api/youtube/thumbnail/generate"),
            json={"title": "قصف عنيف على مدينة غزة | تغطية مباشرة"},
            headers=self.headers(),
            timeout=20,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["width"], body["height"]), (1280, 720))
        self.assertTrue(Path(body["path"]).is_file())
        self.assertFalse(body["applied_now"])
        config = requests.get(self.url("/api/config"), timeout=5).json()
        self.assertTrue(config["youtube_thumbnail_ready"])
        self.assertEqual(config["thumbnail_provider"], "local")

    def test_generate_applies_to_the_live_video(self):
        session = DestinationSession(
            targets=[("youtube", "rtmp://example/live")], youtube=Mock()
        )
        self.controller.status.set_destination_session(session)
        with patch(
            "live_relay.dashboard.update_destination_thumbnail",
            return_value={"youtube": "updated"},
        ) as upload:
            response = requests.post(
                self.url("/api/youtube/thumbnail/generate"),
                json={"title": "عنوان تجريبي للبث المباشر"},
                headers=self.headers(),
                timeout=20,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["applied_now"])
        upload.assert_called_once()

    def test_generate_requires_the_local_token(self):
        response = requests.post(
            self.url("/api/youtube/thumbnail/generate"),
            json={"title": "عنوان"},
            timeout=5,
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
