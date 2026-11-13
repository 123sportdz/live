from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_relay.config import load_settings
from live_relay.destinations import (
    DestinationSession,
    YouTubeDestination,
    build_broadcast_body,
)
from live_relay.metadata import StreamMetadata
from live_relay.monetization import (
    SessionFacts,
    audit_session,
    build_chapters,
    build_transform_plan,
    facts_from_settings,
    read_jsonl,
    record_audit,
    record_session,
    render_plan_markdown,
    sensitive_terms_in,
    summarize,
)
from live_relay.service import _finalize_session

GOOD_TITLE = "تغطية مباشرة لاجتماع مجلس الأمن مع تحليل ميداني"
GOOD_DESCRIPTION = (
    "تغطية مباشرة من فريق التحرير مع تعليق وتحليل. "
    "المصدر: قناة الحدث. نتابع ما يجري ونشرح السياق كل نصف ساعة."
)


class AuditTests(unittest.TestCase):
    def test_plain_relay_of_another_channel_is_blocked(self):
        audit = audit_session(
            {
                "title": GOOD_TITLE,
                "description": GOOD_DESCRIPTION,
                "source_channel": "قناة الحدث",
                "rights": "third_party",
                "transform": "branded",
            }
        )
        codes = [risk.code for risk in audit.risks]
        self.assertEqual(audit.band, "blocked")
        self.assertLessEqual(audit.score, 30)
        self.assertIn("reused_content", codes)
        self.assertIn("cosmetic_transformation", codes)
        self.assertEqual(audit.risks[0].severity, "blocker")

    def test_editorial_layer_removes_the_reuse_blocker(self):
        facts = SessionFacts(
            title=GOOD_TITLE,
            description=GOOD_DESCRIPTION,
            source_channel="قناة الحدث",
            rights="third_party",
            transform="editorial",
            duration_seconds=3600,
            original_segments=4,
        )
        audit = audit_session(facts)
        codes = [risk.code for risk in audit.risks]
        self.assertNotIn("reused_content", codes)
        self.assertNotIn("cosmetic_transformation", codes)
        self.assertEqual(audit.band, "ready")
        self.assertGreaterEqual(audit.score, 85)

    def test_editorial_without_enough_segments_is_flagged(self):
        audit = audit_session(
            {
                "title": GOOD_TITLE,
                "description": GOOD_DESCRIPTION,
                "source_channel": "قناة الحدث",
                "rights": "licensed",
                "transform": "editorial",
                "duration_seconds": 7200,
                "original_segments": 1,
            }
        )
        self.assertIn("thin_commentary", [risk.code for risk in audit.risks])

    def test_sensitive_news_is_explained_not_evaded(self):
        terms = sensitive_terms_in("قصف عنيف وضحايا في المدينة", "")
        self.assertIn("قصف", terms)
        audit = audit_session(
            {
                "title": "قصف عنيف وضحايا في المدينة",
                "description": GOOD_DESCRIPTION,
                "source_channel": "قناة الحدث",
                "rights": "licensed",
                "transform": "editorial",
                "duration_seconds": 1800,
                "original_segments": 2,
            }
        )
        risk = next(item for item in audit.risks if item.code == "sensitive_topic")
        self.assertEqual(risk.severity, "medium")
        self.assertIn("لا تعِد صياغة", risk.action)
        self.assertNotEqual(audit.band, "blocked")

    def test_third_party_music_is_a_high_risk(self):
        audit = audit_session(
            {
                "title": GOOD_TITLE,
                "description": GOOD_DESCRIPTION,
                "source_channel": "قناة الحدث",
                "rights": "licensed",
                "transform": "editorial",
                "duration_seconds": 1800,
                "original_segments": 2,
                "has_third_party_music": True,
            }
        )
        risk = next(item for item in audit.risks if item.code == "third_party_music")
        self.assertEqual(risk.severity, "high")

    def test_english_terms_match_whole_words_only(self):
        self.assertEqual(sensitive_terms_in("The award deadline is near", ""), [])
        self.assertEqual(
            sensitive_terms_in("War and death in the city", ""), ["war", "death"]
        )

    def test_unknown_rights_are_a_high_risk(self):
        audit = audit_session({"title": GOOD_TITLE, "rights": "unknown"})
        self.assertIn("unverified_rights", [risk.code for risk in audit.risks])

    def test_weak_metadata_is_reported_with_reasons(self):
        audit = audit_session({"title": "خبر", "description": ""})
        risk = next(item for item in audit.risks if item.code == "weak_metadata")
        self.assertIn("العنوان", risk.detail)

    def test_long_session_and_gaps_are_flagged(self):
        audit = audit_session(
            {
                "title": GOOD_TITLE,
                "description": GOOD_DESCRIPTION,
                "rights": "owned",
                "transform": "editorial",
                "duration_seconds": 8 * 3600,
                "original_segments": 20,
                "source_gaps": 3,
            }
        )
        codes = [risk.code for risk in audit.risks]
        self.assertIn("long_session", codes)
        self.assertIn("frozen_gaps", codes)

    def test_audit_is_json_ready_and_disclaims_certainty(self):
        payload = audit_session({"rights": "third_party"}).as_dict()
        self.assertIn("disclaimer", payload)
        self.assertIn("YouTube Studio", payload["disclaimer"])
        self.assertIsInstance(payload["risks"], list)
        json.dumps(payload, ensure_ascii=False)

    def test_facts_can_be_built_from_a_settings_namespace(self):
        settings = SimpleNamespace(
            source_rights="third_party",
            transform_level="editorial",
            source_third_party_music=True,
            youtube_category_id="25",
            youtube_privacy="public",
        )
        source = SimpleNamespace(
            title=GOOD_TITLE,
            description=GOOD_DESCRIPTION,
            channel="قناة الحدث",
            platform="youtube",
        )
        facts = facts_from_settings(settings, source, duration_seconds=900)
        self.assertEqual(facts.rights, "third_party")
        self.assertEqual(facts.transform, "editorial")
        self.assertTrue(facts.has_third_party_music)
        self.assertEqual(facts.duration_seconds, 900)


