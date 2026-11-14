from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from live_relay.cutter import (
    CutterClient,
    CutterError,
    CutterSettings,
    clips_from_project,
    latest_recording,
    list_projects,
)


class CutterSettingsTests(unittest.TestCase):
    def test_defaults_and_clamping(self):
        settings = CutterSettings.from_values(
            {
                "CUTTER_ENABLED": "true",
                "CUTTER_HOME": "/tmp/cqt",
                "CUTTER_SEGMENTS": "99",
                "CUTTER_AI_BACKEND": "nope",
                "CUTTER_PLATFORM": "nope",
            }
        )
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.segments, 20)
        self.assertEqual(settings.ai_backend, "gemini")
        self.assertEqual(settings.platform, "tiktok")
        self.assertEqual(settings.workflow, 1)

    def test_command_contains_the_contract(self):
        settings = CutterSettings(
            enabled=True,
            home=Path("/tmp/cqt"),
            python=Path("/tmp/cqt/.venv/bin/python"),
            segments=4,
            platform="yt_shorts",
            ai_backend="manual",
        )
        command = CutterClient(settings).build_command(
            Path("/tmp/session.mp4"), workflow=1
        )
        joined = " ".join(command)
        self.assertIn("main_improved.py", joined)
        self.assertIn("--local-video /tmp/session.mp4", joined)
        self.assertIn("--segments 4", joined)
        self.assertIn("--platform yt_shorts", joined)
        self.assertIn("--workflow 1", joined)
        self.assertIn("--ai-backend manual", joined)
        self.assertIn("--skip-prompts", joined)

    def test_available_reports_missing_home(self):
        settings = CutterSettings(
            enabled=True, home=Path("/does/not/exist"), python=Path("python")
        )
        ok, message = CutterClient(settings).available()
        self.assertFalse(ok)
        self.assertIn("غير موجود", message)


class CutterParsingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cutter-test-")
        self.home = Path(self._tmp.name)
        self.project = self.home / "VIRALS" / "session"
        (self.project / "final").mkdir(parents=True)
        payload = {
            "segments": [
                {
                    "title": "raw one",
                    "recommended_title": "العنوان الأول",
                    "start_time": 10,
                    "end_time": 70,
                    "duration": 60,
                    "selection_score": 88.5,
                    "reasoning": "خطّاف قوي وسؤال واضح",
                    "caption": "وصف",
                    "hashtags": ["#أخبار", "#تحليل"],
                    "quality_status": "verified",
                },
                {
                    "title": "الثاني",
                    "start_time": 80,
                    "end_time": 140,
                    "durarion": 60,
                    "score": 71,
                },
            ]
        }
        (self.project / "viral_segments.txt").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        (self.project / "final" / "000_one.mp4").write_bytes(b"")
        (self.project / "final" / "001_two.mp4").write_bytes(b"")
        (self.project / "publish_blocklist.json").write_text(
            json.dumps({"blocked": [{"index": 1}]}), encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_clips_are_mapped_to_files_and_blocklist(self):
        clips = clips_from_project(self.project)
        self.assertEqual(len(clips), 2)
        self.assertEqual(clips[0].title, "العنوان الأول")
        self.assertEqual(clips[0].score, 88.5)
        self.assertEqual(clips[0].duration, 60)
        self.assertTrue(clips[0].file.endswith("000_one.mp4"))
        self.assertFalse(clips[0].blocked)
        self.assertEqual(clips[1].hashtags, ())
        self.assertTrue(clips[1].blocked)
        self.assertTrue(clips[1].file.endswith("001_two.mp4"))

    def test_delivery_manifest_controls_file_order(self):
        (self.project / "delivery_manifest.json").write_text(
            json.dumps(
                {
                    "outputs": [
                        str(self.project / "final" / "001_two.mp4"),
                        str(self.project / "final" / "000_one.mp4"),
                    ]
                }
            ),
            encoding="utf-8",
        )
        clips = clips_from_project(self.project)
        self.assertTrue(clips[0].file.endswith("001_two.mp4"))
        self.assertTrue(clips[1].file.endswith("000_one.mp4"))

    def test_list_projects(self):
        projects = list_projects(self.home)
        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["name"], "session")
        self.assertEqual(projects[0]["clips"], 2)

    def test_broken_segments_file_is_an_error(self):
        (self.project / "viral_segments.txt").write_text("{", encoding="utf-8")
        with self.assertRaises(CutterError):
            clips_from_project(self.project)


class RecordingDiscoveryTests(unittest.TestCase):
    def test_latest_recording_skips_the_active_file(self):
        with tempfile.TemporaryDirectory(prefix="cutter-rec-") as tmp:
            folder = Path(tmp) / "recordings"
            folder.mkdir()
            old = folder / "old.mp4"
            fresh = folder / "fresh.mp4"
            old.write_bytes(b"")
            fresh.write_bytes(b"")
            os.utime(old, (time.time() - 3600, time.time() - 3600))
            self.assertEqual(latest_recording(Path(tmp)), old)


if __name__ == "__main__":
    unittest.main()
