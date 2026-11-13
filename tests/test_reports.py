from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from live_relay.monetization import session_path
from live_relay.reports import (
    build_report,
    recommendations,
    summarize_sessions,
    write_weekly_report,
)


def record(**overrides):
    base = {
        "timestamp": time.time() - 3600,
        "ended_at": time.time() - 3600,
        "duration_seconds": 3600,
        "score": 70,
        "band": "conditional",
        "editorial_segments": 3,
        "risk_codes": ["sensitive_topic"],
        "video_id": "vid1",
    }
    base.update(overrides)
    return base


class SummaryTests(unittest.TestCase):
    def test_totals_and_averages(self):
        summary = summarize_sessions(
            [
                record(duration_seconds=3600, score=80, editorial_segments=2),
                record(duration_seconds=1800, score=40, band="blocked", editorial_segments=0),
            ]
        )
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["total_seconds"], 5400)
        self.assertEqual(summary["average_score"], 60)
        self.assertEqual(summary["segments"], 2)
        self.assertEqual(summary["bands"]["blocked"], 1)
        self.assertEqual(summary["segments_per_hour"], 1.33)
        self.assertEqual(summary["sessions_without_segments"], 1)

    def test_old_records_are_ignored(self):
        old = record(timestamp=time.time() - 30 * 86400, ended_at=time.time() - 30 * 86400)
        summary = summarize_sessions([old, record()], days=7)
        self.assertEqual(summary["count"], 1)

    def test_empty_period_is_admitted(self):
        summary = summarize_sessions([])
        self.assertEqual(summary["count"], 0)
        self.assertIsNone(summary["average_score"])
        self.assertTrue(recommendations(summary))


class ReportTests(unittest.TestCase):
    def test_report_contains_totals_bands_and_a_table(self):
        report = build_report(
            [
                record(video_id="abc123"),
                record(score=30, band="blocked", risk_codes=["reused_content"], video_id="def456"),
            ]
        )
        self.assertIn("تقرير التشغيل", report)
        self.assertIn("إجمالي وقت البث", report)
        self.assertIn("غير مؤهل على الأرجح", report)
        self.assertIn("reused_content", report)
        self.assertIn("`abc123`", report)
        self.assertIn("## التوصيات", report)

    def test_blocked_sessions_and_low_segments_are_called_out(self):
        report = build_report(
            [record(band="blocked", risk_codes=["reused_content"], editorial_segments=0)]
        )
        self.assertIn("راجع حقوق المصدر", report)
        self.assertIn("أقل من واحد لكل ساعة", report)

    def test_empty_report_is_clear(self):
        report = build_report([])
        self.assertIn("لا توجد جلسات", report)
        self.assertIn("عدد الجلسات:** 0", report)

    def test_write_creates_a_private_markdown_file(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            path = session_path(state_dir)
            state_dir.mkdir(exist_ok=True)
            path.write_text("", encoding="utf-8")
            written = write_weekly_report(state_dir)
            self.assertTrue(written.is_file())
            self.assertTrue(written.name.startswith("weekly-"))
            self.assertTrue(written.read_text(encoding="utf-8").startswith("# تقرير"))
            if os.name != "nt":
                self.assertEqual(written.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