class ChapterTests(unittest.TestCase):
    def test_chapters_start_at_zero_and_keep_order(self):
        lines = build_chapters([(120, "الثاني"), (5, "الأول"), (400, "الثالث")])
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("00:00"))
        self.assertIn("02:00 الثاني", lines[1])
        self.assertIn("06:40 الثالث", lines[2])

    def test_too_few_chapters_are_not_emitted(self):
        self.assertEqual(build_chapters([(0, "واحد"), (60, "اثنان")]), [])

    def test_chapters_closer_than_ten_seconds_are_dropped(self):
        lines = build_chapters([(0, "أ"), (3, "ب"), (60, "ج"), (120, "د")])
        self.assertEqual(len(lines), 3)
        self.assertIn("01:00 ج", lines)

    def test_timestamps_include_hours_when_needed(self):
        lines = build_chapters([(0, "أ"), (3600, "ب"), (7200, "ج")])
        self.assertIn("1:00:00 ب", lines)


class PlanTests(unittest.TestCase):
    def test_plan_carries_attribution_and_disclosure(self):
        facts = SessionFacts(
            title=GOOD_TITLE,
            source_channel="قناة الحدث",
            rights="third_party",
            transform="editorial",
            duration_seconds=3600,
            original_segments=3,
        )
        plan = build_transform_plan(facts, [(0, "بداية"), (60, "تحديث"), (120, "تحليل")])
        self.assertIn("قناة الحدث", plan["description"])
        self.assertIn("نعيد بث المصدر", plan["description"])
        self.assertTrue(any(item["done"] for item in plan["checklist"]))
        self.assertTrue(plan["intro"])
        self.assertTrue(plan["outro"])
        self.assertEqual(len(plan["chapters"]), 3)

    def test_markdown_contains_score_and_checkboxes(self):
        facts = SessionFacts(
            title=GOOD_TITLE,
            source_channel="قناة الحدث",
            rights="third_party",
            transform="branded",
        )
        audit = audit_session(facts)
        markdown = render_plan_markdown(build_transform_plan(facts), audit)
        self.assertIn(f"{audit.score}/100", markdown)
        self.assertIn("[ ]", markdown)
        self.assertIn("خطة التحويل", markdown)


