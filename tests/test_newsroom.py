from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from live_relay.destinations import DestinationSession
from live_relay.service import add_chapter_event, add_editorial_marker
from live_relay.newsroom import (
    Brief,
    build_brief,
    build_session_kit,
    chapter_block,
    fingerprint,
    latest_markdown,
    merge_chapters_into_description,
    normalize_arabic,
    render_brief_markdown,
    split_sentences,
    summarize_markers,
    write_brief,
    write_kit,
)

DESCRIPTION = (
    "أعلنت وزارة الخارجية اليوم أن المحادثات ستستأنف غداً في جنيف. "
    "وقال المتحدث إن الوفدين اتفقا على جدول أعمال من ثلاث نقاط. "
    "وتأتي الجلسة بعد أسبوعين من تعليق المشاركة. "
    "تابعونا للحصول على آخر الأخبار."
)


class SentenceTests(unittest.TestCase):
    def test_short_and_noisy_lines_are_dropped(self):
        sentences = split_sentences(
            "قصير. " + DESCRIPTION + " اشترك في القناة. https://example.com/x"
        )
        self.assertTrue(sentences)
        self.assertFalse(any("اشترك" in item for item in sentences))
        self.assertFalse(any("http" in item for item in sentences))

    def test_normalisation_folds_arabic_variants(self):
        self.assertEqual(normalize_arabic("أحمد"), normalize_arabic("احمد"))
        self.assertEqual(normalize_arabic("مدرسة"), normalize_arabic("مدرسه"))


class BriefTests(unittest.TestCase):
    def test_every_fact_is_quoted_from_the_source(self):
        brief = build_brief(
            "استئناف المحادثات في جنيف",
            DESCRIPTION,
            "قناة الحدث",
            "youtube",
        )
        self.assertTrue(brief.usable)
        for fact in brief.facts:
            self.assertIn(fact, DESCRIPTION)
        self.assertIn("قناة الحدث", render_brief_markdown(brief))
        self.assertIn("ما وقع", render_brief_markdown(brief))

    def test_facts_keep_the_source_order(self):
        brief = build_brief("عنوان", DESCRIPTION)
        positions = [DESCRIPTION.index(fact) for fact in brief.facts]
        self.assertEqual(positions, sorted(positions))

    def test_empty_description_is_admitted_not_invented(self):
        brief = build_brief("عنوان", "", "قناة")
        self.assertFalse(brief.usable)
        self.assertIn("وصف المصدر فارغ", brief.note)
        self.assertTrue(render_brief_markdown(brief))

    def test_brief_dict_is_json_ready(self):
        brief = build_brief("عنوان", DESCRIPTION)
        payload = brief.as_dict()
        self.assertIsInstance(payload["facts"], list)
        self.assertIsInstance(payload["talking_points"], list)
        self.assertGreaterEqual(len(payload["talking_points"]), 3)

    def test_fingerprint_is_stable(self):
        self.assertEqual(fingerprint("abc"), fingerprint("abc"))
        self.assertEqual(len(fingerprint("abc")), 16)


class ChapterTests(unittest.TestCase):
    def test_block_always_starts_at_zero(self):
        lines = chapter_block([(120, "تحليل"), (400, "تحديث")])
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("00:00"))
        self.assertIn("02:00 تحليل", lines[1])

    def test_markers_are_sorted_and_formatted(self):
        markers = summarize_markers(
            [{"seconds": 120, "title": "ب"}, {"seconds": 5, "title": "أ"}]
        )
        self.assertEqual([item["timestamp"] for item in markers], ["00:05", "02:00"])


