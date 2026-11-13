"""A weekly operating report built from the local session log.

No API calls, no new scopes: it answers the questions an operator actually
asks on Monday — how much did we broadcast, how much of it was ours, how many
sessions looked ad-ineligible, and what should change next week.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from .monetization import read_jsonl, session_path
from .newsroom import write_markdown

BAND_LABELS = {
    "ready": "متوافق مبدئياً",
    "conditional": "يحتاج تحسيناً",
    "at_risk": "معرّض للإعلانات المحدودة",
    "blocked": "غير مؤهل على الأرجح",
}


def _duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}س {minutes:02d}د"
    return f"{minutes}د"


def _local_date(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return "—"


def _week_label(now: float) -> str:
    moment = datetime.fromtimestamp(now)
    monday = moment - timedelta(days=moment.weekday())
    sunday = monday + timedelta(days=6)
    return f"{monday:%Y-%m-%d} → {sunday:%Y-%m-%d}"


def summarize_sessions(
    records: Sequence[Mapping[str, Any]], days: int = 7, now: float | None = None
) -> dict[str, Any]:
    now = float(now if now is not None else time.time())
    cutoff = now - max(1, int(days)) * 86400
    selected = [
        dict(item)
        for item in records
        if isinstance(item, Mapping) and float(item.get("timestamp") or 0) >= cutoff
    ]
    total_seconds = sum(int(item.get("duration_seconds") or 0) for item in selected)
    scores = [int(item.get("score") or 0) for item in selected]
    segments = [int(item.get("editorial_segments") or 0) for item in selected]
    bands = Counter(str(item.get("band") or "unknown") for item in selected)
    risks = Counter(
        str(code) for item in selected for code in (item.get("risk_codes") or [])
    )
    hours = total_seconds / 3600
    per_hour = (sum(segments) / hours) if hours > 0 else 0.0
    return {
        "count": len(selected),
        "total_seconds": total_seconds,
        "average_score": round(sum(scores) / len(scores)) if scores else None,
        "bands": dict(bands),
        "risks": dict(risks.most_common(8)),
        "segments": sum(segments),
        "segments_per_hour": round(per_hour, 2),
        "sessions_without_segments": sum(1 for value in segments if value == 0),
        "records": selected,
        "cutoff": cutoff,
        "now": now,
    }


def recommendations(summary: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    if not summary.get("count"):
        return [
            "لا توجد جلسات مسجّلة في هذه الفترة. سجّل جلسة كاملة أولاً حتى يصبح التقرير مفيداً."
        ]
    if summary.get("bands", {}).get("blocked"):
        result.append(
            f"{summary['bands']['blocked']} جلسة مصنّفة «غير مؤهلة على الأرجح»: "
            "راجع حقوق المصدر ومستوى التحويل قبل زيادة ساعات البث."
        )
    if summary.get("segments_per_hour", 0) < 1:
        result.append(
            "متوسط المقاطع الأصلية أقل من واحد لكل ساعة بث. "
            "استهدف مقطعاً كل 20–30 دقيقة (ملخص، سياق، أو تحليل)."
        )
    if summary.get("sessions_without_segments"):
        result.append(
            f"{summary['sessions_without_segments']} جلسة بلا أي مقطع أصلي. "
            "هذه هي الجلسات الأقرب لتصنيف «محتوى معاد استخدامه»."
        )
    if summary.get("average_score") is not None and summary["average_score"] < 60:
        result.append(
            "متوسط التقييم أقل من 60/100؛ ابدأ بعائق واحد فقط هذا الأسبوع "
            "بدل تغيير كل شيء."
        )
    if not result:
        result.append(
            "الاتجاه سليم: استمر في النمط الحالي، وراقب Studio بعد كل تغيير."
        )
    return result


def build_report(
    records: Sequence[Mapping[str, Any]], days: int = 7, now: float | None = None
) -> str:
    summary = summarize_sessions(records, days, now)
    now = float(summary["now"])
    lines = [
        f"# تقرير التشغيل — آخر {days} أيام",
        "",
        f"- **الفترة:** {_week_label(now)}",
        f"- **عدد الجلسات:** {summary['count']}",
        f"- **إجمالي وقت البث:** {_duration(summary['total_seconds'])}",
        f"- **متوسط التقييم:** "
        f"{summary['average_score'] if summary['average_score'] is not None else '—'}/100",
        f"- **المقاطع الأصلية:** {summary['segments']} "
        f"(~{summary['segments_per_hour']} لكل ساعة)",
        "",
        "## التوزيع",
    ]
    if summary["bands"]:
        for band, count in sorted(summary["bands"].items(), key=lambda item: -item[1]):
            lines.append(f"- {BAND_LABELS.get(band, band)}: {count}")
    else:
        lines.append("- لا بيانات.")
    lines.extend(["", "## أكثر أسباب الخطر"])
    if summary["risks"]:
        for code, count in summary["risks"].items():
            lines.append(f"- `{code}`: {count}")
    else:
        lines.append("- لا أسباب مسجّلة.")
    lines.extend(["", "## الجلسات"])
    if summary["records"]:
        lines.append("| التاريخ | المدة | التقييم | المقاطع | الفيديو |")
        lines.append("|---|---|---|---|---|")
        for item in summary["records"][-15:][::-1]:
            lines.append(
                "| {date} | {duration} | {score}/100 ({band}) | {segments} | `{video}` |".format(
                    date=_local_date(item.get("ended_at") or item.get("timestamp") or 0),
                    duration=_duration(int(item.get("duration_seconds") or 0)),
                    score=item.get("score", "—"),
                    band=BAND_LABELS.get(str(item.get("band")), item.get("band") or "—"),
                    segments=item.get("editorial_segments", 0),
                    video=str(item.get("video_id") or "—"),
                )
            )
    else:
        lines.append("لا جلسات.")
    lines.extend(["", "## التوصيات"])
    lines.extend(f"- {item}" for item in recommendations(summary))
    lines.extend(
        [
            "",
            "---",
            "> تقرير محلي من سجل الجلسات. لا يتصل بـ YouTube ولا يعرض حالة الأهلية الرسمية؛ "
            "تلك في Studio فقط.",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def report_period(days: int = 7, now: float | None = None) -> tuple[float, float]:
    now = float(now if now is not None else time.time())
    return now - max(1, int(days)) * 86400, now


def write_weekly_report(
    state_dir: Path, days: int = 7, now: float | None = None
) -> Path:
    records = read_jsonl(session_path(state_dir), limit=500)
    content = build_report(records, days=days, now=now)
    moment = datetime.fromtimestamp(float(now if now is not None else time.time()))
    name = f"weekly-{moment:%Y-W%V}"
    return write_markdown(Path(state_dir), "reports", name, content)
