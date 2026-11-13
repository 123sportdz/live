from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from live_relay.thumbnail_local import (
    HEIGHT,
    WIDTH,
    accent_for,
    compose_thumbnail,
    default_date,
    sanitize,
    split_title,
)


class TitleTests(unittest.TestCase):
    def test_split_title_returns_headline_and_angle(self):
        self.assertEqual(
            split_title("قصف عنيف على مدينة غزة | تغطية مباشرة"),
            ("قصف عنيف على مدينة غزة", "تغطية مباشرة"),
        )

    def test_split_title_without_angle(self):
        self.assertEqual(split_title("خبر عاجل"), ("خبر عاجل", ""))

    def test_sanitize_removes_emoji_and_unsupported_separators(self):
        self.assertEqual(sanitize("خبر 🔴 مهم | الآن"), "خبر مهم الآن")

    def test_accent_is_stable(self):
        self.assertEqual(accent_for("عنوان"), accent_for("عنوان"))
        self.assertTrue(accent_for("عنوان").startswith("#"))

    def test_arabic_date_uses_month_names(self):
        value = default_date("ar")
        self.assertTrue(any(month in value for month in ("يناير", "نوفمبر", "ديسمبر")))


class RenderTests(unittest.TestCase):
    def test_arabic_cover_is_a_valid_1280x720_png(self):
        data = compose_thumbnail(
            title="قصف عنيف على مدينة غزة | تغطية مباشرة",
            channel="Oussama News",
            source_channel="قناة الحدث",
            date_text="13 نوفمبر 2026",
        )
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        self.assertEqual((width, height), (WIDTH, HEIGHT))
        self.assertGreater(len(data), 20_000)

    def test_english_cover_renders(self):
        data = compose_thumbnail(
            title="Champions League final: Real Madrid vs Barcelona | Live coverage",
            channel="Oussama News",
            source_channel="Sports Channel",
            language="en",
        )
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_very_long_title_still_renders(self):
        data = compose_thumbnail(
            title="عنوان طويل جداً " * 12,
            channel="قناة",
        )
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_missing_logo_does_not_break_the_render(self):
        data = compose_thumbnail(
            title="عنوان تجريبي للبث المباشر",
            channel="قناة",
            logo_file="/nonexistent/logo.png",
        )
        self.assertTrue(data.startswith(b"\x89PNG"))


class SaveTests(unittest.TestCase):
    def test_local_generator_saves_a_thumbnail(self):
        from types import SimpleNamespace

        from live_relay.thumbnail_ai import generate_local_thumbnail
        from live_relay.thumbnails import get_thumbnail_path

        settings = SimpleNamespace(
            metadata_language="ar",
            brand_name="Oussama News",
            logo_file="",
        )
        source = SimpleNamespace(channel="قناة الحدث")
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            provider = generate_local_thumbnail(
                settings, source, "قصف عنيف على مدينة غزة | تغطية مباشرة", state_dir
            )
            self.assertEqual(provider, "local")
            path = get_thumbnail_path(state_dir)
            self.assertIsNotNone(path)
            self.assertGreater(path.stat().st_size, 20_000)


if __name__ == "__main__":
    unittest.main()
