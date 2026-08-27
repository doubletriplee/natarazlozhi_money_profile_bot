from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from money_profile_bot.models import DeliveryStatus
from money_profile_bot.services.card import CardRenderer
from money_profile_bot.services.store import Store

logger = logging.getLogger(__name__)


class DeliveryWorker:
    def __init__(self, bot: Bot, store: Store, renderer: CardRenderer, cards_dir: Path) -> None:
        self.bot = bot
        self.store = store
        self.renderer = renderer
        self.cards_dir = cards_dir
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
            order, telegram_id, birth, result, items = await self.store.delivery_context(order_id)
        except Exception:
            logger.exception("delivery context failed", extra={"order_id": order_id})
            return
        for item in items:
            if item.status == DeliveryStatus.SENT:
                continue
            try:
                if item.kind.startswith("message:"):
                    index = int(item.kind.split(":", 1)[1])
                    sent = await self.bot.send_message(telegram_id, result.messages[index])
                elif item.kind == "card":
                    card_path = self.cards_dir / f"{order.profile_id}.png"
                    if not card_path.exists():
                        self.renderer.render(
                            name=birth.name,
                            money_type=result.money_type,
                            strength=result.strength,
                            destination=card_path,
                        )
                        await self.store.save_card_path(order.profile_id, str(card_path))
                    sent = await self.bot.send_photo(
                        telegram_id,
                        FSInputFile(card_path),
                        caption=result.disclaimer,
                    )
                elif item.kind == "feedback":
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=str(rating),
                                    callback_data=f"rating:{order.profile_id}:{rating}",
                                )
                                for rating in range(1, 6)
                            ]
                        ]
                    )
                    sent = await self.bot.send_message(
                        telegram_id,
                        "Насколько разбор оказался полезным? Выберите оценку от 1 до 5. "
                        "Карточку и сообщения можно переслать обычной кнопкой Telegram.",
                        reply_markup=keyboard,
                    )
                else:
                    raise ValueError("unknown delivery item kind")
            except (TelegramAPIError, OSError, ValueError) as exc:
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
        await self.store.complete_delivery_if_ready(order_id)

    async def send_copy(self, order_id: str) -> None:
        order, telegram_id, birth, result, _ = await self.store.delivery_context(order_id)
        for message in result.messages:
            await self.bot.send_message(telegram_id, message)
        card_path = self.cards_dir / f"{order.profile_id}.png"
        if not card_path.exists():
            self.renderer.render(
                name=birth.name,
                money_type=result.money_type,
                strength=result.strength,
                destination=card_path,
            )
            await self.store.save_card_path(order.profile_id, str(card_path))
        await self.bot.send_photo(telegram_id, FSInputFile(card_path), caption=result.disclaimer)
