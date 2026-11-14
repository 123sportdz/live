"""Minimal, resumable YouTube upload for finished episodes and Shorts.

The relay already holds an OAuth token with ``youtube.force-ssl`` (the same
scope used for live broadcasts), which also permits ``videos.insert``. This
module uploads a file as **private by default** and returns its id; publishing
it publicly stays a human decision in YouTube Studio.

Quota note: ``videos.insert`` costs 1600 units of the default 10,000/day
quota, so a job uploads a small, explicit number of files. The caller decides
the cap; this module never loops over an unbounded list.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable

SCOPES = ("https://www.googleapis.com/auth/youtube.force-ssl",)
PRIVACY = ("private", "unlisted", "public")
MAX_TITLE = 100
MAX_DESCRIPTION = 5000
MAX_TAGS = 500


class UploadError(RuntimeError):
    """Upload failed with a message safe to show in the dashboard."""


Progress = Callable[[float], None]


def _clean_tags(tags: Iterable[str]) -> list[str]:
    result: list[str] = []
    used = 0
    for raw in tags:
        tag = str(raw).strip().lstrip("#")
        if not tag or tag in result:
            continue
        # Keep the whole list under YouTube's 500-character limit.
        if used + len(tag) + 1 > MAX_TAGS:
            break
        result.append(tag)
        used += len(tag) + 1
    return result


def build_body(
    *,
    title: str,
    description: str = "",
    tags: Iterable[str] = (),
    privacy: str = "private",
    made_for_kids: bool = False,
    category_id: str = "25",
) -> dict[str, Any]:
    title = str(title or "").strip()
    if not title:
        raise UploadError("عنوان الفيديو مطلوب.")
    if len(title) > MAX_TITLE:
        title = title[: MAX_TITLE - 1].rstrip() + "…"
    if privacy not in PRIVACY:
        raise UploadError("خصوصية الرفع يجب أن تكون private أو unlisted أو public.")
    return {
        "snippet": {
            "title": title,
            "description": str(description or "")[:MAX_DESCRIPTION],
            "tags": _clean_tags(tags),
            "categoryId": str(category_id or "25"),
            "defaultLanguage": "ar",
            "defaultAudioLanguage": "ar",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": bool(made_for_kids),
        },
    }


def token_scopes(token_file: Path | str) -> list[str]:
    try:
        data = json.loads(Path(token_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    scopes = data.get("scopes") or []
    return [str(scope) for scope in scopes] if isinstance(scopes, list) else []


def has_upload_scope(token_file: Path | str) -> bool:
    scopes = set(token_scopes(token_file))
    return bool(
        scopes
        & {
            "https://www.googleapis.com/auth/youtube",
            "https://www.googleapis.com/auth/youtube.force-ssl",
            "https://www.googleapis.com/auth/youtube.upload",
        }
    )


def _service(token_file: Path | str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    path = Path(token_file)
    if not path.is_file():
        raise UploadError("لا يوجد تفويض YouTube محفوظ. اربط الحساب من اللوحة أولاً.")
    try:
        credentials = Credentials.from_authorized_user_file(str(path), list(SCOPES))
    except Exception as exc:
        raise UploadError("ملف تفويض YouTube غير صالح؛ أعد ربط الحساب.") from exc
    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            try:
                path.write_text(credentials.to_json(), encoding="utf-8")
                os.chmod(path, 0o600)
            except OSError:
                pass
        else:
            raise UploadError("انتهى تفويض YouTube؛ أعد ربط الحساب من اللوحة.")
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def upload_video(
    path: Path | str,
    *,
    title: str,
    description: str = "",
    tags: Iterable[str] = (),
    privacy: str = "private",
    token_file: Path | str,
    made_for_kids: bool = False,
    category_id: str = "25",
    progress: Progress | None = None,
    service: Any | None = None,
) -> dict[str, Any]:
    """Upload one file and return ``video_id``/``url``/``privacy``."""
    from googleapiclient.http import MediaFileUpload

    media_path = Path(path)
    if not media_path.is_file() or media_path.stat().st_size <= 0:
        raise UploadError(f"ملف الفيديو غير موجود أو فارغ: {media_path}")
    body = build_body(
        title=title,
        description=description,
        tags=tags,
        privacy=privacy,
        made_for_kids=made_for_kids,
        category_id=category_id,
    )
    client = service if service is not None else _service(token_file)
    media = MediaFileUpload(
        str(media_path),
        mimetype="video/mp4",
        chunksize=8 * 1024 * 1024,
        resumable=True,
    )
    request = client.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=False,
    )
    response: dict[str, Any] | None = None
    while response is None:
        status, response = request.next_chunk()
        if progress is not None and status is not None:
            try:
                progress(float(status.progress()))
            except Exception:
                pass
    video_id = str((response or {}).get("id") or "")
    if not video_id:
        raise UploadError("رفع YouTube لم يُرجع معرّف الفيديو.")
    return {
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "privacy": body["status"]["privacyStatus"],
        "uploaded_at": time.time(),
        "title": body["snippet"]["title"],
    }
