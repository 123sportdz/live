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


class BackupApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(
            prefix=".dashboard-backup-", dir=PROJECT_ROOT
        )
        root = Path(self._tmp.name)
        (root / ".env.example").write_text(
            (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.state_dir = root / "state"
        self.env_path = root / ".env"
        self.env_path.write_text(
            f"LIVE_RELAY_STATE_DIR={self.state_dir}\n"
            "LIVE_PLATFORMS=youtube\n"
            "YOUTUBE_STREAM_KEY=dummy-key\n"
            "GEMINI_API_KEY=very-secret-key\n"
            "BRAND_NAME=Oussama News\n",
            encoding="utf-8",
        )
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

    def post(self, path: str, payload: dict) -> requests.Response:
        return requests.post(
            self.url(path), json=payload, headers=self.headers(), timeout=10
        )

    def test_default_export_omits_secrets(self):
        response = self.post("/api/config/export", {})
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()["payload"]
        self.assertFalse(payload["secrets_included"])
        self.assertNotIn("secrets", payload)
        self.assertNotIn("very-secret-key", response.text)
        self.assertEqual(payload["config"]["BRAND_NAME"], "Oussama News")

    def test_export_with_secrets_is_explicit(self):
        response = self.post("/api/config/export", {"include_secrets": True})
        self.assertEqual(response.status_code, 200)
        payload = response.json()["payload"]
        self.assertTrue(payload["secrets_included"])
        self.assertEqual(payload["secrets"]["GEMINI_API_KEY"], "very-secret-key")

    def test_partial_import_updates_values(self):
        response = self.post(
            "/api/config/import",
            {
                "payload": {
                    "app": "live-relay-config",
                    "config": {
                        "BRAND_NAME": "New Brand",
                        "THUMBNAIL_MODE": "local",
                    },
                }
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["applied"], 2)
        self.assertEqual(body["config"]["brand_name"], "New Brand")
        self.assertIn("New Brand", self.env_path.read_text(encoding="utf-8"))

    def test_secret_import_is_applied(self):
        response = self.post(
            "/api/config/import",
            {
                "payload": {
                    "secrets": {"GEMINI_API_KEY": "another-secret-key"}
                }
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["config"]["gemini_api_key_set"])
        self.assertIn(
            "another-secret-key", self.env_path.read_text(encoding="utf-8")
        )

    def test_unknown_key_is_rejected_without_touching_env(self):
        before = self.env_path.read_bytes()
        response = self.post(
            "/api/config/import",
            {"payload": {"config": {"BRAND_NAME": "x", "EVIL": "y"}}},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.env_path.read_bytes(), before)

    def test_invalid_result_is_rolled_back(self):
        before = self.env_path.read_bytes()
        response = self.post(
            "/api/config/import",
            {
                "payload": {
                    "config": {"LIVE_PLATFORMS": "youtube"},
                    "secrets": {"YOUTUBE_STREAM_KEY": ""},
                }
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.env_path.read_bytes(), before)

    def test_import_is_refused_while_streaming(self):
        self.controller._relay_thread = Mock()
        self.controller._relay_thread.is_alive.return_value = True
        response = self.post(
            "/api/config/import", {"payload": {"config": {"BRAND_NAME": "x"}}}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("أوقف البث", response.json()["error"])

    def test_endpoints_require_the_token(self):
        for path in ("/api/config/export", "/api/config/import"):
            response = requests.post(self.url(path), json={}, timeout=5)
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
