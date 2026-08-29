from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from money_profile_bot.config import Settings
from money_profile_bot.crypto import CryptoBox
from money_profile_bot.database import Database
from money_profile_bot.domain import BirthData
from money_profile_bot.models import (
    AdminIdentity,
    DeliveryItem,
    DeliveryStatus,
    Order,
    OrderStatus,
    Payment,
    ProfileStatus,
)
from money_profile_bot.services.astro import calculate_chart
from money_profile_bot.services.robokassa import Invoice, RobokassaClient
from money_profile_bot.services.rules import generate_profile
from money_profile_bot.services.store import FULL_READING_DELAY, Store


@dataclass
class FakeRobokassa:
    async def create_invoice(
        self, *, invoice_id: int, order_code: str, amount_minor: int, email: str
    ) -> Invoice:
        assert amount_minor > 0
        assert "@" in email
        return Invoice(
            "invoice-uuid-" + order_code, invoice_id, f"https://pay.example/{order_code}"
        )


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[tuple[Store, Database]]:
    settings = Settings(_env_file=None)
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'app.sqlite3').as_posix()}")
    await database.initialize()
    crypto = CryptoBox(settings.app_encryption_key, settings.lookup_hmac_key)
    value = Store(database.sessions, crypto, cast(RobokassaClient, FakeRobokassa()))
    yield value, database
    await database.close()


async def prepared_order(store: Store, birth: BirthData) -> tuple[str, int, str]:
    facts = calculate_chart(birth)
    result = generate_profile(facts)
    profile_id = await store.save_calculation(10001, birth, facts, result)
    link = await store.create_order(
        telegram_id=10001,
        profile_id=profile_id,
        email="buyer@example.ru",
        amount_minor=14900,
    )
    async with store.sessions() as session:
        order = await session.get(Order, link.order_id)
        assert order is not None
        return link.order_id, order.provider_invoice_id, profile_id