class MarkerTests(unittest.TestCase):
    def session(self):
        return DestinationSession(session_started_at=time.time() - 600)

    def test_editorial_marker_is_timestamped(self):
        session = self.session()
        event = add_editorial_marker(session, "تحليل الوضع الميداني")
        self.assertGreaterEqual(event["seconds"], 590)
        self.assertEqual(len(session.editorial_events), 1)

    def test_auto_chapter_rejects_noise_and_duplicates(self):
        session = self.session()
        self.assertIsNone(add_chapter_event(session, "خبر"))
        first = add_chapter_event(session, "استئناف المحادثات في جنيف", 100)
        self.assertIsNotNone(first)
        self.assertIsNone(add_chapter_event(session, "استئناف المحادثات في جنيف", 200))
        self.assertIsNone(add_chapter_event(session, "عنوان جديد تماماً", 130))
        second = add_chapter_event(session, "عنوان جديد تماماً", 300)
        self.assertIsNotNone(second)
        self.assertEqual(len(session.chapter_events), 2)

    def test_auto_events_do_not_count_as_original_segments(self):
        session = self.session()
        add_chapter_event(session, "استئناف المحادثات في جنيف", 100)
        add_chapter_event(session, "وصول الوفد إلى القاعة", 300)
        combined = [*session.chapter_events, *session.editorial_events]
        lines = chapter_block(combined)
        self.assertEqual(len(lines), 3)
        self.assertEqual(len(session.editorial_events), 0)


class DescriptionTests(unittest.TestCase):
    def test_chapters_are_appended_once(self):
        first = merge_chapters_into_description("وصف", ["00:00 بداية", "02:00 تحليل"])
        self.assertIn("⏱ الفصول", first)
        second = merge_chapters_into_description(first, ["00:00 بداية", "02:00 تحليل"])
        self.assertEqual(second, first)

    def test_a_new_block_replaces_the_old_one(self):
        first = merge_chapters_into_description("وصف", ["00:00 بداية", "01:00 قديم"])
        second = merge_chapters_into_description(first, ["00:00 بداية", "02:00 جديد"])
        self.assertEqual(second.count("⏱ الفصول"), 1)
        self.assertIn("02:00 جديد", second)
        self.assertNotIn("01:00 قديم", second)

    def test_long_description_keeps_the_chapter_block(self):
        chapters = ["00:00 بداية", "10:00 تحليل", "20:00 تحديث"]
        merged = merge_chapters_into_description("ا" * 4990, chapters, limit=5000)
        self.assertLessEqual(len(merged), 5000)
        for line in chapters:
            self.assertIn(line, merged)


class KitTests(unittest.TestCase):
    def test_kit_contains_the_five_sections(self):
        brief = build_brief("عنوان الحلقة", DESCRIPTION, "قناة الحدث")
        record = {
            "video_id": "abc123",
            "duration_seconds": 3720,
            "platforms": ["youtube"],
            "score": 62,
            "band": "at_risk",
            "risk_codes": ["sensitive_topic"],
            "end_reason": "source-offline",
            "recording": "/tmp/session.mp4",
        }
        kit = build_session_kit(
            record,
            brief=brief,
            description="وصف",
            chapters=["00:00 بداية", "02:00 تحليل"],
            checklist=[{"label": "مقدمة", "detail": "20 ثانية", "done": True}],
        )
        for needle in ("ملف الحلقة", "abc123", "الموجز التحريري", "الفصول", "الوصف المقترح", "التسجيل"):
            self.assertIn(needle, kit)
        self.assertIn("[x] مقدمة", kit)
        self.assertIn("1س 02د", kit)

    def test_files_are_written_privately_and_latest_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            brief = Brief(title="t", source_channel="c", source_platform="custom")
            first = write_brief(state_dir, brief, "20260101-000000")
            time.sleep(0.01)
            second = write_brief(state_dir, brief, "20260101-000001")
            self.assertTrue(first.is_file() and second.is_file())
            if os.name != "nt":
                self.assertEqual(second.stat().st_mode & 0o777, 0o600)
            latest = latest_markdown(state_dir, "briefs")
            self.assertIsNotNone(latest)
            self.assertEqual(latest[0], second)

    def test_kit_path_is_sanitised(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            path = write_kit(
                state_dir,
                {"ended_at": time.time(), "video_id": "../../etc/passwd"},
                "hello",
            )
            self.assertTrue(path.is_file())
            self.assertNotIn("..", path.name)


if __name__ == "__main__":
    unittest.main()
