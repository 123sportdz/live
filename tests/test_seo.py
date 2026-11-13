from __future__ import annotations

import unittest

from live_relay.seo import (
    build_description,
    build_hashtags,
    build_keywords,
    clean_topic,
    detect_language,
    is_clickbait,
    primary_keyword,
    propose_metadata,
    refine_metadata,
    score_title,
    title_candidates,
)

DESCRIPTION = (
    "أعلنت السلطات أن القصف استهدف حياً سكنياً في المدينة. "
    "وقال المتحدث إن الفرق تعمل على إخراج العالقين من تحت الأنقاض. "
    "وتأتي التطورات بعد ساعات من الهدوء النسبي."
)


class TopicTests(unittest.TestCase):
    def test_labels_and_trailing_markers_are_removed(self):
        self.assertEqual(
            clean_topic("عاجل: قصف عنيف على مدينة غزة الآن"),
            "قصف عنيف على مدينة غزة",
        )

    def test_a_title_that_would_become_too_short_is_kept(self):
        self.assertEqual(clean_topic("عاجل: غزة"), "عاجل: غزة")

    def test_channel_suffix_is_removed(self):
        self.assertEqual(
            clean_topic("تطورات الملف الليبي - قناة الحدث", "قناة الحدث"),
            "تطورات الملف الليبي",
        )

    def test_live_marker_is_not_duplicated(self):
        titles = [
            item["title"]
            for item in title_candidates(
                "بث مباشر: مباراة ريال مدريد وبرشلونة",
                "قناة الرياضة",
                ("ريال مدريد",),
                "ar",
            )
        ]
        self.assertTrue(titles)
        self.assertNotIn("مباشر: بث مباشر: مباراة ريال مدريد وبرشلونة", titles)
        self.assertTrue(any("متابعة" in title for title in titles))


class ClickbaitTests(unittest.TestCase):
    def test_markers_and_shouting_are_detected(self):
        self.assertTrue(is_clickbait("لن تتوقع ما حدث!!!"))
        self.assertTrue(is_clickbait("WATCH THIS NOW"))
        self.assertFalse(is_clickbait("استئناف المحادثات في جنيف"))


class TitleTests(unittest.TestCase):
    def test_candidates_are_unique_and_within_the_limit(self):
        candidates = title_candidates(
            "عاجل: قصف عنيف على مدينة غزة الآن",
            "قناة الحدث",
            ("غزة", "قصف"),
            "ar",
            limit=4,
        )
        titles = [item["title"] for item in candidates]
        self.assertEqual(len(titles), len(set(titles)))
        self.assertTrue(all(len(title) <= 100 for title in titles))
        self.assertTrue(any("تغطية مباشرة" in title for title in titles))

    def test_primary_keyword_stops_at_a_preposition(self):
        self.assertEqual(
            primary_keyword("قصف عنيف على مدينة غزة", (), "ar"), "قصف عنيف"
        )

    def test_keyword_first_and_verbatim_are_scored_differently(self):
        good, reasons = score_title(
            "قصف عنيف على مدينة غزة | تغطية مباشرة",
            "قصف عنيف",
            "عاجل: قصف عنيف على مدينة غزة الآن",
            "ar",
        )
        verbatim, _ = score_title(
            "عاجل: قصف عنيف على مدينة غزة الآن",
            "قصف عنيف",
            "عاجل: قصف عنيف على مدينة غزة الآن",
            "ar",
        )
        self.assertGreater(good, verbatim)
        self.assertIn("الكلمة المفتاحية في البداية", reasons)


class ScoreReasonTests(unittest.TestCase):
    def test_short_title_says_short_not_long(self):
        _, reasons = score_title("عنوان قصير", "عنوان", "", "ar")
        self.assertIn("أقصر من المثالي (30–75 حرفاً)", reasons)
        self.assertNotIn("أطول من اللازم", reasons)


