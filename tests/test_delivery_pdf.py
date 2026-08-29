from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from money_profile_bot.models import DeliveryStatus
from money_profile_bot.services.avatar import (
    FULL_READING_CAPTION,
    SALES_MESSAGE_TEXT,
    STRENGTH_OFFER_CAPTION,
    AvatarAssets,
    avatar_paid_caption,
)
from money_profile_bot.services.delivery import FULL_READING_TRIGGER_TEXT, DeliveryWorker

ASSET_DIRECTORY = Path("assets/avatars")


def _worker_context(kind: str) -> tuple[AsyncMock, AsyncMock, DeliveryWorker]:
    bot = AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(message_id=42)
    bot.send_message.return_value = SimpleNamespace(message_id=43)
    store = AsyncMock()
    store.delivery_context.return_value = (
        SimpleNamespace(id="order-1", profile_id="profile-1"),
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


@pytest.mark.asyncio
async def test_paid_avatar_is_sent_as_photo_with_complete_text() -> None:
    bot, store, worker = _worker_context("avatar_result")

    await worker.deliver("order-1")

    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["caption"] == avatar_paid_caption("Навигатор")
    assert "<b>Онлайн:</b>" in kwargs["caption"]
    assert "<b>Офлайн:</b>" in kwargs["caption"]
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == FULL_READING_TRIGGER_TEXT
    assert button.callback_data == "full:order-1"
    bot.send_message.assert_not_awaited()
    store.mark_delivery_item.assert_awaited_once_with(1, status=DeliveryStatus.SENT, message_id=42)
    store.complete_delivery_if_ready.assert_awaited_once_with("order-1")


@pytest.mark.asyncio
async def test_full_reading_offer_uses_exact_caption_and_prefilled_contact_link() -> None:
    bot, _store, worker = _worker_context("full_reading_offer")

    await worker.deliver("order-1")

    args = bot.send_message.await_args.args
    kwargs = bot.send_message.await_args.kwargs
    assert args == (123456, FULL_READING_CAPTION)
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Хочу денежный разбор"
    assert parse_qs(urlparse(button.url).query) == {"text": [SALES_MESSAGE_TEXT]}
    bot.send_photo.assert_not_awaited()


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
    assert button.text == "Раскрыть силу - 149₽"
    assert button.callback_data == "buy:profile-1"
    store.mark_strength_offer_sent.assert_awaited_once_with("offer-1", 42)


@pytest.mark.asyncio
async def test_legacy_feedback_item_is_completed_without_user_message() -> None:
    bot, store, worker = _worker_context("feedback")

    await worker.deliver("order-1")

    bot.send_photo.assert_not_awaited()
    store.mark_delivery_item.assert_awaited_once_with(1, status=DeliveryStatus.SENT)
