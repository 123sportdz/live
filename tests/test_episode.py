from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from live_relay.episode import (
    EpisodeError,
    MIN_SECONDS,
    format_timestamp,
    load_manifest,
    probe_duration,
    render_card,
    render_episode,
    sample_manifest,
    slugify,
)

FFMPEG = shutil.which("ffmpeg")


def make_clip(path: Path, seconds: float = 2.0, size: str = "320x180") -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x1b4332:s={size}:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=330:duration={seconds}",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


@unittest.skipUnless(FFMPEG, "ffmpeg is required")
class EpisodeManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="episode-test-")
        self.root = Path(self._tmp.name)
        make_clip(self.root / "a.mp4", 2)
        make_clip(self.root / "b.mp4", 2)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, data: dict) -> Path:
        path = self.root / "manifest.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def manifest(self, **overrides) -> dict:
        data = {
            "title": "حلقة اختبار",
            "date": "2026-11-14",
            "brand": "Oussama News",
            "intro_seconds": 1,
            "card_seconds": 1,
            "outro_seconds": 1,
            "segments": [
                {"heading": "الأول", "clip": "a.mp4", "duration": 2},
                {"heading": "الثاني", "clip": "b.mp4", "duration": 2},
            ],
        }
        data.update(overrides)
        return data

    def test_plan_total_and_chapters(self):
        plan = load_manifest(self.write(self.manifest()))
        self.assertEqual(plan.total_seconds, 7)
        self.assertEqual(
            [chapter["time"] for chapter in plan.chapters()],
            ["00:00", "00:01", "00:04"],
        )
        self.assertEqual(plan.items[0].kind, "intro")
        self.assertEqual(plan.items[1].kind, "segment")
        self.assertEqual(plan.items[2].kind, "card")
        self.assertIn("15–20", " ".join(plan.warnings))

    def test_short_manifest_is_flagged_but_loads(self):
        plan = load_manifest(self.write(self.manifest()))
        self.assertLess(plan.total_seconds, MIN_SECONDS)
        self.assertTrue(plan.warnings)

    def test_non_original_rights_warns(self):
        plan = load_manifest(self.write(self.manifest(rights="third_party")))
        self.assertTrue(any("الحقوق" in warning for warning in plan.warnings))

    def test_missing_title_clip_and_segments_are_rejected(self):
        cases = [
            self.manifest(title=""),
            self.manifest(segments=[]),
            self.manifest(segments=[{"heading": "x", "clip": "missing.mp4"}]),
        ]
        for data in cases:
            with self.assertRaises(EpisodeError):
                load_manifest(self.write(data))

    def test_bad_date_is_rejected(self):
        with self.assertRaisesRegex(EpisodeError, "التاريخ"):
            load_manifest(self.write(self.manifest(date="14/11/2026")))

    def test_card_is_rendered_at_requested_size(self):
        png = render_card(
            kicker="حلقة يومية",
            headline="عنوان عربي طويل بعض الشيء",
            subline="سطر توضيحي",
            width=640,
            height=360,
        )
        from PIL import Image
        import io

        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.size, (640, 360))

    def test_slugify_keeps_arabic(self):
        self.assertEqual(slugify("حلقة: اليوم!"), "حلقة-اليوم")

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe is required")
    def test_render_produces_video_and_sidecars(self):
        plan = load_manifest(self.write(self.manifest()))
        output = self.root / "out"
        result = render_episode(plan, output, width=320, height=180)
        self.assertTrue((output / "episode.mp4").is_file())
        self.assertTrue((output / "episode.json").is_file())
        self.assertTrue((output / "chapters.txt").is_file())
        self.assertTrue((output / "description.md").is_file())
        self.assertAlmostEqual(probe_duration(output / "episode.mp4"), 7, delta=1)
        self.assertEqual(result["title"], "حلقة اختبار")
        self.assertIn("00:00 مقدمة", (output / "chapters.txt").read_text(encoding="utf-8"))

    def test_sample_manifest_skips_the_active_recording(self):
        folder = self.root / "recordings"
        folder.mkdir()
        old = folder / "old.mp4"
        fresh = folder / "fresh.mp4"
        make_clip(old, 2)
        make_clip(fresh, 2)
        os.utime(old, (1_700_000_000, 1_700_000_000))
        payload = sample_manifest(
            folder, title="حلقة", brand="Brand", now=1_700_000_500
        )
        self.assertEqual(len(payload["segments"]), 1)
        self.assertIn("old.mp4", payload["segments"][0]["clip"])

    def test_format_timestamp(self):
        self.assertEqual(format_timestamp(0), "00:00")
        self.assertEqual(format_timestamp(65), "01:05")
        self.assertEqual(format_timestamp(3661), "01:01:01")


if __name__ == "__main__":
    unittest.main()