class KeywordTests(unittest.TestCase):
    def test_keywords_are_deduplicated_and_bounded(self):
        keywords = build_keywords(
            "قصف عنيف على مدينة غزة",
            ("غزة", "قصف", "القصف", "أخبار", "x" * 60),
            DESCRIPTION,
            "قناة الحدث",
            "ar",
            limit=60,
        )
        self.assertEqual(keywords[0], "قصف عنيف")
        self.assertIn("غزة", keywords)
        self.assertNotIn("القصف", keywords)
        self.assertEqual(len(keywords), len({k.casefold() for k in keywords}))
        self.assertLessEqual(sum(len(k) + 1 for k in keywords), 60)
        self.assertFalse(any("#" in k for k in keywords))

    def test_hashtags_are_unique_and_valid(self):
        tags = build_hashtags("قصف عنيف", ("غزة", "قصف", "أخبار"), "ar")
        self.assertLessEqual(len(tags), 4)
        self.assertEqual(len(tags), len(set(tags)))
        self.assertTrue(all(tag.startswith("#") and " " not in tag for tag in tags))


class DescriptionTests(unittest.TestCase):
    def test_description_has_structure_and_source_attribution(self):
        description = build_description(
            "قصف عنيف على مدينة غزة",
            "قناة الحدث",
            ["أعلنت السلطات أن القصف استهدف حياً سكنياً."],
            ["00:00 بداية البث", "01:00 تحليل"],
            ("#غزة",),
            "ar",
            rights="third_party",
        )
        self.assertIn("نتابع في هذا البث", description)
        self.assertIn("ما تجده في هذا البث", description)
        self.assertIn("المصدر: قناة الحدث", description)
        self.assertIn("نعيد بث المصدر", description)
        self.assertIn("⏱ الفصول", description)
        self.assertIn("#غزة", description)
        self.assertLessEqual(len(description), 5000)

    def test_long_description_drops_facts_but_keeps_chapters(self):
        facts = [f"نقطة وقائعية رقم {index} " + "ط" * 180 for index in range(3)]
        chapters = [f"{index:02d}:00 فصل رقم {index}" for index in range(20)]
        description = build_description(
            "موضوع طويل جداً " * 10,
            "قناة",
            facts,
            chapters,
            (),
            "ar",
        )
        self.assertLessEqual(len(description), 5000)
        self.assertIn(chapters[-1], description)


class ProposalTests(unittest.TestCase):
    def test_proposal_rewrites_without_inventing(self):
        proposal = propose_metadata(
            source_title="عاجل: قصف عنيف على مدينة غزة الآن",
            source_description=DESCRIPTION,
            source_channel="قناة الحدث",
            source_tags=("غزة", "قصف"),
            language="ar",
            rights="third_party",
        )
        self.assertNotEqual(proposal.title, "عاجل: قصف عنيف على مدينة غزة الآن")
        self.assertIn("قناة الحدث", proposal.description)
        self.assertTrue(proposal.keywords)
        self.assertTrue(proposal.candidates)
        for fact in DESCRIPTION.split(". "):
            if fact.strip(".") and fact.strip(".") in proposal.description:
                self.assertIn(fact.strip("."), DESCRIPTION)

    def test_repeated_suggestions_are_stable(self):
        original = "عاجل: قصف عنيف على مدينة غزة الآن"
        first = propose_metadata(
            source_title=original,
            reference_title=original,
            source_description=DESCRIPTION,
            source_channel="قناة الحدث",
            source_tags=("غزة",),
            language="ar",
        )
        second = propose_metadata(
            source_title=first.title,
            reference_title=original,
            source_description=DESCRIPTION,
            source_channel="قناة الحدث",
            source_tags=("غزة",),
            language="ar",
        )
        self.assertEqual(first.title, second.title)

    def test_clickbait_source_produces_a_warning(self):
        proposal = propose_metadata(
            source_title="لن تتوقع: قصف عنيف على مدينة غزة الآن",
            source_description=DESCRIPTION,
            language="ar",
        )
        self.assertTrue(any("إثارة" in warning for warning in proposal.warnings))

    def test_english_source_gets_english_templates(self):
        proposal = propose_metadata(
            source_title="Live: Champions League final between Real Madrid and Barcelona",
            source_description="The two sides meet in the final after a long season.",
            source_channel="Sports Channel",
            source_tags=("Real Madrid", "Barcelona"),
            language="auto",
        )
        self.assertEqual(proposal.language, "en")
        self.assertIn("Live coverage", proposal.description)
        self.assertIn("Source: Sports Channel", proposal.description)

    def test_french_source_gets_french_templates(self):
        proposal = propose_metadata(
            source_title="En direct : réunion de l'ONU sur la situation",
            source_description="Les délégations se réunissent aujourd'hui à Genève.",
            source_channel="Chaîne Info",
            language="fr",
        )
        self.assertEqual(proposal.language, "fr")
        self.assertIn("Suivez", proposal.description)
        self.assertIn("Source: Chaîne Info", proposal.description)

    def test_language_detection(self):
        self.assertEqual(detect_language("عاجل: قصف عنيف", "auto"), "ar")
        self.assertEqual(detect_language("Breaking news from the city", "auto"), "en")
        self.assertEqual(detect_language("عاجل", "fr"), "fr")


