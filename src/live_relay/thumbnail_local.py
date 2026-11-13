"""A local, offline YouTube thumbnail composer.

The relay already produces a title, a channel name and a logo. This module
turns them into a 1280x720 broadcast cover with **correct Arabic shaping**
(Pillow + Raqm, with a bundled Noto Sans Arabic font), so a thumbnail exists
even when Gemini is disabled, offline, or out of quota.

It deliberately uses no image from the source channel: the cover is original
artwork with the channel's own brand, which is both safer for rights and
consistent with the originality work in ``monetization``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import datetime
from pathlib import Path

WIDTH = 1280
HEIGHT = 720
ARABIC_FONT = Path("fonts") / "NotoSansArabic-Bold.ttf"
LATIN_FONT = Path("fonts") / "DejaVuSans-Bold.ttf"
# A mixed line needs a font with both scripts; DejaVu covers Arabic + Latin.
FALLBACK_ARABIC = (
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
FALLBACK_LATIN = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)

# Accent palette: stable per title, but always legible on the dark card.
PALETTE = ("#35d49a", "#70a7ff", "#ffc66d", "#ff8a7a", "#b49bff")

_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]")
_UNSAFE_SEPARATORS = re.compile(r"[|—–•·]+")
_ARABIC = re.compile(r"[\u0600-\u06FF]")

_LOGO_CANDIDATES = (
    "assets/live-relay-logo.png",
    "stream-logo.png",
)


def _hex(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def _mix(first: tuple[int, int, int], second: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    ratio = max(0.0, min(1.0, ratio))
    return tuple(
        int(first[index] + (second[index] - first[index]) * ratio)
        for index in range(3)
    )


def sanitize(text: str, limit: int = 220) -> str:
    text = _EMOJI.sub(" ", str(text or ""))
    text = _UNSAFE_SEPARATORS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].strip()


def split_title(title: str) -> tuple[str, str]:
    """`topic | angle` becomes a big headline and a small label."""
    raw = str(title or "").strip()
    parts = re.split(r"\s*[|—–]\s*", raw, maxsplit=1)
    headline = sanitize(parts[0], 160)
    label = sanitize(parts[1], 60) if len(parts) > 1 else ""
    if not headline:
        headline = sanitize(raw, 160) or "بث مباشر"
    return headline, label


BADGE = {"ar": "مباشر", "en": "LIVE", "fr": "DIRECT", "es": "EN DIRECTO"}


def badge_for(language: str) -> str:
    return BADGE.get(language, BADGE["ar"])


def accent_for(text: str) -> str:
    digest = hashlib.sha256(str(text or "").encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def _font_path(script: str) -> Path | None:
    module_dir = Path(__file__).resolve().parent
    bundled = module_dir / (ARABIC_FONT if script == "arabic" else LATIN_FONT)
    if bundled.is_file():
        return bundled
    for candidate in (FALLBACK_ARABIC if script == "arabic" else FALLBACK_LATIN):
        if Path(candidate).is_file():
            return Path(candidate)
    # Last resort: the other bundled font, so something renders at least.
    other = module_dir / (LATIN_FONT if script == "arabic" else ARABIC_FONT)
    return other if other.is_file() else None


def _script_for(text: str) -> str:
    has_arabic = bool(_ARABIC.search(text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if has_arabic and not has_latin:
        return "arabic"
    return "latin"


def _load_font(size: int, script: str = "arabic"):
    from PIL import ImageFont

    path = _font_path(script)
    if path is None:
        raise RuntimeError("No usable font was found for the thumbnail")
    return ImageFont.truetype(str(path), size)


def _font_for(text: str, size: int):
    return _load_font(size, _script_for(text))


def _direction(text: str) -> str:
    return "rtl" if _ARABIC.search(text) else "ltr"


def _text_length(draw, text: str, font, direction: str) -> float:
    try:
        return draw.textlength(text, font=font, direction=direction, language="ar" if direction == "rtl" else None)
    except TypeError:
        return draw.textlength(text, font=font)


def _wrap(draw, text: str, font, max_width: int, direction: str) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _text_length(draw, candidate, font, direction) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _fit_headline(draw, text: str, max_width: int, max_height: int, direction: str):
    best = None
    for size in (94, 86, 78, 70, 62, 55, 48, 42):
        font = _font_for(text, size)
        lines = _wrap(draw, text, font, max_width, direction)
        if len(lines) > 3:
            continue
        line_height = int(size * 1.28)
        height = line_height * len(lines)
        if height <= max_height and all(
            _text_length(draw, line, font, direction) <= max_width for line in lines
        ):
            best = (font, lines, line_height, size)
            break
    if best is None:
        font = _font_for(text, 38)
        best = (font, _wrap(draw, text, font, max_width, direction)[:3], 48, 38)
    return best


def _gradient(top: tuple[int, int, int], bottom: tuple[int, int, int]):
    from PIL import Image

    image = Image.new("RGB", (WIDTH, HEIGHT), top)
    pixels = image.load()
    for y in range(HEIGHT):
        color = _mix(top, bottom, y / max(1, HEIGHT - 1))
        for x in range(WIDTH):
            pixels[x, y] = color
    return image


def _logo_path(configured: str = "") -> Path | None:
    if configured and Path(configured).is_file():
        return Path(configured)
    module_dir = Path(__file__).resolve().parent
    root = module_dir.parents[1]
    for relative in _LOGO_CANDIDATES:
        for base in (root, module_dir):
            candidate = base / relative
            if candidate.is_file():
                return candidate
    return None


def compose_thumbnail(
    *,
    title: str,
    channel: str = "",
    source_channel: str = "",
    badge: str = "",
    date_text: str = "",
    logo_file: str = "",
    language: str = "ar",
) -> bytes:
    """Render the cover and return PNG bytes.

    Raises ``RuntimeError`` when Pillow (or a usable font) is missing, so the
    caller can fall back to another provider instead of writing a broken file.
    """
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError("Pillow is not installed") from exc

    headline, label = split_title(title)
    direction = "rtl" if language == "ar" or _direction(headline) == "rtl" else "ltr"
    accent = _hex(accent_for(title))
    background = _gradient(_hex("#0a1420"), _hex("#060a0f"))
    draw = ImageDraw.Draw(background)

    # Soft accent glow on the side opposite the text.
    glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((80, 120, 720, 760), fill=(*accent, 34))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    background = Image.alpha_composite(background.convert("RGBA"), glow).convert("RGB")
    draw = ImageDraw.Draw(background)

    # Accent spine on the text side.
    spine_x = WIDTH - 58 if direction == "rtl" else 58
    draw.rounded_rectangle(
        (spine_x - 3, 64, spine_x + 3, HEIGHT - 64), radius=3, fill=accent
    )

    margin = 118
    text_right = WIDTH - margin
    text_left = margin
    text_width = WIDTH - margin * 2

    # Top row: LIVE badge + channel.
    badge_text = sanitize(badge or ("مباشر" if direction == "rtl" else "LIVE"), 18)
    badge_font = _font_for(badge_text, 30)
    badge_w = int(_text_length(draw, badge_text, badge_font, direction)) + 78
    badge_h = 52
    badge_y = 74
    if direction == "rtl":
        badge_x = text_right - badge_w
    else:
        badge_x = text_left
    draw.rounded_rectangle(
        (badge_x, badge_y, badge_x + badge_w, badge_y + badge_h),
        radius=26,
        fill=_hex("#7a1f2b"),
    )
    dot_x = badge_x + badge_w - 26 if direction == "rtl" else badge_x + 26
    draw.ellipse((dot_x - 6, badge_y + 20, dot_x + 6, badge_y + 32), fill=(255, 255, 255))
    if direction == "rtl":
        draw.text(
            (badge_x + 18, badge_y + 10),
            badge_text,
            font=badge_font,
            fill=(255, 255, 255),
            direction="rtl",
            language="ar",
        )
        channel_x = badge_x - 22
        channel_anchor = "ra"
    else:
        draw.text(
            (badge_x + 42, badge_y + 10),
            badge_text,
            font=badge_font,
            fill=(255, 255, 255),
            direction="ltr",
        )
        channel_x = badge_x + badge_w + 22
        channel_anchor = "la"
    if channel:
        channel_font = _font_for(channel, 28)
        draw.text(
            (channel_x, badge_y + 11),
            sanitize(channel, 40),
            font=channel_font,
            fill=_hex("#9fb2c2"),
            anchor=channel_anchor,
            direction=direction,
            language="ar" if direction == "rtl" else None,
        )

    # Headline: leave room for the label and the footer.
    max_headline_height = 330 if label else 390
    headline_font, lines, line_height, size = _fit_headline(
        draw, headline, text_width, max_headline_height, direction
    )
    block_height = len(lines) * line_height
    headline_top = 176
    if not label:
        # Centre a short headline instead of leaving the lower half empty.
        headline_top = 176 + max(0, (340 - block_height) // 2)
    for index, line in enumerate(lines):
        y = headline_top + index * line_height
        x = text_right if direction == "rtl" else text_left
        anchor = "ra" if direction == "rtl" else "la"
        draw.text(
            (x, y),
            line,
            font=headline_font,
            fill=(244, 248, 251),
            anchor=anchor,
            direction=direction,
            language="ar" if direction == "rtl" else None,
        )
    label_top = headline_top + len(lines) * line_height + 14
    if label:
        label_font = _font_for(label, 40)
        x = text_right if direction == "rtl" else text_left
        anchor = "ra" if direction == "rtl" else "la"
        draw.text(
            (x, label_top),
            label,
            font=label_font,
            fill=accent,
            anchor=anchor,
            direction=direction,
            language="ar" if direction == "rtl" else None,
        )

    # Footer: source and date on the text side, so the logo never collides.
    footer_x = text_right if direction == "rtl" else text_left
    footer_anchor = "ra" if direction == "rtl" else "la"
    source_text = ""
    if source_channel and source_channel.casefold() != (channel or "").casefold():
        source_text = (
            ("المصدر: " if direction == "rtl" else "Source: ")
            + sanitize(source_channel, 40)
        )
    if source_text:
        source_font = _font_for(source_text, 26)
        draw.text(
            (footer_x, HEIGHT - 108),
            source_text,
            font=source_font,
            fill=_hex("#9fb2c2"),
            anchor=footer_anchor,
            direction=direction,
            language="ar" if direction == "rtl" else None,
        )
    if date_text:
        date_value = sanitize(date_text, 30)
        date_font = _font_for(date_value, 24)
        draw.text(
            (footer_x, HEIGHT - 70),
            date_value,
            font=date_font,
            fill=_hex("#6f8496"),
            anchor=footer_anchor,
            direction=direction,
            language="ar" if direction == "rtl" else None,
        )

    # Logo: bottom on the opposite side.
    logo = _logo_path(logo_file)
    if logo is not None:
        try:
            mark = Image.open(logo).convert("RGB")
            mark = mark.resize((128, 128), Image.LANCZOS)
            # Rounded mask keeps the square logo from looking pasted on.
            mask = Image.new("L", (128, 128), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, 127, 127), radius=26, fill=255)
            logo_x = text_left if direction == "rtl" else text_right - 128
            logo_y = HEIGHT - 128 - 64
            background.paste(mark, (logo_x, logo_y), mask)
            draw.rounded_rectangle(
                (logo_x, logo_y, logo_x + 127, logo_y + 127),
                radius=26,
                outline=(*accent, 255),
                width=2,
            )
        except (OSError, ValueError) as exc:
            logging.warning("Could not draw the thumbnail logo: %s", exc)

    draw.rectangle((0, HEIGHT - 6, WIDTH, HEIGHT), fill=accent)

    output = io.BytesIO()
    background.save(output, format="PNG", optimize=True)
    data = output.getvalue()
    if not data.startswith(b"\x89PNG"):
        raise RuntimeError("The thumbnail renderer produced invalid data")
    logging.info("Local thumbnail rendered (%s bytes, %dx%d)", len(data), WIDTH, HEIGHT)
    return data


def default_date(language: str = "ar") -> str:
    today = datetime.now()
    if language == "ar":
        months = (
            "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
            "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
        )
        return f"{today.day} {months[today.month - 1]} {today.year}"
    return today.strftime("%d %b %Y")
