from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from live_relay.publish import (
    UploadError,
    build_body,
    has_upload_scope,
    upload_video,
)


class FakeStatus:
    def progress(self) -> float:
        return 0.5


class FakeRequest:
    def next_chunk(self):
        return FakeStatus(), {"id": "vid123"}


class FakeVideos:
    def __init__(self) -> None:
        self.captured = {}

    def insert(self, **kwargs):
        self.captured.update(kwargs)
        return FakeRequest()


class FakeService:
    def __init__(self) -> None:
        self.videos_api = FakeVideos()

    def videos(self):
        return self.videos_api


class PublishTests(unittest.TestCase):
    def test_body_cleans_and_limits(self):
        body = build_body(
            title="x" * 150,
            description="d" * 6000,
            tags=["أخبار", "#أخبار", "تحليل", ""],
            privacy="unlisted",
        )
        self.assertEqual(len(body["snippet"]["title"]), 100)
        self.assertTrue(body["snippet"]["title"].endswith("…"))
        self.assertEqual(len(body["snippet"]["description"]), 5000)
        self.assertEqual(body["snippet"]["tags"], ["أخبار", "تحليل"])
        self.assertEqual(body["status"]["privacyStatus"], "unlisted")

    def test_invalid_privacy_is_rejected(self):
        with self.assertRaises(UploadError):
            build_body(title="x", privacy="friends")

    def test_upload_uses_the_service_and_returns_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"video-bytes")
            service = FakeService()
            result = upload_video(
                path,
                title="عنوان",
                description="وصف",
                tags=["أخبار"],
                privacy="private",
                token_file=Path(tmp) / "token.json",
                service=service,
            )
        self.assertEqual(result["video_id"], "vid123")
        self.assertEqual(result["url"], "https://youtu.be/vid123")
        self.assertEqual(result["privacy"], "private")
        captured = service.videos_api.captured
        self.assertEqual(captured["body"]["status"]["privacyStatus"], "private")
        self.assertEqual(captured["body"]["snippet"]["title"], "عنوان")

    def test_missing_file_is_rejected(self):
        with self.assertRaises(UploadError):
            upload_video(
                "/does/not/exist.mp4",
                title="x",
                token_file="/tmp/token.json",
                service=FakeService(),
            )

    def test_scope_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token.json"
            token.write_text(
                json.dumps({"scopes": ["https://www.googleapis.com/auth/youtube.force-ssl"]}),
                encoding="utf-8",
            )
            self.assertTrue(has_upload_scope(token))
            token.write_text(json.dumps({"scopes": ["https://www.googleapis.com/auth/drive"]}), encoding="utf-8")
            self.assertFalse(has_upload_scope(token))
            token.unlink()
            self.assertFalse(has_upload_scope(token))


if __name__ == "__main__":
    unittest.main()
