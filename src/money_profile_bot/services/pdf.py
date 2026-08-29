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
            self._draw_cta_page(document, name=name)
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

    def _draw_cta_page(self, document: canvas.Canvas, *, name: str) -> None:
        self._draw_base(document, name=name, page=TOTAL_PAGES)

        left_x = 48
        left_width = 252
        right_x = 322
        right_width = self.WIDTH - right_x - 42

        document.setFillColor(GOLD)
        document.setFont("AvatarSansBold", 9)
        document.drawString(left_x, self.HEIGHT - 118, "ИНДИВИДУАЛЬНЫЙ РАЗБОР")

        title = document.beginText(left_x, self.HEIGHT - 196)
        title.setFont("AvatarSansBold", 31)
        title.setLeading(39)
        title.setCharSpace(-0.78)
        title.setFillColor(CREAM)
        title.textLine("Хочешь полный")
        title.textLine("разбор?")
        document.drawText(title)

        intro_style = ParagraphStyle(
            name="CtaIntro",
            fontName="AvatarSans",
            fontSize=11.2,
            leading=16.8,
            textColor=CREAM,
            alignment=TA_LEFT,
        )
        intro = Paragraph(
            "В этом PDF мы разобрали денежный аватар. Полный разбор по натальной карте "
            "покажет, как именно Венера проявляется в твоих ресурсах, работе и продажах.",
            intro_style,
        )
        _, intro_height = intro.wrap(left_width, 140)
        intro.drawOn(document, left_x, self.HEIGHT - 305 - intro_height)

        bullets = (
            "знак, дом и аспекты Венеры",
            "ресурсы и подходящие направления",
            "ограничения и персональный план роста",
        )
        bullet_y = self.HEIGHT - 386
        for bullet in bullets:
            document.setFillColor(GOLD)
            document.circle(left_x + 4, bullet_y + 4, 2.4, stroke=0, fill=1)
            paragraph = Paragraph(escape(bullet), intro_style)
            _, height = paragraph.wrap(left_width - 20, 50)
            paragraph.drawOn(document, left_x + 16, bullet_y - height + 9)
            bullet_y -= max(36, height + 12)

        button_y = 164
        document.setFillColor(GOLD)
        document.roundRect(left_x, button_y, left_width, 56, 16, stroke=0, fill=1)
        document.setFillColor(BACKGROUND)
        document.setFont("AvatarSansBold", 9.4)
        document.drawCentredString(
            left_x + left_width / 2,
            button_y + 34,
            "ПОЛУЧИТЬ ПОЛНЫЙ РАЗБОР",
        )
        document.setFont("AvatarSansBold", 11.5)
        document.drawCentredString(left_x + left_width / 2, button_y + 16, "1 990 ₽")
        document.linkURL(SITE_URL, (left_x, button_y, left_x + left_width, button_y + 56))

        document.setFillColor(MUTED)
        document.setFont("AvatarSans", 8.2)
        document.drawString(left_x, button_y - 24, "Подробнее на natarazlozhi.ru")
        detail_width = pdfmetrics.stringWidth("Подробнее на natarazlozhi.ru", "AvatarSans", 8.2)
        document.linkURL(
            SITE_URL,
            (left_x, button_y - 27, left_x + detail_width, button_y - 14),
        )

        self._draw_cta_art(document, x=right_x, y=92, width=right_width, height=632)

    def _draw_cta_art(
        self,
        document: canvas.Canvas,
        *,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> None:
        document.setFillColor(HexColor("#1B0E0E"))
        document.setStrokeColor(BORDER)
        document.roundRect(x, y, width, height, 18, stroke=1, fill=1)

        center_x = x + width / 2
        planet_y = y + height * 0.46
        for radius, color in (
            (88, "#3A1E15"),
            (80, "#633621"),
            (69, "#945B37"),
            (57, "#C98B5B"),
            (44, "#E1B58C"),
            (29, "#F3D8BD"),
        ):
            document.setFillColor(HexColor(color))
            document.circle(
                center_x - (88 - radius) * 0.18,
                planet_y + (88 - radius) * 0.16,
                radius,
                stroke=0,
                fill=1,
            )

        document.setStrokeColor(Color(0.94, 0.78, 0.58, alpha=0.32))
        document.setLineWidth(0.6)
        for offset in (-30, -12, 10, 33):
            document.ellipse(
                center_x - 71,
                planet_y + offset - 9,
                center_x + 71,
                planet_y + offset + 9,
                stroke=1,
                fill=0,
            )

        document.setStrokeColor(Color(0.78, 0.48, 0.24, alpha=0.42))
        for radius in (106, 126):
            document.circle(center_x, planet_y, radius, stroke=1, fill=0)

        line_top = y + height - 54
        line_bottom = y + 54
        document.setStrokeColor(GOLD)
        document.setLineWidth(0.8)
        document.line(center_x, line_bottom, center_x, line_top)
        points = (line_bottom + 34, planet_y - 110, planet_y + 112, line_top - 34)
        for index, cy in enumerate(points):
            color = ("#C55A42", "#D5A148", "#8DBE90", "#BD75BF")[index]
            document.setFillColor(HexColor(color))
            document.circle(center_x, cy, 7, stroke=0, fill=1)
            document.setStrokeColor(CREAM)
            document.circle(center_x, cy, 12, stroke=1, fill=0)
