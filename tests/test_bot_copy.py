from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from money_profile_bot.bot.router import (
    _accept_consent,
    _begin,
    _birth_date_is_plausible,
    _birth_date_picker,
    _birth_time_picker,
    _delete_start_command,
    _intro_keyboard,
    _payment_email_prompt,
    _payment_link_message,
    _receipt_email_is_valid,
    _reminder_buttons,
    _request_data_deletion,
    _sales_keyboard,
    _send_free_avatar,
    build_router,
)
from money_profile_bot.bot.states import DeleteForm, PaymentForm, ProfileForm
from money_profile_bot.config import PaymentMode, Settings
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
    strength_offer_caption,
)
from money_profile_bot.services.store import OrderLink

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
    assert "из бота «Денежный аватар»" in SALES_MESSAGE_TEXT
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
    staging_caption = strength_offer_caption(robokassa=True, test_mode=True)
    assert "Тестовый режим Robokassa" in staging_caption
    assert "деньги не списываются" in staging_caption
    production_caption = strength_offer_caption(robokassa=True, test_mode=False)
    assert "результат откроется после подтверждения платежа" in production_caption
    assert "деньги не списываются" not in production_caption


def test_robokassa_email_and_payment_copy_are_explicit() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_mode=True,
        _env_file=None,
    )
    prompt, prompt_keyboard = _payment_email_prompt(settings)
    assert "email для электронного чека" in prompt
    assert "деньги не спишутся" in prompt
    assert prompt_keyboard.inline_keyboard[-1][0].callback_data == "payment:cancel"
    assert _receipt_email_is_valid("buyer@example.ru")
    assert not _receipt_email_is_valid("buyer@example")
    assert not _receipt_email_is_valid("buyer @example.ru")

    text, keyboard = _payment_link_message(
        settings,
        OrderLink("order-1", "MP-12345678", "https://pay.example/order-1", False),
    )
    assert "Тестовый счёт Robokassa готов" in text
    assert "149.00 ₽" in text
    assert "Деньги не списываются" in text
    assert keyboard.inline_keyboard[0][0].url == "https://pay.example/order-1"


def test_birth_date_validation_has_no_adult_age_gate() -> None:
    today = date(2026, 8, 29)
    assert _birth_date_is_plausible(date(2015, 1, 1), today=today)
    assert _birth_date_is_plausible(date(2026, 8, 29), today=today)
    assert not _birth_date_is_plausible(date(2026, 8, 30), today=today)
    assert not _birth_date_is_plausible(date(1900, 1, 1), today=today)


def test_birth_date_picker_covers_valid_range_and_calendar_days() -> None:
    today = date(2026, 8, 30)

    prompt, decades = _birth_date_picker({}, today=today)
    assert prompt.startswith("📅 Когда ты родилась?")
    decade_buttons = [button for row in decades.inline_keyboard for button in row]
    assert decade_buttons[0].text == "1900-е"
    assert decade_buttons[0].callback_data == "birth_date:decade:1900"
    assert decade_buttons[-2].text == "2020-е"
    assert decade_buttons[-1].callback_data == "form:cancel"

    _, first_years = _birth_date_picker(
        {"birth_date_step": "year", "birth_decade": 1900}, today=today
    )
    assert [button.text for row in first_years.inline_keyboard[:-2] for button in row] == [
        "1906",
        "1907",
        "1908",
        "1909",
    ]

    _, current_months = _birth_date_picker(
        {"birth_date_step": "month", "birth_year": 2026}, today=today
    )
    assert [button.text for row in current_months.inline_keyboard[:-2] for button in row] == [
        "янв",
        "фев",
        "мар",
        "апр",
        "май",
        "июн",
        "июл",
        "авг",
    ]

    _, current_days = _birth_date_picker(
        {"birth_date_step": "day", "birth_year": 2026, "birth_month": 8}, today=today
    )
    assert [button.text for row in current_days.inline_keyboard[:-2] for button in row][-1] == "30"

    _, leap_days = _birth_date_picker(
        {"birth_date_step": "day", "birth_year": 2024, "birth_month": 2}, today=today
    )
    assert [button.text for row in leap_days.inline_keyboard[:-2] for button in row][-1] == "29"


