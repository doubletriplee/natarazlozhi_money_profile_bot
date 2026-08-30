from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from money_profile_bot.config import PaymentMode
from money_profile_bot.models import DeliveryStatus
from money_profile_bot.services.avatar import (
    FULL_READING_CAPTION,
    AvatarAssets,
    avatar_paid_caption,
    sales_telegram_url,
    strength_offer_caption,
)
from money_profile_bot.services.store import Store

logger = logging.getLogger(__name__)

FULL_READING_TRIGGER_TEXT = "Узнать всю денежную картину"


class DeliveryWorker:
    def __init__(
        self,
        bot: Bot,
        store: Store,
        avatars: AvatarAssets,
        *,
        sales_telegram_username: str,
        product_price_rub: Decimal,
        payment_mode: PaymentMode = PaymentMode.FAKE,
        robokassa_test_mode: bool = True,
    ) -> None:
        self.bot = bot
        self.store = store
        self.avatars = avatars
        self.sales_telegram_url = sales_telegram_url(sales_telegram_username)
        self.product_price_rub = product_price_rub
        self.payment_mode = payment_mode
        self.robokassa_test_mode = robokassa_test_mode
        self._wake = asyncio.Event()
        self._delivery_lock = asyncio.Lock()
        self._stopping = False

    def notify(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while not self._stopping:
            for reminder_id in await self.store.pending_form_reminder_ids():
                await self.deliver_form_reminder(reminder_id)
            for profile_id in await self.store.pending_strength_offer_profile_ids():
                await self.deliver_strength_offer(profile_id)
            for order_id in await self.store.pending_order_ids():
                await self.deliver(order_id)
            self._wake.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=10)

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def deliver(self, order_id: str) -> None:
        async with self._delivery_lock:
            await self._deliver(order_id)

    async def _deliver(self, order_id: str) -> None:
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
                        reply_markup=self._full_reading_trigger_keyboard(order.id),
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

    async def deliver_form_reminder(self, reminder_id: str) -> bool:
        async with self._delivery_lock:
            try:
                context = await self.store.form_reminder_context(reminder_id)
            except Exception:
                logger.exception("form reminder context failed", extra={"reminder_id": reminder_id})
                return False
            if context is None:
                return False
            reply_markup = (
                InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text=text, callback_data=callback_data)
                            for text, callback_data in row
                        ]
                        for row in context.buttons
                    ]
                )
                if context.buttons
                else None
            )
            try:
                sent = await self.bot.send_message(
                    context.telegram_id,
                    context.text,
                    reply_markup=reply_markup,
                )
            except (TelegramAPIError, OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "form reminder delivery failed",
                    extra={"reminder_id": reminder_id, "error": type(exc).__name__},
                )
                await self.store.mark_form_reminder_failed(
                    reminder_id,
                    type(exc).__name__,
                    context.payload_token,
                )
                return False
            await self.store.mark_form_reminder_sent(
                reminder_id,
                sent.message_id,
                context.payload_token,
            )
            return True

    async def deliver_strength_offer(
        self,
        profile_id: str,
        *,
        telegram_id: int | None = None,
        force: bool = False,
    ) -> bool:
        async with self._delivery_lock:
            try:
                context = await self.store.strength_offer_context(
                    profile_id,
                    telegram_id=telegram_id,
                    force=force,
                )
            except Exception:
                logger.exception("strength offer context failed", extra={"profile_id": profile_id})
                return False
            if context is None:
                return False
            try:
                sent = await self.bot.send_photo(
                    context.telegram_id,
                    FSInputFile(self.avatars.offer_image(context.money_type)),
                    caption=strength_offer_caption(
                        robokassa=self.payment_mode is PaymentMode.ROBOKASSA,
                        test_mode=self.robokassa_test_mode,
                    ),
                    reply_markup=self._strength_offer_keyboard(context.profile_id),
                )
            except (TelegramAPIError, OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "strength offer delivery failed",
                    extra={"profile_id": profile_id, "error": type(exc).__name__},
                )
                await self.store.mark_strength_offer_failed(context.offer_id, type(exc).__name__)
                return False
            await self.store.mark_strength_offer_sent(context.offer_id, sent.message_id)
            return True

    def _strength_offer_keyboard(self, profile_id: str) -> InlineKeyboardMarkup:
        test_suffix = (
            " · тест" if self.payment_mode is PaymentMode.FAKE or self.robokassa_test_mode else ""
        )
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Раскрыть силу — {self.product_price_rub:.0f}₽{test_suffix}",
                        callback_data=f"buy:{profile_id}",
                    )
                ]
            ]
        )

    @staticmethod
    def _full_reading_trigger_keyboard(order_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=FULL_READING_TRIGGER_TEXT,
                        callback_data=f"full:{order_id}",
                    )
                ]
            ]
        )

    async def send_copy(self, order_id: str) -> None:
        order, telegram_id, _birth, result, _ = await self.store.delivery_context(order_id)
        await self.bot.send_photo(
            telegram_id,
            FSInputFile(self.avatars.free_image(result.money_type)),
            caption=avatar_paid_caption(result.money_type),
            reply_markup=self._full_reading_trigger_keyboard(order.id),
        )
