from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _font_candidates(kind: str) -> list[Path]:
    custom = os.getenv("CARD_SERIF_FONT" if kind == "serif" else "CARD_SANS_FONT")
    values = [Path(custom)] if custom else []
    if kind == "serif":
        values.extend(
            Path(item)
            for item in (
                "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
                "C:/Windows/Fonts/liberationserif-regular.ttf",
                "C:/Windows/Fonts/times.ttf",
            )
        )
    else:
        values.extend(
            Path(item)
            for item in (
                "/usr/share/fonts/truetype/inter/Inter-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "C:/Windows/Fonts/InterVariable.ttf",
                "C:/Windows/Fonts/arial.ttf",
            )
        )
    return values


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for path in _font_candidates(kind):
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    raise FileNotFoundError(f"no {kind} font found; configure CARD_{kind.upper()}_FONT")


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    kind: str,
    max_size: int,
    min_size: int,
    width: int,
    max_lines: int,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    for size in range(max_size, min_size - 1, -2):
        font = _font(kind, size)
        lines = _wrap(draw, text, font, width)
        if len(lines) <= max_lines:
            return font, lines
    font = _font(kind, min_size)
    return font, _wrap(draw, text, font, width)[:max_lines]


def _draw_tracking(
    draw: ImageDraw.ImageDraw,
    position: tuple[float, float],
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    fill: str,
    tracking: float = -0.025,
) -> None:
    x, y = position
    spacing = font.size * tracking
    for character in text:
        draw.text((x, y), character, font=font, fill=fill)
        x += draw.textlength(character, font=font) + spacing


class CardRenderer:
    WIDTH = 1080
    HEIGHT = 1350

    def __init__(self, bot_username: str) -> None:
        self.bot_username = bot_username.removeprefix("@")

    def render(self, *, name: str, money_type: str, strength: str, destination: Path) -> Path:
        image = Image.new("RGB", (self.WIDTH, self.HEIGHT), "#000000")
        draw = ImageDraw.Draw(image)
        amber = "#D99A48"
        white = "#FFFFFF"
        muted = "#A3A3A3"
        border = "#333333"
        surface = "#050505"

        draw.rounded_rectangle(
            (48, 48, 1032, 1302), radius=48, fill=surface, outline=border, width=2
        )
        label_font = _font("sans", 24)
        name_font, name_lines = _fit_wrapped(
            draw, name, kind="sans", max_size=38, min_size=28, width=824, max_lines=2
        )
        title_font, title_lines = _fit_wrapped(
            draw,
            money_type,
            kind="serif",
            max_size=108,
            min_size=58,
            width=824,
            max_lines=3,
        )
        body_font, strength_lines = _fit_wrapped(
            draw,
            strength,
            kind="sans",
            max_size=39,
            min_size=30,
            width=760,
            max_lines=4,
        )

        draw.text((128, 128), "ДЕНЕЖНЫЙ ПРОФИЛЬ", font=label_font, fill=amber)
        y = 190
        for line in name_lines:
            draw.text((128, y), line, font=name_font, fill=muted)
            y += int(name_font.size * 1.5)

        y = max(y + 84, 340)
        draw.text((128, y), "ВАШ ДЕНЕЖНЫЙ ТИП", font=label_font, fill=muted)
        y += 52
        for line in title_lines:
            _draw_tracking(draw, (124, y), line, font=title_font, fill=white)
            y += int(title_font.size * 1.02)

        strength_top = max(y + 72, 790)
        draw.rounded_rectangle(
            (104, strength_top, 976, 1138), radius=24, fill="#090909", outline=border, width=2
        )
        draw.text((152, strength_top + 54), "СИЛЬНАЯ СТОРОНА", font=label_font, fill=amber)
        text_y = strength_top + 108
        for line in strength_lines:
            draw.text((152, text_y), line, font=body_font, fill=white)
            text_y += int(body_font.size * 1.45)

        handle = f"@{self.bot_username}"
        handle_box = draw.textbbox((0, 0), handle, font=label_font)
        draw.text(
            (540 - (handle_box[2] - handle_box[0]) / 2, 1222), handle, font=label_font, fill=muted
        )

        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, format="PNG", optimize=True)
        return destination