class RefineTests(unittest.TestCase):
    def test_a_good_generated_title_is_kept(self):
        result = refine_metadata(
            title="قصف عنيف على مدينة غزة | تغطية مباشرة",
            description="نتابع في هذا البث قصف عنيف على مدينة غزة مع تحديثات مباشرة.",
            tags=["غزة", "قصف"],
            source_title="عاجل: قصف عنيف على مدينة غزة الآن",
            source_description=DESCRIPTION,
            source_channel="قناة الحدث",
            language="ar",
        )
        self.assertEqual(result.title, "قصف عنيف على مدينة غزة | تغطية مباشرة")

    def test_clickbait_and_verbatim_titles_are_replaced(self):
        for title in ("WATCH THIS NOW!!!", "عاجل: قصف عنيف على مدينة غزة الآن"):
            result = refine_metadata(
                title=title,
                description="وصف",
                tags=["أخبار"],
                source_title="عاجل: قصف عنيف على مدينة غزة الآن",
                source_description=DESCRIPTION,
                source_channel="قناة الحدث",
                language="ar",
            )
            self.assertNotEqual(result.title, title)
            self.assertTrue(any("عُدّل العنوان" in w for w in result.warnings))

    def test_short_description_is_replaced_by_the_structured_one(self):
        result = refine_metadata(
            title="عنوان مقبول طويل بما يكفي للبحث",
            description="قصير",
            tags=["أخبار"],
            source_title="عاجل: قصف عنيف على مدينة غزة الآن",
            source_description=DESCRIPTION,
            source_channel="قناة الحدث",
            language="ar",
        )
        self.assertGreater(len(result.description), 100)
        self.assertIn("المصدر: قناة الحدث", result.description)

    def test_existing_long_description_is_augmented_not_replaced(self):
        existing = "نص تحريري مكتوب يدوياً. " * 10
        result = refine_metadata(
            title="عنوان مقبول طويل بما يكفي للبحث",
            description=existing,
            tags=["أخبار"],
            source_title="عاجل: قصف عنيف على مدينة غزة الآن",
            source_description=DESCRIPTION,
            source_channel="قناة الحدث",
            language="ar",
            chapters=["00:00 بداية", "01:00 تحليل"],
        )
        self.assertTrue(result.description.startswith(existing.strip()))
        self.assertIn("المصدر: قناة الحدث", result.description)
        self.assertIn("⏱", result.description)


if __name__ == "__main__":
    unittest.main()
