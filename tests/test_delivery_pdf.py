from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from money_profile_bot.models import DeliveryStatus
from money_profile_bot.services.delivery import DeliveryWorker


@pytest.mark.asyncio
async def test_pdf_is_sent_without_caption(tmp_path: Path) -> None:
    bot = AsyncMock()
    bot.send_document.return_value = SimpleNamespace(message_id=73)
    store = AsyncMock()
    store.delivery_context.return_value = (
        SimpleNamespace(profile_id="profile-1"),
        123456,
        SimpleNamespace(name="Наталья"),
        SimpleNamespace(money_type="Навигатор"),
        [SimpleNamespace(id=1, kind="pdf", status=DeliveryStatus.PENDING)],
    )
    worker = DeliveryWorker(bot, store, AsyncMock(), tmp_path)
    worker._ensure_pdf = AsyncMock(return_value=tmp_path / "profile.pdf")  # type: ignore[method-assign]

    await worker.deliver("order-1")

    bot.send_document.assert_awaited_once()
    assert "caption" not in bot.send_document.await_args.kwargs
