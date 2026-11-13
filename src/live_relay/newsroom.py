"""The editorial layer that turns a relay into a produced programme.

A relayed stream is raw material. This module helps a presenter turn it into
something a viewer cannot get from the source channel:

* ``build_brief`` extracts a short, factual brief **only** from the source
  title and description. It never invents a fact, a number, or a quote; the
  rest of the page is made of questions the presenter answers live.
* ``build_chapters``/``chapter_block`` turn editorial markers into the
  timestamp block YouTube needs.
* ``build_session_kit`` packages the brief, the chapters, the description,
  the ad-suitability audit and the recording path into one Markdown file the
  editor can open after the stream.

Nothing here uploads, edits, or talks to YouTube.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import ensure_private_dir
from .monetization import build_chapters, format_timestamp

_ARABIC_DIACRITICS = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u06D6-\u06ED]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟۔])\s+|\n+")
_URL = re.compile(r"https?://\S+")
_HASHTAG = re.compile(r"#[\w\u0600-\u06FF_]+")
_NOISE = (
    "اشترك",
    "subscribe",
    "تابعونا",
    "follow us",
    "لمزيد من",
    "read more",
    "شاهد أيضا",
    "شاهد أيضاً",
    "copyright",
    "جميع الحقوق",
)


def normalize_arabic(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _ARABIC_DIACRITICS.sub("", value)
    value = value.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    value = value.replace("ى", "ي").replace("ة", "ه")
    return value.casefold()


def _clean_text(value: Any, limit: int = 6000) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", str(value or ""))
    return " ".join(text.split())[:limit]


def split_sentences(text: str, limit: int = 40) -> list[str]:
    """Split on sentence punctuation, keeping Arabic and Latin marks."""
    cleaned = _clean_text(text)
    if not cleaned:
        return []
    parts = [part.strip(" -–—•\t") for part in _SENTENCE_SPLIT.split(cleaned)]
    result: list[str] = []
    for part in parts:
        part = _HASHTAG.sub("", part)
        part = _URL.sub("", part).strip()
        if len(part) < 25:
            continue
        lowered = normalize_arabic(part)
        if any(marker in lowered for marker in _NOISE) and len(part) < 120:
            continue
        result.append(part)
        if len(result) >= limit:
            break
    return result


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[\w\u0600-\u06FF]{3,}", normalize_arabic(text))
    stop = {
        "من", "في", "على", "عن", "الى", "إلى", "مع", "هذا", "هذه", "ذلك",
        "التي", "الذي", "كان", "كانت", "بعد", "قبل", "بين", "كما", "قد",
        "the", "and", "for", "with", "that", "this", "from", "were", "was",
    }
    return {word for word in words if word not in stop}


def score_sentence(sentence: str, title_keywords: set[str], position: int) -> float:
    score = 0.0
    length = len(sentence)
    if 40 <= length <= 220:
        score += 2.0
    elif length > 320:
        score -= 2.0
    if re.search(r"\d", sentence):
        score += 1.5
    overlap = len(_keywords(sentence) & title_keywords)
    score += min(3.0, overlap * 0.8)
    if position == 0:
        score += 1.5
    elif position == 1:
        score += 0.5
    score -= position * 0.15
    return score


@dataclass
class Brief:
    title: str
    source_channel: str
    source_platform: str
    facts: list[str] = field(default_factory=list)
    talking_points: list[str] = field(default_factory=list)
    watch_next: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source_channel": self.source_channel,
            "source_platform": self.source_platform,
            "facts": list(self.facts),
            "talking_points": list(self.talking_points),
            "watch_next": list(self.watch_next),
            "note": self.note,
            "generated_at": time.time(),
        }

    @property
    def usable(self) -> bool:
        return bool(self.facts)


def build_brief(
    title: str,
    description: str,
    source_channel: str = "",
    source_platform: str = "custom",
    limit: int = 4,
) -> Brief:
    """An extractive brief: quotes the source, adds no claim of its own."""
    title = _clean_text(title, 300)
    description = _clean_text(description, 6000)
    sentences = split_sentences(description)
    title_keywords = _keywords(title)
    ranked = sorted(
        enumerate(sentences),
        key=lambda item: score_sentence(item[1], title_keywords, item[0]),
        reverse=True,
    )
    selected: list[tuple[int, str]] = []
    seen: set[str] = set()
    for index, sentence in ranked:
        key = normalize_arabic(sentence)[:80]
        if key in seen:
            continue
        seen.add(key)
        selected.append((index, sentence))
        if len(selected) >= limit:
            break
    # Rank decides *what* is kept; the source decides the order it is read in.
    facts = [sentence for _, sentence in sorted(selected)]
    if not facts and description:
        facts = [description[:400]]

    note = ""
    if not facts:
        note = (
            "وصف المصدر فارغ، لذلك لا يوجد موجز وقائعي. اكتب أنت ما وقع في "
            "المقطع الأول، ولا تنسب إلى المصدر ما لم يقله."
        )
    else:
        note = (
            "النقاط أعلاه مقتبسة من وصف المصدر حرفياً. أضف تحليلك وسياقك، "
            "ولا تحوّلها إلى ادّعاءات لم يقلها المصدر."
        )

    return Brief(
        title=title,
        source_channel=_clean_text(source_channel, 200),
        source_platform=_clean_text(source_platform, 40) or "custom",
        facts=facts,
        talking_points=[
            "ما الرقم أو التصريح الأبرز في هذا التحديث؟",
            "من يتأثر مباشرة، وما الذي يتغيّر له؟",
            "ما الذي قد يحدث في الساعات القادمة، وما المؤشر الذي نراقبه؟",
            "ما الذي لم يقله المصدر، ولماذا يهم المشاهد؟",
        ],
        watch_next=[
            "تحديث ميداني جديد من المصدر",
            "بيان أو تعليق رسمي",
            "ردّ فعل الأطراف المعنية",
        ],
        note=note,
    )


def render_brief_markdown(brief: Brief | Mapping[str, Any]) -> str:
    data = brief.as_dict() if isinstance(brief, Brief) else dict(brief)
    lines = [
        "# موجز الحلقة",
        "",
        f"**العنوان:** {data.get('title') or '—'}",
        f"**المصدر:** {data.get('source_channel') or '—'} "
        f"({data.get('source_platform') or '—'})",
        "",
        "## ما وقع (مقتبس من المصدر)",
    ]
    facts = data.get("facts") or []
    if facts:
        lines.extend(f"- {fact}" for fact in facts)
    else:
        lines.append("- لا يوجد وصف كافٍ في المصدر.")
    lines.extend(["", "## نقاط للتعليق (أجب بصوتك على الهواء)"])
    lines.extend(f"- {point}" for point in data.get("talking_points") or [])
    lines.extend(["", "## ما نراقبه بعد ذلك"])
    lines.extend(f"- {item}" for item in data.get("watch_next") or [])
    if data.get("note"):
        lines.extend(["", f"> {data['note']}"])
    return "\n".join(lines).strip() + "\n"


def chapter_block(events: Iterable[Any], start_title: str = "بداية البث") -> list[str]:
    """Chapters from editorial markers, always starting at 00:00."""
    prepared: list[tuple[int, str]] = [(0, start_title)]
    for event in events:
        if isinstance(event, Mapping):
            seconds, title = event.get("seconds"), event.get("title")
        else:
            try:
                seconds, title = event  # type: ignore[misc]
            except (TypeError, ValueError):
                continue
        try:
            prepared.append((max(0, int(seconds)), _clean_text(title, 100)))
        except (TypeError, ValueError):
            continue
    return build_chapters(prepared)


def merge_chapters_into_description(
    description: str, chapters: Sequence[str], limit: int = 5000
) -> str:
    """Append a chapter block once, respecting YouTube's 5,000-char limit."""
    base = str(description or "").rstrip()
    if not chapters:
        return base[:limit]
    marker = "⏱ الفصول:"
    if marker in base:
        # Replace the previous block instead of stacking a second one.
        base = base.split(marker, 1)[0].rstrip()
    block = marker + "\n" + "\n".join(chapters)
    candidate = f"{base}\n\n{block}" if base else block
    if len(candidate) <= limit:
        return candidate
    # Trim the description, never the chapter block.
    room = max(0, limit - len(block) - 2)
    trimmed = base[:room].rstrip()
    return f"{trimmed}\n\n{block}" if trimmed else block[:limit]


