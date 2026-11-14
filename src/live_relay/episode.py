"""Build a finished daily episode from recordings the channel owns.

The relay records streams; a recording alone is not a programme. This module
turns a small JSON rundown into a uniform 1080p MP4: branded intro, the chosen
segments, a full-screen card between them, and an outro. It also writes the
chapters, the description and the cover, so the result can be uploaded without
a video editor.

Everything runs locally with ffmpeg and Pillow. No source-channel imagery is
used: cards are generated from the channel's own brand colours and logo.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .thumbnail_local import compose_thumbnail

LOGGER = logging.getLogger(__name__)

SCHEMA = 1
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
MIN_SECONDS = 10 * 60
MAX_SECONDS = 4 * 60 * 60
MAX_SEGMENTS = 24
CLIP_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".ts"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_NAME = re.compile(r"[^\w\u0600-\u06FF-]+")

Progress = Callable[[int, int, str], None]


class EpisodeError(RuntimeError):
    """A manifest or a render step failed with a message fit for the UI."""


@dataclass(frozen=True)
class Segment:
    heading: str
    clip: Path
    start: float
    duration: float
    voiceover: Path | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "heading": self.heading,
            "clip": str(self.clip),
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "voiceover": str(self.voiceover) if self.voiceover else "",
            "note": self.note,
        }


@dataclass(frozen=True)
class Item:
    kind: str  # intro | segment | card | outro
    label: str
    seconds: float
    segment: Segment | None = None
    index: int = 0

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "label": self.label,
            "seconds": round(self.seconds, 3),
        }
        if self.segment is not None:
            data["segment"] = self.segment.as_dict()
        return data


@dataclass(frozen=True)
class EpisodePlan:
    title: str
    date: str
    brand: str
    intro_headline: str
    intro_subline: str
    outro_headline: str
    outro_subline: str
    source_note: str
    rights: str
    segments: tuple[Segment, ...]
    intro_seconds: float
    card_seconds: float
    outro_seconds: float
    items: tuple[Item, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_seconds(self) -> float:
        return round(sum(item.seconds for item in self.items), 3)

    def chapters(self) -> list[dict[str, Any]]:
        chapters: list[dict[str, Any]] = []
        cursor = 0.0
        for item in self.items:
            if item.kind in {"intro", "segment"}:
                chapters.append(
                    {
                        "seconds": round(cursor, 3),
                        "time": format_timestamp(cursor),
                        "label": item.label,
                    }
                )
            cursor += item.seconds
        return chapters

    def as_dict(self) -> dict[str, Any]:
        return {
            "app": "live-relay-episode",
            "schema": SCHEMA,
            "title": self.title,
            "date": self.date,
            "brand": self.brand,
            "total_seconds": self.total_seconds,
            "rights": self.rights,
            "intro_seconds": self.intro_seconds,
            "card_seconds": self.card_seconds,
            "outro_seconds": self.outro_seconds,
            "chapters": self.chapters(),
            "segments": [segment.as_dict() for segment in self.segments],
            "warnings": list(self.warnings),
        }


def format_timestamp(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _ffmpeg_bin() -> str:
    return os.getenv("FFMPEG") or "ffmpeg"


def _ffprobe_bin() -> str:
    explicit = os.getenv("FFPROBE")
    if explicit:
        return explicit
    ffmpeg = os.getenv("FFMPEG")
    if ffmpeg:
        suffix = ".exe" if os.name == "nt" else ""
        candidate = Path(ffmpeg).with_name("ffprobe" + suffix)
        if candidate.is_file():
            return str(candidate)
    return "ffprobe"


def _ffmpeg_info(path: Path) -> str:
    result = subprocess.run(
        [_ffmpeg_bin(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stderr or ""


def probe_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                _ffprobe_bin(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        info = _ffmpeg_info(path)
        match = DURATION_RE.search(info)
        if not match:
            raise EpisodeError("تعذّر قراءة مدة المقطع: ffprobe غير مثبّت.")
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if result.returncode != 0:
        raise EpisodeError(f"تعذّر قراءة المقطع {path.name}: {result.stderr.strip()[:200]}")
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise EpisodeError(f"مدة غير صالحة للمقطع {path.name}.") from exc


def has_audio(path: Path) -> bool:
    result = subprocess.run(
        [
            _ffprobe_bin(),
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return "Audio:" in _ffmpeg_info(path)
    return bool(result.stdout.strip())


def _number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise EpisodeError(f"{name} يجب أن يكون رقماً.") from exc
    if not minimum <= parsed <= maximum:
        raise EpisodeError(f"{name} خارج النطاق المسموح.")
    return parsed


def _resolve(base: Path, value: Any, *, name: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise EpisodeError(f"{name} مطلوب.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_file():
        raise EpisodeError(f"{name} غير موجود: {path}")
    if path.suffix.lower() not in CLIP_SUFFIXES:
        raise EpisodeError(f"{name} بصيغة غير مدعومة: {path.suffix}")
    return path


def load_manifest(path: Path | str) -> EpisodePlan:
    manifest_path = Path(path).expanduser().resolve()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EpisodeError(f"تعذّر قراءة ملف الحلقة: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise EpisodeError(f"ملف الحلقة ليس JSON صالحاً: {exc.msg}") from exc
    if not isinstance(data, Mapping):
        raise EpisodeError("ملف الحلقة يجب أن يكون كائناً JSON.")

    title = str(data.get("title") or "").strip()
    if not title:
        raise EpisodeError("عنوان الحلقة مطلوب.")
    if len(title) > 120:
        raise EpisodeError("عنوان الحلقة أطول من 120 حرفاً.")

    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise EpisodeError("الحلقة تحتاج مقطعاً واحداً على الأقل.")
    if len(raw_segments) > MAX_SEGMENTS:
        raise EpisodeError(f"الحد الأقصى {MAX_SEGMENTS} مقطعاً.")

    base = manifest_path.parent
    segments: list[Segment] = []
    for index, raw in enumerate(raw_segments, start=1):
        if not isinstance(raw, Mapping):
            raise EpisodeError(f"المقطع {index} غير صالح.")
        heading = str(raw.get("heading") or "").strip()
        if not heading:
            raise EpisodeError(f"عنوان المقطع {index} مطلوب.")
        if len(heading) > 90:
            raise EpisodeError(f"عنوان المقطع {index} أطول من 90 حرفاً.")
        clip = _resolve(base, raw.get("clip"), name=f"مقطع {index}")
        full = probe_duration(clip)
        start = _number(raw.get("start", 0), f"بداية المقطع {index}", minimum=0, maximum=max(0, full - 1))
        available = max(0.0, full - start)
        if available < 1:
            raise EpisodeError(f"المقطع {index} قصير جداً بعد نقطة البداية.")
        if raw.get("duration") in (None, ""):
            duration = available
        else:
            duration = _number(
                raw.get("duration"),
                f"مدة المقطع {index}",
                minimum=1,
                maximum=available,
            )
        voiceover = None
        if raw.get("voiceover"):
            voiceover = _resolve(base, raw.get("voiceover"), name=f"تعليق المقطع {index}")
        note = str(raw.get("note") or "").strip()[:240]
        segments.append(
            Segment(
                heading=heading,
                clip=clip,
                start=start,
                duration=duration,
                voiceover=voiceover,
                note=note,
            )
        )

    date_text = str(data.get("date") or date_cls.today().isoformat()).strip()
    if not DATE_RE.match(date_text):
        raise EpisodeError("التاريخ يجب أن يكون بصيغة YYYY-MM-DD.")
    brand = str(data.get("brand") or "").strip() or "live-relay"
    intro = data.get("intro") if isinstance(data.get("intro"), Mapping) else {}
    outro = data.get("outro") if isinstance(data.get("outro"), Mapping) else {}
    intro_headline = str(intro.get("headline") or title).strip()
    outro_headline = str(outro.get("headline") or "شكراً للمتابعة").strip()
    intro_seconds = _number(data.get("intro_seconds", 6), "مدة المقدمة", minimum=1, maximum=30)
    card_seconds = _number(data.get("card_seconds", 4), "مدة الفاصل", minimum=1, maximum=30)
    outro_seconds = _number(data.get("outro_seconds", 6), "مدة الخاتمة", minimum=1, maximum=30)
    rights = str(data.get("rights") or "original").strip().lower()
    source_note = str(data.get("source_note") or "").strip()[:300]

    items: list[Item] = [
        Item(kind="intro", label="مقدمة", seconds=intro_seconds),
    ]
    for position, segment in enumerate(segments, start=1):
        items.append(
            Item(
                kind="segment",
                label=f"المقطع {position}: {segment.heading}",
                seconds=segment.duration,
                segment=segment,
                index=position,
            )
        )
        if position < len(segments):
            items.append(
                Item(
                    kind="card",
                    label=f"فاصل {position}",
                    seconds=card_seconds,
                    index=position,
                )
            )
    items.append(Item(kind="outro", label="خاتمة", seconds=outro_seconds))

    total = sum(item.seconds for item in items)
    warnings: list[str] = []
    if total > MAX_SECONDS:
        raise EpisodeError("الحلقة أطول من أربع ساعات.")
    if total < MIN_SECONDS:
        warnings.append(
            f"الحلقة {format_timestamp(total)} فقط؛ الحلقة اليومية المستهدفة 15–20 دقيقة."
        )
    if rights != "original":
        warnings.append(
            "المقاطع ليست موسومة كأصلية. راجع الحقوق قبل النشر؛ الأهلية للإعلانات تعتمد على ذلك."
        )

    return EpisodePlan(
        title=title,
        date=date_text,
        brand=brand,
        intro_headline=intro_headline[:120],
        intro_subline=str(intro.get("subline") or "").strip()[:160],
        outro_headline=outro_headline[:120],
        outro_subline=str(outro.get("subline") or "").strip()[:160],
        source_note=source_note,
        rights=rights,
        segments=tuple(segments),
        intro_seconds=intro_seconds,
        card_seconds=card_seconds,
        outro_seconds=outro_seconds,
        items=tuple(items),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def _hex(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _font_path(text: str) -> Path:
    module_dir = Path(__file__).resolve().parent
    arabic = bool(re.search(r"[\u0600-\u06FF]", text))
    latin = bool(re.search(r"[A-Za-z]", text))
    name = "NotoSansArabic-Bold.ttf" if arabic and not latin else "DejaVuSans-Bold.ttf"
    bundled = module_dir / "fonts" / name
    if bundled.is_file():
        return bundled
    fallback = (
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"
        if name.startswith("Noto")
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    )
    if Path(fallback).is_file():
        return Path(fallback)
    other = module_dir / "fonts" / (
        "DejaVuSans-Bold.ttf" if name.startswith("Noto") else "NotoSansArabic-Bold.ttf"
    )
    if other.is_file():
        return other
    raise EpisodeError("لا يوجد خط عربي صالح لرسم بطاقات الحلقة.")


def _font(text: str, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(str(_font_path(text)), max(12, int(size)))


def _direction(text: str) -> str:
    return "rtl" if re.search(r"[\u0600-\u06FF]", text) else "ltr"


def _length(draw, text: str, font, direction: str) -> float:
    try:
        return draw.textlength(
            text,
            font=font,
            direction=direction,
            language="ar" if direction == "rtl" else None,
        )
    except TypeError:  # very old Pillow
        return draw.textlength(text, font=font)


def _wrap(draw, text: str, font, width: float, direction: str, limit: int = 3) -> list[str]:
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or _length(draw, candidate, font, direction) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > limit:
        lines = lines[:limit]
        last = lines[-1]
        while last and _length(draw, last + "…", font, direction) > width:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        lines[-1] = last + "…"
    return lines


def render_card(
    *,
    kicker: str,
    headline: str,
    subline: str = "",
    footer: str = "",
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    accent: str = "#35d49a",
    logo_file: str | Path | None = None,
    date_text: str = "",
) -> bytes:
    """Render one full-screen card as PNG bytes."""
    from PIL import Image, ImageDraw, ImageFilter, ImageOps

    scale = width / DEFAULT_WIDTH
    accent_rgb = _hex(accent)
    mask = Image.linear_gradient("L").rotate(90, expand=True).resize((width, height))
    background = ImageOps.colorize(mask, "#05090d", "#0d1a26").convert("RGB")

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse(
        (int(width * 0.55), int(height * 0.05), int(width * 1.1), int(height * 0.75)),
        fill=(*accent_rgb, 40),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(int(90 * scale)))
    background = Image.alpha_composite(background.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(background)

    direction = _direction(headline or kicker)
    margin = int(140 * scale)
    text_x = width - margin if direction == "rtl" else margin
    anchor = "ra" if direction == "rtl" else "la"
    max_width = width - margin * 2

    draw.rounded_rectangle(
        (0, 0, int(10 * scale), height), radius=0, fill=accent_rgb
    )

    kicker_font = _font(kicker, 34 * scale)
    draw.text(
        (text_x, int(92 * scale)),
        kicker,
        font=kicker_font,
        fill=(*accent_rgb, 255),
        anchor=anchor,
        direction=direction,
        language="ar" if direction == "rtl" else None,
    )

    headline_size = 96
    lines: list[str] = []
    while headline_size >= 54:
        font = _font(headline, headline_size * scale)
        lines = _wrap(draw, headline, font, max_width, direction, limit=3)
        if len(lines) <= 3 and sum(
            int(headline_size * scale * 1.18) for _ in lines
        ) <= int(height * 0.46):
            break
        headline_size -= 6
    font = _font(headline, headline_size * scale)
    y = int(height * 0.30)
    for line in lines:
        draw.text(
            (text_x, y),
            line,
            font=font,
            fill=(245, 248, 250),
            anchor=anchor,
            direction=direction,
            language="ar" if direction == "rtl" else None,
        )
        y += int(headline_size * scale * 1.18)

    if subline:
        sub_font = _font(subline, 40 * scale)
        sub_lines = _wrap(draw, subline, sub_font, max_width, direction, limit=2)
        y += int(26 * scale)
        for line in sub_lines:
            draw.text(
                (text_x, y),
                line,
                font=sub_font,
                fill=(168, 184, 196),
                anchor=anchor,
                direction=direction,
                language="ar" if direction == "rtl" else None,
            )
            y += int(54 * scale)

    if footer:
        footer_font = _font(footer, 26 * scale)
        draw.text(
            (text_x, height - int(88 * scale)),
            footer,
            font=footer_font,
            fill=(132, 148, 160),
            anchor=anchor,
            direction=direction,
            language="ar" if direction == "rtl" else None,
        )

    if date_text:
        small = _font(date_text, 26 * scale)
        draw.text(
            (margin, height - int(88 * scale)),
            date_text,
            font=small,
            fill=(132, 148, 160),
            anchor="la",
            direction="ltr",
        )

    logo = Path(logo_file) if logo_file else None
    if logo and logo.is_file():
        try:
            mark = Image.open(logo).convert("RGBA")
            size = int(width * 0.085)
            mark.thumbnail((size, size))
            position = (
                (margin, int(70 * scale))
                if direction == "rtl"
                else (width - margin - mark.width, int(70 * scale))
            )
            background.paste(mark, position, mark)
        except Exception:  # a broken logo must not break the episode
            LOGGER.warning("logo could not be drawn into the card", exc_info=True)

    import io

    buffer = io.BytesIO()
    background.save(buffer, format="PNG")
    return buffer.getvalue()


def _find_logo() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    for relative in ("assets/live-relay-logo.png", "stream-logo.png"):
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _run(command: Sequence[str], *, timeout: int = 3600) -> None:
    if command and command[0] == "ffmpeg":
        command = [_ffmpeg_bin(), *command[1:]]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-6:]
        raise EpisodeError("فشل ffmpeg: " + " | ".join(tail)[:600])


def _video_filter(width: int, height: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        "setsar=1,fps=30,format=yuv420p"
    )


def _encode_args(width: int, height: int) -> list[str]:
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-g",
        "60",
        "-keyint_min",
        "60",
        "-sc_threshold",
        "0",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
    ]


def _render_card_clip(
    png: bytes, seconds: float, output: Path, *, width: int, height: int
) -> None:
    card_path = output.with_suffix(".png")
    card_path.write_bytes(png)
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(card_path),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            f"{seconds:.3f}",
            "-vf",
            _video_filter(width, height),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-shortest",
            *_encode_args(width, height),
            str(output),
        ],
        timeout=600,
    )


def _render_segment(
    segment: Segment,
    output: Path,
    *,
    width: int,
    height: int,
) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{segment.start:.3f}",
        "-t",
        f"{segment.duration:.3f}",
        "-i",
        str(segment.clip),
    ]
    clip_has_audio = has_audio(segment.clip)
    filters: list[str] = []
    maps: list[str] = ["-map", "[v]"]
    extra_inputs: list[str] = []

    if segment.voiceover:
        command += ["-i", str(segment.voiceover)]
        if clip_has_audio:
            filters.append("[0:a]volume=0.22[bed]")
            filters.append("[1:a]volume=1.0,apad[vo]")
            filters.append(
                "[bed][vo]amix=inputs=2:duration=first:dropout_transition=2[a]"
            )
        else:
            filters.append("[1:a]volume=1.0,apad[a]")
    elif clip_has_audio:
        filters.append("[0:a]volume=1.0[a]")
    else:
        extra_inputs = [
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
        ]
        filters.append("[1:a]anull[a]")

    filter_complex = (
        f"[0:v]{_video_filter(width, height)}[v];" + ";".join(filters)
    )
    command += [
        *extra_inputs,
        "-filter_complex",
        filter_complex,
        *maps,
        "-map",
        "[a]",
        "-t",
        f"{segment.duration:.3f}",
        *_encode_args(width, height),
        str(output),
    ]
    _run(command, timeout=3600)


def _concat(parts: Sequence[Path], output: Path, work: Path) -> None:
    list_path = work / "concat.txt"
    lines = []
    for part in parts:
        escaped = str(part.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        timeout=1800,
    )


def _chapters_text(plan: EpisodePlan) -> str:
    return "\n".join(
        f"{chapter['time']} {chapter['label']}" for chapter in plan.chapters()
    ) + "\n"


def _description(plan: EpisodePlan) -> str:
    lines = [
        plan.title,
        "",
        f"حلقة يومية من {plan.brand} — {plan.date}.",
        "",
        "الفصول:",
        _chapters_text(plan).rstrip(),
        "",
    ]
    if plan.source_note:
        lines.extend([f"المصدر: {plan.source_note}", ""])
    lines.append(
        "الحقوق: "
        + (
            "محتوى أصلي من إنتاج القناة."
            if plan.rights == "original"
            else f"راجع أساس الحقوق ({plan.rights}) قبل النشر."
        )
    )
    return "\n".join(lines).strip() + "\n"


def slugify(title: str) -> str:
    cleaned = SAFE_NAME.sub("-", title).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return (cleaned or "episode")[:48]


def render_episode(
    plan: EpisodePlan,
    output_dir: Path | str,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    progress: Progress | None = None,
) -> dict[str, Any]:
    if width % 2 or height % 2 or width < 320 or height < 180:
        raise EpisodeError("أبعاد الفيديو غير صالحة.")
    out = Path(output_dir).expanduser().resolve()
    created = not out.exists()
    out.mkdir(parents=True, exist_ok=True)
    work = out / ".work"
    work.mkdir(exist_ok=True)
    logo = _find_logo()
    total = len(plan.items)
    parts: list[Path] = []
    try:
        for index, item in enumerate(plan.items):
            if progress:
                progress(index, total, item.kind)
            target = work / f"{index:02d}-{item.kind}.mp4"
            if item.kind in {"intro", "outro"}:
                if item.kind == "intro":
                    kicker = "حلقة يومية"
                    headline = plan.intro_headline
                    subline = plan.intro_subline
                else:
                    kicker = "النهاية"
                    headline = plan.outro_headline
                    subline = plan.outro_subline
                png = render_card(
                    kicker=kicker,
                    headline=headline,
                    subline=subline,
                    footer=plan.brand,
                    date_text=plan.date,
                    width=width,
                    height=height,
                    logo_file=logo,
                )
                _render_card_clip(
                    png, item.seconds, target, width=width, height=height
                )
            elif item.kind == "card":
                assert item.segment is None
                png = render_card(
                    kicker=f"المقطع {item.index + 1} من {len(plan.segments)}",
                    headline=plan.segments[item.index].heading,
                    subline=plan.segments[item.index].note,
                    footer=plan.brand,
                    date_text=plan.date,
                    width=width,
                    height=height,
                    logo_file=logo,
                    accent="#70a7ff",
                )
                _render_card_clip(
                    png, item.seconds, target, width=width, height=height
                )
            else:
                assert item.segment is not None
                _render_segment(item.segment, target, width=width, height=height)
            parts.append(target)

        if progress:
            progress(total, total, "دمج")
        video = out / "episode.mp4"
        _concat(parts, video, work)

        actual = probe_duration(video)
        if actual + 3 < plan.total_seconds:
            raise EpisodeError(
                f"الفيديو الناتج أقصر من المخطط ({format_timestamp(actual)} بدل {format_timestamp(plan.total_seconds)})."
            )

        cover = out / "thumbnail.jpg"
        try:
            from PIL import Image
            import io

            png = compose_thumbnail(
                title=plan.title,
                channel=plan.brand,
                badge="حلقة يومية",
                date_text=plan.date,
                language="ar",
            )
            Image.open(io.BytesIO(png)).convert("RGB").save(
                cover, format="JPEG", quality=90
            )
        except Exception:
            LOGGER.warning("thumbnail generation failed", exc_info=True)

        (out / "chapters.txt").write_text(_chapters_text(plan), encoding="utf-8")
        (out / "description.md").write_text(_description(plan), encoding="utf-8")
        payload = {
            "app": "live-relay-episode",
            "schema": SCHEMA,
            "built_at": time.time(),
            "title": plan.title,
            "date": plan.date,
            "brand": plan.brand,
            "duration_seconds": round(actual, 3),
            "planned_seconds": plan.total_seconds,
            "width": width,
            "height": height,
            "segments": [segment.as_dict() for segment in plan.segments],
            "chapters": plan.chapters(),
            "source_note": plan.source_note,
            "rights": plan.rights,
            "warnings": list(plan.warnings),
            "files": {
                "video": "episode.mp4",
                "thumbnail": "thumbnail.jpg" if cover.is_file() else "",
                "chapters": "chapters.txt",
                "description": "description.md",
            },
        }
        (out / "episode.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if progress:
            progress(total, total, "انتهى")
        return payload
    except Exception:
        if created:
            shutil.rmtree(out, ignore_errors=True)
        else:
            (out / "episode.mp4").unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# Manifest helper
# ---------------------------------------------------------------------------


def sample_manifest(
    recordings_dir: Path | str,
    *,
    title: str = "",
    brand: str = "live-relay",
    date_text: str | None = None,
    limit: int = 6,
    segment_seconds: float = 180.0,
    rights: str = "original",
    now: float | None = None,
) -> dict[str, Any]:
    folder = Path(recordings_dir)
    current = time.time() if now is None else now
    files: list[Path] = []
    if folder.is_dir():
        for candidate in folder.iterdir():
            if (
                candidate.is_file()
                and candidate.suffix.lower() in CLIP_SUFFIXES
                and current - candidate.stat().st_mtime > 90
            ):
                files.append(candidate)
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    files = list(reversed(files[:limit]))
    day = date_text or date_cls.today().isoformat()
    heading = title.strip() or f"{brand} — {day}"
    rights = str(rights or "original").strip().lower()
    segments = []
    for index, clip in enumerate(files, start=1):
        try:
            duration = min(segment_seconds, max(5.0, probe_duration(clip)))
        except EpisodeError:
            continue
        segments.append(
            {
                "heading": f"المقطع {index}",
                "clip": str(clip.resolve()),
                "start": 0,
                "duration": round(duration, 1),
                "note": "عدّل العنوان وأضف تعليقك الصوتي.",
            }
        )
    return {
        "title": heading,
        "date": day,
        "brand": brand,
        "rights": rights,
        "source_note": (
            "تسجيلات أصلية من قناة " + brand
            if rights == "original"
            else ""
        ),
        "intro": {"headline": heading, "subline": "أهم ما في الحلقة"},
        "outro": {"headline": "شكراً للمتابعة", "subline": "اشترك ليصلك الجديد"},
        "segments": segments,
    }


def write_manifest(path: Path | str, payload: Mapping[str, Any]) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="live-relay-episode")
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan", help="تحقق من ملف الحلقة واطبع الخط الزمني")
    plan_parser.add_argument("--manifest", required=True)
    plan_parser.add_argument("--allow-short", action="store_true")
    build_parser = sub.add_parser("build", help="ابنِ الحلقة")
    build_parser.add_argument("--manifest", required=True)
    build_parser.add_argument("--out", default="")
    build_parser.add_argument("--allow-short", action="store_true")
    build_parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    build_parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    sample_parser = sub.add_parser("sample", help="أنشئ ملف حلقة من التسجيلات")
    sample_parser.add_argument("--recordings", required=True)
    sample_parser.add_argument("--out", default="episodes/today.json")
    sample_parser.add_argument("--title", default="")
    sample_parser.add_argument("--brand", default="live-relay")
    sample_parser.add_argument(
        "--rights", default="original", choices=("original", "third_party", "unknown")
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.command == "sample":
            payload = sample_manifest(
                args.recordings,
                title=args.title,
                brand=args.brand,
                rights=getattr(args, "rights", "original"),
            )
            target = write_manifest(args.out, payload)
            print(json.dumps({"ok": True, "manifest": str(target), "segments": len(payload["segments"])}, ensure_ascii=False))
            return 0
        plan = load_manifest(args.manifest)
        if plan.total_seconds < MIN_SECONDS and not args.allow_short:
            raise EpisodeError(
                f"الحلقة {format_timestamp(plan.total_seconds)} فقط. أضف مقاطع، أو استخدم --allow-short للتجربة."
            )
        if args.command == "plan":
            print(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.out:
            out = Path(args.out)
        else:
            try:
                from .config import load_settings

                out = load_settings().state_dir / "episodes" / (
                    f"{plan.date}-{slugify(plan.title)}"
                )
            except Exception:
                out = Path("episodes") / f"{plan.date}-{slugify(plan.title)}"
        result = render_episode(
            plan, out, width=args.width, height=args.height
        )
        print(json.dumps({"ok": True, "output": str(out), "duration": result["duration_seconds"]}, ensure_ascii=False, indent=2))
        return 0
    except EpisodeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
