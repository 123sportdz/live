"""Adapter for OUSSAMA Cutter (ViralCutter fork) — keep it a separate program.

OUSSAMA Cutter is GPL-3.0 and carries a heavy GPU stack. Importing its modules
would drag both the licence and torch/gradio/insightface into the relay. This
module only runs its CLI in its own virtualenv and reads the files it writes:

    <project>/viral_segments.txt     JSON list of chosen segments
    <project>/delivery_manifest.json rendered files and counts
    <project>/final/*.mp4            the finished short clips

That keeps live-relay MIT, lets the cutter update independently, and isolates
crashes. The contract is tested with a fake cutter, so the dashboard does not
need the real stack to be installed during development.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

CLIP_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".ts"}
PLATFORMS = ("tiktok", "reels", "yt_shorts", "yt_standard")
AI_BACKENDS = ("auto", "gemini", "local", "openai", "g4f", "manual")
MAX_SEGMENTS = 20
LogSink = Callable[[str], None]


class CutterError(RuntimeError):
    """A cutter invocation or output failed with a message fit for the UI."""


@dataclass(frozen=True)
class CutterSettings:
    enabled: bool
    home: Path
    python: Path
    ai_backend: str = "gemini"
    segments: int = 6
    platform: str = "tiktok"
    workflow: int = 1
    model: str = "large-v3-turbo"
    timeout: int = 7200
    gemini_key: str = ""

    @classmethod
    def from_values(
        cls, values: Mapping[str, Any], *, base: Path | None = None
    ) -> "CutterSettings":
        home_text = str(values.get("CUTTER_HOME") or "").strip()
        home = Path(home_text).expanduser() if home_text else Path()
        if home_text and base is not None and not home.is_absolute():
            home = (base / home).resolve()
        python_text = str(values.get("CUTTER_PYTHON") or "").strip()
        if python_text:
            python = Path(python_text).expanduser()
            if (
                home_text
                and not python.is_absolute()
                and ("/" in python_text or "\\" in python_text)
            ):
                python = home / python
        elif home_text:
            python = home / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
        else:
            python = Path("python")
        try:
            segments = max(1, min(MAX_SEGMENTS, int(values.get("CUTTER_SEGMENTS") or 6)))
        except (TypeError, ValueError):
            segments = 6
        backend = str(values.get("CUTTER_AI_BACKEND") or "gemini").strip().lower()
        if backend not in AI_BACKENDS:
            backend = "gemini"
        platform = str(values.get("CUTTER_PLATFORM") or "tiktok").strip().lower()
        if platform not in PLATFORMS:
            platform = "tiktok"
        try:
            workflow = int(values.get("CUTTER_WORKFLOW") or 1)
        except (TypeError, ValueError):
            workflow = 1
        if workflow not in (1, 2, 3):
            workflow = 1
        try:
            timeout = max(300, min(6 * 3600, int(values.get("CUTTER_TIMEOUT") or 7200)))
        except (TypeError, ValueError):
            timeout = 7200
        return cls(
            enabled=_as_bool(values.get("CUTTER_ENABLED")),
            home=home,
            python=python,
            ai_backend=backend,
            segments=segments,
            platform=platform,
            workflow=workflow,
            model=str(values.get("CUTTER_MODEL") or "large-v3-turbo").strip(),
            timeout=timeout,
            gemini_key=str(values.get("GEMINI_API_KEY") or "").strip(),
        )

    def resolved_python(self) -> str:
        text = str(self.python)
        if self.python.is_file():
            return text
        found = shutil.which(text)
        return found or text


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Clip:
    index: int
    title: str
    start: float
    end: float
    duration: float
    score: float | None = None
    reason: str = ""
    caption: str = ""
    hashtags: tuple[str, ...] = ()
    file: str = ""
    blocked: bool = False
    quality: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "score": self.score,
            "reason": self.reason,
            "caption": self.caption,
            "hashtags": list(self.hashtags),
            "file": self.file,
            "blocked": self.blocked,
            "quality": self.quality,
        }


@dataclass
class CutterResult:
    project: Path
    clips: list[Clip]
    returncode: int
    log_tail: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project": str(self.project),
            "name": self.project.name,
            "returncode": self.returncode,
            "clips": [clip.as_dict() for clip in self.clips],
            "total": len(self.clips),
        }


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sanitise_name(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in " _-").strip()
    return (cleaned or "Local_Video")[:80]


def _blocked_indexes(project: Path) -> set[int]:
    path = project / "publish_blocklist.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    entries = data
    if isinstance(data, Mapping):
        entries = data.get("blocked") or data.get("items") or data.get("segments") or []
    blocked: set[int] = set()
    if not isinstance(entries, list):
        return blocked
    for entry in entries:
        index = entry.get("index") if isinstance(entry, Mapping) else entry
        try:
            blocked.add(int(index))
        except (TypeError, ValueError):
            continue
    return blocked


def parse_segments(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CutterError(f"تعذّر قراءة ملف المقاطع: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CutterError(f"ملف المقاطع ليس JSON صالحاً: {exc.msg}") from exc
    if isinstance(data, Mapping):
        segments = data.get("segments", [])
    else:
        segments = data
    if not isinstance(segments, list):
        raise CutterError("ملف المقاطع لا يحتوي قائمة segments.")
    return [segment for segment in segments if isinstance(segment, Mapping)]


def _rendered_files(project: Path) -> list[Path]:
    manifest = project / "delivery_manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            outputs = data.get("outputs") or []
            files: list[Path] = []
            for item in outputs:
                if not isinstance(item, str) or not item:
                    continue
                path = Path(item)
                if not path.is_absolute():
                    path = project / path
                if path.is_file() and path.suffix.lower() in CLIP_SUFFIXES:
                    files.append(path)
            if files:
                return files
        except (OSError, json.JSONDecodeError):
            pass
    final_dir = project / "final"
    if not final_dir.is_dir():
        return []
    return sorted(
        [
            path
            for path in final_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() in CLIP_SUFFIXES
            and not path.name.lower().startswith(("temp_", "preview_"))
        ],
        key=lambda item: item.name,
    )


def clips_from_project(project: Path) -> list[Clip]:
    project = Path(project)
    segments = parse_segments(project / "viral_segments.txt")
    files = _rendered_files(project)
    blocked = _blocked_indexes(project)
    clips: list[Clip] = []
    for index, raw in enumerate(segments):
        start = _number(raw.get("start_time", raw.get("start")))
        end = _number(raw.get("end_time", raw.get("end")))
        duration = _number(raw.get("duration"), max(0.0, end - start))
        hashtags = raw.get("hashtags") or []
        if isinstance(hashtags, str):
            hashtags = [part for part in re.split(r"[,\s]+", hashtags) if part]
        title = str(
            raw.get("recommended_title")
            or raw.get("title")
            or f"مقطع {index + 1}"
        ).strip()
        reason = str(raw.get("reasoning") or raw.get("hook") or "").strip()
        score_value = raw.get("selection_score")
        if score_value is None:
            score_value = raw.get("score")
        score = None if score_value in (None, "") else round(_number(score_value), 1)
        clip_file = str(files[index]) if index < len(files) else ""
        clips.append(
            Clip(
                index=index,
                title=title[:160],
                start=start,
                end=end,
                duration=duration,
                score=score,
                reason=reason[:500],
                caption=str(raw.get("caption") or "")[:1000],
                hashtags=tuple(str(tag).lstrip("#") for tag in hashtags)[:15],
                file=clip_file,
                blocked=index in blocked,
                quality=str(raw.get("quality_status") or ""),
            )
        )
    return clips


class CutterClient:
    def __init__(self, settings: CutterSettings) -> None:
        self.settings = settings

    def available(self) -> tuple[bool, str]:
        if not self.settings.enabled:
            return False, "محوّل OUSSAMA Cutter غير مفعّل."
        if not str(self.settings.home):
            return False, "حدّد مجلد OUSSAMA Cutter أولاً."
        if not self.settings.home.is_dir():
            return False, f"مجلد OUSSAMA Cutter غير موجود: {self.settings.home}"
        if not (self.settings.home / "main_improved.py").is_file():
            return False, "main_improved.py غير موجود في مجلد OUSSAMA Cutter."
        python = Path(self.settings.resolved_python())
        if not python.is_file() and shutil.which(str(python)) is None:
            return False, f"مفسّر Python الخاص بـ CQT1 غير موجود: {python}"
        return True, "جاهز."

    def build_command(
        self,
        recording: Path,
        *,
        segments: int | None = None,
        platform: str | None = None,
        ai_backend: str | None = None,
        workflow: int | None = None,
        force: bool = False,
    ) -> list[str]:
        count = int(segments or self.settings.segments)
        if not 1 <= count <= MAX_SEGMENTS:
            raise CutterError(f"عدد المقاطع يجب أن يكون بين 1 و{MAX_SEGMENTS}.")
        platform = (platform or self.settings.platform).lower()
        if platform not in PLATFORMS:
            raise CutterError("منصة الإخراج غير معروفة.")
        backend = (ai_backend or self.settings.ai_backend).lower()
        if backend not in AI_BACKENDS:
            raise CutterError("مزوّد الذكاء الاصطناعي غير معروف.")
        workflow = int(workflow or self.settings.workflow)
        if workflow not in (1, 2, 3):
            raise CutterError("وضع المعالجة يجب أن يكون 1 أو 2 أو 3.")
        command = [
            self.settings.resolved_python(),
            str(self.settings.home / "main_improved.py"),
            "--local-video",
            str(Path(recording).resolve()),
            "--segments",
            str(count),
            "--platform",
            platform,
            "--workflow",
            str(workflow),
            "--ai-backend",
            backend,
            "--model",
            self.settings.model or "large-v3-turbo",
            "--transcription-device",
            "cuda",
            "--safety-mode",
            "block",
            "--skip-prompts",
            "--skip-youtube-subs",
            "--output-aspect",
            "9:16",
        ]
        if force:
            command.append("--force-new-segments")
        return command

    def run(
        self,
        recording: Path,
        *,
        log: LogSink | None = None,
        force: bool = False,
    ) -> CutterResult:
        ok, message = self.available()
        if not ok:
            raise CutterError(message)
        recording = Path(recording).expanduser().resolve()
        if not recording.is_file():
            raise CutterError(f"ملف التسجيل غير موجود: {recording}")
        if recording.suffix.lower() not in CLIP_SUFFIXES:
            raise CutterError("صيغة التسجيل غير مدعومة.")
        command = self.build_command(recording, force=force)
        virals = self.settings.home / "VIRALS"
        before = {
            item.name for item in virals.iterdir() if item.is_dir()
        } if virals.is_dir() else set()
        env = os.environ.copy()
        env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                "CUDA_MODULE_LOADING": "LAZY",
                "OMP_NUM_THREADS": "8",
            }
        )
        if self.settings.gemini_key:
            env.setdefault("VIRALCUTTER_GEMINI_KEY", self.settings.gemini_key)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        started = time.monotonic()
        tail: list[str] = []
        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.settings.home),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise CutterError(f"تعذّر تشغيل CQT1: {exc}") from exc
        try:
            assert process.stdout is not None
            for line in process.stdout:
                text = line.rstrip()
                if not text:
                    continue
                tail.append(text)
                del tail[:-120]
                if log:
                    log(text[:500])
                if time.monotonic() - started > self.settings.timeout:
                    self._terminate(process)
                    raise CutterError("تجاوز التحليل المهلة المحددة.")
            returncode = process.wait(timeout=30)
        finally:
            if process.poll() is None:
                self._terminate(process)
        if returncode != 0:
            detail = " | ".join(tail[-4:])[:600]
            raise CutterError(f"فشل CQT1 (رمز {returncode}): {detail}")
        project = self.find_project(before, started, recording)
        clips = clips_from_project(project)
        return CutterResult(
            project=project,
            clips=clips,
            returncode=returncode,
            log_tail=tail[-40:],
            command=command,
        )

    def find_project(
        self, before: set[str], started: float, recording: Path
    ) -> Path:
        virals = self.settings.home / "VIRALS"
        candidates: list[Path] = []
        if virals.is_dir():
            for item in virals.iterdir():
                if not item.is_dir():
                    continue
                if item.name in before:
                    continue
                if (
                    (item / "viral_segments.txt").is_file()
                    or (item / "delivery_manifest.json").is_file()
                ):
                    candidates.append(item)
        if not candidates:
            guessed = virals / _sanitise_name(Path(recording).stem)
            if guessed.is_dir():
                candidates.append(guessed)
        if not candidates:
            raise CutterError(
                "انتهى CQT1 بلا مجلد نتائج. راجع السجل؛ قد يكون المقطع بلا كلام كافٍ."
            )
        return max(candidates, key=lambda item: item.stat().st_mtime)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def list_projects(home: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    virals = Path(home) / "VIRALS"
    if not virals.is_dir():
        return []
    projects: list[dict[str, Any]] = []
    for folder in sorted(
        virals.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True
    ):
        if not folder.is_dir():
            continue
        segments = folder / "viral_segments.txt"
        if not segments.is_file():
            continue
        try:
            count = len(parse_segments(segments))
        except CutterError:
            continue
        projects.append(
            {
                "name": folder.name,
                "path": str(folder),
                "clips": count,
                "modified": folder.stat().st_mtime,
            }
        )
        if len(projects) >= limit:
            break
    return projects


def latest_recording(state_dir: Path, *, min_age: float = 60.0) -> Path | None:
    folder = Path(state_dir) / "recordings"
    if not folder.is_dir():
        return None
    now = time.time()
    files: list[Path] = []
    for item in folder.iterdir():
        if not item.is_file() or item.suffix.lower() not in CLIP_SUFFIXES:
            continue
        try:
            if now - item.stat().st_mtime < min_age:
                continue
        except OSError:
            continue
        files.append(item)
    if not files:
        return None
    return max(files, key=lambda item: item.stat().st_mtime)
