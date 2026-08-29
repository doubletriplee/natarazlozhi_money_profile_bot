from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from money_profile_bot.bot.router import (
    _begin,
    _birth_date_is_plausible,
    _sales_keyboard,
    _send_avatar_and_offer,
)
from money_profile_bot.bot.states import ProfileForm
from money_profile_bot.config import Settings
from money_profile_bot.services.avatar import (
    AVATAR_PRESENTATIONS,
    FULL_READING_CAPTION,
    INTRO_CAPTION,
    SALES_MESSAGE_TEXT,
    AvatarAssets,
    sales_telegram_url,
)

ASSET_DIRECTORY = Path("assets/avatars")
REQUIRED_HEADINGS = (
    "<b>Сильная сторона:</b>",
    "<b>Формат работы:</b>",
    "<b>Как проявляться и продавать:</b>",
    "<b>Денежная ловушка:</b>",
    "<b>Твой денежный шаг сегодня:</b>",
)


def test_intro_copy_and_image_are_complete() -> None:
    assets = AvatarAssets(ASSET_DIRECTORY)
    assert assets.first_message_image().is_file()
    assert INTRO_CAPTION.startswith("✨Узнай свой Денежный аватар")
    assert INTRO_CAPTION.endswith(
        "Заполни данные рождения — и бот определит твой Денежный аватар по натальной карте."
    )
    expected_avatars = (
        "✨ Муза",
        "🎙 Рассказчица",
        "💎 Мастерица",
        "🧭 Навигатор",
        "🔥 Вдохновительница",
        "🗝 Хранительница",
        "🔮 Проводница",
        "🔍 Искательница",
        "🪄 Создательница",
        "🌸 Эстетка",
    )
    assert all(avatar in INTRO_CAPTION for avatar in expected_avatars)


def test_every_avatar_has_one_image_and_complete_caption() -> None:
    assets = AvatarAssets(ASSET_DIRECTORY)
    assert len(AVATAR_PRESENTATIONS) == 10
    for avatar_name, presentation in AVATAR_PRESENTATIONS.items():
        assert assets.free_image(avatar_name).is_file()
        assert len(presentation.caption) <= 1024
        for heading in REQUIRED_HEADINGS:
            assert heading in presentation.caption


def test_sales_link_prefills_exact_message() -> None:
    url = sales_telegram_url("@simnatali")
    parsed = urlparse(url)
    assert parsed.netloc == "t.me"
    assert parsed.path == "/simnatali"
    assert parse_qs(parsed.query) == {"text": [SALES_MESSAGE_TEXT]}
    assert "1 990₽" in FULL_READING_CAPTION
    assert "990₽" in FULL_READING_CAPTION
    assert "1 990 ₽" not in FULL_READING_CAPTION
    assert "990 ₽" not in FULL_READING_CAPTION


def test_birth_date_validation_has_no_adult_age_gate() -> None:
    today = date(2026, 8, 29)
    assert _birth_date_is_plausible(date(2015, 1, 1), today=today)
    assert _birth_date_is_plausible(date(2026, 8, 29), today=today)
    assert not _birth_date_is_plausible(date(2026, 8, 30), today=today)
    assert not _birth_date_is_plausible(date(1900, 1, 1), today=today)


@pytest.mark.asyncio
async def test_begin_sends_intro_photo_and_skips_age_gate() -> None:
    message = AsyncMock()
    state = AsyncMock()
    settings = Settings(_env_file=None)
    assets = AvatarAssets(ASSET_DIRECTORY)

    await _begin(message, state, settings, assets)

    state.set_state.assert_awaited_once_with(ProfileForm.consent)
    kwargs = message.answer_photo.await_args.kwargs
    assert kwargs["caption"] == INTRO_CAPTION
    buttons = kwargs["reply_markup"].inline_keyboard
    assert buttons[-1][0].text == "Узнать свой аватар"
    assert not hasattr(ProfileForm, "adult")
    assert not hasattr(ProfileForm, "email")


@pytest.mark.asyncio
@pytest.mark.parametrize("avatar_name", AVATAR_PRESENTATIONS)
async def test_all_avatar_results_are_photos_followed_by_sales_offer(
    avatar_name: str,
) -> None:
    message = AsyncMock()
    settings = Settings(_env_file=None)
    assets = AvatarAssets(ASSET_DIRECTORY)

    await _send_avatar_and_offer(
        message,
        money_type=avatar_name,
        settings=settings,
        avatars=assets,
        delay_seconds=0,
    )

    assert message.answer_photo.await_count == 2
    first, second = message.answer_photo.await_args_list
    assert first.kwargs["caption"] == AVATAR_PRESENTATIONS[avatar_name].caption
    assert second.kwargs["caption"] == FULL_READING_CAPTION
    button = second.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Хочу денежный разбор"
    assert parse_qs(urlparse(button.url).query) == {"text": [SALES_MESSAGE_TEXT]}
    assert message.answer_document.await_count == 0


def test_sales_keyboard_uses_single_configured_username() -> None:
    settings = Settings(support_username="@simnatali", _env_file=None)
    button = _sales_keyboard(settings).inline_keyboard[0][0]
    assert button.url == sales_telegram_url(settings.support_username)