class StorageTests(unittest.TestCase):
    def test_audit_and_session_records_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            audit = audit_session({"rights": "third_party"})
            record_audit(state_dir, audit, video_id="abc", trigger="test")
            record_session(
                state_dir,
                video_id="abc",
                started_at=time.time() - 120,
                ended_at=time.time(),
                facts=SessionFacts(duration_seconds=120),
                audit=audit,
                platforms=["youtube"],
                end_reason="test",
            )
            audits = read_jsonl(state_dir / "ads-audit.jsonl")
            sessions = read_jsonl(state_dir / "sessions.jsonl")
            self.assertEqual(audits[0]["video_id"], "abc")
            self.assertEqual(sessions[0]["duration_seconds"], 120)
            summary = summarize(state_dir)
            self.assertEqual(summary["sessions"], 1)
            self.assertEqual(summary["blocked"], 1)
            if os.name != "nt":
                self.assertEqual(
                    (state_dir / "sessions.jsonl").stat().st_mode & 0o777, 0o600
                )

    def test_summary_ignores_old_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            state_dir.mkdir(parents=True)
            old = {"timestamp": time.time() - 40 * 86400, "score": 10, "band": "blocked"}
            (state_dir / "sessions.jsonl").write_text(
                json.dumps(old) + "\n", encoding="utf-8"
            )
            self.assertEqual(summarize(state_dir)["sessions"], 0)

    def test_finalize_session_writes_one_record(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            settings = SimpleNamespace(state_dir=state_dir)
            session = DestinationSession(
                targets=[("youtube", "rtmp://example/live")],
                video_id="vid123",
                source_facts={
                    "title": GOOD_TITLE,
                    "description": GOOD_DESCRIPTION,
                    "source_channel": "قناة الحدث",
                    "rights": "third_party",
                    "transform": "branded",
                },
                session_started_at=time.time() - 300,
                recording_path="/tmp/session.mp4",
                editorial_events=[
                    {"seconds": 60, "title": "تحليل أول"},
                    {"seconds": 120, "title": "تحليل ثان"},
                ],
                chapter_events=[
                    {"seconds": 180, "title": "تغيّر عنوان المصدر", "auto": True}
                ],
            )
            _finalize_session(settings, session, 2, "test")
            sessions = read_jsonl(state_dir / "sessions.jsonl")
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["video_id"], "vid123")
            self.assertEqual(sessions[0]["source_gaps"], 2)
            self.assertEqual(sessions[0]["recording"], "/tmp/session.mp4")
            self.assertEqual(sessions[0]["editorial_segments"], 2)
            self.assertEqual(len(sessions[0]["chapters"]), 4)
            kit_path = Path(sessions[0]["kit"])
            self.assertTrue(kit_path.is_file())
            self.assertIn("ملف الحلقة", kit_path.read_text(encoding="utf-8"))
            self.assertGreaterEqual(sessions[0]["duration_seconds"], 300)


class SessionFinalizeTests(unittest.TestCase):
    def test_finalize_applies_chapters_and_writes_a_kit(self):
        class FakeYouTube:
            def __init__(self):
                self.metadata = None

            def update_metadata(self, metadata):
                self.metadata = metadata

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            destination = FakeYouTube()
            session = DestinationSession(
                targets=[("youtube", "rtmp://example/live")],
                youtube=destination,
                video_id="vid999",
                session_started_at=time.time() - 600,
                metadata=StreamMetadata(
                    title=GOOD_TITLE, description=GOOD_DESCRIPTION, tags=("أخبار",)
                ),
                source_facts={
                    "title": GOOD_TITLE,
                    "description": GOOD_DESCRIPTION,
                    "source_channel": "قناة الحدث",
                    "rights": "licensed",
                    "transform": "editorial",
                },
                editorial_events=[
                    {"seconds": 60, "title": "تحليل أول"},
                    {"seconds": 120, "title": "تحليل ثان"},
                ],
            )
            _finalize_session(SimpleNamespace(state_dir=state_dir), session, 1, "test")
            self.assertIn("⏱ الفصول", session.metadata.description)
            self.assertTrue(session.chapters_applied)
            record = read_jsonl(state_dir / "sessions.jsonl")[-1]
            self.assertEqual(record["editorial_segments"], 2)
            self.assertEqual(len(record["chapters"]), 3)
            self.assertTrue(record["kit"])
            self.assertTrue(record["chapters"][0].startswith("00:00"))
            kit_text = Path(record["kit"]).read_text(encoding="utf-8")
            self.assertIn("⏱ الفصول", kit_text)
            self.assertIn("تحليل أول", kit_text)


