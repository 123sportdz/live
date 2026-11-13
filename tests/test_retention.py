from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from live_relay.retention import prune_from_settings, prune_recordings, usage


def make_recording(directory: Path, name: str, size: int, age_days: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x" * size)
    moment = time.time() - age_days * 86400
    os.utime(path, (moment, moment))
    return path


class RetentionTests(unittest.TestCase):
    def test_old_files_are_deleted_and_recent_ones_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            recordings = state_dir / "recordings"
            old = make_recording(recordings, "old.mp4", 100, 20)
            recent = make_recording(recordings, "recent.mp4", 100, 2)
            result = prune_recordings(state_dir, retention_days=7)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertEqual(result.deleted, ["old.mp4"])
            self.assertEqual(result.kept, 1)

    def test_active_recording_is_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            recordings = state_dir / "recordings"
            active = make_recording(recordings, "active.mp4", 100, 30)
            result = prune_recordings(
                state_dir, retention_days=1, active_path=active
            )
            self.assertTrue(active.exists())
            self.assertEqual(result.deleted, [])

    def test_quota_removes_the_oldest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            recordings = state_dir / "recordings"
            first = make_recording(recordings, "a.mp4", 400, 3)
            second = make_recording(recordings, "b.mp4", 400, 2)
            third = make_recording(recordings, "c.mp4", 400, 1)
            result = prune_recordings(
                state_dir, retention_days=0, max_bytes=900
            )
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertTrue(third.exists())
            self.assertLessEqual(usage(state_dir)[1], 900)
            self.assertIn("a.mp4", result.deleted)

    def test_non_mp4_files_are_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            recordings = state_dir / "recordings"
            recordings.mkdir(parents=True)
            note = recordings / "notes.txt"
            note.write_text("keep", encoding="utf-8")
            prune_recordings(state_dir, retention_days=1)
            self.assertTrue(note.exists())

    def test_zero_limits_disable_pruning(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            recordings = state_dir / "recordings"
            old = make_recording(recordings, "old.mp4", 100, 90)
            prune_recordings(state_dir, retention_days=0, max_bytes=0)
            self.assertTrue(old.exists())

    def test_settings_wrapper_reads_values(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            recordings = state_dir / "recordings"
            old = make_recording(recordings, "old.mp4", 100, 30)
            settings = SimpleNamespace(
                state_dir=state_dir,
                recording_retention_days=7,
                recording_max_gb=0,
            )
            prune_from_settings(settings)
            self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
