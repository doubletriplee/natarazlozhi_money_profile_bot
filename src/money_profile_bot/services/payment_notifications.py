from __future__ import annotations

import asyncio
import html
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from money_profile_bot.crypto import CryptoBox
from money_profile_bot.models import DeliveryStatus, Order, PaymentNotification, utcnow
from money_profile_bot.services.analytics import MSK, as_utc

logger = logging.getLogger(__name__)
RETENTION = timedelta(days=30)


class PaymentNotifications:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        crypto: CryptoBox,
        recipients: frozenset[int] = frozenset(),
        *,
        include_test: bool = False,
    ) -> None:
        self.sessions = sessions
        self.crypto = crypto
        self.recipients = recipients
        self.include_test = include_test
        self._lock = asyncio.Lock()

    @staticmethod
    def is_test(order: Order) -> bool:
        return order.provider == "fake" or order.analytics_mode == "test"

    def allowed(self, order: Order) -> bool:
        return self.include_test if self.is_test(order) else order.analytics_mode == "live"

    async def enqueue(self, session: AsyncSession, order: Order) -> None:
        # Called in the payment transaction, only for the first accepted confirmation.
        if not self.allowed(order):
            return
        for recipient in self.recipients:
            digest = self.crypto.lookup(str(recipient), context="payment-notification-recipient")
            session.add(
                PaymentNotification(
                    order_id=order.id,
                    recipient_hash=digest,
                    recipient_encrypted=self.crypto.encrypt(
                        str(recipient), context=f"payment-notification:{order.id}:{digest}"
                    ),
                )
            )

    async def pending_ids(self, *, limit: int = 10) -> list[str]:
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(PaymentNotification.id)
                        .where(
                            PaymentNotification.status == DeliveryStatus.PENDING,
                            PaymentNotification.available_at <= utcnow(),
                            PaymentNotification.created_at >= utcnow() - RETENTION,
                        )
                        .order_by(PaymentNotification.available_at, PaymentNotification.id)
                        .limit(limit)
                    )
                ).all()
            )

    async def deliver(self, notification_id: str, bot: Bot) -> None:
        async with self._lock:
            async with self.sessions() as session:
                item = await session.get(PaymentNotification, notification_id)
                if (
                    item is None
                    or item.status != DeliveryStatus.PENDING
                    or as_utc(item.available_at) > utcnow()
                    or as_utc(item.created_at) < utcnow() - RETENTION
                ):
                    return
                order = await session.get(Order, item.order_id)
                if order is None:
                    return
                recipient = int(
                    self.crypto.decrypt(
                        item.recipient_encrypted,
                        context=f"payment-notification:{order.id}:{item.recipient_hash}",
                    )
                )
                if recipient not in self.recipients or not self.allowed(order):
                    await session.delete(item)
                    await session.commit()
                    return
                if order.paid_at is None:
                    return
                test = self.is_test(order)
                amount = f"{order.amount_minor // 100:,}".replace(",", " ")
                if order.amount_minor % 100:
                    amount += f",{order.amount_minor % 100:02d}"
                title = (
                    "🧪 Тестовый сценарий завершён\nПокупки и списания денег не произошло."
                    if test
                    else f"💳 <b>Новая оплата · {amount} ₽</b>"
                )
                text = (
                    f"{title}\nРазбор денежного аватара\n"
                    f"Заказ: {html.escape(order.code)}\n"
                    f"{as_utc(order.paid_at).astimezone(MSK):%d.%m.%Y, %H:%M} МСК"
                )
            # Do not keep a DB transaction open while waiting for Telegram.
            error: str | None = None
            permanent = False
            retry_after = 0
            message_id: int | None = None
            try:
                async with asyncio.timeout(10):
                    sent = await bot.send_message(recipient, text, parse_mode="HTML")
                message_id = sent.message_id
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                error, permanent = type(exc).__name__, True
            except TelegramRetryAfter as exc:
                error, retry_after = type(exc).__name__, exc.retry_after
            except (TelegramAPIError, TimeoutError, OSError) as exc:
                error = type(exc).__name__
            async with self.sessions() as session, session.begin():
                item = await session.get(PaymentNotification, notification_id)
                if item is None:
                    return
                item.attempts += 1
                item.last_error_code = error
                if error is None:
                    item.status = DeliveryStatus.SENT
                    item.sent_at = utcnow()
                    item.telegram_message_id = message_id
                elif permanent:
                    item.status = DeliveryStatus.FAILED
                else:
                    delay = max(retry_after, min(3600, 30 * 2 ** min(item.attempts - 1, 7)))
                    item.available_at = utcnow() + timedelta(seconds=delay)
            if error:
                logger.warning("payment notification failed: %s", error)

    async def cleanup(self) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                delete(PaymentNotification).where(
                    PaymentNotification.created_at < utcnow() - RETENTION
                )
            )
