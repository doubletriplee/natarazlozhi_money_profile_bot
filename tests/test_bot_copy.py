from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from money_profile_bot.bot.router import (
    _accept_consent,
    _begin,
    _birth_date_is_plausible,
    _delete_start_command,
    _intro_keyboard,
    _reminder_buttons,
    _request_data_deletion,
    _sales_keyboard,
    _send_free_avatar,
    build_router,
)
from money_profile_bot.bot.states import DeleteForm, ProfileForm
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
    avatar_free_caption,
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
    image_path = assets.first_message_image()
    assert image_path.is_file()
    assert hashlib.sha256(image_path.read_bytes()).hexdigest() == (
        "8ecd2ac85627a2689b2d5718fd03fc76c282615aa24266a50a6bc0ef648e1ab1"
    )
    assert INTRO_CAPTION.startswith("Узнай свой Денежный аватар 💫")
    assert "А кто ты среди них?" in INTRO_CAPTION
    assert "не является финансовой рекомендацией" in INTRO_CAPTION
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
    assert FULL_READING_CAPTION.endswith("реализация и твой способ проявляться.")
    assert "Сохрани эти подсказки — к ним стоит возвращаться 🤍" in FULL_READING_CAPTION
    assert "<b>всю твою денежную картину</b>" in FULL_READING_CAPTION
    assert len(re.sub(r"<[^>]+>", "", FULL_READING_CAPTION)) <= 1024


def test_strength_offer_has_practical_transition_paragraph() -> None:
    assert "Твой денежный шаг уже сегодня\n\nХочешь понять" in STRENGTH_OFFER_CAPTION
    assert "<b>Тестовый режим оплаты:</b>" in STRENGTH_OFFER_CAPTION
    assert STRENGTH_OFFER_CAPTION.endswith("открывает разбор без списания денег.")
    assert len(STRENGTH_OFFER_CAPTION) <= 1024


def test_birth_date_validation_has_no_adult_age_gate() -> None:
    today = date(2026, 8, 29)
    assert _birth_date_is_plausible(date(2015, 1, 1), today=today)
    assert _birth_date_is_plausible(date(2026, 8, 29), today=today)
    assert not _birth_date_is_plausible(date(2026, 8, 30), today=today)
    assert not _birth_date_is_plausible(date(1900, 1, 1), today=today)


def test_form_reminder_keeps_callback_buttons_and_drops_url_rows() -> None:
    buttons = _reminder_buttons(_intro_keyboard(Settings(_env_file=None)))

    assert buttons == ((("✅ Согласен(а), продолжить", "consent:yes"),),)


@pytest.mark.asyncio
async def test_begin_sends_intro_photo_with_four_legal_buttons() -> None:
    message = AsyncMock()
    state = AsyncMock()
    settings = Settings(_env_file=None)
    assets = AvatarAssets(ASSET_DIRECTORY)

    await _begin(message, state, settings, assets)

    state.set_state.assert_awaited_once_with(ProfileForm.consent)
    kwargs = message.answer_photo.await_args.kwargs
    assert kwargs["caption"] == INTRO_CAPTION
    buttons = kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in buttons] == [
        "🔒 Политика обработки данных",
        "📜 Публичная оферта",
        "✍️ Согласие на обработку",
        "✅ Согласен(а), продолжить",
    ]
    assert buttons[0][0].url == f"{settings.public_base_url}/privacy"
    assert buttons[1][0].url == f"{settings.public_base_url}/terms"
    assert buttons[2][0].url == f"{settings.public_base_url}/consent"
    assert buttons[3][0].callback_data == "consent:yes"
    assert not hasattr(ProfileForm, "adult")
    assert not hasattr(ProfileForm, "email")


@pytest.mark.asyncio
async def test_start_command_is_deleted_from_private_chat() -> None:
    message = AsyncMock()

    await _delete_start_command(message)

    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_consent_skips_name_and_requests_birth_date() -> None:
    callback = AsyncMock()
    callback.from_user.id = 123456
    state = AsyncMock()
    store = AsyncMock()
    settings = Settings(_env_file=None)

    await _accept_consent(callback, state, settings, store)

    store.save_consent.assert_awaited_once_with(123456, settings.legal_docs_version)
    store.schedule_form_reminder.assert_awaited_once_with(
        123456,
        state="birth_date",
        text="Введи дату рождения в формате ДД.ММ.ГГГГ.",
        buttons=(),
    )
    state.set_state.assert_awaited_once_with(ProfileForm.birth_date)
    callback.message.answer.assert_awaited_once_with("Введи дату рождения в формате ДД.ММ.ГГГГ.")
    assert all("Как тебя зовут?" not in str(call) for call in callback.mock_calls)


@pytest.mark.asyncio
async def test_delete_command_switches_from_profile_form_to_delete_confirmation() -> None:
    message = AsyncMock()
    state = AsyncMock()

    await _request_data_deletion(message, state)

    state.set_state.assert_awaited_once_with(DeleteForm.confirm)
    text = message.answer.await_args.args[0]
    assert text.startswith("Удалить данные рождения и результат расчёта?")
    assert "Введи дату рождения" not in text
    buttons = message.answer.await_args.kwargs["reply_markup"].inline_keyboard
    assert buttons[0][0].callback_data == "delete:yes"
    assert buttons[1][0].callback_data == "delete:no"


def test_delete_command_has_priority_over_profile_form_answers() -> None:
    router = build_router(
        Settings(_env_file=None),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    handlers = router.message.handlers
    delete_index = next(
        index
        for index, handler in enumerate(handlers)
        if any(
            "delete_my_data" in getattr(filter_.callback, "commands", ())
            for filter_ in handler.filters
        )
    )
    profile_state_indexes = [
        index
        for index, handler in enumerate(handlers)
        if any(
            str(getattr(filter_.callback, "state", "")).startswith("ProfileForm:")
            for filter_ in handler.filters
        )
    ]

    assert profile_state_indexes
    assert delete_index < min(profile_state_indexes)


@pytest.mark.asyncio
@pytest.mark.parametrize("avatar_name", AVATAR_PRESENTATIONS)
async def test_all_free_avatar_results_have_strength_trigger(
    avatar_name: str,
) -> None:
    message = AsyncMock()
    assets = AvatarAssets(ASSET_DIRECTORY)

    await _send_free_avatar(
        message,
        profile_id="profile-1",
        money_type=avatar_name,
        avatars=assets,
    )

    message.answer_photo.assert_awaited_once()
    kwargs = message.answer_photo.await_args.kwargs
    assert kwargs["caption"] == avatar_free_caption(avatar_name)
    assert "<b>Сильная сторона" not in kwargs["caption"]
    assert "<b>А теперь самое интересное — сила твоего аватара.</b>" in kwargs["caption"]
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Узнать силу"
    assert button.callback_data == "strength:profile-1"
    assert message.answer_document.await_count == 0


def test_sales_keyboard_uses_single_configured_username() -> None:
    settings = Settings(support_username="@simnatali", _env_file=None)
    button = _sales_keyboard(settings).inline_keyboard[0][0]
    assert button.url == sales_telegram_url(settings.support_username)
