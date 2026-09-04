import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
from urllib.parse import parse_qs, urlparse

import pytest
from aiogram.types import Message

from money_profile_bot.config import PaymentMode
from money_profile_bot.models import DeliveryStatus, OrderStatus
from money_profile_bot.services.avatar import (
    FULL_READING_CAPTION,
    SALES_MESSAGE_TEXT,
    STRENGTH_OFFER_CAPTION,
    AvatarAssets,
    avatar_paid_caption,
    avatar_paid_caption_pages,
    strength_offer_caption,
)
from money_profile_bot.services.delivery import (
    FULL_READING_TRIGGER_TEXT,
    NEW_PROFILE_TRIGGER_TEXT,
    DeliveryWorker,
)

ASSET_DIRECTORY = Path("assets/avatars")


def _worker_context(kind: str) -> tuple[AsyncMock, AsyncMock, DeliveryWorker]:
    bot = AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(message_id=42)
    bot.send_message.return_value = SimpleNamespace(message_id=43)
    store = AsyncMock()
    store.delivery_context.return_value = (
        SimpleNamespace(id="order-1", profile_id="profile-1", status=OrderStatus.PAID),
        123456,
        SimpleNamespace(name="Наталья"),
        SimpleNamespace(money_type="Навигатор"),
        [SimpleNamespace(id=1, kind=kind, status=DeliveryStatus.PENDING)],
    )
    worker = DeliveryWorker(
        bot,
        store,
        AvatarAssets(ASSET_DIRECTORY),
        sales_telegram_username="simnatali",
        product_price_rub=Decimal("149"),
    )
    return bot, store, worker


def _robokassa_worker(*, test_mode: bool) -> DeliveryWorker:
    return DeliveryWorker(
        AsyncMock(),
        AsyncMock(),
        AvatarAssets(ASSET_DIRECTORY),
        sales_telegram_username="simnatali",
        product_price_rub=Decimal("149"),
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_mode=test_mode,
    )


@pytest.mark.asyncio
async def test_paid_avatar_starts_with_first_short_page() -> None:
    bot, store, worker = _worker_context("avatar_result")

    await worker.deliver("order-1")

    kwargs = bot.send_photo.await_args.kwargs
    pages = avatar_paid_caption_pages("Навигатор")
    assert kwargs["caption"] == f"<b>Часть 1 из {len(pages)}</b>\n\n{pages[0]}"
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == f"Дальше · 2 из {len(pages)} →"
    assert button.callback_data == "avatar_page:order-1:1"
    bot.send_message.assert_not_awaited()
    store.mark_delivery_item.assert_awaited_once_with(1, status=DeliveryStatus.SENT, message_id=42)
    store.complete_delivery_if_ready.assert_awaited_once_with("order-1")


@pytest.mark.asyncio
async def test_long_paid_avatar_is_split_into_short_semantic_pages() -> None:
    bot, store, worker = _worker_context("avatar_result")
    context = list(store.delivery_context.return_value)
    context[3] = SimpleNamespace(money_type="Муза")
    store.delivery_context.return_value = tuple(context)

    await worker.deliver("order-1")

    pages = avatar_paid_caption_pages("Муза")
    assert len(pages) == 6
    assert "\n\n".join(pages) == avatar_paid_caption("Муза")
    assert all(len(page) < 600 for page in pages)
    assert bot.send_photo.await_args.kwargs["caption"].endswith(pages[0])
    bot.send_message.assert_not_awaited()
    store.mark_delivery_item.assert_awaited_once_with(1, status=DeliveryStatus.SENT, message_id=42)


@pytest.mark.asyncio
async def test_paid_avatar_pages_are_edited_in_place_with_navigation() -> None:
    _bot, store, worker = _worker_context("avatar_result")
    context = list(store.delivery_context.return_value)
    context[3] = SimpleNamespace(money_type="Муза")
    store.delivery_context.return_value = tuple(context)
    message = AsyncMock(spec=Message)
    message.edit_caption = AsyncMock()

    shown = await worker.show_paid_avatar_page(
        message,
        telegram_id=123456,
        order_id="order-1",
        page_index=1,
    )

    assert shown
    pages = avatar_paid_caption_pages("Муза")
    kwargs = message.edit_caption.await_args.kwargs
    assert kwargs["caption"] == f"<b>Часть 2 из {len(pages)}</b>\n\n{pages[1]}"
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [button.callback_data for button in buttons] == [
        "avatar_page:order-1:0",
        "avatar_page:order-1:2",
    ]


@pytest.mark.asyncio
async def test_last_paid_avatar_page_exposes_followup_actions() -> None:
    _bot, store, worker = _worker_context("avatar_result")
    context = list(store.delivery_context.return_value)
    context[3] = SimpleNamespace(money_type="Муза")
    store.delivery_context.return_value = tuple(context)
    message = AsyncMock(spec=Message)
    message.edit_caption = AsyncMock()
    last_page_index = len(avatar_paid_caption_pages("Муза")) - 1

    assert await worker.show_paid_avatar_page(
        message,
        telegram_id=123456,
        order_id="order-1",
        page_index=last_page_index,
    )

    rows = message.edit_caption.await_args.kwargs["reply_markup"].inline_keyboard
    assert rows[1][0].text == FULL_READING_TRIGGER_TEXT
    assert rows[1][0].callback_data == "full:order-1"
    assert rows[2][0].text == NEW_PROFILE_TRIGGER_TEXT
    assert rows[2][0].callback_data == "profile:new"


