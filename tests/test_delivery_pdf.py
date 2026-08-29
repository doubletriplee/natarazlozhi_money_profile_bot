from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from money_profile_bot.models import DeliveryStatus
from money_profile_bot.services.delivery import FULL_READING_CAPTION, DeliveryWorker


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


@pytest.mark.asyncio
async def test_full_reading_offer_uses_image_price_and_contact_button(tmp_path: Path) -> None:
    image = tmp_path / "full-reading.png"
    image.write_bytes(b"image")
    bot = AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(message_id=74)
    store = AsyncMock()
    store.delivery_context.return_value = (
        SimpleNamespace(profile_id="profile-1"),
        123456,
        SimpleNamespace(name="Наталья"),
        SimpleNamespace(money_type="Навигатор"),
        [
            SimpleNamespace(
                id=2,
                kind="full_reading_offer",
                status=DeliveryStatus.PENDING,
            )
        ],
    )
    worker = DeliveryWorker(
        bot,
        store,
        AsyncMock(),
        tmp_path,
        full_reading_offer_image=image,
        full_reading_contact_url="https://t.me/simnatali",
    )

    await worker.deliver("order-1")

    bot.send_photo.assert_awaited_once()
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["caption"] == FULL_READING_CAPTION
    assert "<s>1 990 ₽</s>" in kwargs["caption"]
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Получить полный разбор — 990 ₽"
    assert button.url == "https://t.me/simnatali"