class YouTubeComplianceTests(unittest.TestCase):
    def settings(self, **overrides):
        values = {
            "youtube_default_language": "ar",
            "youtube_privacy": "public",
            "youtube_made_for_kids": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def metadata(self):
        return StreamMetadata(
            title=GOOD_TITLE, description=GOOD_DESCRIPTION, tags=("أخبار",)
        )

    def test_broadcast_body_declares_the_audience_only(self):
        body = build_broadcast_body(
            self.metadata(), self.settings(youtube_made_for_kids=True), "2026-11-13T00:00:00Z"
        )
        self.assertTrue(body["status"]["selfDeclaredMadeForKids"])
        self.assertTrue(body["contentDetails"]["recordFromStart"])
        # The liveBroadcast snippet rejects defaultLanguage; it is set on the video.
        self.assertNotIn("defaultLanguage", body["snippet"])
        self.assertNotIn("defaultAudioLanguage", body["snippet"])

    def test_video_update_carries_the_default_language(self):
        destination = YouTubeDestination(self.settings())
        destination.broadcast = {"id": "vid123"}
        destination.service = Mock()
        destination.service.videos.return_value.update.return_value.execute.return_value = {}
        destination._update_video_metadata(self.metadata())
        body = destination.service.videos.return_value.update.call_args.kwargs["body"]
        self.assertEqual(body["snippet"]["defaultLanguage"], "ar")
        self.assertEqual(body["snippet"]["defaultAudioLanguage"], "ar")

    def test_check_status_reports_live_ad_eligibility(self):
        destination = YouTubeDestination(self.settings())
        destination.broadcast = {"id": "vid123"}
        destination.stream = {"id": "stream123"}
        destination.service = Mock()
        destination.service.liveBroadcasts.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "status": {"lifeCycleStatus": "live"},
                    "monetizationDetails": {
                        "adsMonetizationStatus": "off",
                        "eligibleForAdsMonetization": False,
                    },
                }
            ]
        }
        destination.service.liveStreams.return_value.list.return_value.execute.return_value = {
            "items": [{"status": {"streamStatus": "active", "healthStatus": {"status": "good"}}}]
        }
        status = destination.check_status()
        self.assertFalse(status["eligible_for_ads"])
        self.assertEqual(status["ads_monetization_status"], "off")
        self.assertEqual(status["stream_health"], "good")

    def test_video_status_reports_region_block_as_a_claim_signal(self):
        destination = YouTubeDestination(self.settings())
        destination.broadcast = {"id": "vid123"}
        destination.service = Mock()
        destination.service.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "vid123",
                    "status": {
                        "privacyStatus": "public",
                        "uploadStatus": "processed",
                        "madeForKids": False,
                        "license": "youtube",
                    },
                    "contentDetails": {
                        "regionRestriction": {"blocked": ["DE", "FR"]},
                        "duration": "PT1H2M",
                    },
                    "processingDetails": {"processingStatus": "succeeded"},
                    "statistics": {"viewCount": "12"},
                }
            ]
        }
        status = destination.video_status()
        self.assertEqual(status["privacy_status"], "public")
        self.assertEqual(status["region_blocked"], ["DE", "FR"])
        self.assertTrue(status["region_claim_signal"])
        self.assertEqual(status["duration"], "PT1H2M")


class ConfigDefaultsTests(unittest.TestCase):
    def env(self, **overrides):
        values = {
            "LIVE_PLATFORMS": "youtube",
            "YOUTUBE_STREAM_KEY": "dummy-key",
        }
        values.update(overrides)
        return values

    def test_news_defaults_are_explicit(self):
        with patch.dict(os.environ, self.env(), clear=True):
            settings = load_settings()
        self.assertEqual(settings.youtube_category_id, "25")
        self.assertEqual(settings.source_rights, "unknown")
        self.assertEqual(settings.transform_level, "branded")
        self.assertFalse(settings.youtube_made_for_kids)
        self.assertEqual(settings.youtube_default_language, "ar")
        self.assertFalse(settings.source_third_party_music)

    def test_invalid_rights_and_transform_are_rejected(self):
        for env in (
            {"SOURCE_RIGHTS": "borrowed"},
            {"TRANSFORM_LEVEL": "magic"},
        ):
            with (
                patch.dict(os.environ, self.env(**env), clear=True),
                self.assertRaises(ValueError),
            ):
                load_settings()

    def test_a_long_rights_note_is_rejected(self):
        with (
            patch.dict(
                os.environ,
                self.env(SOURCE_RIGHTS_NOTE="x" * 301),
                clear=True,
            ),
            self.assertRaisesRegex(ValueError, "SOURCE_RIGHTS_NOTE"),
        ):
            load_settings()

    def test_invalid_default_language_is_rejected(self):
        with (
            patch.dict(os.environ, self.env(YOUTUBE_DEFAULT_LANGUAGE="!!"), clear=True),
            self.assertRaises(ValueError),
        ):
            load_settings()


if __name__ == "__main__":
    unittest.main()