@pytest.mark.asyncio
async def test_paid_avatar_page_rejects_another_user() -> None:
    _bot, _store, worker = _worker_context("avatar_result")
    message = AsyncMock(spec=Message)
    message.edit_caption = AsyncMock()

    shown = await worker.show_paid_avatar_page(
        message,
        telegram_id=999999,
        order_id="order-1",
        page_index=1,
    )

    assert not shown
    message.edit_caption.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_reading_offer_is_sent_as_one_photo_post() -> None:
    bot, _store, worker = _worker_context("full_reading_offer")

    await worker.deliver("order-1")

    args = bot.send_photo.await_args.args
    kwargs = bot.send_photo.await_args.kwargs
    assert args[0] == 123456
    assert Path(args[1].path) == ASSET_DIRECTORY / "full_reading_offer.png"
    assert kwargs["caption"] == FULL_READING_CAPTION
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Хочу денежный разбор"
    assert parse_qs(urlparse(button.url).query) == {"text": [SALES_MESSAGE_TEXT]}
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_strength_offer_is_sent_with_payment_button() -> None:
    bot, store, worker = _worker_context("avatar_result")
    store.strength_offer_context.return_value = SimpleNamespace(
        offer_id="offer-1",
        profile_id="profile-1",
        telegram_id=123456,
        money_type="Навигатор",
    )

    delivered = await worker.deliver_strength_offer(
        "profile-1",
        telegram_id=123456,
        force=True,
    )

    assert delivered
    store.strength_offer_context.assert_awaited_once_with(
        "profile-1", telegram_id=123456, force=True
    )
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["caption"] == STRENGTH_OFFER_CAPTION
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Раскрыть силу — 149₽ · тест"
    assert button.callback_data == "buy:profile-1"
    store.mark_strength_offer_sent.assert_awaited_once_with("offer-1", 42)


def test_robokassa_payment_button_distinguishes_test_and_live_modes() -> None:
    staging = _robokassa_worker(test_mode=True)
    production = _robokassa_worker(test_mode=False)

    assert (
        staging._strength_offer_keyboard("profile-1").inline_keyboard[0][0].text.endswith("· тест")
    )
    assert production._strength_offer_keyboard("profile-1").inline_keyboard[0][0].text == (
        "Раскрыть силу — 149₽"
    )
    assert "Тестовый режим Robokassa" in strength_offer_caption(robokassa=True, test_mode=True)


@pytest.mark.asyncio
async def test_form_reminder_repeats_prompt_with_its_buttons_once() -> None:
    bot, store, worker = _worker_context("avatar_result")
    store.form_reminder_context.return_value = SimpleNamespace(
        reminder_id="reminder-1",
        telegram_id=123456,
        text="Насколько точно известно время рождения?",
        buttons=((("Знаю точно", "precision:exact"),),),
        payload_token="encrypted-payload",
    )

    delivered = await worker.deliver_form_reminder("reminder-1")

    assert delivered
    bot.send_message.assert_awaited_once()
    args = bot.send_message.await_args.args
    kwargs = bot.send_message.await_args.kwargs
    assert args == (123456, "Насколько точно известно время рождения?")
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Знаю точно"
    assert button.callback_data == "precision:exact"
    store.mark_form_reminder_sent.assert_awaited_once_with("reminder-1", 43, "encrypted-payload")


@pytest.mark.asyncio
async def test_legacy_feedback_item_is_completed_without_user_message() -> None:
    bot, store, worker = _worker_context("feedback")

    await worker.deliver("order-1")

    bot.send_photo.assert_not_awaited()
    store.mark_delivery_item.assert_awaited_once_with(1, status=DeliveryStatus.SENT)


@pytest.mark.asyncio
async def test_delivery_cycle_prioritizes_paid_orders_and_bounds_background_work() -> None:
    _, store, worker = _worker_context("avatar_result")
    store.pending_order_ids.return_value = ["order-1"]
    store.pending_form_reminder_ids.return_value = ["reminder-1"]
    store.pending_strength_offer_profile_ids.return_value = ["profile-1"]
    worker.deliver = AsyncMock()
    worker.deliver_form_reminder = AsyncMock()
    worker.deliver_strength_offer = AsyncMock()

    await worker._deliver_pending_cycle()

    assert store.mock_calls[:3] == [
        call.pending_order_ids(limit=50),
        call.pending_form_reminder_ids(limit=10),
        call.pending_strength_offer_profile_ids(limit=10),
    ]
    worker.deliver.assert_awaited_once_with("order-1")
    worker.deliver_form_reminder.assert_awaited_once_with("reminder-1")
    worker.deliver_strength_offer.assert_awaited_once_with("profile-1")


@pytest.mark.asyncio
async def test_delivery_worker_reports_health_only_while_running() -> None:
    _, store, worker = _worker_context("avatar_result")
    store.pending_order_ids.return_value = []
    store.pending_form_reminder_ids.return_value = []
    store.pending_strength_offer_profile_ids.return_value = []

    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0)

    assert worker.is_healthy()
    worker.stop()
    await task
    assert not worker.is_healthy()


@pytest.mark.asyncio
async def test_delivery_worker_stops_after_repeated_cycle_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, store, worker = _worker_context("avatar_result")
    store.pending_order_ids.side_effect = RuntimeError("database unavailable")
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await worker.run()

    assert store.pending_order_ids.await_count == 3
    assert sleep.await_count == 2
    assert not worker.is_healthy()
