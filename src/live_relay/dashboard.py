from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values, load_dotenv, set_key

from .config import (
    default_state_dir,
    ensure_private_dir,
    load_settings,
    resolve_project_path,
    resolve_project_root,
)
from .destinations import (
    FacebookDestination,
    YouTubeDestination,
    update_destination_metadata,
    update_destination_thumbnail,
)
from .encoder import redact_urls, resolve_ffmpeg
from .metadata import StreamMetadata, clean_description, clean_tags
from .monetization import (
    SessionFacts,
    audit_session,
    build_transform_plan,
    read_jsonl,
    record_audit,
    render_plan_markdown,
    session_path,
    summarize,
)
from .seo import propose_metadata
from .newsroom import (
    brief_from_facts,
    build_brief,
    build_session_kit,
    chapter_block,
    latest_markdown,
    render_brief_markdown,
    summarize_markers,
    write_brief,
    write_kit,
)
from .runtime import RelayStatus
from .reports import write_weekly_report
from .service import (
    add_editorial_marker,
    apply_session_chapters,
    remove_editorial_marker,
)
from .supervisor import RelaySupervisor, heartbeat_age, read_restarts
from .source import clean_title
from .source_platforms import validate_source_platform
from .thumbnail_ai import generate_local_thumbnail
from .thumbnails import (
    MAX_THUMBNAIL_SIZE,
    inspect_thumbnail,
    clear_thumbnail,
    get_thumbnail_path,
    save_thumbnail,
    set_manual_hold,
)

PROJECT_ROOT = resolve_project_root()
SECRET_KEYS = (
    "YOUTUBE_STREAM_KEY",
    "FACEBOOK_ACCESS_TOKEN",
    "FACEBOOK_STREAM_KEY",
    "GEMINI_API_KEY",
)
CONFIG_KEYS = (
    "SOURCE_URL",
    "SOURCE_PLATFORM",
    "SOURCE_RIGHTS",
    "SOURCE_RIGHTS_NOTE",
    "TRANSFORM_LEVEL",
    "SOURCE_THIRD_PARTY_MUSIC",
    "LIVE_PLATFORMS",
    "YOUTUBE_CLIENT_SECRETS_FILE",
    "YOUTUBE_CHANNEL_ID",
    "YOUTUBE_MODE",
    "YOUTUBE_PRIVACY_STATUS",
    "YOUTUBE_MADE_FOR_KIDS",
    "YOUTUBE_DEFAULT_LANGUAGE",
    "YOUTUBE_CATEGORY_ID",
    "YOUTUBE_BACKUP",
    "YOUTUBE_TITLE_OVERRIDE",
    "YOUTUBE_DESCRIPTION_OVERRIDE",
    "YOUTUBE_TAGS_OVERRIDE",
    "FACEBOOK_TARGET_ID",
    "FACEBOOK_GRAPH_VERSION",
    "GEMINI_MODEL",
    "GEMINI_METADATA_ENABLED",
    "SEO_REWRITE",
    "THUMBNAIL_MODE",
    "BRAND_NAME",
    "METADATA_LANGUAGE",
    "LOGO_ENABLED",
    "LOGO_FILE",
    "LOGO_POSITION",
    "LOGO_SIZE_PERCENT",
    "LOGO_OPACITY",
    "FRAME_ENABLED",
    "FRAME_FILE",
    "INITIAL_PROFILE",
    "ADAPTIVE_QUALITY",
    "PIPE_MODE",
    "FFMPEG",
    "FFMPEG_PRESET",
    "YTDLP_FORMAT",
    "YTDLP_COOKIE_FILE",
    "YTDLP_COOKIES_FROM_BROWSER",
    "RECONNECT_DELAY",
    "SOURCE_RETRY",
    "STALL_TIMEOUT",
    "HEARTBEAT",
    "SOURCE_END_GRACE",
)

METADATA_OVERRIDE_KEYS = {
    "YOUTUBE_TITLE_OVERRIDE",
    "YOUTUBE_DESCRIPTION_OVERRIDE",
    "YOUTUBE_TAGS_OVERRIDE",
}


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _default_value(key: str) -> str:
    defaults = {
        "SOURCE_URL": "https://www.youtube.com/channel/UCFmnqispHbyVZKRM6RcybWQ/live",
        "SOURCE_PLATFORM": "auto",
        "SOURCE_RIGHTS": "unknown",
        "SOURCE_RIGHTS_NOTE": "",
        "TRANSFORM_LEVEL": "branded",
        "SOURCE_THIRD_PARTY_MUSIC": "false",
        "LIVE_PLATFORMS": "youtube,facebook",
        "YOUTUBE_CHANNEL_ID": "UCioeMW7bZNeyV48Z09q2vTg",
        "YOUTUBE_MODE": "auto",
        "YOUTUBE_PRIVACY_STATUS": "public",
        "YOUTUBE_MADE_FOR_KIDS": "false",
        "YOUTUBE_DEFAULT_LANGUAGE": "ar",
        "YOUTUBE_CATEGORY_ID": "25",
        "YOUTUBE_TITLE_OVERRIDE": "",
        "YOUTUBE_DESCRIPTION_OVERRIDE": "",
        "YOUTUBE_TAGS_OVERRIDE": "",
        "FACEBOOK_TARGET_ID": "me",
        "FACEBOOK_GRAPH_VERSION": "v26.0",
        "GEMINI_MODEL": "gemini-3.8-flash",
        "GEMINI_METADATA_ENABLED": "true",
        "SEO_REWRITE": "true",
        "THUMBNAIL_MODE": "auto",
        "BRAND_NAME": "Oussama News",
        "METADATA_LANGUAGE": "ar",
        "LOGO_ENABLED": "true",
        "LOGO_POSITION": "bottom-right",
        "LOGO_SIZE_PERCENT": "12",
        "LOGO_OPACITY": "0.9",
        "FRAME_ENABLED": "false",
        "INITIAL_PROFILE": "720p",
        "ADAPTIVE_QUALITY": "true",
        "FFMPEG_PRESET": "veryfast",
        "YTDLP_FORMAT": "b[height<=720]/bv*[height<=720]+ba/best",
        "YOUTUBE_BACKUP": "false",
        "PIPE_MODE": "false",
        "RECONNECT_DELAY": "5",
        "SOURCE_RETRY": "30",
        "STALL_TIMEOUT": "45",
        "HEARTBEAT": "30",
        "SOURCE_END_GRACE": "180",
    }
    return defaults.get(key, "")


