"""End-to-end post-stream automation.

After a session ends, the relay drops a recording. This module turns that
recording into a review-ready package without a human in the loop:

    clips (CQT1) -> daily episode (16:9) -> private YouTube uploads

Every stage is optional and independently retryable. The queue is a JSON file
under the state directory, so a crash does not lose a session. A single worker
thread processes jobs; the relay and the pipeline never share a job lock.

Safety defaults:
* upload is off unless explicitly enabled, and private by default;
* a missing CQT1 or YouTube token skips that stage instead of failing the job;
* at most ``AUTO_UPLOAD_MAX`` files are uploaded per job (YouTube quota);
* outputs are kept on failure so a retry does not redo finished work.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Callable, Mapping

from dotenv import dotenv_values

from .config import default_state_dir, ensure_private_dir, resolve_project_root
from .cutter import CLIP_SUFFIXES, CutterClient, CutterSettings, CutterError
from .episode import EpisodeError, load_manifest, render_episode, write_manifest
from .notifier import Notifier
from .publish import has_upload_scope, upload_video

LOGGER = logging.getLogger(__name__)
QUEUE_VERSION = 1
DEFAULT_POLL = 60
MAX_ATTEMPTS = 3
STALE_RUNNING_SECONDS = 4 * 3600

StatusSink = Callable[[str], None]


class PipelineError(RuntimeError):
    """A pipeline stage failed with a message fit for the dashboard."""


@dataclass(frozen=True)
class PipelineConfig:
    project_root: Path
    state_dir: Path
    enabled: bool
    auto_clips: bool
    auto_episode: bool
    auto_upload: bool
    upload_privacy: str
    upload_max: int
    min_free_gb: float
    poll_seconds: int
    max_attempts: int
    retry_delay: int
    episode_max_segments: int
    brand: str
    rights: str
    rights_note: str
    recording_enabled: bool
    tags: tuple[str, ...]
    category_id: str
    made_for_kids: bool
    youtube_token: Path
    cutter: CutterSettings

    @classmethod
    def from_env(
        cls, project_root: Path | str | None = None
    ) -> "PipelineConfig":
        root = Path(project_root or resolve_project_root()).resolve()
        values: dict[str, Any] = {}
        env_file = root / ".env"
        if env_file.is_file():
            try:
                # The project .env is authoritative; a stale shell variable must
                # not silently enable uploads or change the queue's behaviour.
                values.update(
                    {
                        key: value
                        for key, value in dotenv_values(env_file).items()
                        if value is not None
                    }
                )
            except Exception:
                LOGGER.warning("could not read .env for the pipeline", exc_info=True)
        else:
            values.update(os.environ)
        state_dir = Path(
            str(values.get("LIVE_RELAY_STATE_DIR") or "")
            or str(default_state_dir())
        ).expanduser()
        if not state_dir.is_absolute():
            state_dir = (root / state_dir).resolve()
        privacy = str(values.get("AUTO_UPLOAD_PRIVACY") or "private").lower()
        if privacy not in {"private", "unlisted", "public"}:
            privacy = "private"
        try:
            upload_max = max(0, min(10, int(values.get("AUTO_UPLOAD_MAX") or 3)))
        except (TypeError, ValueError):
            upload_max = 3
        try:
            min_free = max(0.0, float(values.get("PIPELINE_MIN_FREE_GB") or 5))
        except (TypeError, ValueError):
            min_free = 5.0
        try:
            poll = max(15, min(3600, int(values.get("PIPELINE_POLL") or DEFAULT_POLL)))
        except (TypeError, ValueError):
            poll = DEFAULT_POLL
        try:
            max_attempts = max(1, min(10, int(values.get("PIPELINE_MAX_ATTEMPTS") or MAX_ATTEMPTS)))
        except (TypeError, ValueError):
            max_attempts = MAX_ATTEMPTS
        try:
            retry_delay = max(60, min(6 * 3600, int(values.get("PIPELINE_RETRY_DELAY") or 300)))
        except (TypeError, ValueError):
            retry_delay = 300
        try:
            episode_max = max(1, min(20, int(values.get("AUTO_EPISODE_MAX_SEGMENTS") or 8)))
        except (TypeError, ValueError):
            episode_max = 8
        raw_tags = str(values.get("YOUTUBE_TAGS_OVERRIDE") or "")
        tags = tuple(tag.strip() for tag in raw_tags.split(",") if tag.strip())
        return cls(
            project_root=root,
            state_dir=state_dir,
            enabled=_as_bool(values.get("AUTOPILOT_ENABLED")),
            auto_clips=_as_bool(values.get("AUTO_CLIPS", "true")),
            auto_episode=_as_bool(values.get("AUTO_EPISODE", "true")),
            auto_upload=_as_bool(values.get("AUTO_UPLOAD")),
            upload_privacy=privacy,
            upload_max=upload_max,
            min_free_gb=min_free,
            poll_seconds=poll,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
            episode_max_segments=episode_max,
            brand=str(values.get("BRAND_NAME") or "live-relay"),
            rights=str(values.get("SOURCE_RIGHTS") or "unknown"),
            rights_note=str(values.get("SOURCE_RIGHTS_NOTE") or ""),
            recording_enabled=_as_bool(values.get("RECORD_ENABLED")),
            tags=tags,
            category_id=str(values.get("YOUTUBE_CATEGORY_ID") or "25"),
            made_for_kids=_as_bool(values.get("YOUTUBE_MADE_FOR_KIDS")),
            youtube_token=state_dir / "youtube-token.json",
            cutter=CutterSettings.from_values(values, base=root),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auto_clips": self.auto_clips,
            "auto_episode": self.auto_episode,
            "auto_upload": self.auto_upload,
            "upload_privacy": self.upload_privacy,
            "upload_max": self.upload_max,
            "min_free_gb": self.min_free_gb,
            "poll_seconds": self.poll_seconds,
            "max_attempts": self.max_attempts,
            "retry_delay": self.retry_delay,
            "episode_max_segments": self.episode_max_segments,
            "brand": self.brand,
            "rights": self.rights,
            "recording_enabled": self.recording_enabled,
            "youtube_token": str(self.youtube_token),
        }


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_private_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


class PipelineStore:
    """Crash-safe job queue; one process owns it."""

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self.directory = self.state_dir / "pipeline"
        self.path = self.directory / "queue.json"
        self.lock = threading.RLock()
        ensure_private_dir(self.directory)

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": QUEUE_VERSION, "jobs": []}
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            return {"version": QUEUE_VERSION, "jobs": []}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        _write_json(self.path, data)

    def jobs(self) -> list[dict[str, Any]]:
        with self.lock:
            jobs = [dict(job) for job in self._load()["jobs"]]
        return sorted(jobs, key=lambda job: job.get("created_at", 0), reverse=True)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            for job in self._load()["jobs"]:
                if job.get("id") == job_id:
                    return dict(job)
        return None

    def enqueue(
        self,
        recording: Path | str,
        *,
        video_id: str = "",
        ended_at: float = 0,
        force: bool = False,
        manual: bool = False,
    ) -> dict[str, Any]:
        path = str(Path(recording).expanduser().resolve())
        with self.lock:
            data = self._load()
            for job in data["jobs"]:
                if job.get("recording") != path:
                    continue
                if force:
                    break
                status = job.get("status")
                if status in {"pending", "running"}:
                    return dict(job)
                if status == "done" and manual:
                    job["status"] = "pending"
                    job["stage"] = "إعادة التشغيل"
                    job["attempts"] = 0
                    job["error"] = ""
                    job["manual"] = True
                    job["updated_at"] = time.time()
                    self._save(data)
                    return dict(job)
                if status in {"done", "failed"}:
                    if status == "failed" and manual:
                        job["status"] = "pending"
                        job["stage"] = "إعادة المحاولة"
                        job["attempts"] = 0
                        job["error"] = ""
                        job["manual"] = True
                        job["updated_at"] = time.time()
                        self._save(data)
                    return dict(job)
                    if manual:
                        job["status"] = "pending"
                        job["stage"] = "إعادة المحاولة"
                        job["attempts"] = 0
                        job["error"] = ""
                        job["manual"] = True
                        job["updated_at"] = time.time()
                        self._save(data)
                    return dict(job)
            job = {
                "id": uuid.uuid4().hex[:12],
                "recording": path,
                "video_id": str(video_id or ""),
                "created_at": time.time(),
                "updated_at": time.time(),
                "ended_at": float(ended_at or 0),
                "status": "pending",
                "manual": bool(manual),
                "stage": "في الانتظار",
                "attempts": 0,
                "error": "",
                "notes": [],
                "clips": [],
                "clips_project": "",
                "episode": "",
                "episode_title": "",
                "episode_duration": 0,
                "uploads": [],
                "stages": {},
            }
            data["jobs"].append(job)
            data["jobs"] = data["jobs"][-500:]
            self._save(data)
            return dict(job)

    def update(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        with self.lock:
            data = self._load()
            updated: dict[str, Any] | None = None
            for job in data["jobs"]:
                if job.get("id") != job_id:
                    continue
                job.update(fields)
                job["updated_at"] = fields.get("updated_at", time.time())
                updated = dict(job)
                break
            if updated is not None:
                self._save(data)
            return updated

    def add_note(self, job_id: str, note: str) -> None:
        with self.lock:
            data = self._load()
            for job in data["jobs"]:
                if job.get("id") == job_id:
                    notes = job.setdefault("notes", [])
                    if note not in notes:
                        notes.append(note[:500])
                    job["updated_at"] = time.time()
                    self._save(data)
                    return

    def next_pending(self) -> dict[str, Any] | None:
        with self.lock:
            pending = [
                job
                for job in self._load()["jobs"]
                if job.get("status") == "pending"
            ]
        pending.sort(key=lambda job: job.get("created_at", 0))
        return dict(pending[0]) if pending else None

    def reset_running(self) -> int:
        """A fresh process owns the queue; no previous worker is alive."""
        with self.lock:
            data = self._load()
            count = 0
            for job in data["jobs"]:
                if job.get("status") == "running":
                    job["status"] = "pending"
                    job["stage"] = "استئناف بعد إعادة تشغيل"
                    job["updated_at"] = time.time()
                    count += 1
            if count:
                self._save(data)
            return count

    def requeue_failed(
        self,
        max_attempts: int,
        *,
        delay: float = 300,
        now: float | None = None,
    ) -> int:
        current = time.time() if now is None else now
        count = 0
        with self.lock:
            data = self._load()
            for job in data["jobs"]:
                if job.get("status") != "failed":
                    continue
                if int(job.get("attempts") or 0) >= max_attempts:
                    continue
                if current - float(job.get("updated_at") or 0) < delay:
                    continue
                job["status"] = "pending"
                job["stage"] = "إعادة تلقائية"
                job["error"] = ""
                job["manual"] = False
                job["updated_at"] = current
                count += 1
            if count:
                self._save(data)
        return count

    def retry(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            data = self._load()
            for job in data["jobs"]:
                if job.get("id") != job_id:
                    continue
                job["status"] = "pending"
                job["stage"] = "إعادة المحاولة"
                job["attempts"] = 0
                job["error"] = ""
                job["manual"] = True
                job["updated_at"] = time.time()
                self._save(data)
                return dict(job)
        return None

    def counts(self) -> dict[str, int]:
        counts = {"pending": 0, "running": 0, "done": 0, "failed": 0}
        for job in self.jobs():
            status = str(job.get("status") or "")
            if status in counts:
                counts[status] += 1
        return counts


def scan_recordings(
    store: PipelineStore,
    state_dir: Path | str,
    *,
    now: float | None = None,
) -> int:
    """Enqueue recordings that appeared since the last scan.

    First run records ``now`` as the baseline, so enabling automation does not
    process years of old files. The session-end hook covers the normal path.
    """
    folder = Path(state_dir) / "recordings"
    current = time.time() if now is None else now
    scan_file = store.directory / "scan.json"
    try:
        last = float(json.loads(scan_file.read_text(encoding="utf-8")).get("last_scan", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        last = 0.0
    if last <= 0:
        _write_json(scan_file, {"last_scan": current})
        return 0
    added = 0
    if folder.is_dir():
        for path in sorted(folder.iterdir(), key=lambda item: item.stat().st_mtime):
            if not path.is_file() or path.suffix.lower() not in CLIP_SUFFIXES:
                continue
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if modified <= last or modified > current - 120:
                continue
            store.enqueue(path, ended_at=modified)
            added += 1
    _write_json(scan_file, {"last_scan": current})
    return added


def build_episode_manifest(
    job: Mapping[str, Any],
    config: PipelineConfig,
    recording: Path,
) -> Path:
    clips = [
        clip
        for clip in (job.get("clips") or [])
        if isinstance(clip, Mapping) and not clip.get("blocked")
    ]
    clips.sort(key=lambda clip: float(clip.get("start") or 0))
    clips = clips[: config.episode_max_segments]
    if not clips:
        raise PipelineError("لا توجد مقاطع صالحة لبناء الحلقة.")
    ended = float(job.get("ended_at") or 0)
    day = (
        date_cls.fromtimestamp(ended).isoformat()
        if ended > 0
        else date_cls.today().isoformat()
    )
    title = str(job.get("episode_title") or "").strip() or f"{config.brand} — {day}"
    payload = {
        "title": title[:120],
        "date": day,
        "brand": config.brand,
        "rights": config.rights,
        "source_note": config.rights_note,
        "intro": {"headline": "أبرز ما في البث", "subline": day},
        "outro": {"headline": "شكراً للمتابعة", "subline": "اشترك ليصلك الجديد"},
        "intro_seconds": 5,
        "card_seconds": 3,
        "outro_seconds": 5,
        "segments": [
            {
                "heading": str(clip.get("title") or f"مقطع {index + 1}")[:90],
                "clip": str(recording),
                "start": max(0.0, float(clip.get("start") or 0)),
                "duration": max(1.0, float(clip.get("duration") or 1)),
                "note": str(clip.get("reason") or "")[:240],
            }
            for index, clip in enumerate(clips)
        ],
    }
    manifest = (
        config.state_dir / "pipeline" / "manifests" / f"{job.get('id')}.json"
    )
    write_manifest(manifest, payload)
    return manifest


def _upload_description(clip: Mapping[str, Any]) -> str:
    caption = str(clip.get("caption") or "").strip()
    hashtags = " ".join(f"#{tag}" for tag in clip.get("hashtags") or [])
    return (caption + ("\n\n" + hashtags if hashtags else "")).strip()


class PipelineWorker:
    def __init__(
        self,
        project_root: Path | str,
        state_dir: Path | str,
        *,
        busy: Callable[[], bool] | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.store = PipelineStore(self.state_dir)
        self.busy = busy or (lambda: False)
        self.notifier = notifier or Notifier()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._log: deque[str] = deque(maxlen=400)
        self._current = ""
        self._stage = ""
        self._last_error = ""
        self._lock_path = self.store.directory / "worker.lock"
        self._owns_lock = False
        self._blocked_reason = ""

    # -- lifecycle -----------------------------------------------------

    def _take_lock(self) -> bool:
        self._blocked_reason = ""
        if self._owns_lock:
            return True
        try:
            descriptor = os.open(
                self._lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            age = 0.0
            pid = "?"
            try:
                data = json.loads(self._lock_path.read_text(encoding="utf-8"))
                age = time.time() - float(data.get("started_at") or 0)
                pid = data.get("pid", "?")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                age = STALE_RUNNING_SECONDS * 2
            if age < STALE_RUNNING_SECONDS * 1.5:
                self._blocked_reason = f"عامل أتمتة آخر يعمل بالفعل (PID {pid})."
                return False
            try:
                self._lock_path.unlink()
                descriptor = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except OSError:
                self._blocked_reason = "تعذّر الحصول على قفل عامل الأتمتة."
                return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_at": time.time()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(self._lock_path, 0o600)
        except OSError:
            pass
        self._owns_lock = True
        return True

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self._take_lock():
            return
        self._stop.clear()
        self.store.reset_running()
        self._thread = threading.Thread(
            target=self._loop, name="pipeline-worker", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._owns_lock:
            try:
                self._lock_path.unlink()
            except OSError:
                pass
            self._owns_lock = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _log_line(self, message: str) -> None:
        text = f"{time.strftime('%H:%M:%S')} {message}"[:800]
        with self._lock:
            self._log.append(text)
        LOGGER.info("pipeline: %s", message)

    def _set_stage(self, job_id: str, stage: str) -> None:
        with self._lock:
            self._current = job_id
            self._stage = stage
        self.store.update(job_id, stage=stage)
        self._log_line(stage)

    def status(self) -> dict[str, Any]:
        with self._lock:
            log = list(self._log)
            current = self._current
            stage = self._stage
            error = self._last_error
        return {
            "running": self.running,
            "blocked_reason": self._blocked_reason,
            "current": current,
            "stage": stage,
            "last_error": error,
            "log": log[-80:],
            "queue": self.store.counts(),
            "jobs": self.store.jobs()[:12],
        }

    # -- loop ----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            config = PipelineConfig.from_env(self.project_root)
            job = self.store.next_pending()
            manual = bool(job and job.get("manual"))
            if not config.enabled and not manual:
                self._wait(config.poll_seconds)
                continue
            if self._safe_busy():
                self._wait(config.poll_seconds)
                continue
            if config.enabled:
                self.store.requeue_failed(
                    config.max_attempts, delay=config.retry_delay
                )
                try:
                    scan_recordings(self.store, self.state_dir)
                except Exception:
                    LOGGER.warning("pipeline scan failed", exc_info=True)
            if job is None:
                self._wait(config.poll_seconds)
                continue
            self._process(job, config)
        self._log_line("توقف عامل الأتمتة.")

    def _wait(self, seconds: float) -> None:
        self._wake.wait(timeout=max(1, seconds))
        self._wake.clear()

    def _safe_busy(self) -> bool:
        try:
            return bool(self.busy())
        except Exception:
            return False

    def wake(self) -> None:
        self._wake.set()

    # -- job -----------------------------------------------------------

    def process_now(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job is None:
            raise PipelineError("المهمة غير موجودة.")
        config = PipelineConfig.from_env(self.project_root)
        return self._process(job, config)

    def _process(self, job: dict[str, Any], config: PipelineConfig) -> dict[str, Any]:
        job_id = str(job.get("id"))
        attempts = int(job.get("attempts") or 0) + 1
        if attempts > config.max_attempts and job.get("status") != "running":
            self.store.update(
                job_id,
                status="failed",
                stage="تجاوز المحاولات",
                error=f"تجاوزت المحاولات المسموحة ({config.max_attempts}).",
            )
            return self.store.get(job_id) or job
        self.store.update(
            job_id,
            status="running",
            attempts=attempts,
            error="",
            stage="بدء",
        )
        self._log_line(f"بدء المهمة {job_id}: {Path(job['recording']).name}")
        self._current = job_id
        self._last_error = ""
        try:
            self._run_stages(job, config)
        except Exception as exc:
            message = str(exc)[:800]
            self._last_error = message
            self.store.update(
                job_id, status="failed", stage="فشل", error=message
            )
            self._log_line(f"فشل المهمة: {message}")
            self.notifier.notify(
                "pipeline_failed", title=Path(job["recording"]).name, reason=message
            )
        finally:
            with self._lock:
                self._current = ""
                self._stage = ""
        return self.store.get(job_id) or job

    def _run_stages(self, job: dict[str, Any], config: PipelineConfig) -> None:
        job_id = str(job["id"])
        recording = Path(job["recording"])
        if not recording.is_file():
            raise PipelineError(f"التسجيل غير موجود: {recording}")
        free_gb = shutil.disk_usage(self.state_dir).free / (1024**3)
        if free_gb < config.min_free_gb:
            raise PipelineError(
                f"المساحة الحرة {free_gb:.1f}GB أقل من الحد {config.min_free_gb:.1f}GB."
            )

        stages = dict(job.get("stages") or {})
        clips: list[dict[str, Any]] = list(job.get("clips") or [])
        uploads: list[dict[str, Any]] = list(job.get("uploads") or [])

        # 1) clips
        if config.auto_clips and not (
            job.get("clips_project") and job.get("clips")
        ):
            self._set_stage(job_id, "تحليل المقاطع")
            client = CutterClient(config.cutter)
            available, reason = client.available()
            if not available:
                stages["clips"] = "skipped"
                self.store.add_note(job_id, f"تخطي المقاطع: {reason}")
                self._log_line(f"تخطي المقاطع: {reason}")
            else:
                try:
                    result = client.run(recording, log=self._log_line)
                except CutterError as exc:
                    stages["clips"] = "failed"
                    self.store.add_note(job_id, f"فشل تحليل المقاطع: {exc}")
                    self._log_line(f"فشل تحليل المقاطع: {exc}")
                else:
                    clips = [clip.as_dict() for clip in result.clips]
                    stages["clips"] = "done"
                    self.store.update(
                        job_id,
                        clips=clips,
                        clips_project=str(result.project),
                    )
                    if not clips:
                        self.store.add_note(job_id, "CQT1 لم يعد بمقاطع صالحة.")
                    self._log_line(f"اختيار {len(clips)} مقطعاً.")
        elif not config.auto_clips:
            stages["clips"] = "disabled"
        else:
            stages["clips"] = "done"

        # 2) episode
        episode = str(job.get("episode") or "")
        if config.auto_episode and not (episode and Path(episode).is_file()):
            if not clips:
                stages["episode"] = "skipped"
                self.store.add_note(job_id, "تخطي الحلقة: لا مقاطع متاحة.")
                self._log_line("تخطي الحلقة: لا مقاطع متاحة.")
            else:
                self._set_stage(job_id, "بناء الحلقة")
                try:
                    manifest = build_episode_manifest(
                        {**job, "clips": clips}, config, recording
                    )
                    plan = load_manifest(manifest)
                    output = (
                        self.state_dir
                        / "episodes"
                        / f"auto-{plan.date}-{job_id}"
                    )
                    result = render_episode(
                        plan,
                        output,
                        progress=lambda done, total, phase: self._log_line(
                            f"الحلقة: {phase} ({done}/{total})"
                        ),
                    )
                except (EpisodeError, OSError, ValueError) as exc:
                    stages["episode"] = "failed"
                    self.store.add_note(job_id, f"فشل بناء الحلقة: {exc}")
                    self._log_line(f"فشل بناء الحلقة: {exc}")
                else:
                    episode = str(output / "episode.mp4")
                    stages["episode"] = "done"
                    self.store.update(
                        job_id,
                        episode=episode,
                        episode_title=plan.title,
                        episode_duration=result.get("duration_seconds", 0),
                    )
                    self._log_line(f"الحلقة جاهزة: {episode}")
        elif not config.auto_episode:
            stages["episode"] = "disabled"
        else:
            stages["episode"] = "done"

        # 3) upload
        if config.auto_upload:
            self._set_stage(job_id, "رفع نسخة خاصة")
            if not config.youtube_token.is_file() or not has_upload_scope(
                config.youtube_token
            ):
                stages["upload"] = "skipped"
                self.store.add_note(
                    job_id, "تخطي الرفع: لا يوجد تفويض YouTube بصلاحية الرفع."
                )
                self._log_line("تخطي الرفع: لا يوجد تفويض صالح.")
            else:
                targets: list[dict[str, Any]] = []
                if episode and Path(episode).is_file():
                    description = ""
                    description_file = Path(episode).with_name("description.md")
                    if description_file.is_file():
                        try:
                            description = description_file.read_text(
                                encoding="utf-8"
                            )[:5000]
                        except OSError:
                            description = ""
                    targets.append(
                        {
                            "path": episode,
                            "title": str(
                                job.get("episode_title")
                                or f"{config.brand} — {Path(episode).parent.name}"
                            )[:100],
                            "description": description,
                            "tags": list(config.tags),
                        }
                    )
                for clip in sorted(
                    clips,
                    key=lambda item: float(item.get("score") or 0),
                    reverse=True,
                ):
                    if len(targets) >= config.upload_max:
                        break
                    if clip.get("blocked"):
                        continue
                    path = str(clip.get("file") or "")
                    if not path or not Path(path).is_file():
                        continue
                    targets.append(
                        {
                            "path": path,
                            "title": str(clip.get("title") or "مقطع")[:100],
                            "description": _upload_description(clip),
                            "tags": list(clip.get("hashtags") or []),
                        }
                    )
                targets = [
                    target
                    for target in targets
                    if target["path"] not in {upload.get("path") for upload in uploads}
                ][: max(0, config.upload_max - len(uploads))]
                errors: list[str] = []
                for target in targets:
                    try:
                        result = upload_video(
                            target["path"],
                            title=target["title"],
                            description=target["description"],
                            tags=target["tags"],
                            privacy=config.upload_privacy,
                            token_file=config.youtube_token,
                            made_for_kids=config.made_for_kids,
                            category_id=config.category_id,
                            progress=lambda fraction: self._log_line(
                                f"رفع {Path(target['path']).name}: {int(fraction * 100)}%"
                            ),
                        )
                    except Exception as exc:  # HttpError, quota, network
                        message = str(exc)[:300]
                        errors.append(message)
                        self._log_line(
                            f"فشل رفع {Path(target['path']).name}: {message}"
                        )
                        if "quota" in message.lower():
                            break
                    else:
                        uploads.append({**target, **result})
                        self.store.update(job_id, uploads=uploads)
                        self._log_line(
                            f"رُفع {result['video_id']} ({result['privacy']})."
                        )
                self.store.update(job_id, uploads=uploads)
                if errors:
                    stages["upload"] = "failed"
                    self.store.add_note(job_id, "فشل الرفع: " + " | ".join(errors)[:400])
                else:
                    stages["upload"] = "done"
        else:
            stages["upload"] = "disabled"

        failed = [name for name, state in stages.items() if state == "failed"]
        status = "failed" if failed else "done"
        self.store.update(
            job_id,
            status=status,
            stage="انتهى" if status == "done" else "فشل جزئي",
            stages=stages,
            clips=clips,
            uploads=uploads,
            error="" if status == "done" else "بعض المراحل فشلت؛ راجع السجل.",
        )
        self._log_line("اكتملت المهمة." if status == "done" else "اكتملت مع أخطاء.")
        self.notifier.notify(
            "pipeline_done" if status == "done" else "pipeline_failed",
            title=Path(job["recording"]).name,
            clips=len(clips),
            episode=Path(episode).name if episode else "",
            uploads=len(uploads) if config.auto_upload else 0,
        )


def enqueue_finished(
    state_dir: Path | str,
    recording: Path | str,
    *,
    video_id: str = "",
    ended_at: float = 0,
    project_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """Session-end hook: queue a job only when automation is enabled."""
    config = PipelineConfig.from_env(project_root)
    if not config.enabled:
        return None
    store = PipelineStore(state_dir)
    return store.enqueue(recording, video_id=video_id, ended_at=ended_at)


def automation_warnings(config: PipelineConfig) -> list[str]:
    warnings: list[str] = []
    if not config.enabled:
        warnings.append("الأتمتة متوقفة.")
    if (
        config.auto_clips or config.auto_episode or config.auto_upload
    ) and not config.recording_enabled:
        warnings.append("التسجيل غير مفعّل؛ لن توجد ملفات للمعالجة. فعّل RECORD_ENABLED.")
    if config.rights not in {"owned", "licensed", "original"}:
        warnings.append(
            f"أساس الحقوق «{config.rights}»؛ راجع كل مقطع قبل النشر."
        )
    if config.auto_episode and not config.auto_clips:
        warnings.append("بناء الحلقة يحتاج تحليل المقاطع؛ سيُتخطى.")
    if config.auto_clips:
        available, reason = CutterClient(config.cutter).available()
        if not available:
            warnings.append(f"المقاطع: {reason}")
    if config.auto_upload:
        if not config.youtube_token.is_file():
            warnings.append("الرفع: لا يوجد تفويض YouTube محفوظ.")
        elif not has_upload_scope(config.youtube_token):
            warnings.append("الرفع: التفويض الحالي لا يتضمن صلاحية الرفع.")
        if config.upload_privacy == "public":
            warnings.append("الرفع عام: سيظهر الفيديو للجمهور مباشرة.")
    try:
        free_gb = shutil.disk_usage(config.state_dir).free / (1024**3)
        if free_gb < config.min_free_gb:
            warnings.append(
                f"المساحة الحرة {free_gb:.1f}GB أقل من الحد {config.min_free_gb:.1f}GB."
            )
    except OSError:
        warnings.append("تعذّر قراءة مساحة القرص.")
    return warnings
