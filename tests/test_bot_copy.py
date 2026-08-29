from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from money_profile_bot.bot.router import (
    OFFER_DELAY_SECONDS,
    _accept_consent,
    _begin,
    _birth_date_is_plausible,
    _sales_keyboard,
    _send_free_avatar_and_offer,
)
from money_profile_bot.bot.states import ProfileForm
from money_profile_bot.config import Settings
from money_profile_bot.services.avatar import (
    AVATAR_CHANNELS,
    AVATAR_PRESENTATIONS,
    AVATAR_PROFESSIONS,
    FULL_READING_CAPTION,
    INTRO_CAPTION,
    SALES_MESSAGE_TEXT,
    STRENGTH_OFFER_CAPTION,
    AvatarAssets,
    avatar_paid_caption,
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
    assert INTRO_CAPTION.startswith("Узнай свой Денежный аватар 💫")
    assert INTRO_CAPTION.endswith("А кто ты среди них?")
    assert "Мой авторский метод, который покажет" in INTRO_CAPTION
    assert (
        "<b>через что тебе легче создавать ценность, проявляться, продавать и "
        "приходить к доходу.</b>"
    ) in INTRO_CAPTION
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
        assert assets.offer_image(avatar_name).is_file()
        assert len(presentation.caption) <= 1024
        for heading in REQUIRED_HEADINGS:
            assert heading in presentation.caption
            assert heading in avatar_paid_caption(avatar_name)
        assert avatar_paid_caption(avatar_name).startswith(f"<b>{avatar_name}</b>\n\n")


def test_every_avatar_has_channel_and_professions_copy() -> None:
    assert AVATAR_CHANNELS.keys() == AVATAR_PRESENTATIONS.keys()
    assert AVATAR_PROFESSIONS.keys() == AVATAR_PRESENTATIONS.keys()
    for avatar_name in AVATAR_PRESENTATIONS:
        assert AVATAR_CHANNELS[avatar_name].startswith("Твой основной канал —")
        paid_caption = avatar_paid_caption(avatar_name)
        assert AVATAR_PROFESSIONS[avatar_name] in paid_caption
        assert "<b>Профессии:</b>" in paid_caption
        assert "<b>Онлайн:</b>" in paid_caption
        assert "<b>Офлайн:</b>" in paid_caption
        assert paid_caption.index("<b>Сильная сторона:</b>") < paid_caption.index(
            "<b>Профессии:</b>"
        )
        assert paid_caption.index("<b>Профессии:</b>") < paid_caption.index("<b>Формат работы:</b>")
        assert len(re.sub(r"<[^>]+>", "", paid_caption)) <= 1024


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
    assert "Обычная стоимость — <s>1 990₽</s>" in FULL_READING_CAPTION
    assert "<b>Твоя цена после Денежного аватара — 990₽</b>" in FULL_READING_CAPTION
    assert len(FULL_READING_CAPTION) <= 4096


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
async def test_consent_skips_name_and_requests_birth_date() -> None:
    callback = AsyncMock()
    callback.from_user.id = 123456
    state = AsyncMock()
    store = AsyncMock()
    settings = Settings(_env_file=None)

    await _accept_consent(callback, state, settings, store)

    store.save_consent.assert_awaited_once_with(123456, settings.legal_docs_version)
    state.set_state.assert_awaited_once_with(ProfileForm.birth_date)
    callback.message.answer.assert_awaited_once_with("Введи дату рождения в формате ДД.ММ.ГГГГ.")
    assert all("Как тебя зовут?" not in str(call) for call in callback.mock_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("avatar_name", AVATAR_PRESENTATIONS)
async def test_all_free_avatar_results_are_followed_by_strength_offer(
    avatar_name: str,
) -> None:
    message = AsyncMock()
    settings = Settings(_env_file=None)
    assets = AvatarAssets(ASSET_DIRECTORY)

    free_insight = (
        f"<b>Ваш денежный аватар — {avatar_name}</b>\n\n"
        "<b>Основной канал</b>\nТест.\n\n"
        "<b>Сильная сторона</b>\nТест."
    )
    await _send_free_avatar_and_offer(
        message,
        profile_id="profile-1",
        money_type=avatar_name,
        free_insight=free_insight,
        settings=settings,
        avatars=assets,
        delay_seconds=0,
    )

    assert message.answer_photo.await_count == 2
    first, second = message.answer_photo.await_args_list
    assert first.kwargs["caption"] == free_insight
    assert second.kwargs["caption"] == STRENGTH_OFFER_CAPTION
    button = second.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Раскрыть силу - 149₽"
    assert button.callback_data == "buy:profile-1"
    assert message.answer_document.await_count == 0


@pytest.mark.asyncio
async def test_strength_offer_is_delayed_by_four_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    message = AsyncMock()
    settings = Settings(_env_file=None)
    assets = AvatarAssets(ASSET_DIRECTORY)
    sleep = AsyncMock()
    monkeypatch.setattr("money_profile_bot.bot.router.asyncio.sleep", sleep)

    await _send_free_avatar_and_offer(
        message,
        profile_id="profile-1",
        money_type="Муза",
        free_insight="Бесплатный результат",
        settings=settings,
        avatars=assets,
    )

    assert OFFER_DELAY_SECONDS == 4
    sleep.assert_awaited_once_with(4)


def test_sales_keyboard_uses_single_configured_username() -> None:
    settings = Settings(support_username="@simnatali", _env_file=None)
    button = _sales_keyboard(settings).inline_keyboard[0][0]
    assert button.url == sales_telegram_url(settings.support_username)