@pytest.mark.asyncio
async def test_order_creation_reuses_active_invoice(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, _ = store
    order_id, _, profile_id = await prepared_order(service, birth)
    repeated = await service.create_order(
        telegram_id=10001,
        profile_id=profile_id,
        email="buyer@example.ru",
        amount_minor=14900,
    )
    assert repeated.reused
    assert repeated.order_id == order_id


@pytest.mark.asyncio
async def test_successful_callback_locks_profile_and_builds_delivery_queue(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    order_id, invoice_id, profile_id = await prepared_order(service, birth)
    callback = await service.accept_payment_callback(
        invoice_id=invoice_id, amount_minor=14900, email="buyer@example.ru"
    )
    assert callback.newly_paid
    access = await service.profile_access(10001)
    assert access and access.profile_status == ProfileStatus.PAID
    assert access.order_status == OrderStatus.PAID
    async with database.sessions() as session:
        count = await session.scalar(
            select(func.count()).select_from(DeliveryItem).where(DeliveryItem.order_id == order_id)
        )
        payments = await session.scalar(select(func.count()).select_from(Payment))
    assert count == 3
    assert payments == 1


@pytest.mark.asyncio
async def test_duplicate_payment_callback_is_idempotent(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    _, invoice_id, _ = await prepared_order(service, birth)
    first = await service.accept_payment_callback(
        invoice_id=invoice_id, amount_minor=14900, email=None
    )
    second = await service.accept_payment_callback(
        invoice_id=invoice_id, amount_minor=14900, email=None
    )
    assert first.newly_paid and not second.newly_paid
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1


@pytest.mark.asyncio
async def test_concurrent_payment_callbacks_are_serialized(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    _, invoice_id, _ = await prepared_order(service, birth)
    results = await asyncio.gather(
        service.accept_payment_callback(invoice_id=invoice_id, amount_minor=14900, email=None),
        service.accept_payment_callback(invoice_id=invoice_id, amount_minor=14900, email=None),
    )
    assert sorted(result.newly_paid for result in results) == [False, True]
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1


@pytest.mark.asyncio
async def test_callback_rejects_wrong_amount(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, _ = store
    _, invoice_id, _ = await prepared_order(service, birth)
    with pytest.raises(ValueError, match="amount"):
        await service.accept_payment_callback(invoice_id=invoice_id, amount_minor=1, email=None)


@pytest.mark.asyncio
async def test_callback_rejects_unknown_invoice(store: tuple[Store, Database]) -> None:
    service, _ = store
    with pytest.raises(LookupError):
        await service.accept_payment_callback(invoice_id=999999, amount_minor=14900, email=None)


@pytest.mark.asyncio
async def test_delete_removes_personal_profile_but_keeps_payment_journal(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    _, invoice_id, _ = await prepared_order(service, birth)
    await service.accept_payment_callback(invoice_id=invoice_id, amount_minor=14900, email=None)
    assert await service.delete_personal_data(10001) == []
    assert await service.profile_access(10001) is None
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1


@pytest.mark.asyncio
async def test_fake_payment_has_zero_revenue_and_builds_delivery_queue(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    facts = calculate_chart(birth)
    result = generate_profile(facts)
    profile_id = await service.save_calculation(10001, birth, facts, result)

    link = await service.create_fake_paid_order(telegram_id=10001, profile_id=profile_id)
    repeated = await service.create_fake_paid_order(telegram_id=10001, profile_id=profile_id)

    assert not link.reused
    assert repeated.reused
    assert repeated.order_id == link.order_id
    async with database.sessions() as session:
        order = await session.get(Order, link.order_id)
        assert order is not None
        assert order.provider == "fake"
        assert order.amount_minor == 0
        assert order.status == OrderStatus.PAID
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(DeliveryItem)
                .where(DeliveryItem.order_id == link.order_id)
            )
            == 3
        )


@pytest.mark.asyncio
async def test_full_reading_offer_is_scheduled_three_minutes_after_pdf(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    order_id, invoice_id, _ = await prepared_order(service, birth)
    await service.accept_payment_callback(
        invoice_id=invoice_id, amount_minor=14900, email="buyer@example.ru"
    )

    async with database.sessions() as session:
        items = list(
            (
                await session.scalars(
                    select(DeliveryItem)
                    .where(DeliveryItem.order_id == order_id)
                    .order_by(DeliveryItem.sequence)
                )
            ).all()
        )
    pdf, feedback, followup = items
    assert [item.kind for item in items] == ["pdf", "feedback", "full_reading_offer"]
    assert followup.status == DeliveryStatus.SCHEDULED
    assert followup.available_at is None

    await service.mark_delivery_item(pdf.id, status=DeliveryStatus.SENT, message_id=10)
    await service.mark_delivery_item(feedback.id, status=DeliveryStatus.SENT, message_id=11)
    assert await service.complete_delivery_if_ready(order_id)

    async with database.sessions() as session:
        order = await session.get(Order, order_id)
        sent_pdf = await session.get(DeliveryItem, pdf.id)
        scheduled = await session.get(DeliveryItem, followup.id)
        assert order is not None and order.status == OrderStatus.DELIVERED
        assert sent_pdf is not None and sent_pdf.sent_at is not None
        assert scheduled is not None
        assert scheduled.status == DeliveryStatus.PENDING
        assert scheduled.available_at is not None
        assert scheduled.sent_at is None
        delay = scheduled.available_at.replace(tzinfo=None) - sent_pdf.sent_at.replace(tzinfo=None)
        assert delay == FULL_READING_DELAY

    assert order_id not in await service.pending_order_ids()


@pytest.mark.asyncio
async def test_first_start_admin_claim_is_atomic(store: tuple[Store, Database]) -> None:
    service, database = store
    claims = await asyncio.gather(
        service.claim_admin_if_unset(10001),
        service.claim_admin_if_unset(20002),
    )

    assert sum(claim.newly_claimed for claim in claims) == 1
    winner = 10001 if claims[0].is_admin else 20002
    loser = 20002 if winner == 10001 else 10001
    assert await service.is_admin(winner, frozenset())
    assert not await service.is_admin(loser, frozenset())
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdminIdentity)) == 1