class DashboardController:
    def __init__(self, project_root: Path = PROJECT_ROOT) -> None:
        self.project_root = project_root.resolve()
        self.env_path = self.project_root / ".env"
        self.example_path = self.project_root / ".env.example"
        self._lock = threading.RLock()
        self._relay_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self.status = RelayStatus()
        self.control_token = secrets.token_urlsafe(32)
        self.ensure_env_file()
        self.discover_youtube_client_file()
        load_dotenv(self.env_path, override=True)

    def ensure_env_file(self) -> None:
        if self.env_path.exists():
            try:
                os.chmod(self.env_path, 0o600)
            except OSError:
                pass
        if not self.env_path.exists():
            template_path = self.example_path
            if not template_path.is_file():
                template_path = Path(__file__).with_name(".env.example")
            if not template_path.is_file():
                raise RuntimeError("The .env.example setup template is missing")
            shutil.copyfile(template_path, self.env_path)
            try:
                os.chmod(self.env_path, 0o600)
            except OSError:
                pass

    def discover_youtube_client_file(self) -> str:
        values = dotenv_values(self.env_path)
        configured = str(values.get("YOUTUBE_CLIENT_SECRETS_FILE") or "").strip()
        if configured:
            return configured
        candidates = sorted(self.project_root.glob("client_secret*.json"))
        candidates.extend(sorted(self.project_root.glob("oauth-client*.json")))
        for candidate in candidates:
            try:
                content = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if any(key in content for key in ("installed", "web")):
                configured = str(candidate.resolve())
                set_key(str(self.env_path), "YOUTUBE_CLIENT_SECRETS_FILE", configured)
                try:
                    os.chmod(self.env_path, 0o600)
                except OSError:
                    pass
                return configured
        return ""

    def _values(self) -> dict[str, str]:
        raw = dotenv_values(self.env_path)
        return {key: str(value) for key, value in raw.items() if value is not None}

    def _current_state_dir(self, values: dict[str, str] | None = None) -> Path:
        values = values or self._values()
        configured = values.get("LIVE_RELAY_STATE_DIR", "").strip()
        return Path(configured).expanduser() if configured else default_state_dir()

    def _token_meta_path(self, values: dict[str, str] | None = None) -> Path:
        return self._current_state_dir(values) / "facebook-token-meta.json"

    def _read_token_meta(self, values: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            data = json.loads(self._token_meta_path(values).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_token_meta(self, **fields: Any) -> None:
        path = self._token_meta_path()
        try:
            ensure_private_dir(path.parent)
            meta = self._read_token_meta()
            meta.update(fields)
            meta["updated_at"] = time.time()
            path.write_text(json.dumps(meta), encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError:
            logging.warning("Could not update the Facebook token metadata file")

    def _facebook_token_age_days(
        self, values: dict[str, str] | None = None
    ) -> int | None:
        saved_at = self._read_token_meta(values).get("saved_at")
        if not isinstance(saved_at, (int, float)):
            return None
        return max(0, int((time.time() - saved_at) // 86400))

    def _facebook_token_stale(self, values: dict[str, str] | None = None) -> bool:
        age = self._facebook_token_age_days(values)
        return bool(age is not None and age > 45)

    def _is_running(self) -> bool:
        return bool(self._relay_thread and self._relay_thread.is_alive())

    def public_config(self) -> dict[str, Any]:
        values = self._values()
        platforms = [
            part.strip().lower()
            for part in values.get("LIVE_PLATFORMS", "youtube,facebook").split(",")
            if part.strip()
        ]
        state_dir = self._current_state_dir(values)
        thumbnail_path = get_thumbnail_path(state_dir)
        thumbnail_id = ""
        if thumbnail_path:
            try:
                thumbnail_stat = thumbnail_path.stat()
                thumbnail_id = (
                    f"{thumbnail_path.name}:{thumbnail_stat.st_mtime_ns}:"
                    f"{thumbnail_stat.st_size}"
                )
            except OSError:
                thumbnail_path = None
        client_file = (
            values.get("YOUTUBE_CLIENT_SECRETS_FILE", "").strip()
            or self.discover_youtube_client_file()
        )
        client_exists = bool(client_file and Path(client_file).expanduser().is_file())
        token_file = state_dir / "youtube-token.json"
        missing: list[str] = []
        notes: list[str] = []
        youtube_mode = values.get("YOUTUBE_MODE", "auto").strip().lower()
        youtube_api_enabled = youtube_mode == "api" or (
            youtube_mode == "auto" and bool(client_file)
        )
        facebook_api_enabled = bool(values.get("FACEBOOK_ACCESS_TOKEN", "").strip())
        metadata_api_enabled = ("youtube" in platforms and youtube_api_enabled) or (
            "facebook" in platforms and facebook_api_enabled
        )
        youtube_key_set = bool(values.get("YOUTUBE_STREAM_KEY", "").strip())
        if "youtube" in platforms:
            if youtube_api_enabled:
                if not client_file:
                    missing.append(
                        "وضع API في YouTube يتطلب ملف Google OAuth client JSON."
                    )
                elif not client_exists:
                    missing.append(
                        "تعذر العثور على ملف Google OAuth المحدد لـ YouTube."
                    )
            elif youtube_key_set:
                notes.append(
                    "وضع المفتاح الثابت لـ YouTube: أنشئ البث في Studio يدوياً (بالمفتاح نفسه)؛ لن ينشئ البرنامج البث ولا يزامن العنوان."
                )
            elif youtube_mode == "static":
                missing.append(
                    "وضع المفتاح الثابت لـ YouTube يتطلب YOUTUBE_STREAM_KEY."
                )
            else:
                missing.append(
                    "للبث إلى YouTube، أضف ملف Google OAuth client JSON أو مفتاح بث ثابت."
                )
        if (
            "facebook" in platforms
            and not values.get("FACEBOOK_ACCESS_TOKEN", "").strip()
        ):
            if values.get("FACEBOOK_STREAM_KEY", "").strip():
                notes.append(
                    "وضع المفتاح الثابت لـ Facebook: ابدأ البث من Live Producer (بالمفتاح نفسه)؛ لن ينشئ البرنامج البث ولا يزامن العنوان."
                )
            else:
                missing.append(
                    "للبث إلى Facebook، أضف رمز وصول Meta Live Video API أو مفتاح بث ثابت."
                )
        gemini_enabled = _as_bool(values.get("GEMINI_METADATA_ENABLED", "true"))
        gemini_key_set = bool(values.get("GEMINI_API_KEY", "").strip())
        if gemini_enabled and metadata_api_enabled and not gemini_key_set:
            notes.append(
                "أضف مفتاح Gemini لتفعيل إنشاء العنوان والوصف والكلمات المفتاحية تلقائياً."
            )
        logo_enabled = _as_bool(values.get("LOGO_ENABLED", "true"))
        configured_logo = values.get("LOGO_FILE", "").strip()
        logo_file = (
            resolve_project_path(configured_logo)
            if configured_logo
            else resolve_project_path("assets/live-relay-logo.png")
        )
        if not configured_logo and not logo_file.is_file():
            logo_file = Path(__file__).with_name("stream-logo.png")
        if logo_enabled and not logo_file.is_file():
            missing.append(
                "LOGO_FILE must point to an existing image, or disable the logo."
            )
        frame_enabled = _as_bool(values.get("FRAME_ENABLED", "false"))
        configured_frame = values.get("FRAME_FILE", "").strip()
        frame_file = (
            resolve_project_path(configured_frame) if configured_frame else None
        )
        if (
            frame_enabled
            and frame_file is not None
            and (frame_file.suffix.lower() != ".png" or not frame_file.is_file())
        ):
            missing.append("FRAME_FILE must point to an existing PNG image.")
        config = {
            "source_url": values.get("SOURCE_URL", _default_value("SOURCE_URL")),
            "source_platform": values.get("SOURCE_PLATFORM", "auto"),
            "source_rights": values.get("SOURCE_RIGHTS", "unknown"),
            "source_rights_note": values.get("SOURCE_RIGHTS_NOTE", ""),
            "transform_level": values.get("TRANSFORM_LEVEL", "branded"),
            "source_third_party_music": _as_bool(
                values.get("SOURCE_THIRD_PARTY_MUSIC", "false")
            ),
            "platforms": platforms,
            "youtube_client_secrets": client_file,
            "youtube_client_file_exists": client_exists,
            "youtube_channel_id": values.get(
                "YOUTUBE_CHANNEL_ID", _default_value("YOUTUBE_CHANNEL_ID")
            ),
            "youtube_mode": youtube_mode,
            "youtube_effective_mode": "api" if youtube_api_enabled else "static",
            "youtube_api_enabled": youtube_api_enabled,
            "youtube_privacy": values.get(
                "YOUTUBE_PRIVACY_STATUS", _default_value("YOUTUBE_PRIVACY_STATUS")
            ),
            "youtube_made_for_kids": _as_bool(
                values.get("YOUTUBE_MADE_FOR_KIDS", "false")
            ),
            "youtube_default_language": values.get(
                "YOUTUBE_DEFAULT_LANGUAGE", "ar"
            ),
            "youtube_category_id": values.get("YOUTUBE_CATEGORY_ID", "25"),
            "youtube_title_override": values.get("YOUTUBE_TITLE_OVERRIDE", ""),
            "youtube_description_override": values.get(
                "YOUTUBE_DESCRIPTION_OVERRIDE", ""
            ),
            "youtube_tags_override": values.get("YOUTUBE_TAGS_OVERRIDE", ""),
            "youtube_backup": _as_bool(values.get("YOUTUBE_BACKUP", "false")),
            "youtube_authorized": token_file.is_file(),
            "youtube_static_key_set": bool(
                values.get("YOUTUBE_STREAM_KEY", "").strip()
            ),
            "facebook_target_id": values.get("FACEBOOK_TARGET_ID", "me"),
            "facebook_graph_version": values.get("FACEBOOK_GRAPH_VERSION", "v26.0"),
            "gemini_api_key_set": gemini_key_set,
            "gemini_model": values.get("GEMINI_MODEL", _default_value("GEMINI_MODEL")),
            "thumbnail_mode": values.get("THUMBNAIL_MODE", "auto"),
            "brand_name": values.get("BRAND_NAME", "Oussama News"),
            "gemini_metadata_enabled": gemini_enabled,
            "seo_rewrite": _as_bool(values.get("SEO_REWRITE", "true")),
            "metadata_api_enabled": metadata_api_enabled,
            "youtube_thumbnail_ready": bool(thumbnail_path),
            "youtube_thumbnail_name": thumbnail_path.name if thumbnail_path else "",
            "youtube_thumbnail_id": thumbnail_id,
            "thumbnail_provider": self.status.snapshot().get("thumbnail_provider", ""),
            "metadata_language": values.get("METADATA_LANGUAGE", "ar"),
            "logo_enabled": logo_enabled,
            "logo_file": str(logo_file),
            "logo_position": values.get("LOGO_POSITION", "bottom-right"),
            "logo_size_percent": int(values.get("LOGO_SIZE_PERCENT", "12")),
            "logo_opacity": float(values.get("LOGO_OPACITY", "0.9")),
            "frame_enabled": frame_enabled,
            "frame_file": str(frame_file) if frame_file else "",
            "facebook_token_set": bool(values.get("FACEBOOK_ACCESS_TOKEN", "").strip()),
            "facebook_static_key_set": bool(
                values.get("FACEBOOK_STREAM_KEY", "").strip()
            ),
            "facebook_token_age_days": self._facebook_token_age_days(values),
            "facebook_token_stale": self._facebook_token_stale(values),
            "initial_profile": values.get("INITIAL_PROFILE", "720p"),
            "adaptive_quality": _as_bool(values.get("ADAPTIVE_QUALITY", "true")),
            "pipe_mode": _as_bool(values.get("PIPE_MODE", "false")),
            "ffmpeg": values.get("FFMPEG", ""),
            "ffmpeg_preset": values.get("FFMPEG_PRESET", "veryfast"),
            "ytdlp_format": values.get("YTDLP_FORMAT", _default_value("YTDLP_FORMAT")),
            "cookie_file": values.get("YTDLP_COOKIE_FILE", ""),
            "cookies_from_browser": values.get("YTDLP_COOKIES_FROM_BROWSER", ""),
            "is_running": self._is_running(),
            "missing": missing,
            "notes": notes,
        }
        return config

    def save_config(
        self, data: dict[str, Any], allow_running: bool = False
    ) -> dict[str, Any]:
        with self._lock:
            if self._is_running() and not allow_running:
                raise RuntimeError("Stop the relay before changing settings")
            if allow_running and not set(data).issubset(METADATA_OVERRIDE_KEYS):
                raise RuntimeError("Only live metadata can be changed while streaming")
            updates: dict[str, str] = {}
            for key in CONFIG_KEYS:
                if key in data:
                    updates[key] = str(data[key]).strip()
            source_url = updates.get("SOURCE_URL", self._values().get("SOURCE_URL", ""))
            if source_url:
                parsed_source = urlsplit(source_url)
                if (
                    parsed_source.scheme not in {"http", "https"}
                    or not parsed_source.hostname
                ):
                    raise ValueError("The source URL must be a valid HTTP or HTTPS URL")
            source_platform = updates.get(
                "SOURCE_PLATFORM", self._values().get("SOURCE_PLATFORM", "auto")
            ).lower()
            if not source_url:
                source_url = self._values().get(
                    "SOURCE_URL", _default_value("SOURCE_URL")
                )
            validate_source_platform(source_url, source_platform)
            platforms_raw = updates.get(
                "LIVE_PLATFORMS",
                self._values().get("LIVE_PLATFORMS", "youtube,facebook"),
            )
            platforms = [
                part.strip().lower()
                for part in platforms_raw.split(",")
                if part.strip()
            ]
            if not platforms or any(
                item not in {"youtube", "facebook"} for item in platforms
            ):
                raise ValueError("Choose YouTube, Facebook, or both")
            if "YOUTUBE_MODE" in updates and updates["YOUTUBE_MODE"] not in {
                "auto",
                "api",
                "static",
            }:
                raise ValueError("Choose auto, api, or static for the YouTube mode")
            if "YOUTUBE_CHANNEL_ID" in updates and not re.fullmatch(
                r"UC[A-Za-z0-9_-]{22}", updates["YOUTUBE_CHANNEL_ID"]
            ):
                raise ValueError(
                    "The YouTube channel ID should begin with UC and contain 24 characters"
                )
            if "YOUTUBE_PRIVACY_STATUS" in updates and updates[
                "YOUTUBE_PRIVACY_STATUS"
            ] not in {"public", "unlisted", "private"}:
                raise ValueError(
                    "Choose public, unlisted, or private for YouTube privacy"
                )
            if "SOURCE_RIGHTS" in updates and updates["SOURCE_RIGHTS"] not in {
                "owned",
                "licensed",
                "third_party",
                "unknown",
            }:
                raise ValueError(
                    "SOURCE_RIGHTS must be owned, licensed, third_party, or unknown"
                )
            if (
                "SOURCE_RIGHTS_NOTE" in updates
                and len(updates["SOURCE_RIGHTS_NOTE"]) > 300
            ):
                raise ValueError("Keep the rights note under 300 characters")
            if "THUMBNAIL_MODE" in updates and updates["THUMBNAIL_MODE"] not in {
                "auto",
                "local",
                "gemini",
                "off",
            }:
                raise ValueError("THUMBNAIL_MODE must be auto, local, gemini, or off")
            if "BRAND_NAME" in updates and len(updates["BRAND_NAME"]) > 60:
                raise ValueError("Keep the brand name under 60 characters")
            if "TRANSFORM_LEVEL" in updates and updates["TRANSFORM_LEVEL"] not in {
                "raw",
                "branded",
                "editorial",
            }:
                raise ValueError("TRANSFORM_LEVEL must be raw, branded, or editorial")
            if "YOUTUBE_DEFAULT_LANGUAGE" in updates and not re.fullmatch(
                r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?",
                updates["YOUTUBE_DEFAULT_LANGUAGE"],
            ):
                raise ValueError("The default language should look like ar or ar-DZ")
            if "YOUTUBE_CATEGORY_ID" in updates and updates[
                "YOUTUBE_CATEGORY_ID"
            ] not in {
                "10",
                "17",
                "20",
                "22",
                "23",
                "24",
                "25",
                "26",
                "27",
            }:
                raise ValueError("Choose a supported YouTube category")
            if "YOUTUBE_TITLE_OVERRIDE" in updates:
                title = " ".join(updates["YOUTUBE_TITLE_OVERRIDE"].split())
                if len(title) > 100:
                    raise ValueError(
                        "The YouTube title must be 100 characters or fewer"
                    )
                updates["YOUTUBE_TITLE_OVERRIDE"] = clean_title(title) if title else ""
            if "YOUTUBE_DESCRIPTION_OVERRIDE" in updates:
                description = updates["YOUTUBE_DESCRIPTION_OVERRIDE"]
                if len(description) > 5000:
                    raise ValueError(
                        "The description must be 5,000 characters or fewer"
                    )
                updates["YOUTUBE_DESCRIPTION_OVERRIDE"] = clean_description(description)
            if "YOUTUBE_TAGS_OVERRIDE" in updates:
                raw_tags = updates["YOUTUBE_TAGS_OVERRIDE"]
                if len(raw_tags) > 500:
                    raise ValueError("The keywords must be 500 characters or fewer")
                updates["YOUTUBE_TAGS_OVERRIDE"] = ", ".join(
                    clean_tags(
                        raw_tags.replace(";", ",")
                        .replace("،", ",")
                        .replace(chr(10), ",")
                        .split(",")
                    )
                )
            if "GEMINI_MODEL" in updates and not re.fullmatch(
                r"gemini-[A-Za-z0-9._-]+", updates["GEMINI_MODEL"]
            ):
                raise ValueError("Enter a valid Gemini model ID")
            if "METADATA_LANGUAGE" in updates and updates["METADATA_LANGUAGE"] not in {
                "ar",
                "en",
                "fr",
                "es",
                "auto",
            }:
                raise ValueError(
                    "Choose Arabic, English, French, Spanish, or automatic"
                )
            for key in (
                "GEMINI_METADATA_ENABLED",
                "SEO_REWRITE",
                "LOGO_ENABLED",
                "FRAME_ENABLED",
                "YOUTUBE_MADE_FOR_KIDS",
                "SOURCE_THIRD_PARTY_MUSIC",
            ):
                if key in updates and updates[key].lower() not in {
                    "1",
                    "true",
                    "yes",
                    "on",
                    "0",
                    "false",
                    "no",
                    "off",
                }:
                    raise ValueError(f"{key} must be a boolean value")
            if "LOGO_POSITION" in updates and updates["LOGO_POSITION"] not in {
                "top-left",
                "top-right",
                "bottom-left",
                "bottom-right",
            }:
                raise ValueError("Choose a corner for the logo")
            if (
                "LOGO_SIZE_PERCENT" in updates
                and not updates["LOGO_SIZE_PERCENT"].isdigit()
            ):
                raise ValueError("Logo size must be a whole-number percentage")
            if (
                "LOGO_SIZE_PERCENT" in updates
                and not 5 <= int(updates["LOGO_SIZE_PERCENT"]) <= 25
            ):
                raise ValueError("Logo size must be between 5 and 25 percent")
            if "LOGO_OPACITY" in updates:
                try:
                    opacity = float(updates["LOGO_OPACITY"])
                except ValueError as exc:
                    raise ValueError("Logo opacity must be a number") from exc
                if not 0.1 <= opacity <= 1:
                    raise ValueError("Logo opacity must be between 0.1 and 1")
            if "FRAME_FILE" in updates and updates["FRAME_FILE"].strip():
                frame_file = resolve_project_path(updates["FRAME_FILE"])
                if frame_file.suffix.lower() != ".png" or not frame_file.is_file():
                    raise ValueError("FRAME_FILE must point to an existing PNG image")
            if "INITIAL_PROFILE" in updates and updates["INITIAL_PROFILE"] not in {
                "360p",
                "480p",
                "720p",
                "1080p",
            }:
                raise ValueError("Choose 360p, 480p, 720p, or 1080p")
            if "FACEBOOK_GRAPH_VERSION" in updates and not re.fullmatch(
                r"v[0-9]+\.[0-9]+", updates["FACEBOOK_GRAPH_VERSION"]
            ):
                raise ValueError("The Facebook Graph version should look like v26.0")
            if "FFMPEG_PRESET" in updates and updates["FFMPEG_PRESET"] not in {
                "ultrafast",
                "superfast",
                "veryfast",
                "faster",
                "fast",
            }:
                raise ValueError("Choose a supported FFmpeg preset")
            current_values = self._values()
            youtube_mode = updates.get(
                "YOUTUBE_MODE", current_values.get("YOUTUBE_MODE", "auto")
            ).lower()
            client_path = updates.get(
                "YOUTUBE_CLIENT_SECRETS_FILE",
                current_values.get("YOUTUBE_CLIENT_SECRETS_FILE", ""),
            )
            if (
                youtube_mode != "static"
                and client_path
                and not Path(client_path).expanduser().is_file()
            ):
                raise ValueError("The selected Google OAuth JSON file does not exist")
            for key in SECRET_KEYS:
                if key in data:
                    value = str(data[key]).strip()
                    if value:
                        updates[key] = value
                    elif data.get(f"clear_{key.lower()}"):
                        updates[key] = ""
            for key in CONFIG_KEYS + SECRET_KEYS:
                if key in updates:
                    set_key(str(self.env_path), key, updates[key], quote_mode="auto")
            try:
                os.chmod(self.env_path, 0o600)
            except OSError:
                pass
            load_dotenv(self.env_path, override=True)
            if updates.get("FACEBOOK_ACCESS_TOKEN", "").strip():
                self._write_token_meta(saved_at=time.time())
            return self.public_config()

    def _metadata_from_request(self, data: dict[str, Any]) -> StreamMetadata:
        raw_title = str(data.get("title", ""))
        title = " ".join(raw_title.split())
        if len(title) > 100:
            raise ValueError("The YouTube title must be 100 characters or fewer")
        raw_description = str(data.get("description", ""))
        if len(raw_description) > 5000:
            raise ValueError("The description must be 5,000 characters or fewer")
        raw_tags = data.get("tags", "")
        tag_text = (
            ",".join(str(tag) for tag in raw_tags)
            if isinstance(raw_tags, list)
            else str(raw_tags)
        )
        if len(tag_text) > 500:
            raise ValueError("The keywords must be 500 characters or fewer")
        if isinstance(raw_tags, list):
            tags = clean_tags(raw_tags)
        else:
            tag_text = (
                tag_text.replace(";", ",").replace("،", ",").replace(chr(10), ",")
            )
            tags = clean_tags(tag_text.split(","))
        values = self._values()
        return StreamMetadata(
            title=clean_title(title) if title else "",
            description=clean_description(raw_description),
            tags=tags,
            category_id=values.get("YOUTUBE_CATEGORY_ID", "22"),
            provider="manual",
        )

    def apply_metadata(self, data: dict[str, Any]) -> dict[str, Any]:
        metadata = self._metadata_from_request(data)
        updates = {
            "YOUTUBE_TITLE_OVERRIDE": metadata.title,
            "YOUTUBE_DESCRIPTION_OVERRIDE": metadata.description,
            "YOUTUBE_TAGS_OVERRIDE": ", ".join(metadata.tags),
        }
        with self._lock:
            running = self._is_running()
            if running and not metadata.title:
                raise ValueError("Enter a title, or choose automatic metadata")
            self.save_config(updates, allow_running=True)
            self.status.set_metadata_overrides(updates if running else None)
            if not running:
                return {
                    "ok": True,
                    "saved": True,
                    "applied_now": False,
                    "message": "حُفظت البيانات للبث القادم. اترك الحقول فارغة لاستخدام Gemini أو بيانات المصدر.",
                }
            session = self.status.destination_session
            if session is None:
                return {
                    "ok": True,
                    "saved": True,
                    "applied_now": False,
                    "message": "حُفظت البيانات؛ سيُطبّقها البرنامج عند توفر وجهة بث متصلة عبر API.",
                }
            results = update_destination_metadata(session, metadata, manual=True)
            updated = [name for name, result in results.items() if result == "updated"]
            if updated:
                self.status.update(
                    title=metadata.title,
                    description=metadata.description,
                    tags=list(metadata.tags),
                    metadata_provider="manual",
                    metadata_manual=True,
                )
                message = (
                    "حُفظت البيانات وطُبّقت على البث الحالي عبر الوجهات المتصلة بـ API."
                )
            else:
                message = "حُفظت البيانات للبث القادم؛ الوجهة الحالية تعمل بمفتاح ثابت ولا تسمح بتعديل العنوان مباشرةً."
            return {
                "ok": True,
                "saved": True,
                "applied_now": bool(updated),
                "results": results,
                "message": message,
            }

    def reset_metadata(self) -> dict[str, Any]:
        updates = {key: "" for key in METADATA_OVERRIDE_KEYS}
        with self._lock:
            running = self._is_running()
            self.save_config(updates, allow_running=True)
            self.status.set_metadata_overrides({} if running else None)
            if not running:
                return {
                    "ok": True,
                    "saved": True,
                    "applied_now": False,
                    "message": "أُزيل التخصيص اليدوي؛ سيعود Gemini أو بيانات المصدر في البث القادم.",
                }
            session = self.status.destination_session
            if session is None or session.base_metadata is None:
                return {
                    "ok": True,
                    "saved": True,
                    "applied_now": False,
                    "message": "أُزيل التخصيص للبث القادم؛ لا توجد وجهة API مفتوحة لتحديث البث الحالي.",
                }
            base_metadata = session.base_metadata
            results = update_destination_metadata(
                session,
                base_metadata,
                manual=False,
                base_metadata=base_metadata,
                disable_config_overrides=True,
            )
            updated = [name for name, result in results.items() if result == "updated"]
            if updated:
                self.status.update(
                    title=base_metadata.title,
                    description=base_metadata.description,
                    tags=list(base_metadata.tags),
                    metadata_provider=base_metadata.provider,
                    metadata_manual=False,
                )
            return {
                "ok": True,
                "saved": True,
                "applied_now": bool(updated),
                "results": results,
                "message": (
                    "عاد البث الحالي إلى بيانات Gemini أو المصدر، وأُزيل التخصيص للبث القادم."
                    if updated
                    else "أُزيل التخصيص للبث القادم؛ لم تقبل أي وجهة API التحديث الحالي."
                ),
            }

    def youtube_thumbnail_path(self) -> Path | None:
        return get_thumbnail_path(self._current_state_dir())

    def upload_youtube_thumbnail(self, data: bytes) -> dict[str, Any]:
        state_dir = self._current_state_dir()
        thumbnail_path, info = save_thumbnail(state_dir, data)
        set_manual_hold(state_dir, True)
        self.status.update(thumbnail_provider="manual")
        set_manual_hold(state_dir, True)
        session = self.status.destination_session if self._is_running() else None
        results = (
            update_destination_thumbnail(session, thumbnail_path) if session else {}
        )
        applied = results.get("youtube") == "updated"
        self.status.update(thumbnail_ready=True)
        if applied:
            message = "حُفظت الصورة وطُبّقت على بث YouTube الحالي."
        elif results.get("youtube") == "YouTube API destination is not active":
            message = (
                "حُفظت الصورة؛ وضع المفتاح الثابت يتطلب رفعها يدوياً من YouTube Studio."
            )
        elif results:
            message = "حُفظت الصورة، لكن YouTube لم يقبل تطبيقها الآن؛ سيعيد البرنامج المحاولة في البث القادم."
        else:
            message = "حُفظت الصورة للبثوث القادمة التي ينشئها البرنامج عبر YouTube API."
        return {
            "ok": True,
            "saved": True,
            "applied_now": applied,
            "results": results,
            "message": message,
            **info,
        }

    def clear_youtube_thumbnail(self) -> dict[str, Any]:
        state_dir = self._current_state_dir()
        clear_thumbnail(state_dir)
        set_manual_hold(state_dir, False)
        self.status.update(thumbnail_provider="", thumbnail_ready=False)
        return {
            "ok": True,
            "message": "حُذفت الصورة المصغرة المحفوظة؛ سيعيد Gemini توليدها تلقائياً إذا كانت مفعّلة.",
        }

    def verify_facebook_access(self) -> dict[str, str]:
        load_dotenv(self.env_path, override=True)
        settings = load_settings(platforms_override=("facebook",))
        if not settings.facebook_access_token:
            raise RuntimeError(
                "Save a Facebook User Access Token or Page Access Token before checking."
            )
        result = FacebookDestination(settings).verify_access()
        self._write_token_meta(last_verified_at=time.time())
        return result

    def verify_youtube_access(self) -> dict[str, str]:
        load_dotenv(self.env_path, override=True)
        settings = load_settings(platforms_override=("youtube",))
        if not settings.youtube_api_enabled:
            raise RuntimeError(
                "Select YouTube API mode before checking the linked YouTube account."
            )
        if not settings.youtube_client_secrets:
            raise RuntimeError(
                "Select a Google OAuth JSON file before checking YouTube."
            )
        if not (settings.state_dir / "youtube-token.json").is_file():
            raise ValueError(
                "لم يتم ربط YouTube بعد على هذا الجهاز. اضغط «ربط YouTube» أولاً."
            )
        return YouTubeDestination(settings).verify_access()

    def youtube_broadcast_status(self) -> dict[str, str]:
        load_dotenv(self.env_path, override=True)
        settings = load_settings(platforms_override=("youtube",))
        if not settings.youtube_api_enabled:
            raise RuntimeError(
                "Select YouTube API mode before checking the linked YouTube account."
            )
        if not settings.youtube_client_secrets:
            raise RuntimeError(
                "Select a Google OAuth JSON file before checking YouTube."
            )
        if not (settings.state_dir / "youtube-token.json").is_file():
            raise ValueError(
                "لم يتم ربط YouTube بعد على هذا الجهاز. اضغط «ربط YouTube» أولاً."
            )
        destination = YouTubeDestination(settings)
        destination.authenticate(allow_browser=False)
        return destination.check_status()

    def generate_thumbnail_now(self, data: dict[str, Any]) -> dict[str, Any]:
        """Render the local cover now, then push it to the live video if possible."""
        values = self._values()
        snapshot = self.status.snapshot()
        title = str(
            data.get("title")
            or snapshot.get("title")
            or values.get("YOUTUBE_TITLE_OVERRIDE", "")
            or ""
        ).strip()
        if len(title) < 3:
            raise RuntimeError(
                "لا يوجد عنوان بعد. انتظر ظهور المصدر أو اكتب عنواناً في حقل العنوان."
            )
        state_dir = self._current_state_dir(values)
        logo = values.get("LOGO_FILE", "").strip()
        logo_path = str(resolve_project_path(logo)) if logo else ""
        settings = SimpleNamespace(
            metadata_language=values.get(
                "METADATA_LANGUAGE", str(data.get("language") or "ar")
            ),
            brand_name=values.get("BRAND_NAME", "Oussama News"),
            logo_file=logo_path,
        )
        source = SimpleNamespace(channel=snapshot.get("source_channel", ""))
        generate_local_thumbnail(settings, source, title, state_dir)
        path = get_thumbnail_path(state_dir)
        if path is None:
            raise RuntimeError("تعذر حفظ الصورة المصغرة.")
        info = inspect_thumbnail(path.read_bytes())
        applied = False
        session = self.status.destination_session
        if session is not None and session.youtube is not None:
            results = update_destination_thumbnail(session, path)
            applied = results.get("youtube") == "updated"
        self.status.update(
            thumbnail_provider="local",
            thumbnail_ready=True,
        )
        return {
            "ok": True,
            "provider": "local",
            "path": str(path),
            "width": info["width"],
            "height": info["height"],
            "applied_now": applied,
            "message": (
                "وُلّدت الصورة محلياً وطُبّقت على البث الحالي."
                if applied
                else "وُلّدت الصورة محلياً؛ ستُطبق على البث القادم عبر API."
            ),
        }

    def youtube_video_status(self) -> dict[str, Any]:
        """Signals YouTube exposes about the current/last video.

        Ad eligibility itself is not in the Data API; this returns the fields
        that do exist (privacy, processing, made-for-kids, region blocks) and
        points the user to Studio for the monetization state.
        """
        session = self.status.destination_session
        if session is None or session.youtube is None:
            raise RuntimeError(
                "لا يوجد بث YouTube مفتوح الآن. شغّل البث أو افحص الفيديو من Studio."
            )
        return session.youtube.video_status()

    def monetization_summary(self) -> dict[str, Any]:
        state_dir = self._current_state_dir()
        data = summarize(state_dir)
        data["current"] = {
            key: self.status.snapshot().get(key)
            for key in (
                "ads_score",
                "ads_band",
                "ads_headline",
                "ads_risks",
                "ads_disclaimer",
            )
        }
        return data

    def monetization_audit(self, data: dict[str, Any]) -> dict[str, Any]:
        """Score facts the user can edit, then return a concrete plan.

        Nothing is published and nothing is sent to YouTube. The result is
        stored locally so a later limited-ads decision can be explained.
        """
        values = self._values()
        base = SessionFacts.from_mapping(
            {
                "rights": values.get("SOURCE_RIGHTS", "unknown"),
                "rights_note": values.get("SOURCE_RIGHTS_NOTE", ""),
                "transform": values.get("TRANSFORM_LEVEL", "branded"),
                "has_third_party_music": values.get(
                    "SOURCE_THIRD_PARTY_MUSIC", "false"
                ),
                "category_id": values.get("YOUTUBE_CATEGORY_ID", "25"),
                "privacy": values.get("YOUTUBE_PRIVACY_STATUS", "public"),
                "duration_seconds": int(data.get("duration_seconds") or 0),
                "original_segments": int(data.get("original_segments") or 0),
                "source_gaps": int(data.get("source_gaps") or 0),
            }
        )
        merged = base.as_dict()
        for key in (
            "title",
            "description",
            "source_channel",
            "source_platform",
            "rights",
            "transform",
            "duration_seconds",
            "original_segments",
            "source_gaps",
            "has_third_party_music",
            "category_id",
            "privacy",
        ):
            if key in data and data[key] not in (None, ""):
                merged[key] = data[key]
        merged["duration_seconds"] = (
            merged.get("duration_seconds")
            or self.status.snapshot().get("uptime_seconds", 0)
        )
        facts = SessionFacts.from_mapping(merged)
        audit = audit_session(facts)
        plan = build_transform_plan(
            facts,
            data.get("chapters") or [],
            channel_name=str(data.get("channel_name") or ""),
        )
        record_audit(
            self._current_state_dir(values),
            audit,
            trigger="dashboard",
        )
        return {
            "ok": True,
            "audit": audit.as_dict(),
            "plan": plan,
            "markdown": render_plan_markdown(plan, audit),
        }

    def metadata_suggest(self, data: dict[str, Any]) -> dict[str, Any]:
        """Rewrite the title/description/keywords without inventing facts."""
        snapshot = self.status.snapshot()
        values = self._values()
        raw_tags = data.get("tags")
        if isinstance(raw_tags, str):
            tags = [
                part.strip()
                for part in re.split(r"[,;،\n]+", raw_tags)
                if part.strip()
            ]
        elif isinstance(raw_tags, (list, tuple)):
            tags = [str(item) for item in raw_tags if str(item).strip()]
        else:
            tags = [str(item) for item in snapshot.get("tags") or []]
        chapters: list[str] = []
        session = self.status.destination_session
        if session is not None and isinstance(
            getattr(session, "editorial_events", None), list
        ):
            chapters = chapter_block(
                [*session.chapter_events, *session.editorial_events],
                "بداية البث",
            )
        base_title = str(data.get("title") or snapshot.get("title") or "")
        original_title = str(
            data.get("source_title")
            or snapshot.get("source_title")
            or base_title
        )
        proposal = propose_metadata(
            source_title=base_title,
            reference_title=original_title,
            source_description=str(
                data.get("description") or snapshot.get("description") or ""
            ),
            source_channel=str(
                data.get("channel") or snapshot.get("source_channel") or ""
            ),
            source_tags=tags,
            language=str(data.get("language") or values.get("METADATA_LANGUAGE", "ar")),
            chapters=chapters,
            rights=str(values.get("SOURCE_RIGHTS", "unknown")),
        )
        return {"ok": True, **proposal.as_dict()}

    def newsroom_brief(self, data: dict[str, Any]) -> dict[str, Any]:
        """A factual brief from the source text, plus presenter prompts."""
        snapshot = self.status.snapshot()
        title = str(data.get("title") or snapshot.get("title") or "")
        description = str(data.get("description") or snapshot.get("description") or "")
        brief = build_brief(
            title,
            description,
            str(data.get("source_channel") or snapshot.get("source_channel") or ""),
            str(data.get("source_platform") or snapshot.get("source_platform") or "custom"),
        )
        markdown = render_brief_markdown(brief)
        path = write_brief(self._current_state_dir(), brief)
        self.status.update(brief_facts=len(brief.facts), brief_path=str(path))
        return {
            "ok": True,
            "brief": brief.as_dict(),
            "markdown": markdown,
            "path": str(path),
        }

    def session_markers(self) -> dict[str, Any]:
        session = self.status.destination_session
        events = session.editorial_events if session is not None else []
        chapters = chapter_block(events, "بداية البث") if events else []
        return {
            "ok": True,
            "markers": summarize_markers(events),
            "chapters": chapters,
            "chapters_applied": bool(
                getattr(session, "chapters_applied", False) if session else False
            ),
        }

    def add_session_marker(self, data: dict[str, Any]) -> dict[str, Any]:
        session = self.status.destination_session
        if session is None:
            raise RuntimeError(
                "لا يوجد بث نشط. سجّل المقاطع أثناء البث ليصبح لها وقت وفصل."
            )
        seconds = data.get("seconds")
        event = add_editorial_marker(
            session,
            str(data.get("title") or ""),
            int(seconds) if seconds not in (None, "") else None,
        )
        markers = summarize_markers(session.editorial_events)
        self.status.update(editorial_markers=markers)
        return {
            "ok": True,
            "marker": event,
            "markers": markers,
            "chapters": chapter_block(session.editorial_events, "بداية البث"),
        }

    def remove_session_marker(self, data: dict[str, Any]) -> dict[str, Any]:
        session = self.status.destination_session
        if session is None:
            raise RuntimeError("لا يوجد بث نشط.")
        try:
            index = int(data.get("index"))
        except (TypeError, ValueError):
            raise ValueError("رقم المقطع غير صالح.") from None
        events = remove_editorial_marker(session, index)
        markers = summarize_markers(events)
        self.status.update(editorial_markers=markers)
        return {
            "ok": True,
            "markers": markers,
            "chapters": chapter_block(events, "بداية البث"),
        }

    def apply_chapters_now(self) -> dict[str, Any]:
        session = self.status.destination_session
        if session is None:
            raise RuntimeError("لا يوجد بث نشط لتطبيق الفصول عليه.")
        result = apply_session_chapters(session)
        if session.metadata is not None:
            self.status.update(
                description=session.metadata.description,
                chapters_applied=bool(result.get("updated") or session.chapters_applied),
            )
        return {"ok": True, **result}

    def weekly_report(self, days: int = 7) -> dict[str, Any]:
        state_dir = self._current_state_dir()
        path = write_weekly_report(state_dir, days=max(1, min(int(days), 90)))
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("تعذر قراءة التقرير.") from exc
        return {"ok": True, "path": str(path), "markdown": markdown}

    def session_kit(self) -> dict[str, Any]:
        state_dir = self._current_state_dir()
        latest = latest_markdown(state_dir, "kits")
        if latest is not None:
            path, markdown = latest
            self.status.update(last_kit=str(path))
            return {"ok": True, "path": str(path), "markdown": markdown}
        records = read_jsonl(session_path(state_dir), limit=1)
        if not records:
            raise RuntimeError(
                "لا توجد جلسة محفوظة بعد. ينشأ ملف الحلقة تلقائياً عند انتهاء البث."
            )
        record = records[-1]
        brief = brief_from_facts(record.get("facts"))
        markdown = build_session_kit(
            record,
            brief=brief,
            chapters=record.get("chapters") or [],
            rights_note=str((record.get("facts") or {}).get("rights_note") or ""),
        )
        path = write_kit(state_dir, record, markdown)
        self.status.update(last_kit=str(path))
        return {"ok": True, "path": str(path), "markdown": markdown}

    def preflight(self) -> dict[str, Any]:
        load_dotenv(self.env_path, override=True)
        config = self.public_config()
        missing = config["missing"]
        if missing:
            raise RuntimeError("الإعداد غير جاهز: " + " ".join(missing))
        if config.get("notes"):
            logging.warning(
                "Manual fallback mode active: %s", " ".join(config["notes"])
            )
        settings = load_settings()
        ffmpeg = resolve_ffmpeg(settings.ffmpeg_path)
        ytdlp = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.strip()
        state_dir = settings.state_dir
        youtube_needs_auth = (
            "youtube" in settings.platforms
            and settings.youtube_api_enabled
            and not (state_dir / "youtube-token.json").is_file()
        )
        return {
            "ok": True,
            "ffmpeg": Path(ffmpeg).name,
            "yt_dlp": ytdlp,
            "platforms": list(settings.platforms),
            "youtube_needs_auth": youtube_needs_auth,
            "modes": {
                "youtube": settings.effective_youtube_mode.upper(),
                "facebook": "API" if settings.facebook_access_token else "Static key",
            },
        }

    def authorize_youtube(self) -> None:
        if self._is_running():
            raise RuntimeError("Stop the relay before authorizing YouTube")
        load_dotenv(self.env_path, override=True)
        original_platforms = os.environ.get("LIVE_PLATFORMS")
        os.environ["LIVE_PLATFORMS"] = "youtube"
        try:
            settings = load_settings()
        finally:
            if original_platforms is None:
                os.environ.pop("LIVE_PLATFORMS", None)
            else:
                os.environ["LIVE_PLATFORMS"] = original_platforms
        if not settings.youtube_api_enabled:
            raise RuntimeError("Select YouTube API mode before authorizing an account.")
        if not settings.youtube_client_secrets:
            raise RuntimeError(
                "Set the Google OAuth client JSON path before authorizing YouTube"
            )
        YouTubeDestination(settings).authenticate()

    def _apply_platform_override(self, value: str) -> None:
        self._platform_override_previous = os.environ.get("LIVE_PLATFORMS")
        os.environ["LIVE_PLATFORMS"] = value

    def _restore_platform_override(self) -> None:
        previous = getattr(self, "_platform_override_previous", None)
        if previous is None:
            os.environ.pop("LIVE_PLATFORMS", None)
        else:
            os.environ["LIVE_PLATFORMS"] = previous

        if self._is_running():
            raise RuntimeError("Stop the relay before authorizing YouTube")
        load_dotenv(self.env_path, override=True)
        original_platforms = os.environ.get("LIVE_PLATFORMS")
        os.environ["LIVE_PLATFORMS"] = "youtube"
        try:
            settings = load_settings()
        finally:
            if original_platforms is None:
                os.environ.pop("LIVE_PLATFORMS", None)
            else:
                os.environ["LIVE_PLATFORMS"] = original_platforms
        if not settings.youtube_api_enabled:
            raise RuntimeError("Select YouTube API mode before authorizing an account.")
        if not settings.youtube_client_secrets:
            raise RuntimeError(
                "Set the Google OAuth client JSON path before authorizing YouTube"
            )
        url = YouTubeDestination(settings).manual_auth_url()
        return {"authorization_url": url}

    def youtube_manual_auth_exchange(self, code: str) -> None:
        if self._is_running():
            raise RuntimeError("Stop the relay before authorizing YouTube")
        load_dotenv(self.env_path, override=True)
        original_platforms = os.environ.get("LIVE_PLATFORMS")
        os.environ["LIVE_PLATFORMS"] = "youtube"
        try:
            settings = load_settings()
        finally:
            if original_platforms is None:
                os.environ.pop("LIVE_PLATFORMS", None)
            else:
                os.environ["LIVE_PLATFORMS"] = original_platforms
        if not settings.youtube_api_enabled:
            raise RuntimeError("Select YouTube API mode before authorizing an account.")
        if not settings.youtube_client_secrets:
            raise RuntimeError(
                "Set the Google OAuth client JSON path before authorizing YouTube"
            )
        YouTubeDestination(settings).manual_auth_exchange(code)

    def start_relay(self) -> dict[str, Any]:
        with self._lock:
            if self._is_running():
                raise RuntimeError("The relay is already running")
            load_dotenv(self.env_path, override=True)
            missing = self.public_config()["missing"]
            if missing:
                raise RuntimeError("الإعداد غير جاهز: " + " ".join(missing))
            settings = load_settings()
            resolve_ffmpeg(settings.ffmpeg_path)
            if "youtube" in settings.platforms and settings.youtube_api_enabled:
                if not (settings.state_dir / "youtube-token.json").is_file():
                    raise RuntimeError(
                        "Authorize the YouTube account first using the YouTube authorization button"
                    )
            self.status.set_destination_session(None)
            self.status.set_metadata_overrides(None)
            self._stop_event = threading.Event()
            self.status.update(
                mode="starting",
                message="Starting relay",
                title="",
                description="",
                tags=[],
                metadata_provider="",
                metadata_manual=False,
                quality=settings.initial_profile,
                bitrate="",
                started_at=time.time(),
                retry_count=0,
                last_error="",
                active_platforms=[],
                destination_errors={},
                editorial_markers=[],
                chapters_applied=False,
            )
            self._relay_thread = threading.Thread(
                target=self._run_relay,
                args=(settings, self._stop_event),
                daemon=True,
                name="live-relay",
            )
            self._relay_thread.start()
            return self.get_status()

    def _run_relay(self, settings, stopped: threading.Event) -> None:
        try:
            RelaySupervisor(settings, stopped, self.status).run()
        except Exception as exc:
            message = redact_urls(str(exc).replace("\n", " "))
            for secret in (
                getattr(settings, "youtube_static_key", ""),
                getattr(settings, "facebook_access_token", ""),
                getattr(settings, "facebook_static_key", ""),
            ):
                if secret:
                    message = message.replace(secret, "[secret hidden]")
            message = message[:400]
            logging.error("Relay failed (%s): %s", type(exc).__name__, message)
            self.status.set_destination_session(None)
            self.status.update(
                mode="error",
                message="Relay stopped because of an error",
                last_error=message,
                started_at=None,
                metadata_manual=False,
                active_platforms=[],
                destination_errors={},
            )

    def stop_relay(self) -> dict[str, Any]:
        with self._lock:
            if not self._is_running() or self._stop_event is None:
                return self.get_status()
            self.status.update(mode="stopping", message="Stopping relay safely")
            self._stop_event.set()
            return self.get_status()

    def get_status(self) -> dict[str, Any]:
        data = self.status.snapshot()
        state_dir = self._current_state_dir()
        session = self.status.destination_session
        events = getattr(session, "editorial_events", None) if session else None
        if isinstance(events, list):
            # The session is the source of truth, so a page reload never
            # shows an empty marker list while markers exist.
            data["editorial_markers"] = summarize_markers(events)
            data["chapters_applied"] = bool(
                getattr(session, "chapters_applied", False)
            )
        data["is_running"] = self._is_running()
        data["heartbeat_age_seconds"] = heartbeat_age(state_dir)
        data["restarts"] = read_restarts(state_dir)[-3:]
        return data

    def get_logs(self, limit: int = 120) -> list[str]:
        values = self._values()
        log_path = self._current_state_dir(values) / "live-relay.log"
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return lines[-limit:]
        except OSError:
            return []


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_handler_class(
    controller: DashboardController, html_path: Path
) -> type[BaseHTTPRequestHandler]:
    """HTTP handler for the local dashboard, bound to one controller instance."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "LiveRelayDashboard/1"

        def _host_is_local(self) -> bool:
            host = self.headers.get("Host", "")
            hostname = urlsplit(f"//{host}").hostname
            return hostname in {"127.0.0.1", "localhost", "::1"}

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, status: int = 200) -> None:
            self._send(
                json.dumps(data, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

        def _authorized(self) -> bool:
            if not self._host_is_local():
                self._json(
                    {"error": "The dashboard only accepts local connections."}, 403
                )
                return False
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers.get('Host', '')}":
                self._json({"error": "Cross-origin requests are not allowed."}, 403)
                return False
            if not secrets.compare_digest(
                self.headers.get("X-Relay-Token", ""), controller.control_token
            ):
                self._json(
                    {"error": "Invalid local control token. Reload the dashboard."}, 403
                )
                return False
            return True

        def do_GET(self) -> None:
            if not self._host_is_local():
                self._json(
                    {"error": "The dashboard only accepts local connections."}, 403
                )
                return
            request_path = urlsplit(self.path).path
            if request_path == "/":
                try:
                    html = html_path.read_text(encoding="utf-8").replace(
                        "__CONTROL_TOKEN__", controller.control_token
                    )
                except OSError:
                    self._json({"error": "Dashboard page is missing."}, 500)
                    return
                self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            elif request_path == "/api/status":
                self._json(controller.get_status())
            elif request_path == "/api/config":
                self._json(controller.public_config())
            elif request_path == "/api/monetization":
                self._json(controller.monetization_summary())
            elif request_path == "/api/session/markers":
                self._json(controller.session_markers())
            elif request_path == "/api/reports/weekly":
                query = urlsplit(self.path).query
                days = 7
                for part in query.split("&"):
                    if part.startswith("days="):
                        try:
                            days = int(part.split("=", 1)[1])
                        except ValueError:
                            days = 7
                self._json(controller.weekly_report(days))
            elif request_path == "/api/logs":
                self._json({"lines": controller.get_logs()})
            elif request_path == "/api/youtube/thumbnail":
                if not self._authorized():
                    return
                thumbnail_path = controller.youtube_thumbnail_path()
                if thumbnail_path is None:
                    self._json({"error": "No saved YouTube thumbnail."}, 404)
                    return
                try:
                    content = thumbnail_path.read_bytes()
                except OSError:
                    self._json({"error": "The saved thumbnail could not be read."}, 500)
                    return
                mime_type = (
                    "image/png"
                    if thumbnail_path.suffix.lower() == ".png"
                    else "image/jpeg"
                )
                self._send(content, mime_type)
            else:
                self._json({"error": "Not found"}, 404)

        def do_POST(self) -> None:
            if not self._authorized():
                return
            try:
                request_path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                limit = (
                    MAX_THUMBNAIL_SIZE
                    if request_path == "/api/youtube/thumbnail"
                    else 65536
                )
                if length < 0 or length > limit:
                    self._json({"error": "حجم الطلب أكبر من الحد المسموح."}, 413)
                    return
                raw = self.rfile.read(length) if length else b"{}"
                if len(raw) != length:
                    raise ValueError("لم يكتمل رفع الملف؛ أعد المحاولة.")
                if request_path == "/api/youtube/thumbnail":
                    self._json(controller.upload_youtube_thumbnail(raw))
                    return
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("Request body must be a JSON object")
                if request_path == "/api/config":
                    self._json(controller.save_config(data))
                elif request_path == "/api/metadata/apply":
                    self._json(controller.apply_metadata(data))
                elif request_path == "/api/metadata/reset":
                    self._json(controller.reset_metadata())
                elif request_path == "/api/check":
                    self._json(controller.preflight())
                elif request_path == "/api/youtube/authorize":
                    controller.authorize_youtube()
                    self._json(
                        {
                            "ok": True,
                            "message": "YouTube account authorized on this computer.",
                        }
                    )
                elif request_path == "/api/youtube/manual-auth/url":
                    self._json(controller.youtube_manual_auth_url())
                elif request_path == "/api/youtube/manual-auth/exchange":
                    controller.youtube_manual_auth_exchange(str(data.get("code", "")))
                    self._json(
                        {
                            "ok": True,
                            "message": "YouTube account authorized with the manual code.",
                        }
                    )
                elif request_path == "/api/youtube/verify":
                    self._json({"ok": True, **controller.verify_youtube_access()})
                elif request_path == "/api/youtube/status":
                    self._json({"ok": True, **controller.youtube_broadcast_status()})
                elif request_path == "/api/facebook/verify":
                    self._json({"ok": True, **controller.verify_facebook_access()})
                elif request_path == "/api/monetization/audit":
                    self._json(controller.monetization_audit(data))
                elif request_path == "/api/youtube/thumbnail/generate":
                    self._json(controller.generate_thumbnail_now(data))
                elif request_path == "/api/metadata/suggest":
                    self._json(controller.metadata_suggest(data))
                elif request_path == "/api/newsroom/brief":
                    self._json(controller.newsroom_brief(data))
                elif request_path == "/api/session/marker":
                    self._json(controller.add_session_marker(data))
                elif request_path == "/api/session/marker/remove":
                    self._json(controller.remove_session_marker(data))
                elif request_path == "/api/session/chapters":
                    self._json(controller.apply_chapters_now())
                elif request_path == "/api/sessions/kit":
                    self._json(controller.session_kit())
                elif request_path == "/api/youtube/video-status":
                    self._json({"ok": True, **controller.youtube_video_status()})
                elif request_path == "/api/start":
                    self._json(controller.start_relay(), 202)
                elif request_path == "/api/stop":
                    self._json(controller.stop_relay())
                else:
                    self._json({"error": "Not found"}, 404)
            except (
                ValueError,
                RuntimeError,
                OSError,
                subprocess.SubprocessError,
            ) as exc:
                self._json({"error": redact_urls(str(exc))[:400]}, 400)
            except Exception as exc:
                logging.exception(
                    "Dashboard request failed: %s", redact_urls(str(exc))[:300]
                )
                self._json(
                    {
                        "error": "The request failed. Check the dashboard log for details."
                    },
                    500,
                )

        def do_DELETE(self) -> None:
            if not self._authorized():
                return
            try:
                request_path = urlsplit(self.path).path
                if request_path == "/api/youtube/thumbnail":
                    self._json(controller.clear_youtube_thumbnail())
                    return
                self._json({"error": "Not found"}, 404)
            except Exception as exc:
                self._json(
                    {"error": redact_urls(str(exc))[:300]},
                    500,
                )

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def run_dashboard(
    project_root: Path = PROJECT_ROOT, port: int = 8765, open_browser: bool = True
) -> None:
    controller = DashboardController(project_root)
    values = controller._values()
    state_dir = controller._current_state_dir(values)
    ensure_private_dir(state_dir)
    log_level = values.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                state_dir / "live-relay.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
        ],
    )
    html_path = Path(__file__).with_name("dashboard.html")

    Handler = build_handler_class(controller, html_path)

    server: DashboardServer | None = None
    requested_port = max(0, min(int(port), 65535))
    try:
        server = DashboardServer(("127.0.0.1", requested_port), Handler)
    except OSError:
        if requested_port == 0:
            raise
        logging.warning(
            "Dashboard port %s is busy; selecting an available local port",
            requested_port,
        )
        server = DashboardServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    logging.info("Local dashboard: %s", url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logging.info("Dashboard shutdown requested")
    finally:
        controller.stop_relay()
        relay_thread = controller._relay_thread
        if relay_thread:
            relay_thread.join(timeout=70)
        server.server_close()
