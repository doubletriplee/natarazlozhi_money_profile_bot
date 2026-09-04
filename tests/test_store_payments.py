from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import pytest_asyncio
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import func, select, update

from money_profile_bot.bot.router import CITY_PROMPT, form_reminder_payload
from money_profile_bot.bot.states import ProfileForm
from money_profile_bot.bot.storage import EncryptedDatabaseStorage
from money_profile_bot.config import Settings
from money_profile_bot.crypto import CryptoBox
from money_profile_bot.database import Database
from money_profile_bot.domain import BirthData
from money_profile_bot.models import (
    AdminIdentity,
    Consent,
    DeliveryItem,
    DeliveryStatus,
    FormReminder,
    FsmRecord,
    Order,
    OrderStatus,
    Payment,
    ProfileStatus,
    StrengthOffer,
)
from money_profile_bot.services.astro import calculate_chart
from money_profile_bot.services.robokassa import Invoice, RobokassaClient, RobokassaError
from money_profile_bot.services.rules import generate_profile
from money_profile_bot.services.store import (
    FORM_REMINDER_DELAY,
    FULL_READING_DELAY,
    STRENGTH_OFFER_DELAY,
    Store,
)


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


@dataclass
class FailingRobokassa:
    async def create_invoice(
        self, *, invoice_id: int, order_code: str, amount_minor: int, email: str
    ) -> Invoice:
        raise RobokassaError("test invoice failure")


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
async def test_consent_does_not_record_unasked_adult_confirmation(
    store: tuple[Store, Database],
) -> None:
    service, database = store

    await service.save_consent(10001, "legal-v1")

    async with database.sessions() as session:
        consent = await session.scalar(select(Consent))
    assert consent is not None
    assert consent.documents_version == "legal-v1"
    assert consent.adult_confirmed is False


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
async def test_concurrent_order_creation_reuses_one_invoice(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    facts = calculate_chart(birth)
    result = generate_profile(facts)
    profile_id = await service.save_calculation(10001, birth, facts, result)

    first, second = await asyncio.gather(
        service.create_order(
            telegram_id=10001,
            profile_id=profile_id,
            email="buyer@example.ru",
            amount_minor=14900,
        ),
        service.create_order(
            telegram_id=10001,
            profile_id=profile_id,
            email="buyer@example.ru",
            amount_minor=14900,
        ),
    )

    assert first.order_id == second.order_id
    assert sorted((first.reused, second.reused)) == [False, True]
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 1


@pytest.mark.asyncio
async def test_failed_invoice_creation_marks_order_failed(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    service.robokassa = cast(RobokassaClient, FailingRobokassa())
    facts = calculate_chart(birth)
    result = generate_profile(facts)
    profile_id = await service.save_calculation(10001, birth, facts, result)

    with pytest.raises(RobokassaError, match="invoice failure"):
        await service.create_order(
            telegram_id=10001,
            profile_id=profile_id,
            email="buyer@example.ru",
            amount_minor=14900,
        )

    async with database.sessions() as session:
        order = await session.scalar(select(Order).where(Order.profile_id == profile_id))
    assert order is not None
    assert order.status == OrderStatus.FAILED
    assert order.receipt_email_encrypted == "invoice-failed"


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
    assert count == 2
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
    order_id, invoice_id, _ = await prepared_order(service, birth)
    await service.accept_payment_callback(invoice_id=invoice_id, amount_minor=14900, email=None)
    assert await service.delete_personal_data(10001) == []
    assert await service.profile_access(10001) is None
    assert await service.pending_order_ids() == []
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.provider_invoice_uuid is None
        assert order.payment_url is None
        assert order.receipt_email_encrypted == "deleted"


@pytest.mark.asyncio
async def test_expired_payment_contacts_are_scrubbed_before_test_journal_is_removed(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    order_id, invoice_id, profile_id = await prepared_order(service, birth)
    await service.accept_payment_callback(
        invoice_id=invoice_id, amount_minor=14900, email="buyer@example.ru"
    )
    old_received_at = datetime.now(UTC) - timedelta(days=31)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(Payment)
            .where(Payment.order_id == order_id)
            .values(
                received_at=old_received_at,
                provider_operation_hash="operation-hash",
                provider_operation_encrypted="encrypted-operation",
                provider_payment_method="card",
                refund_request_id="refund-id",
                refund_status="finished",
            )
        )
        await session.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(
                refund_confirmation_hash="confirmation-hash",
                refund_confirmation_expires_at=datetime.now(UTC),
            )
        )

    cutoff = datetime.now(UTC) - timedelta(days=30)
    assert await service.cleanup_expired_payment_contacts(cutoff) == 1
    assert await service.cleanup_expired_payment_contacts(cutoff) == 0

    access = await service.profile_access(10001)
    assert access is not None
    assert access.profile_id == profile_id
    assert access.order_status == OrderStatus.PAID
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        payment = await session.scalar(select(Payment).where(Payment.order_id == order_id))
        assert payment is not None
        assert payment.amount_minor == 14900
        assert payment.currency == "RUB"
        assert payment.provider_operation_hash == "operation-hash"
        assert payment.provider_operation_encrypted is None
        assert payment.provider_payment_method is None
        assert payment.notification_email_encrypted is None
        assert payment.refund_request_id is None
        assert payment.refund_status == "finished"
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.provider_invoice_uuid is None
        assert order.payment_url is None
        assert order.receipt_email_encrypted == "expired"
        assert order.refund_confirmation_hash is None
        assert order.refund_confirmation_expires_at is None

    assert await service.cleanup_expired_test_payments(cutoff) == 1
    assert await service.cleanup_expired_test_payments(cutoff) == 0
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        assert await session.scalar(select(func.count()).select_from(DeliveryItem)) == 0


@pytest.mark.asyncio
async def test_old_real_payment_is_removed_only_after_personal_data_deletion(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    order_id, invoice_id, _ = await prepared_order(service, birth)
    await service.accept_payment_callback(invoice_id=invoice_id, amount_minor=14900, email=None)
    cutoff = datetime.now(UTC) - timedelta(days=5 * 365)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(Payment)
            .where(Payment.order_id == order_id)
            .values(received_at=cutoff - timedelta(days=2))
        )

    assert await service.cleanup_expired_anonymized_payment_records(cutoff) == 0
    assert await service.delete_personal_data(10001) == []
    assert await service.cleanup_expired_anonymized_payment_records(cutoff) == 1
    assert await service.cleanup_expired_anonymized_payment_records(cutoff) == 0

    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        assert await session.scalar(select(func.count()).select_from(DeliveryItem)) == 0


@pytest.mark.asyncio
async def test_unresolved_refund_is_not_removed_after_record_retention_period(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    order_id, invoice_id, _ = await prepared_order(service, birth)
    await service.accept_payment_callback(invoice_id=invoice_id, amount_minor=14900, email=None)
    cutoff = datetime.now(UTC) - timedelta(days=5 * 365)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(Payment)
            .where(Payment.order_id == order_id)
            .values(
                received_at=cutoff - timedelta(days=2),
                provider_operation_encrypted="encrypted-operation",
                refund_request_id="refund-request",
                refund_status="uncertain",
            )
        )

    assert await service.delete_personal_data(10001) == []
    assert await service.cleanup_expired_payment_contacts(cutoff) == 0
    assert await service.cleanup_expired_anonymized_payment_records(cutoff) == 0
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
        assert await session.scalar(select(func.count()).select_from(DeliveryItem)) == 2
        payment = await session.scalar(select(Payment).where(Payment.order_id == order_id))
        assert payment is not None
        assert payment.provider_operation_encrypted == "encrypted-operation"
        assert payment.refund_request_id == "refund-request"


@pytest.mark.asyncio
async def test_expired_unpaid_order_scrubs_email_and_payment_link(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    _, _, profile_id = await prepared_order(service, birth)
    old_created_at = datetime.now(UTC) - timedelta(days=31)
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(Order).where(Order.profile_id == profile_id).values(created_at=old_created_at)
        )

    cutoff = datetime.now(UTC) - timedelta(days=30)
    assert await service.cleanup_expired_unpaid_orders(cutoff) == 1
    assert await service.cleanup_expired_unpaid_orders(cutoff) == 0

    async with database.sessions() as session:
        order = await session.scalar(select(Order).where(Order.profile_id == profile_id))
    assert order is not None
    assert order.status == OrderStatus.FAILED
    assert order.payment_url is None
    assert order.provider_invoice_uuid is None
    assert order.receipt_email_encrypted == "expired"


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
            == 2
        )


