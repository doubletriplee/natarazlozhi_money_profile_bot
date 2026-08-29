from __future__ import annotations

import re
import secrets
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

from money_profile_bot.domain import GeneratedProfile
from money_profile_bot.services.avatar import AvatarAssets, display_avatar_name

PAGE_TITLES = (
    "Твой денежный аватар",
    "Твоя сильная сторона",
    "Подходящий формат работы",
    "Как проявляться и продавать",
    "Денежная ловушка",
    "Эксперимент на 7 дней",
)


def _font_path(*candidates: str) -> Path:
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    raise FileNotFoundError("a Cyrillic TrueType font is required for PDF rendering")


def _register_fonts() -> None:
    if "AvatarSans" in pdfmetrics.getRegisteredFontNames():
        return
    regular = _font_path(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
    )
    bold = _font_path(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    )
    pdfmetrics.registerFont(TTFont("AvatarSans", regular))
    pdfmetrics.registerFont(TTFont("AvatarSansBold", bold))


def _clean_block(value: str) -> str:
    value = re.sub(r"^\d/6\.\s*", "", value.strip())
    return value.replace("—", "-").replace("–", "-").replace("‑", "-")


class PdfRenderer:
    WIDTH, HEIGHT = A4

    def __init__(self, avatars: AvatarAssets) -> None:
        self.avatars = avatars
        _register_fonts()

    def render(
        self,
        *,
        name: str,
        result: GeneratedProfile,
        destination: Path,
    ) -> Path:
        if len(result.messages) != len(PAGE_TITLES):
            raise ValueError("the paid profile must contain exactly six blocks")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
        hero = ImageReader(self.avatars.free_image(result.money_type))
        avatar_name = display_avatar_name(result.money_type)
        document = canvas.Canvas(str(temporary), pagesize=A4, pageCompression=1)
        document.setTitle(f"Денежный потенциал - {avatar_name}")
        document.setAuthor("Наталья Симоненко")

        try:
            for index, (title, raw_block) in enumerate(
                zip(PAGE_TITLES, result.messages, strict=True), start=1
            ):
                self._draw_page(
                    document,
                    hero=hero,
                    name=name,
                    avatar_name=avatar_name,
                    title=title,
                    body=_clean_block(raw_block),
                    page=index,
                    total=len(PAGE_TITLES),
                )
                document.showPage()
            document.save()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _draw_page(
        self,
        document: canvas.Canvas,
        *,
        hero: ImageReader,
        name: str,
        avatar_name: str,
        title: str,
        body: str,
        page: int,
        total: int,
    ) -> None:
        cream = HexColor("#F3DFCB")
        muted = HexColor("#B8A28F")
        gold = HexColor("#B8793E")
        border = HexColor("#5B3823")
        surface = HexColor("#100B08")

        document.setFillColor(HexColor("#080503"))
        document.rect(0, 0, self.WIDTH, self.HEIGHT, stroke=0, fill=1)
        document.setStrokeColor(border)
        document.setLineWidth(0.7)
        document.roundRect(24, 24, self.WIDTH - 48, self.HEIGHT - 48, 18, stroke=1, fill=0)

        document.setFillColor(muted)
        document.setFont("AvatarSans", 8.5)
        document.drawString(42, self.HEIGHT - 46, name.upper())
        document.drawRightString(
            self.WIDTH - 42, self.HEIGHT - 46, f"ДЕНЕЖНЫЙ ПОТЕНЦИАЛ  {page:02d}/{total:02d}"
        )

        image_x = 42
        image_width = self.WIDTH - 84
        image_height = image_width / 2.5
        image_y = self.HEIGHT - 72 - image_height
        document.drawImage(
            hero,
            image_x,
            image_y,
            width=image_width,
            height=image_height,
            preserveAspectRatio=True,
            mask="auto",
        )
        document.setStrokeColor(border)
        document.roundRect(image_x, image_y, image_width, image_height, 12, stroke=1, fill=0)

        label_y = image_y - 35
        document.setFillColor(gold)
        document.setFont("AvatarSans", 9)
        document.drawString(46, label_y, avatar_name.upper())

        title_y = label_y - 42
        document.setFillColor(cream)
        document.setFont("AvatarSansBold", 21)
        document.drawString(44, title_y, title)
        document.setStrokeColor(gold)
        document.setLineWidth(1)
        document.line(44, title_y - 16, self.WIDTH - 44, title_y - 16)

        panel_top = title_y - 38
        panel_bottom = 76
        panel_height = panel_top - panel_bottom
        document.setFillColor(surface)
        document.setStrokeColor(border)
        document.roundRect(42, panel_bottom, self.WIDTH - 84, panel_height, 14, stroke=1, fill=1)

        paragraph_width = self.WIDTH - 124
        paragraph_height = panel_height - 42
        paragraph: Paragraph | None = None
        for font_size in (12.2, 11.7, 11.2, 10.7):
            style = ParagraphStyle(
                name="AvatarBody",
                fontName="AvatarSans",
                fontSize=font_size,
                leading=font_size * 1.52,
                textColor=cream,
                alignment=TA_LEFT,
                spaceAfter=0,
            )
            candidate = Paragraph(escape(body), style)
            _, height = candidate.wrap(paragraph_width, paragraph_height)
            paragraph = candidate
            if height <= paragraph_height:
                break
        if paragraph is None:
            raise RuntimeError("failed to lay out PDF paragraph")
        _, paragraph_actual_height = paragraph.wrap(paragraph_width, paragraph_height)
        if paragraph_actual_height > paragraph_height:
            raise ValueError(f"PDF block {page} does not fit on its page")
        paragraph.drawOn(
            document,
            62,
            panel_top - 22 - paragraph_actual_height,
        )

        document.setFillColor(muted)
        document.setFont("AvatarSans", 7.5)
        document.drawString(
            44,
            47,
            "Астрологическая интерпретация для самонаблюдения. Не финансовая рекомендация.",
        )
        document.setFillColor(gold)
        document.circle(self.WIDTH - 47, 49, 2.2, stroke=0, fill=1)