def test_birth_time_picker_uses_hour_range_and_exact_minute_buttons() -> None:
    prompt, hours = _birth_time_picker({"time_precision": "exact"})
    assert prompt.startswith("🕐 Во сколько ты родилась?")
    hour_buttons = [button for row in hours.inline_keyboard[:-2] for button in row]
    assert [button.text for button in hour_buttons] == [f"{hour:02d}" for hour in range(24)]

    prompt, ranges = _birth_time_picker(
        {"birth_time_step": "minute_range", "birth_hour": 23, "time_precision": "exact"}
    )
    assert prompt == "🕐 Час: 23 — выбери диапазон минут:"
    assert [button.text for row in ranges.inline_keyboard[:-2] for button in row] == [
        "23:00–09",
        "23:10–19",
        "23:20–29",
        "23:30–39",
        "23:40–49",
        "23:50–59",
    ]

    prompt, minutes = _birth_time_picker(
        {
            "birth_time_step": "minute",
            "birth_hour": 23,
            "birth_minute_start": 40,
            "time_precision": "exact",
        }
    )
    assert prompt == "🕐 23:40–49 — выбери точную минуту:"
    assert [button.text for row in minutes.inline_keyboard[:-2] for button in row] == [
        f"23:{minute:02d}" for minute in range(40, 50)
    ]


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
    assert hasattr(PaymentForm, "email")


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
    state.update_data.assert_awaited_once_with(
        birth_date_step="decade",
        birth_decade=None,
        birth_year=None,
        birth_month=None,
    )
    reminder = store.schedule_form_reminder.await_args
    assert reminder.args == (123456,)
    assert reminder.kwargs["state"] == "birth_date"
    assert reminder.kwargs["text"].startswith("📅 Когда ты родилась?")
    assert reminder.kwargs["buttons"][0][0][1].startswith("birth_date:decade:")
    state.set_state.assert_awaited_once_with(ProfileForm.birth_date)
    answer = callback.message.answer.await_args
    assert answer.args[0].startswith("📅 Когда ты родилась?")
    assert answer.kwargs["reply_markup"].inline_keyboard
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
async def test_robokassa_buy_requests_receipt_email() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_mode=True,
        _env_file=None,
    )
    store = AsyncMock()
    store.profile_access.return_value = SimpleNamespace(
        profile_id="profile-1",
        order_id=None,
        order_status=None,
        payment_url=None,
        order_code=None,
    )
    router = build_router(
        settings,
        store,
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    handler = next(
        item.callback for item in router.callback_query.handlers if item.callback.__name__ == "buy"
    )
    callback = AsyncMock()
    callback.data = "buy:profile-1"
    callback.from_user.id = 123456
    state = AsyncMock()

    await handler(callback, state)

    state.update_data.assert_awaited_once_with(payment_profile_id="profile-1")
    state.set_state.assert_awaited_once_with(PaymentForm.email)
    prompt = callback.message.answer.await_args.args[0]
    assert "email для электронного чека" in prompt
    store.create_fake_paid_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_robokassa_buy_is_blocked_while_payments_are_paused() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_mode=False,
        live_payments_enabled=False,
        _env_file=None,
    )
    store = AsyncMock()
    store.profile_access.return_value = SimpleNamespace(
        profile_id="profile-1",
        order_id=None,
        order_status=None,
        payment_url=None,
        order_code=None,
    )
    router = build_router(settings, store, AsyncMock(), AsyncMock(), AsyncMock())
    handler = next(
        item.callback for item in router.callback_query.handlers if item.callback.__name__ == "buy"
    )
    callback = AsyncMock()
    callback.data = "buy:profile-1"
    callback.from_user.id = 123456
    state = AsyncMock()

    await handler(callback, state)

    state.clear.assert_awaited_once()
    state.set_state.assert_not_awaited()
    store.create_order.assert_not_awaited()
    assert "Оплата временно приостановлена" in callback.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_receipt_email_creates_robokassa_invoice_link() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_mode=True,
        _env_file=None,
    )
    store = AsyncMock()
    store.profile_access.return_value = SimpleNamespace(profile_id="profile-1")
    store.create_order.return_value = OrderLink(
        "order-1",
        "MP-12345678",
        "https://pay.example/order-1",
        False,
    )
    router = build_router(
        settings,
        store,
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    handler = next(
        item.callback
        for item in router.message.handlers
        if item.callback.__name__ == "payment_email"
    )
    message = AsyncMock()
    message.from_user.id = 123456
    message.text = " buyer@example.ru "
    state = AsyncMock()
    state.get_data.return_value = {"payment_profile_id": "profile-1"}

    await handler(message, state)

    store.create_order.assert_awaited_once_with(
        telegram_id=123456,
        profile_id="profile-1",
        email="buyer@example.ru",
        amount_minor=14900,
    )
    state.clear.assert_awaited_once()
    text = message.answer.await_args.args[0]
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert "Тестовый счёт Robokassa готов" in text
    assert keyboard.inline_keyboard[0][0].url == "https://pay.example/order-1"


@pytest.mark.asyncio
async def test_live_payment_pause_is_rechecked_before_invoice_creation() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_mode=False,
        live_payments_enabled=False,
        _env_file=None,
    )
    store = AsyncMock()
    router = build_router(settings, store, AsyncMock(), AsyncMock(), AsyncMock())
    handler = next(
        item.callback
        for item in router.message.handlers
        if item.callback.__name__ == "payment_email"
    )
    message = AsyncMock()
    message.from_user.id = 123456
    message.text = "buyer@example.ru"
    state = AsyncMock()

    await handler(message, state)

    state.clear.assert_awaited_once()
    state.get_data.assert_not_awaited()
    store.create_order.assert_not_awaited()
    assert "Оплата временно приостановлена" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_birth_date_button_saves_date_and_opens_time_precision() -> None:
    store = AsyncMock()
    router = build_router(
        Settings(_env_file=None),
        store,
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    handler = next(
        item.callback
        for item in router.callback_query.handlers
        if item.callback.__name__ == "birth_date_choice"
    )
    callback = AsyncMock()
    callback.data = "birth_date:day:1996:2:8"
    callback.from_user.id = 123456
    callback.message = None
    state = AsyncMock()

    await handler(callback, state)

    state.update_data.assert_awaited_once_with(
        birth_date="1996-02-08",
        birth_date_step=None,
        birth_decade=None,
        birth_year=None,
        birth_month=None,
    )
    state.set_state.assert_awaited_once_with(ProfileForm.time_precision)
    reminder = store.schedule_form_reminder.await_args
    assert reminder.kwargs["state"] == "time_precision"
    assert reminder.kwargs["buttons"][0][0] == ("Знаю точно", "precision:exact")


@pytest.mark.asyncio
async def test_birth_time_button_saves_exact_minute_and_opens_city() -> None:
    store = AsyncMock()
    router = build_router(
        Settings(_env_file=None),
        store,
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    handler = next(
        item.callback
        for item in router.callback_query.handlers
        if item.callback.__name__ == "birth_time_choice"
    )
    callback = AsyncMock()
    callback.data = "birth_time:minute:23:45"
    callback.from_user.id = 123456
    callback.message = None
    state = AsyncMock()

    await handler(callback, state)

    state.update_data.assert_awaited_once_with(
        birth_time="23:45:00",
        birth_time_step=None,
        birth_hour=None,
        birth_minute_start=None,
    )
    state.set_state.assert_awaited_once_with(ProfileForm.city)
    reminder = store.schedule_form_reminder.await_args
    assert reminder.kwargs["state"] == "city"
    assert reminder.kwargs["text"].startswith("Введи только город рождения")


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
