from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from money_profile_bot.models import DeliveryStatus
from money_profile_bot.services.delivery import DeliveryWorker


@pytest.mark.asyncio
async def test_legacy_feedback_item_is_completed_without_user_message(tmp_path: Path) -> None:
    bot = AsyncMock()
    store = AsyncMock()
    store.delivery_context.return_value = (
        SimpleNamespace(profile_id="profile-1"),
        123456,
        SimpleNamespace(name="Наталья"),
        SimpleNamespace(money_type="Навигатор"),
        [SimpleNamespace(id=1, kind="feedback", status=DeliveryStatus.PENDING)],
    )
    worker = DeliveryWorker(bot, store, AsyncMock(), tmp_path)

    await worker.deliver("order-1")

    bot.send_message.assert_not_awaited()
    store.mark_delivery_item.assert_awaited_once_with(1, status=DeliveryStatus.SENT)