@pytest.mark.asyncio
async def test_stats_excludes_finished_refunds_from_revenue(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    order_id, invoice_id, _ = await prepared_order(service, birth)
    await service.accept_payment_callback(
        invoice_id=invoice_id, amount_minor=14900, email="buyer@example.ru"
    )

    before_refund = await service.stats(None)
    assert before_refund["payments"] == 1
    assert before_refund["revenue_rub"] == 149

    async with database.sessions() as session, session.begin():
        await session.execute(
            update(Payment)
            .where(Payment.order_id == order_id)
            .values(refund_status="finished", refunded_at=datetime.now(UTC))
        )

    after_refund = await service.stats(None)
    assert after_refund["payments"] == 1
    assert after_refund["revenue_rub"] == 0


@pytest.mark.asyncio
async def test_full_reading_offer_is_scheduled_one_hour_after_avatar_result(
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
    avatar_result, followup = items
    assert [item.kind for item in items] == ["avatar_result", "full_reading_offer"]
    assert followup.status == DeliveryStatus.SCHEDULED
    assert followup.available_at is None

    await service.mark_delivery_item(avatar_result.id, status=DeliveryStatus.SENT, message_id=10)
    assert await service.complete_delivery_if_ready(order_id)

    async with database.sessions() as session:
        order = await session.get(Order, order_id)
        sent_avatar = await session.get(DeliveryItem, avatar_result.id)
        scheduled = await session.get(DeliveryItem, followup.id)
        assert order is not None and order.status == OrderStatus.DELIVERED
        assert sent_avatar is not None and sent_avatar.sent_at is not None
        assert scheduled is not None
        assert scheduled.status == DeliveryStatus.PENDING
        assert scheduled.available_at is not None
        assert scheduled.sent_at is None
        delay = scheduled.available_at.replace(tzinfo=None) - sent_avatar.sent_at.replace(
            tzinfo=None
        )
        assert delay == FULL_READING_DELAY
        assert delay == timedelta(hours=1)

    assert order_id not in await service.pending_order_ids()
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(DeliveryItem)
            .where(DeliveryItem.id == followup.id)
            .values(available_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    assert order_id in await service.pending_order_ids()


@pytest.mark.asyncio
async def test_strength_offer_is_scheduled_for_one_hour_and_can_be_revealed(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    facts = calculate_chart(birth)
    result = generate_profile(facts)
    profile_id = await service.save_calculation(10001, birth, facts, result)

    await service.schedule_strength_offer(profile_id)
    await service.schedule_strength_offer(profile_id)

    async with database.sessions() as session:
        offers = list((await session.scalars(select(StrengthOffer))).all())
        assert len(offers) == 1
        offer = offers[0]
        delay = offer.available_at.replace(tzinfo=None) - offer.created_at.replace(tzinfo=None)
        assert delay == STRENGTH_OFFER_DELAY
        assert delay == timedelta(hours=1)

    assert await service.pending_strength_offer_profile_ids() == []
    assert await service.strength_offer_context(profile_id, telegram_id=99999, force=True) is None
    context = await service.strength_offer_context(profile_id, telegram_id=10001, force=True)
    assert context is not None
    assert context.profile_id == profile_id
    assert context.telegram_id == 10001
    assert context.money_type == result.money_type

    await service.mark_strength_offer_sent(context.offer_id, message_id=42)
    assert await service.strength_offer_context(profile_id, telegram_id=10001, force=True) is None


@pytest.mark.asyncio
async def test_strength_offer_becomes_pending_after_one_hour(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    facts = calculate_chart(birth)
    result = generate_profile(facts)
    profile_id = await service.save_calculation(10001, birth, facts, result)
    await service.schedule_strength_offer(profile_id)

    async with database.sessions() as session, session.begin():
        await session.execute(
            update(StrengthOffer)
            .where(StrengthOffer.profile_id == profile_id)
            .values(available_at=datetime(2000, 1, 1, tzinfo=UTC))
        )

    assert await service.pending_strength_offer_profile_ids() == [profile_id]


@pytest.mark.asyncio
async def test_full_reading_offer_can_be_revealed_by_its_owner(
    store: tuple[Store, Database], birth: BirthData
) -> None:
    service, database = store
    order_id, invoice_id, _ = await prepared_order(service, birth)
    await service.accept_payment_callback(
        invoice_id=invoice_id, amount_minor=14900, email="buyer@example.ru"
    )

    assert not await service.reveal_full_reading_offer(99999, order_id)
    assert await service.reveal_full_reading_offer(10001, order_id)

    async with database.sessions() as session:
        offer = await session.scalar(
            select(DeliveryItem).where(
                DeliveryItem.order_id == order_id,
                DeliveryItem.kind == "full_reading_offer",
            )
        )
        assert offer is not None
        assert offer.status == DeliveryStatus.PENDING
        assert offer.available_at is not None

    assert order_id in await service.pending_order_ids()


@pytest.mark.asyncio
async def test_form_reminder_is_replaced_and_sent_only_once_after_one_hour(
    store: tuple[Store, Database],
) -> None:
    service, database = store
    await service.ensure_user(10001)
    first_buttons = ((("Знаю точно", "precision:exact"),),)
    await service.schedule_form_reminder(
        10001,
        state="birth_date",
        text="Введи дату рождения.",
    )
    await service.schedule_form_reminder(
        10001,
        state="time_precision",
        text="Насколько точно известно время рождения?",
        buttons=first_buttons,
    )

    async with database.sessions() as session:
        reminders = list((await session.scalars(select(FormReminder))).all())
        assert len(reminders) == 1
        reminder = reminders[0]
        assert reminder.state == "time_precision"
        delay = reminder.available_at.replace(tzinfo=None) - reminder.created_at.replace(
            tzinfo=None
        )
        assert delay == FORM_REMINDER_DELAY
        assert delay == timedelta(hours=1)

    assert await service.pending_form_reminder_ids() == []
    async with database.sessions() as session, session.begin():
        await session.execute(
            update(FormReminder)
            .where(FormReminder.id == reminder.id)
            .values(available_at=datetime(2000, 1, 1, tzinfo=UTC))
        )

    assert await service.pending_form_reminder_ids() == [reminder.id]
    context = await service.form_reminder_context(reminder.id)
    assert context is not None
    assert context.telegram_id == 10001
    assert context.text == "Насколько точно известно время рождения?"
    assert context.buttons == first_buttons

    await service.mark_form_reminder_sent(
        reminder.id,
        message_id=77,
        payload_token=context.payload_token,
    )
    assert await service.pending_form_reminder_ids() == []
    assert await service.form_reminder_context(reminder.id) is None

    async with database.sessions() as session:
        sent = await session.get(FormReminder, reminder.id)
        assert sent is not None
        assert sent.status == DeliveryStatus.SENT
        assert sent.payload_encrypted == "sent"


@pytest.mark.asyncio
async def test_form_reminder_can_be_cancelled_when_step_is_completed(
    store: tuple[Store, Database],
) -> None:
    service, database = store
    await service.ensure_user(10001)
    await service.schedule_form_reminder(
        10001,
        state="city",
        text="Введи город рождения.",
    )

    await service.cancel_form_reminder(10001)

    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(FormReminder)) == 0


@pytest.mark.asyncio
async def test_existing_incomplete_form_is_backfilled_only_once(
    store: tuple[Store, Database],
) -> None:
    service, database = store
    await service.ensure_user(10001)
    bot_id = 8530273489
    storage = EncryptedDatabaseStorage(database.sessions, service.crypto)
    key = StorageKey(bot_id=bot_id, chat_id=10001, user_id=10001)
    await storage.set_state(key, ProfileForm.city)

    assert await service.backfill_form_reminders(bot_id, form_reminder_payload) == 1
    assert await service.backfill_form_reminders(bot_id, form_reminder_payload) == 0

    async with database.sessions() as session:
        reminder = await session.scalar(select(FormReminder))
        assert reminder is not None
        assert reminder.state == ProfileForm.city.state
        delay = reminder.available_at.replace(tzinfo=None) - reminder.created_at.replace(
            tzinfo=None
        )
        assert delay == timedelta(hours=1)

    async with database.sessions() as session, session.begin():
        await session.execute(
            update(FormReminder)
            .where(FormReminder.id == reminder.id)
            .values(available_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    context = await service.form_reminder_context(reminder.id)
    assert context is not None
    assert context.text == CITY_PROMPT


@pytest.mark.asyncio
async def test_expired_encrypted_form_state_and_reminder_are_deleted(
    store: tuple[Store, Database],
) -> None:
    service, database = store
    await service.ensure_user(10001)
    storage = EncryptedDatabaseStorage(database.sessions, service.crypto)
    key = StorageKey(bot_id=1, chat_id=10001, user_id=10001)
    await storage.set_state(key, ProfileForm.city)
    await storage.set_data(key, {"birth_date": "01.01.2000", "city": "Москва"})
    await service.schedule_form_reminder(10001, state="city", text=CITY_PROMPT)
    old = datetime.now(UTC) - timedelta(days=31)
    async with database.sessions() as session, session.begin():
        await session.execute(update(FsmRecord).values(updated_at=old))
        await session.execute(update(FormReminder).values(created_at=old))

    cutoff = datetime.now(UTC) - timedelta(days=30)
    assert await service.cleanup_expired_form_data(cutoff) == 2
    assert await service.cleanup_expired_form_data(cutoff) == 0
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(FsmRecord)) == 0
        assert await session.scalar(select(func.count()).select_from(FormReminder)) == 0


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


@pytest.mark.asyncio
async def test_configured_admin_list_overrides_bootstrap_identity(
    store: tuple[Store, Database],
) -> None:
    service, _ = store
    claim = await service.claim_admin_if_unset(10001)
    assert claim.is_admin

    assert not await service.is_admin(10001, frozenset({20002}))
    assert await service.is_admin(20002, frozenset({20002}))
