from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from pydantic import ValidationError
from sqlalchemy import func, select, update

from money_profile_bot.config import Settings
from money_profile_bot.crypto import CryptoBox
from money_profile_bot.database import Database
from money_profile_bot.models import DeliveryStatus, Order, PaymentNotification, Profile, utcnow
from money_profile_bot.services.payment_notifications import PaymentNotifications
from money_profile_bot.services.robokassa import RobokassaClient
from money_profile_bot.services.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    settings = Settings(_env_file=None)
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'notifications.sqlite3').as_posix()}")
    await database.initialize()
    value = Store(
        database.sessions,
        CryptoBox(settings.app_encryption_key, settings.lookup_hmac_key),
        cast(RobokassaClient, AsyncMock()),
        analytics_mode="live",
        payment_notification_ids=frozenset({101, 202}),
    )
    yield value
    await database.close()


async def order_for(store: Store, *, mode: str = "live", provider: str = "robokassa") -> Order:
    user = await store.ensure_user(10001)
    async with store.sessions() as session, session.begin():
        profile = Profile(user_id=user.id, birth_data_encrypted="private", status="calculated")
        session.add(profile)
        await session.flush()
        number = 1 + (await session.scalar(select(func.count()).select_from(Order)))
        order = Order(
            code=f"MP-TEST{number}",
            user_id=user.id,
            profile_id=profile.id,
            amount_minor=14900 if provider == "robokassa" else 0,
            provider=provider,
            analytics_mode=mode,
            provider_invoice_id=number,
            receipt_email_encrypted="private-email",
            status="invoice_created",
        )
        session.add(order)
        await session.flush()
        return order


async def pay(store: Store, order: Order) -> None:
    await store.accept_payment_callback(
        invoice_id=order.provider_invoice_id, amount_minor=order.amount_minor, email=None
    )


async def notices(store: Store) -> list[PaymentNotification]:
    async with store.sessions() as session:
        return list((await session.scalars(select(PaymentNotification))).all())


async def make_due(store: Store) -> None:
    async with store.sessions() as session, session.begin():
        await session.execute(
            update(PaymentNotification).values(available_at=utcnow() - timedelta(seconds=1))
        )


async def test_confirmation_atomically_queues_once_per_recipient(store: Store) -> None:
    order = await order_for(store)
    assert await notices(store) == []
    with pytest.raises(ValueError):
        await store.accept_payment_callback(
            invoice_id=order.provider_invoice_id, amount_minor=1, email=None
        )
    assert await notices(store) == []
    await asyncio.gather(pay(store, order), pay(store, order))
    queued = await notices(store)
    assert len(queued) == 2
    assert len({item.recipient_hash for item in queued}) == 2
    assert all(item.recipient_encrypted not in {"101", "202"} for item in queued)
    assert all(item.order_id == order.id for item in queued)


async def test_queue_rolls_back_if_payment_transaction_fails(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = await order_for(store)
    monkeypatch.setattr(store.analytics, "add", AsyncMock(side_effect=RuntimeError("test failure")))
    with pytest.raises(RuntimeError):
        await pay(store, order)
    assert await notices(store) == []
    async with store.sessions() as session:
        assert (await session.get(Order, order.id)).status == "invoice_created"


@pytest.mark.parametrize(
    "mode,provider", [("test", "robokassa"), ("test", "fake"), ("unknown", "robokassa")]
)
async def test_test_and_unknown_payments_are_silent_by_default(
    store: Store, mode: str, provider: str
) -> None:
    await pay(store, await order_for(store, mode=mode, provider=provider))
    assert await notices(store) == []


async def test_restart_retry_and_repeated_delivery_do_not_duplicate_success(store: Store) -> None:
    await pay(store, await order_for(store))
    restarted = PaymentNotifications(store.sessions, store.crypto, frozenset({101, 202}))
    identifier = (await restarted.pending_ids())[0]
    bot = AsyncMock(spec=Bot)
    bot.send_message.side_effect = TelegramNetworkError(
        method=SendMessage(chat_id=101, text="x"), message="offline"
    )
    await restarted.deliver(identifier, bot)
    assert identifier not in await restarted.pending_ids()
    item = next(item for item in await notices(store) if item.id == identifier)
    assert item.status == DeliveryStatus.PENDING and item.attempts == 1
    await make_due(store)
    bot.send_message.side_effect = None
    bot.send_message.return_value.message_id = 77
    await asyncio.gather(restarted.deliver(identifier, bot), restarted.deliver(identifier, bot))
    assert bot.send_message.await_count == 2  # failed attempt and one successful retry
    sent_text = bot.send_message.call_args.args[1]
    assert "Новая оплата · 149 ₽" in sent_text
    assert "MP-TEST1" in sent_text and "МСК" in sent_text
    assert "10001" not in sent_text and "private" not in sent_text
    item = next(item for item in await notices(store) if item.id == identifier)
    assert item.status == DeliveryStatus.SENT and item.telegram_message_id == 77


async def test_blocked_recipient_does_not_prevent_other_delivery(store: Store) -> None:
    await pay(store, await order_for(store))
    identifiers = await store.notifications.pending_ids()
    bot = AsyncMock(spec=Bot)
    bot.send_message.side_effect = [
        TelegramForbiddenError(method=SendMessage(chat_id=101, text="x"), message="blocked"),
        type("Sent", (), {"message_id": 42})(),
    ]
    for identifier in identifiers:
        await store.notifications.deliver(identifier, bot)
    assert {item.status for item in await notices(store)} == {
        DeliveryStatus.FAILED,
        DeliveryStatus.SENT,
    }
    assert await store.notifications.pending_ids() == []


async def test_telegram_retry_after_is_respected_and_revoked_recipient_is_skipped(
    store: Store,
) -> None:
    await pay(store, await order_for(store))
    identifier = (await store.notifications.pending_ids())[0]
    bot = AsyncMock(spec=Bot)
    bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=101, text="x"), message="rate limit", retry_after=600
    )
    await store.notifications.deliver(identifier, bot)
    item = next(item for item in await notices(store) if item.id == identifier)
    assert item.available_at.replace(tzinfo=utcnow().tzinfo) > utcnow() + timedelta(seconds=590)
    await make_due(store)
    revoked = PaymentNotifications(store.sessions, store.crypto)
    for identifier in await revoked.pending_ids():
        await revoked.deliver(identifier, bot)
    assert bot.send_message.await_count == 1
    assert await notices(store) == []


async def test_test_notice_is_explicit_and_queue_expires(store: Store) -> None:
    store.notifications.include_test = True
    await pay(store, await order_for(store, mode="test", provider="fake"))
    bot = AsyncMock(spec=Bot)
    bot.send_message.return_value.message_id = 1
    await store.notifications.deliver((await store.notifications.pending_ids())[0], bot)
    text = bot.send_message.call_args.args[1]
    assert "Покупки и списания денег не произошло" in text
    assert "Новая оплата" not in text
    async with store.sessions() as session, session.begin():
        await session.execute(
            update(PaymentNotification).values(created_at=utcnow() - timedelta(days=31))
        )
    await store.notifications.cleanup()
    assert await notices(store) == []


def test_recipient_configuration_does_not_grant_admin_access() -> None:
    settings = Settings(
        _env_file=None, admin_telegram_ids="1", payment_notification_telegram_ids="101,202,101"
    )
    assert settings.payment_notification_ids == {101, 202}
    assert settings.admin_ids == {1}
    assert not settings.payment_notifications_include_test
    for value in ("-100", "0", "name", str(2**52)):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, payment_notification_telegram_ids=value)
