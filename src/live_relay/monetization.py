"""Ad-suitability guidance for relayed live streams.

This module does not decide anything on YouTube's behalf, and it never talks
to YouTube. It turns what the project already knows about a session (source,
rights, how much original production surrounds the relay, metadata, gaps)
into a transparent, reproducible readiness estimate with Arabic actions.

YouTube does publish ``monetizationDetails.eligibleForAdsMonetization`` for a
live broadcast; the relay shows that separate, authoritative signal when the
API returns it. This module never pretends to have it.

Two rules shape every line here:

* A logo, a frame or a re-encode is not transformation. YouTube's
  "reused content" policy looks for original, authentic value.
* Sensitive news is allowed. It is simply served with limited ads most of
  the time. Rewording a title to dodge that is evasion, not a fix, so the
  module never suggests it.

The output is advice, not a ruling. Only YouTube Studio shows the real
status of a video.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import ensure_private_dir

POLICY_REVISION = "2026-11"

SEVERITY_ORDER = {"blocker": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_LABEL = {
    "blocker": "مانع",
    "high": "خطر مرتفع",
    "medium": "خطر متوسط",
    "low": "تحسين",
}
SEVERITY_WEIGHT = {"blocker": 45, "high": 20, "medium": 10, "low": 5}

# A high score must never hide a known limitation. A sensitive topic keeps the
# video out of "ready" even when everything else is perfect, because limited
# ads are the expected outcome — not a defect the user can edit away.
SCORE_CAPS = {
    "sensitive_topic": 79,
    "duplicate_reupload": 82,
    "cosmetic_transformation": 84,
}

TRANSFORM_LEVELS = {"raw", "branded", "editorial"}
RIGHTS_VALUES = {"owned", "licensed", "third_party", "unknown"}

POLICY_REFERENCES = (
    {
        "title": "إرشادات المحتوى المناسب للمعلنين",
        "url": "https://support.google.com/youtube/answer/6162278",
        "note": "تشرح لماذا تُعرض بعض الأخبار الحساسة بإعلانات محدودة.",
    },
    {
        "title": "سياسات تحقيق الدخل في قنوات YouTube",
        "url": "https://support.google.com/youtube/answer/1311392",
        "note": "تشرح «المحتوى المعاد استخدامه» ولماذا لا يكفي الشعار أو إعادة الرفع.",
    },
    {
        "title": "نظرة عامة على برنامج شركاء YouTube",
        "url": "https://support.google.com/youtube/answer/72851",
        "note": "الشروط العامة للأهلية، ومنها المحتوى الأصلي والأصيل.",
    },
)

SENSITIVE_TERMS = (
    # Arabic (with the common inflections used in news copy)
    "حرب",
    "حربي",
    "قصف",
    "غارة",
    "غارات",
    "مقتل",
    "قتيل",
    "قتلى",
    "وفاة",
    "وفيات",
    "مجزرة",
    "دماء",
    "دمار",
    "هجوم",
    "تفجير",
    "انفجار",
    "كارثة",
    "زلزال",
    "فيضان",
    "مجاعة",
    "انتحار",
    "اغتصاب",
    "تعذيب",
    "إعدام",
    "رهائن",
    "جثة",
    "جثث",
    "جرحى",
    "إصابات",
    "خطف",
    "اختطاف",
    "تهجير",
    "لاجئين",
    "وباء",
    "جائحة",
    # English
    "war",
    "airstrike",
    "bombing",
    "killed",
    "death",
    "dead",
    "massacre",
    "blood",
    "attack",
    "explosion",
    "disaster",
    "earthquake",
    "flood",
    "famine",
    "suicide",
    "rape",
    "torture",
    "execution",
    "hostage",
    "corpse",
    "wounded",
    "casualties",
    "pandemic",
)

_CLICKBAIT_MARKERS = (
    "لن تتوقع",
    "صدمة",
    "عاجل جدا",
    "خطير جدا",
    "shocking",
    "you won't believe",
    "must see",
)

_HASHTAG = re.compile(r"#[\w\u0600-\u06FF_]+")


def _normalize(value: Any, limit: int = 5000) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", str(value or ""))
    return " ".join(text.split())[:limit]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _choice(value: Any, allowed: set[str], default: str) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in allowed else default


@dataclass(frozen=True)
class SessionFacts:
    """Everything the audit needs. Nothing here leaves the machine."""

    title: str = ""
    description: str = ""
    source_channel: str = ""
    source_platform: str = "custom"
    rights: str = "unknown"
    rights_note: str = ""
    transform: str = "branded"
    duration_seconds: int = 0
    original_segments: int = 0
    has_third_party_music: bool = False
    source_gaps: int = 0
    category_id: str = "25"
    privacy: str = "public"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "SessionFacts":
        data = data or {}
        return cls(
            title=_normalize(data.get("title"), 200),
            description=_normalize(data.get("description"), 5000),
            source_channel=_normalize(data.get("source_channel"), 200),
            source_platform=_normalize(data.get("source_platform"), 40) or "custom",
            rights=_choice(data.get("rights"), RIGHTS_VALUES, "unknown"),
            rights_note=_normalize(data.get("rights_note"), 300),
            transform=_choice(data.get("transform"), TRANSFORM_LEVELS, "branded"),
            duration_seconds=_as_int(data.get("duration_seconds")),
            original_segments=_as_int(data.get("original_segments")),
            has_third_party_music=_as_bool(data.get("has_third_party_music")),
            source_gaps=_as_int(data.get("source_gaps")),
            category_id=_normalize(data.get("category_id"), 4) or "25",
            privacy=_normalize(data.get("privacy"), 20) or "public",
        )

    @property
    def transforms_are_cosmetic(self) -> bool:
        """A logo, a frame or a re-encode never changes what is being said."""
        return self.transform == "branded"

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "source_channel": self.source_channel,
            "source_platform": self.source_platform,
            "rights": self.rights,
            "rights_note": self.rights_note,
            "transform": self.transform,
            "duration_seconds": self.duration_seconds,
            "original_segments": self.original_segments,
            "source_gaps": self.source_gaps,
            "has_third_party_music": self.has_third_party_music,
            "category_id": self.category_id,
            "privacy": self.privacy,
        }


@dataclass(frozen=True)
class Risk:
    code: str
    severity: str
    label: str
    detail: str
    action: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "severity_label": SEVERITY_LABEL.get(self.severity, self.severity),
            "label": self.label,
            "detail": self.detail,
            "action": self.action,
        }


@dataclass
class Audit:
    score: int
    band: str
    headline: str
    risks: list[Risk] = field(default_factory=list)
    facts: SessionFacts = field(default_factory=SessionFacts)
    policy_revision: str = POLICY_REVISION

    @property
    def blockers(self) -> list[Risk]:
        return [risk for risk in self.risks if risk.severity == "blocker"]

    def actions(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for risk in sorted(self.risks, key=lambda item: SEVERITY_ORDER[item.severity]):
            if risk.action and risk.action not in seen:
                seen.add(risk.action)
                result.append(risk.action)
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "band": self.band,
            "band_label": BAND_LABEL.get(self.band, self.band),
            "headline": self.headline,
            "risks": [risk.as_dict() for risk in self.risks],
            "actions": self.actions(),
            "facts": self.facts.as_dict(),
            "policy_revision": self.policy_revision,
            "disclaimer": (
                "تقدير استشاري مبني على الإرشادات المنشورة، وليس حكماً من YouTube. "
                "الحالة الفعلية تظهر في YouTube Studio فقط."
            ),
        }


BAND_LABEL = {
    "ready": "متوافق مبدئياً",
    "conditional": "يحتاج تحسيناً",
    "at_risk": "معرّض للإعلانات المحدودة",
    "blocked": "غير مؤهل على الأرجح",
}


def _band_for(score: int) -> str:
    if score >= 85:
        return "ready"
    if score >= 60:
        return "conditional"
    if score >= 35:
        return "at_risk"
    return "blocked"


def _headline_for(band: str, risks: Sequence[Risk]) -> str:
    blocker = next((risk for risk in risks if risk.severity == "blocker"), None)
    if band == "blocked" and blocker:
        return blocker.label
    if band == "blocked":
        return "المخرَج الحالي لا يبدو مؤهلاً للإعلانات"
    if band == "at_risk":
        return "الإعلانات المحدودة هي النتيجة المرجّحة"
    if band == "conditional":
        return "قابل للتحسين قبل النشر"
    return "لا عوائق جوهرية ظاهرة"


def _term_present(term: str, haystack: str) -> bool:
    """Latin terms match whole words; Arabic terms match stems.

    Without the boundary check, "award" would match "war" and "deadline"
    would match "dead", producing noise that teaches the user to ignore the
    audit.
    """
    if term.isascii():
        return re.search(
            rf"(?<![a-z]){re.escape(term)}(?![a-z])", haystack
        ) is not None
    return term in haystack


def sensitive_terms_in(*values: str) -> list[str]:
    """Return the sensitive terms found, without altering the source text."""
    haystack = " ".join(_normalize(value, 5000).casefold() for value in values)
    found: list[str] = []
    for term in SENSITIVE_TERMS:
        if _term_present(term.casefold(), haystack) and term not in found:
            found.append(term)
    return found


def audit_session(facts: SessionFacts | Mapping[str, Any]) -> Audit:
    """Score a planned or finished session against the published rules."""
    if not isinstance(facts, SessionFacts):
        facts = SessionFacts.from_mapping(facts)

    risks: list[Risk] = []

    def add(
        code: str, severity: str, label: str, detail: str, action: str
    ) -> None:
        if any(risk.code == code for risk in risks):
            return
        risks.append(Risk(code, severity, label, detail, action))

    # 1. Rights and reuse -------------------------------------------------
    if facts.rights == "unknown":
        add(
            "unverified_rights",
            "high",
            "حقوق المصدر غير محددة",
            "لا يعرف المشروع إن كنت تملك المحتوى أو لديك ترخيص له. "
            "بدون ذلك لا يمكن تقييم أهلية الفيديو، ولا الدفاع عنه عند مطالبة.",
            "حدّد حقوق المصدر في الإعدادات: تملكه، لديك ترخيص، أو هو بث قناة أخرى.",
        )
    if facts.rights == "third_party" and facts.transform != "editorial":
        add(
            "reused_content",
            "blocker",
            "محتوى معاد استخدامه",
            "إعادة بث قناة أخرى كما هي — مع شعار أو إطار أو إعادة قياس — "
            "تُصنّف محتوى معاد استخدامه. النتيجة المعتادة: الفيديو بلا إعلانات، "
            "وقد تتأثر أهلية القناة كلها لا هذا الفيديو وحده.",
            "أضف طبقة تحريرية أصلية قبل النشر: مقدمة وخاتمة، تعليق صوتي، "
            "تحليل مقروء، فصول موقّتة، ومقاطع من إنتاجك.",
        )
    if facts.rights in {"owned", "licensed"} and facts.transform == "raw":
        add(
            "duplicate_reupload",
            "medium",
            "نشر متطابق دون إضافة",
            "حتى حين تملك الحقوق، نشر البث نفسه حرفياً على أكثر من قناة "
            "قد يُقرأ كمحتوى مكرر إذا لم يضف شيئاً للمشاهد.",
            "أضف مقدمة وخاتمة وقراءة تحليلية، أو انشر نسخة واحدة وأشر إلى الأخرى.",
        )
    if facts.transforms_are_cosmetic:
        add(
            "cosmetic_transformation",
            "high",
            "تحويل شكلي فقط",
            "الشعار والإطار وتغيير الدقة لا يغيّران المحتوى. "
            "YouTube يبحث عن قيمة أصلية يقدّمها صاحب القناة.",
            "خصّص دقيقة أصلية على الأقل لكل ربع ساعة: تعليق، تحليل، أو شرح سياق.",
        )

    # 2. Audio rights -----------------------------------------------------
    if facts.has_third_party_music:
        add(
            "third_party_music",
            "high",
            "موسيقى أو صوت طرف ثالث",
            "الموسيقى في البث تجلب مطالبة Content ID، فيتحول دخل الفيديو "
            "إلى صاحب الحق أو يُحجب في بعض البلدان.",
            "أزل الموسيقى أو استبدلها بأصوات مرخّصة، وأبقِ الصوت البشري غالباً.",
        )

    # 3. Sensitive news ---------------------------------------------------
    terms = sensitive_terms_in(facts.title, facts.description)
    if terms:
        sample = "، ".join(terms[:4])
        add(
            "sensitive_topic",
            "medium",
            "موضوع حساس ومحدود الإعلانات",
            f"العنوان أو الوصف يحمل موضوعاً حسّاساً ({sample}). "
            "الأخبار عن الحروب والكوارث والوفيات تُعرض عادةً بإعلانات محدودة "
            "حتى في القنوات الأصلية. هذا ليس عقاباً على القناة.",
            "اقبل الإعلانات المحدودة لهذه الحلقة، وقدّم سياقاً تحريرياً واضحاً، "
            "وتجنّب الصور الصادمة في المصغّرة. لا تعِد صياغة العنوان للتهرّب من التصنيف.",
        )

    # 4. Session shape ----------------------------------------------------
    if facts.duration_seconds > 6 * 3600:
        add(
            "long_session",
            "low",
            "جلسة متصلة طويلة",
            "بث متصل يتجاوز ست ساعات ينتج VOD طويلاً يصعب مشاهدته، "
            "وتقل فيه نسبة ما أنتجته أنت إلى ما نقلته.",
            "اقسم التغطية إلى مقاطع 15–45 دقيقة بعناوين وفصول، "
            "واحتفظ بالبث المتصل للحظات الحيّة فقط.",
        )
    if facts.source_gaps:
        add(
            "frozen_gaps",
            "medium",
            "انقطاعات وفجوات في المصدر",
            f"سُجّلت {facts.source_gaps} فجوة. الإطار المجمّد والصوت الصامت "
            "في الـVOD يخفضان الجودة المدركة ويضعفان تقييم المحتوى.",
            "أنهِ الجلسة أو اعرض شاشة انتظار بهوية القناة عند فقدان المصدر، "
            "ولا تترك آخر إطار مجمّداً.",
        )
    if facts.transform == "editorial":
        minimum = max(1, facts.duration_seconds // 1800)
        if facts.original_segments < minimum:
            add(
                "thin_commentary",
                "medium",
                "تعليق أصلي أقل من المطلوب",
                f"الخطة التحريرية تحتاج {minimum} مقاطع أصلية لهذه المدة، "
                f"والمسجّل {facts.original_segments}.",
                "أضف مقطعاً أصلياً واحداً كل نصف ساعة على الأقل: ملخص، سياق، أو تحليل.",
            )

    # 5. Metadata ---------------------------------------------------------
    metadata_issues: list[str] = []
    title = facts.title.strip()
    description = facts.description.strip()
    if not title:
        metadata_issues.append("العنوان فارغ")
    elif len(title) < 20:
        metadata_issues.append("العنوان أقصر من أن يشرح الفيديو")
    if title and any(marker in title.casefold() for marker in _CLICKBAIT_MARKERS):
        metadata_issues.append("العنوان يستخدم أسلوب إثارة")
    if title and title.upper() == title and any(ch.isalpha() for ch in title):
        metadata_issues.append("العنوان بحروف كبيرة بالكامل")
    if len(_HASHTAG.findall(title)) > 3:
        metadata_issues.append("هاشتاغات كثيرة في العنوان")
    if len(description) < 150:
        metadata_issues.append("الوصف قصير جداً ولا يشرح السياق")
    if facts.source_channel and facts.source_channel.casefold() not in (
        title + " " + description
    ).casefold():
        metadata_issues.append("لا ذكر للمصدر في العنوان أو الوصف")
    if metadata_issues:
        add(
            "weak_metadata",
            "low",
            "بيانات وصفية ضعيفة",
            "؛ ".join(metadata_issues) + ". البيانات الضعيفة لا تمنع الإعلانات بذاتها، "
            "لكنها تخفض المشاهدة والتصنيف وتُضعف أي اعتراض لاحق.",
            "اكتب عنواناً واضحاً (40–75 حرفاً)، ووصفاً يشرح السياق ويسمّي المصدر، "
            "وأضف فصولاً موقّتة.",
        )

    score = 100 - sum(SEVERITY_WEIGHT[risk.severity] for risk in risks)
    if any(risk.severity == "blocker" for risk in risks):
        score = min(score, 30)
    for risk in risks:
        cap = SCORE_CAPS.get(risk.code)
        if cap is not None:
            score = min(score, cap)
    score = max(0, min(100, score))
    band = _band_for(score)
    return Audit(
        score=score,
        band=band,
        headline=_headline_for(band, risks),
        risks=risks,
        facts=facts,
    )


def format_timestamp(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def build_chapters(events: Iterable[Any], minimum: int = 3) -> list[str]:
    """Turn (seconds, title) events into a YouTube chapter block.

    YouTube needs at least three chapters, the first at 00:00, each at least
    ten seconds long. Anything else is returned as an empty list instead of
    producing a block that YouTube will ignore.
    """
    cleaned: list[tuple[int, str]] = []
    for event in events:
        if isinstance(event, Mapping):
            seconds, title = event.get("seconds"), event.get("title")
        else:
            try:
                seconds, title = event  # type: ignore[misc]
            except (TypeError, ValueError):
                continue
        title = _normalize(title, 100)
        if not title:
            continue
        cleaned.append((_as_int(seconds), title))
    cleaned.sort(key=lambda item: item[0])
    unique: list[tuple[int, str]] = []
    for seconds, title in cleaned:
        if unique and seconds - unique[-1][0] < 10:
            continue
        if unique and title.casefold() == unique[-1][1].casefold():
            continue
        unique.append((seconds, title))
    if not unique:
        return []
    if unique[0][0] > 0:
        unique.insert(0, (0, "بداية البث"))
        # Re-check spacing: a chapter 5s after 00:00 is invalid on YouTube.
        spaced: list[tuple[int, str]] = []
        for seconds, title in unique:
            if spaced and seconds - spaced[-1][0] < 10:
                continue
            spaced.append((seconds, title))
        unique = spaced
    if len(unique) < minimum:
        return []
    return [f"{format_timestamp(seconds)} {title}" for seconds, title in unique]


def _disclosure_line(facts: SessionFacts) -> str:
    if facts.rights == "third_party":
        return (
            "نعيد بث المصدر مع تعليق وتحليل من فريقنا؛ الحقوق تعود لأصحابها."
        )
    if facts.rights == "licensed":
        return "نشر بموجب ترخيص من صاحب الحقوق."
    if facts.rights == "owned":
        return "محتوى من إنتاجنا."
    return "المصدر مذكور أعلاه؛ راجع وصف القناة لتفاصيل الحقوق."


def build_transform_plan(
    facts: SessionFacts | Mapping[str, Any],
    chapters: Iterable[Any] | None = None,
    channel_name: str = "",
) -> dict[str, Any]:
    """A concrete editorial layer the user can apply to the next session."""
    if not isinstance(facts, SessionFacts):
        facts = SessionFacts.from_mapping(facts)
    title = facts.title or "هذه التغطية"
    source = facts.source_channel or "المصدر"
    channel = channel_name or "قناتنا"

    intro = (
        f"مرحباً بكم في {channel}. نتابع الآن {title} مع {source}. "
        "خلال هذه التغطية نوضّح ثلاث نقاط: ما وقع، ولماذا يهم المشاهد، "
        "وما الذي ننتظره بعد ذلك. ننقل الوقائع أولاً، ثم نضيف قراءة تحليلية موجزة."
    )
    outro = (
        "بهذا نصل إلى ختام هذه التغطية. أعدنا أهم ثلاث نقاط، "
        "وذكرنا المصدر الذي استندنا إليه. تابعونا للحلقات القادمة."
    )
    chapter_lines = build_chapters(chapters or [])
    description_lines = [
        f"🔴 {title}",
        "",
        "تغطية مباشرة مع تعليق وتحليل.",
        f"المصدر: {source}",
        _disclosure_line(facts),
        "",
    ]
    if chapter_lines:
        description_lines.append("⏱ الفصول:")
        description_lines.extend(chapter_lines)
        description_lines.append("")
    description_lines.append("#أخبار #بث_مباشر #تحليل")
    description = "\n".join(description_lines).strip()

    plan_audit = audit_session(facts)
    checklist = [
        {
            "id": "rights",
            "label": "حقوق المصدر محددة",
            "detail": "تملكه، لديك ترخيص، أو هو بث قناة أخرى — مع معرفة النتيجة.",
            "done": facts.rights in {"owned", "licensed"},
        },
        {
            "id": "intro",
            "label": "مقدمة أصلية 15–25 ثانية",
            "detail": "تسمّي الموضوع والمصدر وتقول للمشاهد ماذا سيجد.",
            "done": facts.transform == "editorial",
        },
        {
            "id": "commentary",
            "label": "تعليق أصلي كل 20–30 دقيقة",
            "detail": "60–90 ثانية بصوتك: ما الجديد، لماذا يهم، وما الذي نراقبه.",
            "done": facts.original_segments >= max(1, facts.duration_seconds // 1800),
        },
        {
            "id": "outro",
            "label": "خاتمة أصلية",
            "detail": "ثلاث نقاط ختامية وإحالة إلى المصدر.",
            "done": facts.transform == "editorial",
        },
        {
            "id": "chapters",
            "label": "فصول موقّتة في الوصف",
            "detail": "ثلاثة فصول على الأقل، أولها 00:00.",
            "done": bool(chapter_lines),
        },
        {
            "id": "music",
            "label": "لا موسيقى طرف ثالث",
            "detail": "الموسيقى غير المرخّصة تسحب دخل الفيديو إلى صاحب الحق.",
            "done": not facts.has_third_party_music,
        },
        {
            "id": "metadata",
            "label": "عنوان ووصف واضحان",
            "detail": "عنوان 40–75 حرفاً، ووصف يشرح السياق ويسمّي المصدر.",
            "done": not any(
                risk.code == "weak_metadata" for risk in plan_audit.risks
            ),
        },
        {
            "id": "gaps",
            "label": "لا إطارات مجمّدة",
            "detail": "أنهِ الجلسة أو اعرض شاشة انتظار عند فقدان المصدر.",
            "done": facts.source_gaps == 0,
        },
    ]
    return {
        "headline": "خطة التحويل التحريري",
        "intro": intro,
        "segments": [
            {
                "every_minutes": 20,
                "prompts": [
                    "ما الجديد منذ المقطع السابق؟",
                    "لماذا يهم هذا المشاهد الآن؟",
                    "ما الذي ننتظره أو نراقبه بعد ذلك؟",
                ],
            }
        ],
        "outro": outro,
        "description": description,
        "chapters": chapter_lines,
        "checklist": checklist,
        "disclaimer": (
            "الخطة إطار عمل تحريري. اكتب التعليق بكلماتك واستند إلى وقائع "
            "موثّقة؛ لا تحوّل النقل إلى ادّعاء ملكية."
        ),
    }


def render_plan_markdown(
    plan: Mapping[str, Any], audit: Audit | Mapping[str, Any] | None = None
) -> str:
    audit_dict = audit.as_dict() if isinstance(audit, Audit) else (audit or {})
    lines = ["# خطة التحويل التحريري", ""]
    if audit_dict:
        lines.extend(
            [
                f"- التقدير: **{audit_dict.get('score')}/100** "
                f"({audit_dict.get('band_label', '')})",
                f"- الخلاصة: {audit_dict.get('headline', '')}",
                "",
            ]
        )
        risks = audit_dict.get("risks") or []
        if risks:
            lines.append("## العوائق")
            for risk in risks:
                lines.append(
                    f"- **{risk.get('label')}** ({risk.get('severity_label')}): "
                    f"{risk.get('detail')}"
                )
            lines.append("")
    lines.extend(
        [
            "## المقدمة",
            str(plan.get("intro", "")),
            "",
            "## المقاطع الأصلية",
        ]
    )
    for segment in plan.get("segments", []):
        lines.append(f"- كل {segment.get('every_minutes')} دقيقة:")
        for prompt in segment.get("prompts", []):
            lines.append(f"  - {prompt}")
    lines.extend(["", "## الخاتمة", str(plan.get("outro", "")), "", "## الوصف المقترح", "```"])
    lines.append(str(plan.get("description", "")))
    lines.append("```")
    lines.append("")
    lines.append("## قائمة التحقق")
    for item in plan.get("checklist", []):
        mark = "x" if item.get("done") else " "
        lines.append(f"- [{mark}] {item.get('label')} — {item.get('detail')}")
    lines.append("")
    lines.append(f"> {plan.get('disclaimer', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Local, private history
# ---------------------------------------------------------------------------

AUDIT_FILE = "ads-audit.jsonl"
SESSION_FILE = "sessions.jsonl"
_MAX_RECORD_BYTES = 512 * 1024
_MAX_RECORDS = 500


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    ensure_private_dir(path.parent)
    line = json.dumps(dict(record), ensure_ascii=False) + "\n"
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if path.stat().st_size > _MAX_RECORD_BYTES:
            _trim_jsonl(path)
    except OSError as exc:  # history must never break a broadcast
        logging.warning("Could not write the monetization log: %s", exc)


def _trim_jsonl(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-_MAX_RECORDS:]
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        logging.warning("Could not rotate the monetization log: %s", exc)


def read_jsonl(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    result: list[dict[str, Any]] = []
    for line in lines[-max(1, limit) :]:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def audit_path(state_dir: Path) -> Path:
    return Path(state_dir) / AUDIT_FILE


def session_path(state_dir: Path) -> Path:
    return Path(state_dir) / SESSION_FILE


def _compact_facts(facts: SessionFacts) -> dict[str, Any]:
    """Keep the useful facts without archiving a full copy of the description."""
    data = facts.as_dict()
    data["description"] = _normalize(data.get("description"), 400)
    return data


def record_audit(
    state_dir: Path,
    audit: Audit,
    *,
    video_id: str = "",
    trigger: str = "dashboard",
) -> dict[str, Any]:
    record = {
        "timestamp": time.time(),
        "video_id": video_id,
        "trigger": trigger,
        **audit.as_dict(),
    }
    record["facts"] = _compact_facts(audit.facts)
    _append_jsonl(audit_path(state_dir), record)
    return record


def record_session(
    state_dir: Path,
    *,
    video_id: str = "",
    started_at: float | None = None,
    ended_at: float | None = None,
    facts: SessionFacts | None = None,
    audit: Audit | None = None,
    platforms: Sequence[str] = (),
    end_reason: str = "",
    recording: str = "",
    editorial_segments: int = 0,
    chapters: Sequence[str] = (),
    kit: str = "",
) -> dict[str, Any]:
    facts = facts or SessionFacts()
    audit = audit or audit_session(facts)
    start = float(started_at or time.time())
    end = float(ended_at or time.time())
    record = {
        "timestamp": end,
        "video_id": video_id,
        "started_at": start,
        "ended_at": end,
        "duration_seconds": max(0, int(end - start)),
        "source_gaps": facts.source_gaps,
        "platforms": list(platforms),
        "end_reason": end_reason,
        "recording": str(recording or ""),
        "editorial_segments": int(editorial_segments or 0),
        "chapters": list(chapters or ()),
        "kit": str(kit or ""),
        "score": audit.score,
        "band": audit.band,
        "risk_codes": [risk.code for risk in audit.risks],
        "facts": _compact_facts(facts),
    }
    _append_jsonl(session_path(state_dir), record)
    return record


def summarize(state_dir: Path, days: int = 14) -> dict[str, Any]:
    """Small aggregate for the dashboard; no personal data leaves the disk."""
    cutoff = time.time() - max(1, days) * 86400
    sessions = [
        item
        for item in read_jsonl(session_path(state_dir), limit=_MAX_RECORDS)
        if float(item.get("timestamp") or 0) >= cutoff
    ]
    audits = [
        item
        for item in read_jsonl(audit_path(state_dir), limit=_MAX_RECORDS)
        if float(item.get("timestamp") or 0) >= cutoff
    ]
    scores = [int(item.get("score") or 0) for item in sessions]
    count = len(sessions)
    blocked = sum(1 for item in sessions if item.get("band") == "blocked")
    at_risk = sum(1 for item in sessions if item.get("band") == "at_risk")
    return {
        "days": days,
        "sessions": count,
        "audits": len(audits),
        "average_score": round(sum(scores) / count) if count else None,
        "blocked": blocked,
        "at_risk": at_risk,
        "last_session": sessions[-1] if sessions else None,
        "recent": sessions[-10:][::-1],
    }


def facts_from_settings(
    settings: Any,
    source: Any = None,
    *,
    duration_seconds: int = 0,
    original_segments: int = 0,
    source_gaps: int = 0,
) -> SessionFacts:
    """Build facts from what the relay already knows. Safe with SimpleNamespace."""
    title = getattr(source, "title", "") if source is not None else ""
    description = getattr(source, "description", "") if source is not None else ""
    channel = getattr(source, "channel", "") if source is not None else ""
    platform = getattr(source, "platform", "") if source is not None else ""
    return SessionFacts(
        title=_normalize(title, 200),
        description=_normalize(description, 5000),
        source_channel=_normalize(channel, 200),
        source_platform=_normalize(platform, 40) or "custom",
        rights=_choice(
            getattr(settings, "source_rights", "unknown"), RIGHTS_VALUES, "unknown"
        ),
        rights_note=_normalize(
            getattr(settings, "source_rights_note", ""), 300
        ),
        transform=_choice(
            getattr(settings, "transform_level", "branded"),
            TRANSFORM_LEVELS,
            "branded",
        ),
        duration_seconds=_as_int(duration_seconds),
        original_segments=_as_int(original_segments),
        has_third_party_music=_as_bool(
            getattr(settings, "source_third_party_music", False)
        ),
        source_gaps=_as_int(source_gaps),
        category_id=_normalize(getattr(settings, "youtube_category_id", "25"), 4),
        privacy=_normalize(getattr(settings, "youtube_privacy", "public"), 20),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Small CLI so the estimate works without the dashboard.

    python -m live_relay.monetization audit --rights third_party --transform branded
    python -m live_relay.monetization plan  --rights licensed --transform editorial
    python -m live_relay.monetization summary --state-dir ~/.config/live-relay
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m live_relay.monetization",
        description="Ad-suitability estimate and editorial plan for a relay session",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_facts(target: argparse.ArgumentParser) -> None:
        target.add_argument("--title", default="")
        target.add_argument("--description", default="")
        target.add_argument("--source-channel", default="")
        target.add_argument(
            "--rights",
            default="unknown",
            choices=sorted(RIGHTS_VALUES),
            help="owned, licensed, third_party, or unknown",
        )
        target.add_argument(
            "--transform",
            default="branded",
            choices=sorted(TRANSFORM_LEVELS),
            help="raw, branded, or editorial",
        )
        target.add_argument("--duration", type=int, default=0, help="seconds")
        target.add_argument("--segments", type=int, default=0)
        target.add_argument("--gaps", type=int, default=0)
        target.add_argument("--music", action="store_true")
        target.add_argument("--json", action="store_true")

    audit_cmd = sub.add_parser("audit", help="score the session")
    add_facts(audit_cmd)
    plan_cmd = sub.add_parser("plan", help="print the editorial plan as Markdown")
    add_facts(plan_cmd)
    summary_cmd = sub.add_parser("summary", help="show recent local sessions")
    summary_cmd.add_argument("--state-dir", default=str(Path.home() / ".config" / "live-relay"))
    summary_cmd.add_argument("--days", type=int, default=14)

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "summary":
        data = summarize(Path(args.state_dir).expanduser(), days=args.days)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    facts = SessionFacts(
        title=args.title,
        description=args.description,
        source_channel=args.source_channel,
        rights=args.rights,
        transform=args.transform,
        duration_seconds=args.duration,
        original_segments=args.segments,
        source_gaps=args.gaps,
        has_third_party_music=args.music,
    )
    audit = audit_session(facts)
    if args.command == "plan":
        print(render_plan_markdown(build_transform_plan(facts), audit))
        return 0
    if args.json:
        print(json.dumps(audit.as_dict(), ensure_ascii=False, indent=2))
        return 0
    print(f"{audit.score}/100 — {BAND_LABEL.get(audit.band, audit.band)}")
    print(audit.headline)
    for risk in audit.risks:
        print(f"  [{SEVERITY_LABEL[risk.severity]}] {risk.label}: {risk.action}")
    print("\n" + audit.as_dict()["disclaimer"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
