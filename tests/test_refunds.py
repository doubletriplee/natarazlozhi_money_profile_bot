from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from money_profile_bot.config import Settings
from money_profile_bot.crypto import CryptoBox
from money_profile_bot.database import Database
from money_profile_bot.models import Order, OrderStatus, Payment, Profile, ProfileStatus, User
from money_profile_bot.services.robokassa import (
    OperationState,
    RobokassaClient,
    RobokassaError,
    RobokassaTransportError,
)
from money_profile_bot.services.store import Store


@pytest_asyncio.fixture
async def refund_store(tmp_path: Path) -> AsyncIterator[tuple[Store, Database, AsyncMock, str]]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'refunds.sqlite3').as_posix()}")
    await database.initialize()
    settings = Settings(_env_file=None)
    crypto = CryptoBox(settings.app_encryption_key, settings.lookup_hmac_key)
    robokassa = AsyncMock(spec=RobokassaClient)
    robokassa.operation_state.return_value = OperationState(100, "operation-key", 14900, "BankCard")
    robokassa.refund.return_value = "refund-request-1"
    service = Store(database.sessions, crypto, cast(RobokassaClient, robokassa))

    user = User(
        telegram_id_hash=crypto.lookup("10001", context="telegram-user"),
        telegram_id_encrypted=crypto.encrypt("10001", context="user.telegram_id:user-1"),
    )
    user.id = "user-1"
    profile = Profile(
        id="profile-1",
        user_id=user.id,
        status=ProfileStatus.PAID,
        birth_data_encrypted="encrypted-birth",
        result_encrypted="encrypted-result",
        locked=True,
    )
    order = Order(
        id="order-1",
        code="MP-REFUND1",
        user_id=user.id,
        profile_id=profile.id,
        amount_minor=14900,
        provider="robokassa",
        provider_invoice_id=12345,
        receipt_email_encrypted="encrypted-email",
        status=OrderStatus.PAID,
    )
    payment = Payment(
        id="payment-1",
        order_id=order.id,
        amount_minor=14900,
        currency="RUB",
    )
    async with database.sessions() as session, session.begin():
        session.add(user)
        await session.flush()
        session.add(profile)
        await session.flush()
        session.add(order)
        await session.flush()
        session.add(payment)

    try:
        yield service, database, robokassa, order.code
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_refund_confirmation_can_be_consumed_only_once(
    refund_store: tuple[Store, Database, AsyncMock, str],
) -> None:
    service, database, robokassa, order_code = refund_store
    token = await service.prepare_refund(order_code)

    results = await asyncio.gather(
        service.execute_refund(order_code, token),
        service.execute_refund(order_code, token),
        return_exceptions=True,
    )

    assert results.count("refund-request-1") == 1
    failures = [item for item in results if isinstance(item, Exception)]
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    robokassa.operation_state.assert_awaited_once_with(12345)
    robokassa.refund.assert_awaited_once()
    async with database.sessions() as session:
        order = await session.scalar(select(Order).where(Order.code == order_code))
        payment = await session.scalar(select(Payment).where(Payment.order_id == "order-1"))
    assert order is not None
    assert order.refund_confirmation_hash is None
    assert order.refund_confirmation_expires_at is None
    assert payment is not None
    assert payment.refund_status == "processing"
    assert payment.refund_request_id == "refund-request-1"


@pytest.mark.asyncio
async def test_uncertain_refund_response_blocks_automatic_retry(
    refund_store: tuple[Store, Database, AsyncMock, str],
) -> None:
    service, database, robokassa, order_code = refund_store
    token = await service.prepare_refund(order_code)
    robokassa.refund.side_effect = RobokassaTransportError("timeout")

    with pytest.raises(RobokassaError, match="outcome is uncertain"):
        await service.execute_refund(order_code, token)

    async with database.sessions() as session:
        payment = await session.scalar(select(Payment).where(Payment.order_id == "order-1"))
    assert payment is not None
    assert payment.refund_status == "uncertain"
    with pytest.raises(ValueError, match="requires review"):
        await service.prepare_refund(order_code)


@pytest.mark.asyncio
async def test_explicit_refund_rejection_allows_new_confirmation(
    refund_store: tuple[Store, Database, AsyncMock, str],
) -> None:
    service, database, robokassa, order_code = refund_store
    token = await service.prepare_refund(order_code)
    robokassa.refund.side_effect = RobokassaError("refund was rejected")

    with pytest.raises(RobokassaError, match="rejected"):
        await service.execute_refund(order_code, token)

    async with database.sessions() as session:
        payment = await session.scalar(select(Payment).where(Payment.order_id == "order-1"))
    assert payment is not None
    assert payment.refund_status is None
    assert await service.prepare_refund(order_code) != token


@pytest.mark.asyncio
async def test_refund_refresh_continues_after_one_provider_failure(
    refund_store: tuple[Store, Database, AsyncMock, str],
) -> None:
    service, database, robokassa, _ = refund_store
    async with database.sessions() as session, session.begin():
        first = await session.get(Payment, "payment-1")
        assert first is not None
        first.refund_request_id = "refund-request-1"
        first.refund_status = "processing"
        session.add(
            Order(
                id="order-2",
                code="MP-REFUND2",
                user_id="user-1",
                profile_id="profile-1",
                amount_minor=14900,
                provider="robokassa",
                provider_invoice_id=12346,
                receipt_email_encrypted="encrypted-email",
                status=OrderStatus.PAID,
            )
        )
        await session.flush()
        session.add(
            Payment(
                id="payment-2",
                order_id="order-2",
                amount_minor=14900,
                currency="RUB",
                refund_request_id="refund-request-2",
                refund_status="processing",
            )
        )
    robokassa.refund_state.side_effect = [
        RobokassaTransportError("temporary failure"),
        "finished",
    ]

    assert await service.refresh_refunds() == 1

    async with database.sessions() as session:
        first = await session.get(Payment, "payment-1")
        second = await session.get(Payment, "payment-2")
        second_order = await session.get(Order, "order-2")
    assert first is not None and first.refund_status == "processing"
    assert second is not None and second.refund_status == "finished"
    assert second_order is not None and second_order.status == OrderStatus.REFUNDED
