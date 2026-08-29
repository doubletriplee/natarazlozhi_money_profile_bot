from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from money_profile_bot.models import DeliveryStatus
from money_profile_bot.services.avatar import (
    FULL_READING_CAPTION,
    AvatarAssets,
    avatar_paid_caption,
    sales_telegram_url,
)
from money_profile_bot.services.store import Store

logger = logging.getLogger(__name__)


class DeliveryWorker:
    def __init__(
        self,
        bot: Bot,
        store: Store,
        avatars: AvatarAssets,
        *,
        sales_telegram_username: str,
    ) -> None:
        self.bot = bot
        self.store = store
        self.avatars = avatars
        self.sales_telegram_url = sales_telegram_url(sales_telegram_username)
        self._wake = asyncio.Event()
        self._stopping = False

    def notify(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while not self._stopping:
            for order_id in await self.store.pending_order_ids():
                await self.deliver(order_id)
            self._wake.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=10)

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def deliver(self, order_id: str) -> None:
        try:
            order, telegram_id, _birth, result, items = await self.store.delivery_context(order_id)
        except Exception:
            logger.exception("delivery context failed", extra={"order_id": order_id})
            return
        for item in items:
            if item.status in (DeliveryStatus.SCHEDULED, DeliveryStatus.SENT):
                continue
            try:
                if item.kind == "avatar_result":
                    sent = await self.bot.send_photo(
                        telegram_id,
                        FSInputFile(self.avatars.free_image(result.money_type)),
                        caption=avatar_paid_caption(result.money_type),
                    )
                elif item.kind == "full_reading_offer":
                    sent = await self.bot.send_photo(
                        telegram_id,
                        FSInputFile(self.avatars.full_reading_offer_image()),
                        caption=FULL_READING_CAPTION,
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="Хочу денежный разбор",
                                        url=self.sales_telegram_url,
                                    )
                                ]
                            ]
                        ),
                    )
                elif item.kind in {"pdf", "feedback", "card"} or item.kind.startswith("message:"):
                    await self.store.mark_delivery_item(item.id, status=DeliveryStatus.SENT)
                    continue
                else:
                    raise ValueError("unknown delivery item kind")
            except (TelegramAPIError, OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "delivery item failed",
                    extra={"order_id": order_id, "kind": item.kind, "error": type(exc).__name__},
                )
                await self.store.mark_delivery_item(
                    item.id, status=DeliveryStatus.FAILED, error_code=type(exc).__name__
                )
                return
            await self.store.mark_delivery_item(
                item.id, status=DeliveryStatus.SENT, message_id=sent.message_id
            )
        await self.store.complete_delivery_if_ready(order.id)

    async def send_copy(self, order_id: str) -> None:
        _, telegram_id, _birth, result, _ = await self.store.delivery_context(order_id)
        await self.bot.send_photo(
            telegram_id,
            FSInputFile(self.avatars.free_image(result.money_type)),
            caption=avatar_paid_caption(result.money_type),
        )
