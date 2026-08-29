from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from money_profile_bot.domain import BirthData
from money_profile_bot.services.astro import calculate_chart
from money_profile_bot.services.avatar import AVATAR_SLUGS, AvatarAssets, display_avatar_name
from money_profile_bot.services.pdf import (
    INSTAGRAM_URL,
    PAGE_TITLES,
    PERSONAL_CONTACT_URL,
    SITE_URL,
    TELEGRAM_URL,
    TOTAL_PAGES,
    PdfRenderer,
)
from money_profile_bot.services.rules import generate_profile

ASSET_DIRECTORY = Path("assets/avatars")


def test_every_avatar_has_free_and_offer_images() -> None:
    assets = AvatarAssets(ASSET_DIRECTORY)
    for avatar in AVATAR_SLUGS:
        assert assets.free_image(avatar).is_file()
        assert assets.offer_image(avatar).is_file()
    assert assets.full_reading_offer_image().is_file()


def test_legacy_names_resolve_to_new_avatar_assets() -> None:
    assert display_avatar_name("Коммуникатор") == "Рассказчица"
    assert display_avatar_name("Стратег") == "Навигатор"
    assert display_avatar_name("Управленец") == "Вдохновительница"
    assert display_avatar_name("Создатель ценности или Коммуникатор") == "Мастерица"


def _image_count(page: object) -> int:
    resources = page["/Resources"].get_object()  # type: ignore[index]
    xobjects = resources.get("/XObject", {})
    return sum(item.get_object().get("/Subtype") == "/Image" for item in xobjects.values())


def _links(page: object) -> set[str]:
    urls: set[str] = set()
    for reference in page.get("/Annots", []):  # type: ignore[attr-defined]
        annotation = reference.get_object()
        action = annotation.get("/A")
        if action and action.get("/URI"):
            urls.add(str(action["/URI"]))
    return urls


def test_paid_profile_renders_as_seven_page_pdf(tmp_path: Path, birth: BirthData) -> None:
    result = generate_profile(calculate_chart(birth))
    destination = tmp_path / "money-profile.pdf"
    PdfRenderer(AvatarAssets(ASSET_DIRECTORY)).render(
        name="Наталья",
        result=result,
        destination=destination,
    )

    reader = PdfReader(destination)
    assert len(reader.pages) == TOTAL_PAGES
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    for title in PAGE_TITLES:
        assert title in extracted
    assert "Хочешь полный" in extracted
    assert "Астрологическая интерпретация для самонаблюдения" not in extracted
    assert "Астрология и Таро" in extracted
    assert "natarazlozhi.ru" in extracted

    final_page_text = reader.pages[-1].extract_text() or ""
    assert "Хочешь полный разбор?" in final_page_text
    assert "Пиши «Хочу денежный разбор»" in final_page_text
    assert "@simnatali" in final_page_text
    assert "Астрология и Таро" not in final_page_text
    assert "1 990" not in final_page_text
    assert "В этом PDF" not in final_page_text

    assert _image_count(reader.pages[0]) >= 1
    assert all(_image_count(page) == 0 for page in reader.pages[1:])

    urls = set().union(*(_links(page) for page in reader.pages))
    assert {SITE_URL, INSTAGRAM_URL, TELEGRAM_URL, PERSONAL_CONTACT_URL} <= urls
