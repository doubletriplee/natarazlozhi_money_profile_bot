from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from money_profile_bot.domain import BirthData
from money_profile_bot.services.astro import calculate_chart
from money_profile_bot.services.avatar import AVATAR_SLUGS, AvatarAssets, display_avatar_name
from money_profile_bot.services.pdf import PAGE_TITLES, PdfRenderer
from money_profile_bot.services.rules import generate_profile

ASSET_DIRECTORY = Path("assets/avatars")


def test_every_avatar_has_free_and_offer_images() -> None:
    assets = AvatarAssets(ASSET_DIRECTORY)
    for avatar in AVATAR_SLUGS:
        assert assets.free_image(avatar).is_file()
        assert assets.offer_image(avatar).is_file()


def test_legacy_names_resolve_to_new_avatar_assets() -> None:
    assert display_avatar_name("Коммуникатор") == "Рассказчица"
    assert display_avatar_name("Стратег") == "Навигатор"
    assert display_avatar_name("Управленец") == "Вдохновительница"
    assert display_avatar_name("Создатель ценности или Коммуникатор") == "Мастерица"


def test_paid_profile_renders_as_six_page_pdf(tmp_path: Path, birth: BirthData) -> None:
    result = generate_profile(calculate_chart(birth))
    destination = tmp_path / "money-profile.pdf"
    PdfRenderer(AvatarAssets(ASSET_DIRECTORY)).render(
        name="Наталья",
        result=result,
        destination=destination,
    )

    reader = PdfReader(destination)
    assert len(reader.pages) == 6
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    for title in PAGE_TITLES:
        assert title in extracted
