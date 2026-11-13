"""Keep long-running installations healthy on disk.

A 24/7 relay writes an MP4 per session. Without a policy the recordings
folder grows until the disk is full, and then the encoder fails mid-stream.
This module applies two independent limits — age and total size — and never
touches the file that is currently being written.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PruneResult:
    deleted: list[str] = field(default_factory=list)
    kept: int = 0
    freed_bytes: int = 0
    total_bytes: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "deleted": list(self.deleted),
            "kept": self.kept,
            "freed_bytes": self.freed_bytes,
            "total_bytes": self.total_bytes,
        }


def recordings_dir(state_dir: os.PathLike | str) -> Path:
    return Path(state_dir) / "recordings"


def _recording_files(directory: Path, active: Path | None) -> list[Path]:
    if not directory.is_dir():
        return []
    files = []
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() != ".mp4":
            continue
        if active is not None and path.resolve() == active:
            continue
        files.append(path)
    return files


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def usage(state_dir: os.PathLike | str) -> tuple[int, int]:
    """Return (file_count, total_bytes) for the recordings folder."""
    directory = recordings_dir(state_dir)
    files = _recording_files(directory, None)
    return len(files), sum(_safe_size(path) for path in files)


def prune_recordings(
    state_dir: os.PathLike | str,
    *,
    retention_days: int = 14,
    max_bytes: int = 0,
    active_path: os.PathLike | str = "",
    now: float | None = None,
) -> PruneResult:
    """Delete old recordings, then the oldest ones over the size budget.

    ``retention_days <= 0`` disables the age limit and ``max_bytes <= 0``
    disables the size limit. The active recording is always protected.
    """
    directory = recordings_dir(state_dir)
    active = None
    if active_path:
        try:
            active = Path(active_path).resolve()
        except OSError:
            active = None
    files = _recording_files(directory, active)
    files.sort(key=_safe_mtime)
    result = PruneResult()

    if retention_days > 0 and files:
        cutoff = (now if now is not None else time.time()) - retention_days * 86400
        remaining: list[Path] = []
        for path in files:
            try:
                if _safe_mtime(path) < cutoff:
                    size = _safe_size(path)
                    path.unlink()
                    result.deleted.append(path.name)
                    result.freed_bytes += size
                    logging.info("Removed an expired recording: %s", path.name)
                else:
                    remaining.append(path)
            except OSError as exc:
                logging.warning("Could not remove the recording %s: %s", path.name, exc)
        files = remaining

    if max_bytes > 0:
        total = sum(_safe_size(path) for path in files)
        while total > max_bytes and files:
            path = files.pop(0)
            try:
                size = _safe_size(path)
                path.unlink()
                total -= size
                result.deleted.append(path.name)
                result.freed_bytes += size
                logging.info("Removed an old recording to respect the quota: %s", path.name)
            except OSError as exc:
                logging.warning("Could not remove the recording %s: %s", path.name, exc)

    remaining_files = _recording_files(directory, active)
    result.kept = len(remaining_files)
    result.total_bytes = sum(_safe_size(path) for path in remaining_files)
    return result


def prune_from_settings(settings: object, *, active_path: str = "") -> PruneResult:
    """Convenience wrapper used by the relay with a Settings-like object."""
    state_dir = getattr(settings, "state_dir", None)
    if state_dir is None:
        return PruneResult()
    days = int(getattr(settings, "recording_retention_days", 14) or 0)
    gigabytes = float(getattr(settings, "recording_max_gb", 0) or 0)
    return prune_recordings(
        state_dir,
        retention_days=days,
        max_bytes=int(gigabytes * 1024**3) if gigabytes > 0 else 0,
        active_path=active_path,
    )
