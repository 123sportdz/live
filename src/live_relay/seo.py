"""Search and originality helpers for stream metadata.

The relay knows the source title and description, but copying them verbatim is
both weak for search and a visible signal of reused content. This module
rewrites them **without adding a single fact**:

* the topic is extracted from the source title, not invented;
* the description keeps the source's own sentences (via the newsroom brief)
  and adds structure, attribution and chapters;
* keywords come from the source tags and the topic, deduplicated and ordered;
* every candidate is scored and the reasons are shown to the user.

It is deterministic, offline, and safe to run on every stream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .newsroom import build_brief, normalize_arabic

LANGS = {"ar", "en", "fr", "es"}

LIVE_SUFFIX = {
    "ar": "تغطية مباشرة",
    "en": "Live coverage",
    "fr": "En direct",
    "es": "En directo",
}
ANALYSIS_SUFFIX = {
    "ar": "متابعة وتحليل",
    "en": "Analysis and updates",
    "fr": "Analyse et suivi",
    "es": "Análisis y seguimiento",
}
LIVE_PREFIX = {"ar": "مباشر", "en": "Live", "fr": "Direct", "es": "En directo"}
SOURCE_LABEL = {"ar": "المصدر", "en": "Source", "fr": "Source", "es": "Fuente"}
WATCH_LABEL = {
    "ar": "ما تجده في هذا البث",
    "en": "What you will find here",
    "fr": "Au programme",
    "es": "Qué encontrarás",
}
DISCLOSURE = {
    "ar": "نعيد بث المصدر مع تعليق وتحليل؛ الحقوق تعود لأصحابها.",
    "en": "Rebroadcast with original commentary and analysis; rights belong to their owners.",
    "fr": "Rediffusion avec commentaire et analyse originaux ; les droits appartiennent à leurs titulaires.",
    "es": "Redifusión con comentario y análisis propios; los derechos pertenecen a sus titulares.",
}
HOOK = {
    "ar": "نتابع في هذا البث {topic} مع تحديثات مباشرة وتحليل{from_channel}.",
    "en": "Live coverage of {topic}, with updates and analysis{from_channel}.",
    "fr": "Suivez {topic} en direct, avec mises à jour et analyse{from_channel}.",
    "es": "Seguimos {topic} en directo, con actualizaciones y análisis{from_channel}.",
}
FROM_CHANNEL = {
    "ar": " من {channel}",
    "en": " from {channel}",
    "fr": " de {channel}",
    "es": " de {channel}",
}

STOPWORDS = {
    "ar": {
        "من", "في", "على", "عن", "الى", "إلى", "مع", "هذا", "هذه", "ذلك",
        "التي", "الذي", "كان", "كانت", "بعد", "قبل", "بين", "كما", "قد",
        "كل", "أو", "او", "ثم", "لكن", "حتى", "هو", "هي", "هم", "ما",
        "لا", "لم", "لن", "إن", "ان", "أن", "عند", "خلال", "ضد", "نحو",
        "أكثر", "اكثر", "أقل", "اقل", "جدا", "جداً", "الآن", "الان",
        # Reporting verbs and filler that never help a search query.
        "أعلن", "اعلن", "أعلنت", "اعلنت", "قال", "وقال", "قالت", "وقالت",
        "أكد", "اكد", "أوضح", "اوضح", "أشار", "اشار", "ذكر", "كشف",
        "شدد", "تأتي", "ياتي", "يأتي", "حسب", "وفق", "بحسب", "خلال",
        "اليوم", "أمس", "امس", "غدا", "غداً", "مؤخرا", "مؤخراً",
    },
    "en": {
        "the", "and", "for", "with", "that", "this", "from", "were", "was",
        "are", "you", "your", "have", "has", "will", "live", "now", "into",
        "about", "after", "before", "over", "under", "more", "than", "its",
    },
    "fr": {
        "les", "des", "une", "dans", "pour", "avec", "sur", "par", "aux",
        "est", "sont", "plus", "pas", "que", "qui", "son", "ses", "cette",
    },
    "es": {
        "los", "las", "una", "con", "por", "para", "del", "que", "este",
        "esta", "más", "mas", "sin", "sus", "como",
    },
}

CLICKBAIT = (
    "لن تتوقع",
    "صدمة",
    "صادم",
    "خطير جدا",
    "خطير جداً",
    "عاجل جدا",
    "لا تفوت",
    "شاهد قبل الحذف",
    "you won't believe",
    "shocking",
    "must see",
    "gone wrong",
    "لا يصدق",
)

_LEADING_LABEL = re.compile(
    r"^(?:عاجل|breaking|urgent|حصري|exclusive|بث\s+مباشر|live|مباشر|"
    r"تغطية\s+مباشرة|live\s+coverage|متابعة\s+وتحليل|analysis\s+and\s+updates|"
    r"en\s+direct|en\s+directo)"
    r"\s*[:\-–—]\s*",
    re.IGNORECASE,
)
_TRAILING_MARKER = re.compile(
    r"\s*[-–—|]?\s*(?:الآن|الان|now|مباشر|live|بث\s+مباشر)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_PREPOSITIONS = re.compile(
    r"\s+(?:على|في|من|مع|بين|عن|إلى|الى|خلال|بعد|قبل|ضد|حول|داخل|خارج|"
    r"at|in|on|with|from|after|before|between|over)\s+",
    re.IGNORECASE,
)
_HASHTAG = re.compile(r"#[\w\u0600-\u06FF_]+")
_URL = re.compile(r"https?://\S+")
_SPACE = re.compile(r"\s+")


def detect_language(text: str, requested: str = "auto") -> str:
    requested = (requested or "auto").strip().lower()
    if requested in LANGS:
        return requested
    sample = str(text or "")
    if not sample:
        return "ar"
    arabic = len(re.findall(r"[\u0600-\u06FF]", sample))
    letters = len(re.findall(r"[A-Za-z\u0600-\u06FF]", sample))
    return "ar" if letters and arabic / letters > 0.3 else "en"


def clean_text(value: Any, limit: int = 500) -> str:
    text = _HASHTAG.sub(" ", str(value or ""))
    text = _URL.sub(" ", text)
    return _SPACE.sub(" ", text).strip()[:limit]


def is_clickbait(title: str) -> bool:
    lowered = normalize_arabic(title)
    if any(marker in lowered for marker in CLICKBAIT):
        return True
    if re.search(r"[!?]{2,}", title):
        return True
    letters = [char for char in title if char.isalpha()]
    if letters and all(char.isupper() for char in letters) and len(letters) > 6:
        return True
    return False


def clean_topic(title: str, channel: str = "") -> str:
    """Strip labels and markers, never the subject itself."""
    original = clean_text(title, 200)
    topic = original
    topic = _LEADING_LABEL.sub("", topic)
    topic = _TRAILING_MARKER.sub("", topic)
    # Our own suffixes must be idempotent: pressing "suggest" twice must not
    # stack "| تغطية مباشرة | متابعة وتحليل".
    known_suffixes = [
        *LIVE_SUFFIX.values(),
        *ANALYSIS_SUFFIX.values(),
        *LIVE_PREFIX.values(),
    ]
    changed = True
    while changed:
        changed = False
        for marker in known_suffixes:
            for separator in (" | ", " — ", " – ", " - "):
                suffix = f"{separator}{marker}"
                if topic.casefold().endswith(suffix.casefold()):
                    topic = topic[: -len(suffix)].rstrip()
                    changed = True
    if channel:
        for separator in (" - ", " | ", " — ", " – "):
            suffix = f"{separator}{channel}"
            if topic.casefold().endswith(suffix.casefold()):
                topic = topic[: -len(suffix)]
    topic = topic.strip(" -–—|:،,.\"'«»")
    topic = _SPACE.sub(" ", topic)
    if len(topic) < 12:
        return original
    if len(topic) < len(original) * 0.45:
        return original
    return topic


def _words(text: str) -> list[str]:
    return re.findall(r"[\w\u0600-\u06FF]+", str(text or ""))


_STOP_CACHE: dict[str, set[str]] = {}


def stopwords(language: str) -> set[str]:
    if language not in _STOP_CACHE:
        _STOP_CACHE[language] = {
            normalize_arabic(word)
            for word in STOPWORDS.get(language, STOPWORDS["ar"])
        }
    return _STOP_CACHE[language]


def content_words(text: str, language: str) -> list[str]:
    stop = stopwords(language)
    result: list[str] = []
    for word in _words(text):
        normalized = normalize_arabic(word)
        if len(normalized) < 3 or normalized in stop:
            continue
        if word.isdigit():
            continue
        result.append(word)
    return result


def keyword_key(value: str) -> str:
    """Comparison key: case, Arabic variants and the definite article folded."""
    return re.sub(r"^ال", "", normalize_arabic(value))


def primary_keyword(topic: str, tags: Sequence[str], language: str) -> str:
    cleaned_tags = [clean_text(tag, 40) for tag in tags or ()]
    cleaned_tags = [tag for tag in cleaned_tags if len(tag) >= 3]
    normalized_topic = normalize_arabic(topic)
    for tag in cleaned_tags:
        if normalize_arabic(tag) in normalized_topic and len(tag) >= 8:
            return tag
    head = _PREPOSITIONS.split(topic, maxsplit=1)[0]
    words = content_words(head, language)
    if not words:
        words = content_words(topic, language)
    if not words:
        return cleaned_tags[0][:40] if cleaned_tags else ""
    phrase = " ".join(words[:4])
    if len(phrase) > 40:
        phrase = " ".join(words[:3])[:40].strip()
    return phrase


def _fit(text: str, limit: int = 100) -> str:
    text = _SPACE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.strip(" -–—|:،,")


def _contains_live_marker(text: str) -> bool:
    lowered = normalize_arabic(text)
    return any(
        marker in lowered
        for marker in ("مباشر", "بث مباشر", "live", "en direct", "directo")
    )


def score_title(
    title: str,
    primary: str,
    source_title: str,
    language: str,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    length = len(title)
    if 40 <= length <= 75:
        score += 3
        reasons.append("الطول مناسب للبحث (40–75 حرفاً)")
    elif 30 <= length <= 90:
        score += 1
        reasons.append("الطول مقبول")
    elif length < 30:
        score -= 2
        reasons.append("أقصر من المثالي (30–75 حرفاً)")
    else:
        score -= 2
        reasons.append("أطول من اللازم")
    if primary and normalize_arabic(primary) in normalize_arabic(title[:60]):
        score += 3
        reasons.append("الكلمة المفتاحية في البداية")
    if _contains_live_marker(title):
        score += 1
        reasons.append("يذكر أنه بث مباشر")
    if is_clickbait(title):
        score -= 5
        reasons.append("يستخدم أسلوب إثارة")
    if re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", title):
        score -= 2
        reasons.append("يحتوي رموزاً تعبيرية")
    lowered_words = [normalize_arabic(word) for word in _words(title)]
    if len(lowered_words) != len(set(lowered_words)):
        score -= 1
        reasons.append("فيه كلمة مكررة")
    if source_title and normalize_arabic(title) == normalize_arabic(source_title):
        score -= 3
        reasons.append("منسوخ من عنوان المصدر حرفياً")
    elif source_title:
        score += 1
        reasons.append("صياغة مختلفة عن المصدر")
    return score, reasons


def title_candidates(
    source_title: str,
    channel: str,
    tags: Sequence[str],
    language: str,
    limit: int = 4,
    reference_title: str = "",
) -> list[dict[str, Any]]:
    topic = clean_topic(source_title, channel)
    reference = reference_title or source_title
    primary = primary_keyword(topic, tags, language)
    live = LIVE_SUFFIX.get(language, LIVE_SUFFIX["ar"])
    analysis = ANALYSIS_SUFFIX.get(language, ANALYSIS_SUFFIX["ar"])
    prefix = LIVE_PREFIX.get(language, LIVE_PREFIX["ar"])
    has_live = _contains_live_marker(topic)

    raw: list[str] = []
    if has_live:
        raw.append(f"{topic} | {analysis}")
        raw.append(f"{topic} — {analysis}")
    else:
        raw.append(f"{topic} | {live}")
        raw.append(f"{prefix}: {topic}")
        raw.append(f"{topic} | {analysis}")
    if channel:
        raw.append(f"{topic} | {channel}")

    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in raw:
        title = _fit(value, 100)
        key = normalize_arabic(title)
        if not title or key in seen:
            continue
        seen.add(key)
        score, reasons = score_title(title, primary, reference, language)
        result.append(
            {
                "title": title,
                "score": score,
                "reasons": reasons,
                "primary_keyword": primary,
            }
        )
        if len(result) >= limit:
            break
    return result


def build_keywords(
    topic: str,
    tags: Sequence[str],
    description: str,
    channel: str,
    language: str,
    limit: int = 500,
    max_items: int = 15,
) -> tuple[str, ...]:
    primary = primary_keyword(topic, tags, language)
    candidates: list[str] = []
    if primary:
        candidates.append(primary)
    candidates.extend(clean_text(tag, 40) for tag in tags or ())
    raw_words = _words(topic)
    stop = stopwords(language)
    for size in (3, 2):
        for index in range(0, max(0, len(raw_words) - size + 1)):
            window = raw_words[index : index + size]
            if any(normalize_arabic(word) in stop for word in window):
                continue
            phrase = " ".join(window)
            if 8 <= len(phrase) <= 40:
                candidates.append(phrase)
    seen_terms: dict[str, list[Any]] = {}
    for word in content_words(description, language):
        key = keyword_key(word)
        if key in seen_terms:
            seen_terms[key][0] += 1
        else:
            seen_terms[key] = [1, re.sub(r"[\u0617-\u061A\u064B-\u0652\u0670]", "", word)]
    frequent = [
        item for item in seen_terms.values() if item[0] >= 2
    ]
    frequent.sort(key=lambda item: item[0], reverse=True)
    candidates.extend(str(item[1]) for item in frequent[:3])

    result: list[str] = []
    total = 0
    seen: set[str] = set()
    for candidate in candidates:
        value = " ".join(clean_text(candidate, 40).split()).strip(" ,#")
        key = keyword_key(value)
        if not value or len(value) < 3 or key in seen:
            continue
        if value.casefold() == (channel or "").casefold():
            continue
        if len(result) >= max_items:
            break
        extra = len(value) + (1 if result else 0)
        if total + extra > limit:
            continue
        seen.add(key)
        result.append(value)
        total += extra
    return tuple(result)


def build_hashtags(
    primary: str,
    keywords: Sequence[str],
    language: str,
    limit: int = 4,
) -> tuple[str, ...]:
    raw: list[str] = []
    if primary:
        raw.append(primary)
    raw.extend(keywords[: limit + 2])
    result: list[str] = []
    seen: set[str] = set()
    for value in raw:
        words = content_words(value, language) or _words(value)
        if not words:
            continue
        separator = "" if language == "en" else "_"
        tag = "#" + separator.join(words[:3]).replace("#", "")
        tag = re.sub(r"[^\w\u0600-\u06FF_#]", "", tag)
        key = tag.casefold()
        if len(tag) < 4 or key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) >= limit:
            break
    return tuple(result)


def build_description(
    topic: str,
    channel: str,
    facts: Sequence[str],
    chapters: Sequence[str],
    hashtags: Sequence[str],
    language: str,
    rights: str = "unknown",
) -> str:
    hook = HOOK.get(language, HOOK["ar"]).format(
        topic=topic,
        from_channel=(
            FROM_CHANNEL.get(language, FROM_CHANNEL["ar"]).format(channel=channel)
            if channel
            else ""
        ),
    )
    parts: list[str] = [hook]
    clean_facts = []
    for fact in facts or ():
        cleaned_fact = clean_text(fact, 220)
        if cleaned_fact:
            clean_facts.append(cleaned_fact)
    if clean_facts:
        label = WATCH_LABEL.get(language, WATCH_LABEL["ar"])
        parts.append(label + ":\n" + "\n".join(f"• {fact}" for fact in clean_facts[:3]))
    if channel:
        parts.append(f"{SOURCE_LABEL.get(language, 'المصدر')}: {channel}")
    if rights == "third_party":
        parts.append(DISCLOSURE.get(language, DISCLOSURE["ar"]))
    if chapters:
        parts.append("⏱ " + ("الفصول" if language == "ar" else "Chapters") + ":\n" + "\n".join(chapters))
    if hashtags:
        parts.append(" ".join(hashtags))
    text = "\n\n".join(parts)
    if len(text) <= 5000:
        return text
    # Drop extracted facts first; they are the most redundant section.
    while clean_facts and len(text) > 5000:
        clean_facts.pop()
        rebuilt = [hook]
        if clean_facts:
            label = WATCH_LABEL.get(language, WATCH_LABEL["ar"])
            rebuilt.append(
                label + ":\n" + "\n".join(f"• {fact}" for fact in clean_facts)
            )
        if channel:
            rebuilt.append(f"{SOURCE_LABEL.get(language, 'المصدر')}: {channel}")
        if rights == "third_party":
            rebuilt.append(DISCLOSURE.get(language, DISCLOSURE["ar"]))
        if chapters:
            rebuilt.append(
                "⏱ " + ("الفصول" if language == "ar" else "Chapters") + ":\n" + "\n".join(chapters)
            )
        if hashtags:
            rebuilt.append(" ".join(hashtags))
        text = "\n\n".join(rebuilt)
    return text[:5000]


@dataclass
class MetadataProposal:
    title: str
    description: str
    keywords: tuple[str, ...]
    hashtags: tuple[str, ...]
    candidates: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    language: str = "ar"
    provider: str = "local-seo"

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "keywords": list(self.keywords),
            "hashtags": list(self.hashtags),
            "candidates": self.candidates,
            "warnings": self.warnings,
            "language": self.language,
            "provider": self.provider,
        }


def propose_metadata(
    *,
    source_title: str,
    source_description: str = "",
    source_channel: str = "",
    source_tags: Sequence[str] = (),
    language: str = "auto",
    chapters: Sequence[str] = (),
    rights: str = "unknown",
    limit: int = 4,
    reference_title: str = "",
) -> MetadataProposal:
    language = detect_language(source_title, language)
    topic = clean_topic(source_title, source_channel)
    candidates = title_candidates(
        source_title,
        source_channel,
        source_tags,
        language,
        limit=limit,
        reference_title=reference_title,
    )
    warnings: list[str] = []
    if is_clickbait(source_title):
        warnings.append(
            "عنوان المصدر يستخدم أسلوب إثارة؛ الاقتراحات تعيد صياغته بلا مبالغة."
        )
    if not candidates:
        best = _fit(topic, 100)
        candidates = [
            {
                "title": best,
                "score": 0,
                "reasons": ["تعذر توليد بديل؛ هذا تنظيف للعنوان الأصلي"],
                "primary_keyword": "",
            }
        ]
        warnings.append("العنوان قصير جداً؛ أضف وصفاً للموضوع يدوياً.")
    best_candidate = max(
        enumerate(candidates), key=lambda item: (item[1]["score"], -item[0])
    )[1]
    title = best_candidate["title"]
    primary = str(best_candidate.get("primary_keyword") or "")
    brief = build_brief(
        source_title, source_description, source_channel, "custom", limit=3
    )
    keywords = build_keywords(
        topic, source_tags, source_description, source_channel, language
    )
    hashtags = build_hashtags(primary, keywords, language)
    description = build_description(
        topic,
        source_channel,
        brief.facts,
        chapters,
        hashtags,
        language,
        rights,
    )
    if normalize_arabic(title) == normalize_arabic(reference_title or source_title):
        warnings.append("العنوان ما زال مطابقاً للمصدر؛ أضف زاوية تحريرية.")
    if not keywords:
        warnings.append("لا توجد كلمات مفتاحية كافية؛ أضف وسوماً للمصدر.")
    if source_channel and source_channel.casefold() not in (
        title + " " + description
    ).casefold():
        warnings.append("اسم المصدر غير مذكور في البيانات.")
    return MetadataProposal(
        title=title,
        description=description,
        keywords=keywords,
        hashtags=hashtags,
        candidates=candidates,
        warnings=warnings,
        language=language,
    )


def _augment_description(
    existing: str,
    fallback: str,
    channel: str,
    chapters: Sequence[str],
    hashtags: Sequence[str],
    language: str,
    rights: str,
) -> str:
    text = str(existing or "").strip()
    if not text or len(text) < 80:
        # A five-word blurb is not a description; the structured one is better.
        return fallback
    additions: list[str] = []
    lowered = normalize_arabic(text)
    if channel and channel.casefold() not in text.casefold():
        additions.append(f"{SOURCE_LABEL.get(language, 'المصدر')}: {channel}")
    disclosure = DISCLOSURE.get(language, DISCLOSURE["ar"])
    if rights == "third_party" and normalize_arabic(disclosure) not in lowered:
        additions.append(DISCLOSURE.get(language, DISCLOSURE["ar"]))
    if chapters and "⏱" not in text:
        additions.append(
            "⏱ " + ("الفصول" if language == "ar" else "Chapters") + ":\n" + "\n".join(chapters)
        )
    if hashtags and "#" not in text:
        additions.append(" ".join(hashtags))
    if not additions:
        return text[:5000]
    return (text + "\n\n" + "\n\n".join(additions))[:5000]


def refine_metadata(
    *,
    title: str,
    description: str,
    tags: Sequence[str],
    source_title: str,
    source_description: str = "",
    source_channel: str = "",
    source_tags: Sequence[str] = (),
    language: str = "auto",
    chapters: Sequence[str] = (),
    rights: str = "unknown",
) -> MetadataProposal:
    """Keep a good generated title, repair a bad one, never invent facts."""
    proposal = propose_metadata(
        source_title=source_title,
        source_description=source_description,
        source_channel=source_channel,
        source_tags=source_tags,
        language=language,
        chapters=chapters,
        rights=rights,
    )
    cleaned_title = clean_text(title, 200)
    hard_fail = (
        len(cleaned_title) < 20
        or len(cleaned_title) > 100
        or is_clickbait(cleaned_title)
        or (
            source_title
            and normalize_arabic(cleaned_title) == normalize_arabic(source_title)
        )
    )
    if hard_fail:
        proposal.warnings.append(
            "عُدّل العنوان المولَّد تلقائياً لأنه طويل أو مثير أو منسوخ."
        )
    else:
        score, reasons = score_title(
            cleaned_title, proposal.candidates[0].get("primary_keyword", ""),
            source_title, proposal.language,
        )
        proposal.candidates.insert(
            0,
            {
                "title": cleaned_title,
                "score": score,
                "reasons": reasons,
                "primary_keyword": proposal.candidates[0].get("primary_keyword", ""),
            },
        )
        proposal.title = cleaned_title

    cleaned_tags = [
        " ".join(clean_text(tag, 40).split()).strip(" ,#")
        for tag in tags or ()
    ]
    cleaned_tags = [tag for tag in cleaned_tags if len(tag) >= 3]
    merged: list[str] = []
    seen: set[str] = set()
    for value in list(cleaned_tags) + list(proposal.keywords):
        key = keyword_key(value)
        if key and key not in seen:
            seen.add(key)
            merged.append(value)
    proposal.keywords = tuple(merged[:15])

    proposal.description = _augment_description(
        description,
        proposal.description,
        source_channel,
        chapters,
        proposal.hashtags,
        proposal.language,
        rights,
    )
    return proposal
