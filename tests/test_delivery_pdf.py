from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from money_profile_bot.models import DeliveryStatus
from money_profile_bot.services.avatar import (
    FULL_READING_CAPTION,
    SALES_MESSAGE_TEXT,
    AvatarAssets,
    avatar_paid_caption,
)
from money_profile_bot.services.delivery import DeliveryWorker

ASSET_DIRECTORY = Path("assets/avatars")


def _worker_context(kind: str) -> tuple[AsyncMock, AsyncMock, DeliveryWorker]:
    bot = AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(message_id=42)
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
    )
    return bot, store, worker


@pytest.mark.asyncio
async def test_paid_avatar_is_sent_as_photo_with_complete_text() -> None:
    bot, store, worker = _worker_context("avatar_result")

    await worker.deliver("order-1")

    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["caption"] == avatar_paid_caption("Навигатор")
    store.mark_delivery_item.assert_awaited_once_with(1, status=DeliveryStatus.SENT, message_id=42)
    store.complete_delivery_if_ready.assert_awaited_once_with("order-1")


@pytest.mark.asyncio
async def test_full_reading_offer_uses_exact_caption_and_prefilled_contact_link() -> None:
    bot, _store, worker = _worker_context("full_reading_offer")

    await worker.deliver("order-1")

    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["caption"] == FULL_READING_CAPTION
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Хочу денежный разбор"
    assert parse_qs(urlparse(button.url).query) == {"text": [SALES_MESSAGE_TEXT]}


@pytest.mark.asyncio
async def test_legacy_feedback_item_is_completed_without_user_message() -> None:
    bot, store, worker = _worker_context("feedback")

    await worker.deliver("order-1")

    bot.send_photo.assert_not_awaited()
    store.mark_delivery_item.assert_awaited_once_with(1, status=DeliveryStatus.SENT)
