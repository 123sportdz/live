from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from live_relay.pipeline import (
    PipelineConfig,
    PipelineStore,
    PipelineWorker,
    automation_warnings,
    build_episode_manifest,
    scan_recordings,
)


def write_env(root: Path, **values: str) -> Path:
    path = root / ".env"
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return path


class PipelineConfigTests(unittest.TestCase):
    def test_defaults_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_env(root, LIVE_RELAY_STATE_DIR=str(root / "state"))
            config = PipelineConfig.from_env(root)
        self.assertFalse(config.enabled)
        self.assertTrue(config.auto_clips)
        self.assertTrue(config.auto_episode)
        self.assertFalse(config.auto_upload)
        self.assertEqual(config.upload_privacy, "private")
        self.assertEqual(config.upload_max, 3)

    def test_invalid_values_are_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_env(
                root,
                LIVE_RELAY_STATE_DIR=str(root / "state"),
                AUTO_UPLOAD_PRIVACY="friends",
                AUTO_UPLOAD_MAX="99",
                PIPELINE_MIN_FREE_GB="nope",
                AUTO_EPISODE_MAX_SEGMENTS="99",
            )
            config = PipelineConfig.from_env(root)
        self.assertEqual(config.upload_privacy, "private")
        self.assertEqual(config.upload_max, 10)
        self.assertEqual(config.min_free_gb, 5)
        self.assertEqual(config.episode_max_segments, 20)


class PipelineStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.recording = self.root / "session.mp4"
        self.recording.write_bytes(b"")
        self.store = PipelineStore(self.root / "state")

    def tearDown(self):
        self._tmp.cleanup()

    def test_enqueue_is_idempotent(self):
        first = self.store.enqueue(self.recording)
        second = self.store.enqueue(self.recording)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.jobs()), 1)

    def test_manual_retry_resets_a_failed_job(self):
        job = self.store.enqueue(self.recording)
        self.store.update(job["id"], status="failed", attempts=3, error="boom")
        again = self.store.enqueue(self.recording, manual=True)
        self.assertEqual(again["id"], job["id"])
        self.assertEqual(again["status"], "pending")
        self.assertEqual(again["attempts"], 0)
        self.assertTrue(again["manual"])

    def test_manual_run_reopens_a_done_job(self):
        job = self.store.enqueue(self.recording)
        self.store.update(job["id"], status="done", stage="انتهى")
        again = self.store.enqueue(self.recording, manual=True)
        self.assertEqual(again["id"], job["id"])
        self.assertEqual(again["status"], "pending")
        self.assertEqual(again["attempts"], 0)

    def test_failed_jobs_are_requeued_automatically(self):
        job = self.store.enqueue(self.recording)
        self.store.update(
            job["id"], status="failed", attempts=1, updated_at=1000
        )
        self.assertEqual(
            self.store.requeue_failed(3, delay=300, now=2000), 1
        )
        again = self.store.get(job["id"])
        self.assertEqual(again["status"], "pending")
        self.assertEqual(again["stage"], "إعادة تلقائية")

        self.store.update(job["id"], status="failed", attempts=3, updated_at=1000)
        self.assertEqual(
            self.store.requeue_failed(3, delay=300, now=2000), 0
        )

    def test_retry_sets_manual(self):
        job = self.store.enqueue(self.recording)
        self.store.update(job["id"], status="failed")
        retried = self.store.retry(job["id"])
        self.assertEqual(retried["status"], "pending")
        self.assertTrue(retried["manual"])

    def test_scan_ignores_old_files_on_first_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            recordings = state / "recordings"
            recordings.mkdir()
            old = recordings / "old.mp4"
            old.write_bytes(b"")
            os.utime(old, (500, 500))
            store = PipelineStore(state)
            self.assertEqual(scan_recordings(store, state, now=1000), 0)
            self.assertEqual(len(store.jobs()), 0)

    def test_scan_picks_files_newer_than_the_last_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            recordings = state / "recordings"
            recordings.mkdir()
            store = PipelineStore(state)
            scan_recordings(store, state, now=1000)
            fresh = recordings / "fresh.mp4"
            fresh.write_bytes(b"")
            os.utime(fresh, (1200, 1200))
            self.assertEqual(scan_recordings(store, state, now=2000), 1)
            self.assertEqual(len(store.jobs()), 1)


class ManifestTests(unittest.TestCase):
    def test_manifest_uses_non_blocked_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            write_env(
                root,
                LIVE_RELAY_STATE_DIR=str(state),
                BRAND_NAME="Test",
                SOURCE_RIGHTS="original",
            )
            config = PipelineConfig.from_env(root)
            recording = root / "session.mp4"
            recording.write_bytes(b"")
            job = {
                "id": "abc",
                "ended_at": 1_760_000_000,
                "clips": [
                    {
                        "title": "الثاني",
                        "start": 60,
                        "duration": 30,
                        "reason": "سبب",
                        "blocked": False,
                    },
                    {
                        "title": "الأول",
                        "start": 10,
                        "duration": 20,
                        "reason": "سبب",
                        "blocked": False,
                    },
                    {"title": "ممنوع", "start": 0, "duration": 10, "blocked": True},
                ],
            }
            path = build_episode_manifest(job, config, recording)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["segments"][0]["heading"], "الأول")
        self.assertEqual(payload["segments"][1]["heading"], "الثاني")
        self.assertEqual(len(payload["segments"]), 2)
        self.assertEqual(payload["brand"], "Test")

    def test_no_clips_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_env(root, LIVE_RELAY_STATE_DIR=str(root / "state"))
            config = PipelineConfig.from_env(root)
            with self.assertRaises(Exception):
                build_episode_manifest({"id": "x", "clips": []}, config, root / "x.mp4")


