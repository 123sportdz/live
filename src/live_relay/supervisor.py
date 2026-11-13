"""Keep the relay alive across unexpected failures.

``run()`` already recovers from most stream errors internally. This module
handles the rest: an uncaught exception, a bug, or a platform failure that
escapes the retry loop. It restarts the relay with exponential backoff,
records every restart, and writes a heartbeat so the dashboard and an external
monitor can tell whether the process is alive.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .config import Settings, ensure_private_dir
from .encoder import redact_urls
from .notifier import Notifier, notifier_from_settings

HEARTBEAT_FILE = "heartbeat.json"
RESTART_FILE = "restarts.json"
HEARTBEAT_INTERVAL = 15.0
_STABLE_SECONDS = 600.0


def _heartbeat_path(state_dir: os.PathLike | str) -> Path:
    return Path(state_dir) / HEARTBEAT_FILE


def write_heartbeat(
    state_dir: os.PathLike | str,
    *,
    mode: str = "running",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "updated_at": time.time(),
        "pid": os.getpid(),
        "mode": mode,
    }
    if extra:
        payload.update(extra)
    path = _heartbeat_path(state_dir)
    try:
        ensure_private_dir(path.parent)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".heartbeat-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
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
    except OSError as exc:
        logging.warning("Could not write the heartbeat: %s", exc)
    return payload


def read_heartbeat(state_dir: os.PathLike | str) -> dict[str, Any]:
    try:
        data = json.loads(_heartbeat_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def heartbeat_age(state_dir: os.PathLike | str) -> float | None:
    updated = read_heartbeat(state_dir).get("updated_at")
    if not isinstance(updated, (int, float)):
        return None
    return max(0.0, time.time() - float(updated))


def _record_restart(
    state_dir: os.PathLike | str, *, attempt: int, reason: str, delay: float
) -> list[dict[str, Any]]:
    path = Path(state_dir) / RESTART_FILE
    history: list[dict[str, Any]] = []
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(current, list):
            history = [item for item in current if isinstance(item, dict)]
    except (OSError, ValueError):
        history = []
    history.append(
        {
            "timestamp": time.time(),
            "attempt": attempt,
            "reason": reason,
            "retry_in": round(delay, 1),
        }
    )
    history = history[-20:]
    try:
        ensure_private_dir(path.parent)
        path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        logging.warning("Could not write the restart log: %s", exc)
    return history


def read_restarts(state_dir: os.PathLike | str) -> list[dict[str, Any]]:
    try:
        current = json.loads((Path(state_dir) / RESTART_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in current if isinstance(item, dict)] if isinstance(current, list) else []


class RelaySupervisor:
    """Run ``run()`` and restart it after an unexpected failure."""

    def __init__(
        self,
        settings: Settings,
        stopped: threading.Event,
        status: Any = None,
        *,
        max_consecutive: int = 10,
        base_delay: float = 5.0,
    ) -> None:
        self.settings = settings
        self.stopped = stopped
        self.status = status
        self.max_consecutive = max(1, int(max_consecutive))
        self.base_delay = max(0.0, float(base_delay))
        self.notifier: Notifier = notifier_from_settings(settings)
        self._heartbeat_stop = threading.Event()

    def _heartbeat_loop(self) -> None:
        state_dir = getattr(self.settings, "state_dir", None)
        if state_dir is None:
            return
        while not self.stopped.is_set() and not self._heartbeat_stop.is_set():
            snapshot = {}
            if self.status is not None:
                try:
                    snapshot = self.status.snapshot()
                except Exception:
                    snapshot = {}
            write_heartbeat(
                state_dir,
                mode=str(snapshot.get("mode") or "running"),
                extra={
                    "uptime_seconds": int(snapshot.get("uptime_seconds") or 0),
                    "title": str(snapshot.get("title") or "")[:120],
                },
            )
            self._heartbeat_stop.wait(HEARTBEAT_INTERVAL)

    def run(self) -> None:
        from .service import run as relay_run

        state_dir = getattr(self.settings, "state_dir", None)
        thread = threading.Thread(
            target=self._heartbeat_loop, name="relay-heartbeat", daemon=True
        )
        thread.start()
        attempt = 0
        delay = self.base_delay
        try:
            while not self.stopped.is_set():
                started = time.monotonic()
                try:
                    relay_run(self.settings, self.stopped, self.status)
                except Exception as exc:
                    reason = redact_urls(str(exc).replace("\n", " "))[:300]
                    logging.exception("Relay failed unexpectedly: %s", reason)
                    if self.stopped.is_set():
                        break
                    if time.monotonic() - started >= _STABLE_SECONDS:
                        attempt = 0
                        delay = self.base_delay
                    attempt += 1
                    if attempt > self.max_consecutive:
                        logging.error(
                            "Relay failed %d times in a row; leaving it stopped",
                            attempt - 1,
                        )
                        if self.status is not None:
                            self.status.update(
                                mode="error",
                                message="توقف المُرحّل بعد أعطال متتالية",
                                last_error=reason,
                                active_platforms=[],
                            )
                        self.notifier.notify("relay_crashed", reason=reason)
                        break
                    _record_restart(
                        state_dir or ".",
                        attempt=attempt,
                        reason=reason,
                        delay=delay,
                    )
                    if self.status is not None:
                        self.status.update(
                            mode="restarting",
                            message=(
                                "أعاد المشرف تشغيل المُرحّل بعد عطل غير متوقع"
                            ),
                            last_error=reason,
                            active_platforms=[],
                        )
                    self.notifier.notify(
                        "relay_crashed", reason=reason, reconnect_in=f"{delay:.0f}s"
                    )
                    if self.stopped.wait(delay):
                        break
                    delay = min(max(delay * 2, 5.0), 300.0)
                else:
                    # A clean return means the relay was asked to stop.
                    break
        finally:
            self._heartbeat_stop.set()
            thread.join(timeout=3)
            if state_dir is not None:
                write_heartbeat(state_dir, mode="stopped")