def build_session_kit(
    record: Mapping[str, Any],
    *,
    brief: Brief | Mapping[str, Any] | None = None,
    description: str = "",
    chapters: Sequence[str] = (),
    checklist: Sequence[Mapping[str, Any]] = (),
    rights_note: str = "",
) -> str:
    """One Markdown file with everything the editor needs after a session."""
    record = dict(record or {})
    brief_data = (
        brief.as_dict() if isinstance(brief, Brief) else dict(brief or {})
    )
    duration = int(record.get("duration_seconds") or 0)
    hours, remainder = divmod(duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    readable = (
        f"{hours}س {minutes:02d}د" if hours else f"{minutes}د {seconds:02d}ث"
    )
    lines = [
        "# ملف الحلقة",
        "",
        f"- **معرّف الفيديو:** `{record.get('video_id') or '—'}`",
        f"- **المدة:** {readable}",
        f"- **المنصات:** {', '.join(record.get('platforms') or []) or '—'}",
        f"- **تقدير الإعلانات:** {record.get('score', '—')}/100 "
        f"({record.get('band') or '—'})",
        f"- **سبب الإنهاء:** {record.get('end_reason') or '—'}",
        f"- **التسجيل:** `{record.get('recording') or 'غير مفعّل'}`",
        f"- **أساس الحقوق:** {rights_note or 'غير مسجّل — أضفه في إعدادات المصدر'}",
        "",
        "## أسباب الخطر",
    ]
    codes = record.get("risk_codes") or []
    lines.extend([f"- `{code}`" for code in codes] or ["- لا شيء مسجّل."])
    lines.extend(["", "## الموجز التحريري", ""])
    lines.append(
        render_brief_markdown(brief_data) if brief_data else "لم يُولَّد موجز."
    )
    lines.extend(["", "## الفصول"])
    lines.extend([f"- {line}" for line in chapters] or ["- لا فصول بعد."])
    lines.extend(["", "## الوصف المقترح", "```", description or "—", "```"])
    if checklist:
        lines.extend(["", "## قائمة التحقق"])
        for item in checklist:
            mark = "x" if item.get("done") else " "
            lines.append(f"- [{mark}] {item.get('label')} — {item.get('detail')}")
    lines.extend(
        [
            "",
            "---",
            "> ملف محلي. راجع كل جملة قبل النشر؛ لا ينشر شيئاً بنفسه.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def _safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w\u0600-\u06FF.-]+", "-", str(value or ""))
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip("-.")
    return (cleaned or fallback)[:80]


def write_markdown(state_dir: Path, folder: str, name: str, content: str) -> Path:
    directory = (Path(state_dir) / folder).resolve()
    ensure_private_dir(directory)
    path = (directory / f"{_safe_name(name, 'session')}.md").resolve()
    if directory not in path.parents:
        path = directory / "session.md"
    path.write_text(content, encoding="utf-8")
    try:
        import os

        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def write_brief(state_dir: Path, brief: Brief, stamp: str = "") -> Path:
    name = f"{stamp or time.strftime('%Y%m%d-%H%M%S')}-brief"
    return write_markdown(state_dir, "briefs", name, render_brief_markdown(brief))


def write_kit(
    state_dir: Path, record: Mapping[str, Any], content: str
) -> Path:
    stamp = time.strftime(
        "%Y%m%d-%H%M%S", time.localtime(float(record.get("ended_at") or time.time()))
    )
    return write_markdown(state_dir, "kits", f"{stamp}-{record.get('video_id') or 'session'}", content)


def fingerprint(text: str) -> str:
    """Stable short hash, used to prove which brief a kit contained."""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def brief_from_facts(
    facts: Mapping[str, Any] | None, limit: int = 4
) -> Brief:
    """Build a brief from a stored session's facts dict."""
    facts = facts or {}
    return build_brief(
        str(facts.get("title") or ""),
        str(facts.get("description") or ""),
        str(facts.get("source_channel") or ""),
        str(facts.get("source_platform") or "custom"),
        limit=limit,
    )


def latest_markdown(state_dir: Path, folder: str) -> tuple[Path, str] | None:
    """Newest file in a private folder, for the "open the last kit" button."""
    directory = Path(state_dir) / folder
    if not directory.is_dir():
        return None
    files = sorted(
        (path for path in directory.glob("*.md") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in files:
        try:
            return path, path.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def summarize_markers(events: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        try:
            seconds = max(0, int(event.get("seconds") or 0))
        except (TypeError, ValueError):
            continue
        result.append(
            {
                "seconds": seconds,
                "timestamp": format_timestamp(seconds),
                "title": _clean_text(event.get("title"), 100),
            }
        )
    return sorted(result, key=lambda item: item["seconds"])
