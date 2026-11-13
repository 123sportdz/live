from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from live_relay import dashboard as dashboard_module
from live_relay.dashboard import PROJECT_ROOT, DashboardController, build_handler_class
from live_relay.destinations import DestinationSession
from live_relay.metadata import StreamMetadata
from live_relay.monetization import SessionFacts, audit_session, record_session
from live_relay.supervisor import _record_restart, write_heartbeat

DESCRIPTION = (
    "أعلنت وزارة الخارجية اليوم أن المحادثات ستستأنف غداً في جنيف. "
    "وقال المتحدث إن الوفدين اتفقا على جدول أعمال من ثلاث نقاط. "
    "وتأتي الجلسة بعد أسبوعين من تعليق المشاركة."
)


class NewsroomApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix=".dashboard-news-", dir=PROJECT_ROOT)
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

    def post(self, path: str, payload: dict | None = None) -> requests.Response:
        return requests.post(
            self.url(path), json=payload or {}, headers=self.headers(), timeout=5
        )

    def live_session(self) -> DestinationSession:
        session = DestinationSession(
            targets=[("youtube", "rtmp://example/live")],
            youtube=Mock(),
            video_id="vid123",
            session_started_at=time.time() - 400,
            metadata=StreamMetadata(
                title="تغطية مباشرة", description="وصف البث", tags=("أخبار",)
            ),
        )
        self.controller.status.set_destination_session(session)
        return session

    def test_status_exposes_heartbeat_and_restarts(self):
        write_heartbeat(self.state_dir, mode="streaming")
        _record_restart(self.state_dir, attempt=1, reason="boom", delay=5)
        status = requests.get(self.url("/api/status"), timeout=5).json()
        self.assertIsNotNone(status["heartbeat_age_seconds"])
        self.assertGreaterEqual(status["heartbeat_age_seconds"], 0)
        self.assertEqual(status["restarts"][-1]["attempt"], 1)

    def test_weekly_report_endpoint_returns_markdown(self):
        facts = SessionFacts(
            title="عنوان الحلقة",
            description=DESCRIPTION,
            source_channel="قناة الحدث",
            rights="licensed",
            transform="editorial",
            duration_seconds=600,
            original_segments=2,
        )
        record_session(
            self.state_dir,
            video_id="vid-report",
            started_at=time.time() - 600,
            ended_at=time.time(),
            facts=facts,
            audit=audit_session(facts),
            platforms=["youtube"],
            end_reason="test",
        )
        response = requests.get(self.url("/api/reports/weekly"), timeout=5)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("تقرير التشغيل", body["markdown"])
        self.assertIn("vid-report", body["markdown"])
        self.assertTrue(Path(body["path"]).is_file())

    def test_metadata_suggest_rewrites_and_returns_candidates(self):
        response = self.post(
            "/api/metadata/suggest",
            {
                "title": "عاجل: قصف عنيف على مدينة غزة الآن",
                "description": DESCRIPTION,
                "tags": "غزة, قصف, أخبار",
                "channel": "قناة الحدث",
                "language": "ar",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotEqual(body["title"], "عاجل: قصف عنيف على مدينة غزة الآن")
        self.assertTrue(body["candidates"])
        self.assertIn("قناة الحدث", body["description"])
        self.assertTrue(body["keywords"])
        self.assertIn("غزة", body["keywords"])

    def test_metadata_suggest_includes_live_chapters(self):
        session = self.live_session()
        session.metadata = StreamMetadata(
            title="تغطية مباشرة", description=DESCRIPTION, tags=("أخبار",)
        )
        self.post("/api/session/marker", {"title": "تحليل أول", "seconds": 60})
        self.post("/api/session/marker", {"title": "تحليل ثان", "seconds": 200})
        response = self.post(
            "/api/metadata/suggest",
            {
                "title": "استئناف المحادثات في جنيف",
                "description": DESCRIPTION,
                "channel": "قناة الحدث",
                "language": "ar",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("⏱", response.json()["description"])

    def test_metadata_suggest_requires_the_local_token(self):
        response = requests.post(
            self.url("/api/metadata/suggest"),
            json={"title": "عنوان"},
            timeout=5,
        )
        self.assertEqual(response.status_code, 403)

    def test_brief_endpoint_returns_and_stores_markdown(self):
        response = self.post(
            "/api/newsroom/brief",
            {
                "title": "استئناف المحادثات في جنيف",
                "description": DESCRIPTION,
                "source_channel": "قناة الحدث",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("ما وقع", body["markdown"])
        for fact in body["brief"]["facts"]:
            self.assertIn(fact, DESCRIPTION)
        self.assertTrue(Path(body["path"]).is_file())

    def test_markers_require_a_live_session(self):
        response = self.post("/api/session/marker", {"title": "تحليل"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("بث نشط", response.json()["error"])

    def test_marker_add_and_remove(self):
        self.live_session()
        added = self.post("/api/session/marker", {"title": "قراءة في البيان"})
        self.assertEqual(added.status_code, 200, added.text)
        body = added.json()
        self.assertEqual(len(body["markers"]), 1)
        self.assertGreaterEqual(body["markers"][0]["seconds"], 390)
        listed = requests.get(self.url("/api/session/markers"), timeout=5).json()
        self.assertEqual(len(listed["markers"]), 1)
        removed = self.post("/api/session/marker/remove", {"index": 0})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["markers"], [])

    def test_chapters_are_written_into_the_description(self):
        session = self.live_session()
        self.post("/api/session/marker", {"title": "تحليل أول", "seconds": 60})
        self.post("/api/session/marker", {"title": "تحليل ثان", "seconds": 120})
        with patch(
            "live_relay.service.update_destination_metadata",
            return_value={"youtube": "updated"},
        ) as update:
            response = self.post("/api/session/chapters")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["updated"])
        self.assertEqual(len(body["chapters"]), 3)
        self.assertIn("⏱ الفصول", session.metadata.description)
        self.assertIn("00:00", session.metadata.description)
        update.assert_called_once()

    def test_static_mode_returns_the_chapters_for_manual_paste(self):
        session = DestinationSession(
            targets=[("youtube", "rtmp://example/live")],
            video_id="vid-static",
            session_started_at=time.time() - 600,
            metadata=StreamMetadata(title="تغطية", description="وصف", tags=()),
        )
        self.controller.status.set_destination_session(session)
        self.post("/api/session/marker", {"title": "تحليل أول", "seconds": 60})
        self.post("/api/session/marker", {"title": "تحليل ثان", "seconds": 200})
        response = self.post("/api/session/chapters")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["updated"])
        self.assertEqual(body["reason"], "manual-mode")
        self.assertIn("Studio", body["message"])
        self.assertEqual(len(body["chapters"]), 3)

    def test_chapters_need_two_markers(self):
        self.live_session()
        self.post("/api/session/marker", {"title": "تحليل وحيد"})
        response = self.post("/api/session/chapters")
        self.assertEqual(response.status_code, 400)
        self.assertIn("مقطعين", response.json()["error"])

    def test_kit_needs_a_session_then_returns_markdown(self):
        empty = self.post("/api/sessions/kit")
        self.assertEqual(empty.status_code, 400)
        facts = SessionFacts(
            title="عنوان الحلقة",
            description=DESCRIPTION,
            source_channel="قناة الحدث",
            rights="licensed",
            transform="editorial",
            duration_seconds=600,
            original_segments=2,
        )
        record_session(
            self.state_dir,
            video_id="vid123",
            started_at=time.time() - 600,
            ended_at=time.time(),
            facts=facts,
            audit=audit_session(facts),
            platforms=["youtube"],
            end_reason="test",
        )
        response = self.post("/api/sessions/kit")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("ملف الحلقة", body["markdown"])
        self.assertIn("vid123", body["markdown"])
        self.assertTrue(Path(body["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