class WorkerTests(unittest.TestCase):
    def test_all_stages_disabled_finishes_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            recording = state / "recordings" / "session.mp4"
            recording.parent.mkdir(parents=True)
            recording.write_bytes(b"")
            write_env(
                root,
                LIVE_RELAY_STATE_DIR=str(state),
                AUTOPILOT_ENABLED="true",
                AUTO_CLIPS="false",
                AUTO_EPISODE="false",
                AUTO_UPLOAD="false",
            )
            worker = PipelineWorker(root, state)
            job = worker.store.enqueue(recording, manual=True)
            result = worker.process_now(job["id"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["stages"], {"clips": "disabled", "episode": "disabled", "upload": "disabled"})

    def test_full_stage_orchestration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            home = root / "cutter"
            home.mkdir()
            (home / "main_improved.py").write_text("", encoding="utf-8")
            recording = state / "recordings" / "session.mp4"
            recording.parent.mkdir(parents=True)
            recording.write_bytes(b"")
            clip_file = root / "clip.mp4"
            clip_file.write_bytes(b"clip")
            token = state / "youtube-token.json"
            token.write_text(
                json.dumps({"scopes": ["https://www.googleapis.com/auth/youtube.force-ssl"]}),
                encoding="utf-8",
            )
            write_env(
                root,
                LIVE_RELAY_STATE_DIR=str(state),
                AUTOPILOT_ENABLED="true",
                AUTO_CLIPS="true",
                AUTO_EPISODE="true",
                AUTO_UPLOAD="true",
                AUTO_UPLOAD_MAX="3",
                BRAND_NAME="Test",
                SOURCE_RIGHTS="original",
                CUTTER_ENABLED="true",
                CUTTER_HOME=str(home),
                CUTTER_PYTHON=sys.executable,
            )

            class FakeCutter:
                def __init__(self, settings):
                    self.settings = settings

                def available(self):
                    return True, ""

                def run(self, recording, log=None):
                    return SimpleNamespace(
                        project=home / "VIRALS" / "session",
                        clips=[
                            SimpleNamespace(
                                as_dict=lambda: {
                                    "title": "مقطع",
                                    "start": 10,
                                    "duration": 20,
                                    "reason": "سبب",
                                    "score": 90,
                                    "blocked": False,
                                    "file": str(clip_file),
                                    "caption": "وصف",
                                    "hashtags": ["أخبار"],
                                }
                            )
                        ],
                    )

            fake_plan = SimpleNamespace(date="2026-11-14", title="Test — 2026-11-14")

            def fake_render(plan, output, progress=None):
                output = Path(output)
                output.mkdir(parents=True, exist_ok=True)
                (output / "episode.mp4").write_bytes(b"episode")
                (output / "description.md").write_text("وصف الحلقة", encoding="utf-8")
                return {"duration_seconds": 40}

            uploaded: list[str] = []

            def fake_upload(path, **kwargs):
                uploaded.append(str(path))
                return {
                    "video_id": f"v{len(uploaded)}",
                    "url": "https://youtu.be/x",
                    "privacy": kwargs.get("privacy", "private"),
                }

            worker = PipelineWorker(root, state)
            job = worker.store.enqueue(recording, manual=True)
            with unittest.mock.patch("live_relay.pipeline.CutterClient", FakeCutter), \
                    unittest.mock.patch("live_relay.pipeline.load_manifest", return_value=fake_plan), \
                    unittest.mock.patch("live_relay.pipeline.render_episode", side_effect=fake_render), \
                    unittest.mock.patch("live_relay.pipeline.has_upload_scope", return_value=True), \
                    unittest.mock.patch("live_relay.pipeline.upload_video", side_effect=fake_upload):
                result = worker.process_now(job["id"])
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(len(result["clips"]), 1)
        self.assertTrue(result["episode"].endswith("episode.mp4"))
        self.assertEqual(len(result["uploads"]), 2)
        self.assertEqual(uploaded[0], result["episode"])


class WorkerLockTests(unittest.TestCase):
    def test_a_second_worker_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            write_env(root, LIVE_RELAY_STATE_DIR=str(state))
            first = PipelineWorker(root, state)
            second = PipelineWorker(root, state)
            first.start()
            try:
                second.start()
                self.assertIn("آخر", second.status()["blocked_reason"])
            finally:
                first.stop()
            second.start()
            try:
                self.assertFalse(second.status()["blocked_reason"])
            finally:
                second.stop()


class WarningTests(unittest.TestCase):
    def test_disabled_config_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_env(root, LIVE_RELAY_STATE_DIR=str(root / "state"))
            config = PipelineConfig.from_env(root)
            warnings = automation_warnings(config)
        self.assertTrue(any("متوقفة" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
