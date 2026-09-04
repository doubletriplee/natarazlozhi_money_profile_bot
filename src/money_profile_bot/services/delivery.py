from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from decimal import Decimal
from time import monotonic

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from money_profile_bot.config import PaymentMode
from money_profile_bot.models import DeliveryStatus, OrderStatus
from money_profile_bot.services.avatar import (
    FULL_READING_CAPTION,
    AvatarAssets,
    avatar_paid_caption_pages,
    sales_telegram_url,
    strength_offer_caption_pages,
)
from money_profile_bot.services.store import Store

logger = logging.getLogger(__name__)

FULL_READING_TRIGGER_TEXT = "Узнать всю денежную картину"
NEW_PROFILE_TRIGGER_TEXT = "Рассчитать другой аватар"
DELIVERY_ORDER_BATCH_SIZE = 50
DELIVERY_BACKGROUND_BATCH_SIZE = 10
DELIVERY_MAX_CONSECUTIVE_FAILURES = 3
DELIVERY_HEALTH_TIMEOUT_SECONDS = 30.0
AVATAR_PAGE_CALLBACK_PREFIX = "avatar_page"
STRENGTH_PAGE_CALLBACK_PREFIX = "strength_page"


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
        self._running = False
        self._heartbeat_at = 0.0
        self._consecutive_cycle_failures = 0

    def notify(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        self._running = True
        try:
            while not self._stopping:
                self._wake.clear()
                self._heartbeat()
                try:
                    await self._deliver_pending_cycle()
                except Exception:
                    self._consecutive_cycle_failures += 1
                    logger.exception(
                        "delivery worker cycle failed",
                        extra={"failures": self._consecutive_cycle_failures},
                    )
                    if self._consecutive_cycle_failures >= DELIVERY_MAX_CONSECUTIVE_FAILURES:
                        raise
                    await asyncio.sleep(1)
                    continue
                self._consecutive_cycle_failures = 0
                self._heartbeat()
                if self._wake.is_set():
                    continue
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=10)
        finally:
            self._running = False

    async def _deliver_pending_cycle(self) -> None:
        for order_id in await self.store.pending_order_ids(limit=DELIVERY_ORDER_BATCH_SIZE):
            self._heartbeat()
            await self.deliver(order_id)
        for reminder_id in await self.store.pending_form_reminder_ids(
            limit=DELIVERY_BACKGROUND_BATCH_SIZE
        ):
            self._heartbeat()
            await self.deliver_form_reminder(reminder_id)
        for profile_id in await self.store.pending_strength_offer_profile_ids(
            limit=DELIVERY_BACKGROUND_BATCH_SIZE
        ):
            self._heartbeat()
            await self.deliver_strength_offer(profile_id)

    def _heartbeat(self) -> None:
        self._heartbeat_at = monotonic()

    def is_healthy(self) -> bool:
        return (
            self._running
            and self._consecutive_cycle_failures == 0
            and monotonic() - self._heartbeat_at <= DELIVERY_HEALTH_TIMEOUT_SECONDS
        )

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
                    sent = await self._send_paid_avatar(
                        telegram_id,
                        money_type=result.money_type,
                        order_id=order.id,
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
            pages = strength_offer_caption_pages(
                robokassa=self.payment_mode is PaymentMode.ROBOKASSA,
                test_mode=self.robokassa_test_mode,
            )
            try:
                sent = await self.bot.send_photo(
                    context.telegram_id,
                    FSInputFile(self.avatars.offer_image(context.money_type)),
                    caption=self._strength_offer_page_caption(pages, 0),
                    reply_markup=self._strength_offer_page_keyboard(
                        context.profile_id,
                        page_index=0,
                        page_count=len(pages),
                    ),
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

    def _strength_offer_page_keyboard(
        self, profile_id: str, *, page_index: int, page_count: int
    ) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        navigation: list[InlineKeyboardButton] = []
        if page_index > 0:
            navigation.append(
                InlineKeyboardButton(
                    text=f"← {page_index} из {page_count}",
                    callback_data=(
                        f"{STRENGTH_PAGE_CALLBACK_PREFIX}:{profile_id}:{page_index - 1}"
                    ),
                )
            )
        if page_index + 1 < page_count:
            navigation.append(
                InlineKeyboardButton(
                    text=f"Дальше · {page_index + 2} из {page_count} →",
                    callback_data=(
                        f"{STRENGTH_PAGE_CALLBACK_PREFIX}:{profile_id}:{page_index + 1}"
                    ),
                )
            )
        if navigation:
            rows.append(navigation)
        if page_index == page_count - 1:
            rows.extend(self._strength_offer_keyboard(profile_id).inline_keyboard)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _strength_offer_page_caption(pages: tuple[str, ...], page_index: int) -> str:
        return f"<b>Часть {page_index + 1} из {len(pages)}</b>\n\n{pages[page_index]}"

    async def show_strength_offer_page(
        self,
        message: Message,
        *,
        telegram_id: int,
        profile_id: str,
        page_index: int,
    ) -> bool:
        try:
            access = await self.store.profile_access(telegram_id, profile_id=profile_id)
        except Exception as exc:
            logger.warning(
                "strength offer page context unavailable",
                extra={"profile_id": profile_id, "error": type(exc).__name__},
            )
            return False
        if not access or access.profile_id != profile_id:
            return False
        pages = strength_offer_caption_pages(
            robokassa=self.payment_mode is PaymentMode.ROBOKASSA,
            test_mode=self.robokassa_test_mode,
        )
        if not 0 <= page_index < len(pages):
            return False
        try:
            await message.edit_caption(
                caption=self._strength_offer_page_caption(pages, page_index),
                reply_markup=self._strength_offer_page_keyboard(
                    profile_id,
                    page_index=page_index,
                    page_count=len(pages),
                ),
            )
        except (TelegramAPIError, OSError, RuntimeError, ValueError):
            logger.warning(
                "strength offer page edit failed",
                extra={"profile_id": profile_id, "page_index": page_index},
            )
            return False
        return True

    @staticmethod
    def _full_reading_trigger_keyboard(order_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=FULL_READING_TRIGGER_TEXT,
                        callback_data=f"full:{order_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=NEW_PROFILE_TRIGGER_TEXT,
                        callback_data="profile:new",
                    )
                ],
            ]
        )

    def _paid_avatar_keyboard(
        self, order_id: str, *, page_index: int, page_count: int
    ) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        navigation: list[InlineKeyboardButton] = []
        if page_index > 0:
            navigation.append(
                InlineKeyboardButton(
                    text=f"← {page_index} из {page_count}",
                    callback_data=f"{AVATAR_PAGE_CALLBACK_PREFIX}:{order_id}:{page_index - 1}",
                )
            )
        if page_index + 1 < page_count:
            navigation.append(
                InlineKeyboardButton(
                    text=f"Дальше · {page_index + 2} из {page_count} →",
                    callback_data=f"{AVATAR_PAGE_CALLBACK_PREFIX}:{order_id}:{page_index + 1}",
                )
            )
        if navigation:
            rows.append(navigation)
        if page_index == page_count - 1:
            rows.extend(self._full_reading_trigger_keyboard(order_id).inline_keyboard)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _paid_avatar_page_caption(pages: tuple[str, ...], page_index: int) -> str:
        return f"<b>Часть {page_index + 1} из {len(pages)}</b>\n\n{pages[page_index]}"

    async def _send_paid_avatar(
        self, telegram_id: int, *, money_type: str, order_id: str
    ) -> Message:
        pages = avatar_paid_caption_pages(money_type)
        return await self.bot.send_photo(
            telegram_id,
            FSInputFile(self.avatars.free_image(money_type)),
            caption=self._paid_avatar_page_caption(pages, 0),
            reply_markup=self._paid_avatar_keyboard(
                order_id,
                page_index=0,
                page_count=len(pages),
            ),
        )

    async def show_paid_avatar_page(
        self,
        message: Message,
        *,
        telegram_id: int,
        order_id: str,
        page_index: int,
    ) -> bool:
        try:
            order, owner_telegram_id, _birth, result, _items = await self.store.delivery_context(
                order_id
            )
        except Exception as exc:
            logger.warning(
                "paid avatar page context unavailable",
                extra={"order_id": order_id, "error": type(exc).__name__},
            )
            return False
        if owner_telegram_id != telegram_id or order.status not in (
            OrderStatus.PAID,
            OrderStatus.DELIVERED,
        ):
            return False
        pages = avatar_paid_caption_pages(result.money_type)
        if not 0 <= page_index < len(pages):
            return False
        try:
            await message.edit_caption(
                caption=self._paid_avatar_page_caption(pages, page_index),
                reply_markup=self._paid_avatar_keyboard(
                    order_id,
                    page_index=page_index,
                    page_count=len(pages),
                ),
            )
        except (TelegramAPIError, OSError, RuntimeError, ValueError):
            logger.warning(
                "paid avatar page edit failed",
                extra={"order_id": order_id, "page_index": page_index},
            )
            return False
        return True

    async def send_copy(self, order_id: str) -> None:
        order, telegram_id, _birth, result, _ = await self.store.delivery_context(order_id)
        await self._send_paid_avatar(
            telegram_id,
            money_type=result.money_type,
            order_id=order.id,
        )
