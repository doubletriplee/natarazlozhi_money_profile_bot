from __future__ import annotations

import re
import secrets
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import Color, HexColor
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
CTA_TITLE = "Хочешь полный разбор?"
TOTAL_PAGES = len(PAGE_TITLES) + 1

SITE_URL = "https://natarazlozhi.ru/"
INSTAGRAM_URL = "https://www.instagram.com/it_natali/"
TELEGRAM_URL = "https://t.me/natarazlozhi"
PERSONAL_CONTACT_URL = "https://t.me/simnatali"

BACKGROUND = HexColor("#090604")
SURFACE = HexColor("#15100C")
SURFACE_ALT = HexColor("#1C130E")
CREAM = HexColor("#F6E5D2")
MUTED = HexColor("#BAA38E")
GOLD = HexColor("#C78A4A")
BORDER = HexColor("#5B3924")


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


def _balanced_paragraphs(value: str) -> tuple[str, ...]:
    explicit = tuple(part.strip() for part in re.split(r"\n\s*\n", value) if part.strip())
    if len(explicit) > 1:
        return explicit

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", value) if part.strip()]
    if len(sentences) < 4:
        return (value.strip(),)

    target = max(1, round(sum(len(sentence) for sentence in sentences) / 3))
    groups: list[str] = []
    current: list[str] = []
    current_length = 0
    for sentence in sentences:
        if current and current_length >= target and len(groups) < 2:
            groups.append(" ".join(current))
            current = []
            current_length = 0
        current.append(sentence)
        current_length += len(sentence)
    if current:
        groups.append(" ".join(current))
    return tuple(groups)


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
        document.setSubject("Персональный разбор денежного аватара")
        document.setKeywords("астрология, денежный аватар, Наталья Симоненко")

        try:
            for index, (title, raw_block) in enumerate(
                zip(PAGE_TITLES, result.messages, strict=True), start=1
            ):
                self._draw_content_page(
                    document,
                    hero=hero if index == 1 else None,
                    name=name,
                    avatar_name=avatar_name,
                    title=title,
                    body=_clean_block(raw_block),
                    page=index,
                )
                document.showPage()
            self._draw_cta_page(document)
            document.showPage()
            document.save()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _draw_base(self, document: canvas.Canvas, *, name: str, page: int) -> None:
        document.setFillColor(BACKGROUND)
        document.rect(0, 0, self.WIDTH, self.HEIGHT, stroke=0, fill=1)

        document.setStrokeColor(BORDER)
        document.setLineWidth(0.7)
        document.roundRect(24, 24, self.WIDTH - 48, self.HEIGHT - 48, 18, stroke=1, fill=0)

        self._draw_orbit_texture(document, page)

        document.setFillColor(MUTED)
        document.setFont("AvatarSans", 8.3)
        document.drawString(42, self.HEIGHT - 46, name.upper())
        document.drawRightString(
            self.WIDTH - 42,
            self.HEIGHT - 46,
            f"ДЕНЕЖНЫЙ АВАТАР  {page:02d}/{TOTAL_PAGES:02d}",
        )
        self._draw_footer(document)

    def _draw_orbit_texture(self, document: canvas.Canvas, page: int) -> None:
        document.saveState()
        document.setStrokeColor(Color(0.45, 0.25, 0.12, alpha=0.22))
        document.setLineWidth(0.45)
        center_x = self.WIDTH - 42 - (page % 3) * 12
        center_y = self.HEIGHT - 138 - (page % 2) * 22
        for radius in (30, 53, 78):
            document.circle(center_x, center_y, radius, stroke=1, fill=0)
        document.setFillColor(Color(0.78, 0.49, 0.24, alpha=0.35))
        document.circle(center_x - 31, center_y + 42, 2.2, stroke=0, fill=1)
        document.circle(center_x + 46, center_y - 26, 1.6, stroke=0, fill=1)
        document.restoreState()

    def _draw_content_page(
        self,
        document: canvas.Canvas,
        *,
        hero: ImageReader | None,
        name: str,
        avatar_name: str,
        title: str,
        body: str,
        page: int,
    ) -> None:
        self._draw_base(document, name=name, page=page)

        if hero is not None:
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
            document.setStrokeColor(BORDER)
            document.roundRect(image_x, image_y, image_width, image_height, 12, stroke=1, fill=0)
            label_y = image_y - 32
            title_y = label_y - 40
            content_top = title_y - 30
        else:
            label_y = self.HEIGHT - 108
            title_y = label_y - 52
            content_top = title_y - 48

        document.setFillColor(GOLD)
        document.setFont("AvatarSans", 9)
        document.drawString(46, label_y, avatar_name.upper())
        self._draw_tracked_title(document, title, x=44, y=title_y, font_size=22)

        document.setStrokeColor(GOLD)
        document.setLineWidth(1)
        document.line(44, content_top + 15, self.WIDTH - 44, content_top + 15)

        self._draw_body_sections(
            document,
            body=body,
            top=content_top,
            bottom=82,
            page=page,
        )

    def _draw_tracked_title(
        self,
        document: canvas.Canvas,
        value: str,
        *,
        x: float,
        y: float,
        font_size: float,
    ) -> None:
        document.setFillColor(CREAM)
        text = document.beginText(x, y)
        text.setFont("AvatarSansBold", font_size)
        text.setCharSpace(-font_size * 0.025)
        text.textLine(value)
        document.drawText(text)

    def _draw_body_sections(
        self,
        document: canvas.Canvas,
        *,
        body: str,
        top: float,
        bottom: float,
        page: int,
    ) -> None:
        sections = _balanced_paragraphs(body)
        available_height = top - bottom
        card_gap = 10
        card_width = self.WIDTH - 100
        text_width = card_width - 54

        paragraphs: list[Paragraph] = []
        heights: list[float] = []
        for font_size in (11.4, 11.0, 10.6, 10.2, 9.8):
            style = ParagraphStyle(
                name=f"AvatarBody{page}",
                fontName="AvatarSans",
                fontSize=font_size,
                leading=font_size * 1.5,
                textColor=CREAM,
                alignment=TA_LEFT,
                spaceAfter=0,
            )
            paragraphs = [Paragraph(escape(section), style) for section in sections]
            heights = [paragraph.wrap(text_width, available_height)[1] for paragraph in paragraphs]
            total_height = sum(height + 24 for height in heights) + card_gap * (len(heights) - 1)
            if total_height <= available_height:
                break
        else:
            raise ValueError(f"PDF block {page} does not fit on its page")

        y = top
        for index, (paragraph, paragraph_height) in enumerate(
            zip(paragraphs, heights, strict=True), start=1
        ):
            card_height = paragraph_height + 24
            card_y = y - card_height
            document.setFillColor(SURFACE if index % 2 else SURFACE_ALT)
            document.setStrokeColor(BORDER)
            document.roundRect(50, card_y, card_width, card_height, 13, stroke=1, fill=1)

            document.setFillColor(GOLD)
            document.circle(66, y - 18, 8, stroke=0, fill=1)
            document.setFillColor(BACKGROUND)
            document.setFont("AvatarSansBold", 7.5)
            document.drawCentredString(66, y - 20.6, str(index))

            paragraph.drawOn(document, 78, card_y + 12)
            y = card_y - card_gap

    def _draw_footer(self, document: canvas.Canvas) -> None:
        footer_y = 47
        document.setStrokeColor(BORDER)
        document.setLineWidth(0.6)
        document.line(42, 69, self.WIDTH - 42, 69)

        document.setFillColor(MUTED)
        document.setFont("AvatarSansBold", 7.2)
        document.drawString(42, footer_y, "Астрология и Таро")

        document.setFont("AvatarSans", 7.2)
        site_label = "natarazlozhi.ru"
        site_width = pdfmetrics.stringWidth(site_label, "AvatarSans", 7.2)
        site_x = (self.WIDTH - site_width) / 2
        document.setFillColor(CREAM)
        document.drawString(site_x, footer_y, site_label)
        document.linkURL(SITE_URL, (site_x, footer_y - 2, site_x + site_width, footer_y + 8))

        instagram_x = 348
        self._draw_instagram_icon(document, instagram_x, footer_y - 1)
        document.setFillColor(CREAM)
        document.setFont("AvatarSans", 6.9)
        instagram_label = "@it_natali"
        document.drawString(instagram_x + 13, footer_y, instagram_label)
        instagram_width = pdfmetrics.stringWidth(instagram_label, "AvatarSans", 6.9)
        document.linkURL(
            INSTAGRAM_URL,
            (instagram_x, footer_y - 3, instagram_x + 13 + instagram_width, footer_y + 9),
        )

        telegram_x = 425
        self._draw_telegram_icon(document, telegram_x, footer_y - 1)
        document.setFillColor(CREAM)
        telegram_label = "@natarazlozhi"
        document.drawString(telegram_x + 13, footer_y, telegram_label)
        telegram_width = pdfmetrics.stringWidth(telegram_label, "AvatarSans", 6.9)
        document.linkURL(
            TELEGRAM_URL,
            (telegram_x, footer_y - 3, telegram_x + 13 + telegram_width, footer_y + 9),
        )

    def _draw_instagram_icon(self, document: canvas.Canvas, x: float, y: float) -> None:
        document.saveState()
        document.setStrokeColor(HexColor("#E96A8D"))
        document.setLineWidth(1.05)
        document.roundRect(x, y, 9, 9, 2.5, stroke=1, fill=0)
        document.circle(x + 4.5, y + 4.5, 2.1, stroke=1, fill=0)
        document.setFillColor(HexColor("#E96A8D"))
        document.circle(x + 7.1, y + 7.0, 0.65, stroke=0, fill=1)
        document.restoreState()

    def _draw_telegram_icon(self, document: canvas.Canvas, x: float, y: float) -> None:
        document.saveState()
        document.setFillColor(HexColor("#2AABEE"))
        document.circle(x + 4.5, y + 4.5, 4.8, stroke=0, fill=1)
        document.setFillColor(HexColor("#FFFFFF"))
        plane = document.beginPath()
        plane.moveTo(x + 1.8, y + 4.7)
        plane.lineTo(x + 7.7, y + 7.3)
        plane.lineTo(x + 6.1, y + 1.6)
        plane.lineTo(x + 4.5, y + 3.6)
        plane.lineTo(x + 3.3, y + 2.9)
        plane.close()
        document.drawPath(plane, stroke=0, fill=1)
        document.restoreState()

    def _draw_cta_page(self, document: canvas.Canvas) -> None:
        document.setFillColor(BACKGROUND)
        document.rect(0, 0, self.WIDTH, self.HEIGHT, stroke=0, fill=1)
        document.setStrokeColor(BORDER)
        document.setLineWidth(0.7)
        document.roundRect(24, 24, self.WIDTH - 48, self.HEIGHT - 48, 18, stroke=1, fill=0)

        title_size = 29
        title_width = pdfmetrics.stringWidth(CTA_TITLE, "AvatarSansBold", title_size)
        title = document.beginText((self.WIDTH - title_width) / 2, self.HEIGHT / 2 + 70)
        title.setFont("AvatarSansBold", title_size)
        title.setCharSpace(-title_size * 0.025)
        title.setFillColor(CREAM)
        title.textLine(CTA_TITLE)
        document.drawText(title)

        document.setStrokeColor(GOLD)
        document.setLineWidth(1)
        document.line(
            self.WIDTH / 2 - 54, self.HEIGHT / 2 + 38, self.WIDTH / 2 + 54, self.HEIGHT / 2 + 38
        )

        instruction = "Пиши «Хочу денежный разбор»"
        document.setFillColor(MUTED)
        document.setFont("AvatarSans", 14)
        document.drawCentredString(self.WIDTH / 2, self.HEIGHT / 2 - 4, instruction)

        contact = "@simnatali"
        contact_size = 18
        contact_width = pdfmetrics.stringWidth(contact, "AvatarSansBold", contact_size)
        pill_width = contact_width + 52
        pill_x = (self.WIDTH - pill_width) / 2
        pill_y = self.HEIGHT / 2 - 86
        document.setFillColor(SURFACE_ALT)
        document.setStrokeColor(GOLD)
        document.roundRect(pill_x, pill_y, pill_width, 48, 16, stroke=1, fill=1)
        document.setFillColor(CREAM)
        document.setFont("AvatarSansBold", contact_size)
        document.drawCentredString(self.WIDTH / 2, pill_y + 15, contact)
        document.linkURL(
            PERSONAL_CONTACT_URL,
            (pill_x, pill_y, pill_x + pill_width, pill_y + 48),
        )
