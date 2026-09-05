from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TelegramUser
from sqlalchemy import func, select, update

from money_profile_bot.bot.analytics import JourneyMiddleware
from money_profile_bot.bot.stats import register_stats
from money_profile_bot.config import Settings
from money_profile_bot.crypto import CryptoBox
from money_profile_bot.database import Database
from money_profile_bot.models import (
    DeliveryItem,
    Event,
    Journey,
    JourneyEvent,
    Order,
    Payment,
    PaymentNotification,
    Profile,
    StrengthOffer,
    utcnow,
)
from money_profile_bot.services.analytics import callback_key, form_step, period_since
from money_profile_bot.services.robokassa import (
    Invoice,
    OperationState,
    RobokassaClient,
    RobokassaError,
)
from money_profile_bot.services.store import Store


@pytest.fixture
async def analytics_store(tmp_path: Path) -> AsyncIterator[Store]:
    settings = Settings(_env_file=None)
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'analytics.sqlite3').as_posix()}")
    await database.initialize()

    async def invoice(**kwargs: object) -> Invoice:
        return Invoice("invoice", int(kwargs["invoice_id"]), "https://pay.example/invoice")

    provider = SimpleNamespace(create_invoice=AsyncMock(side_effect=invoice))
    store = Store(
        database.sessions,
        CryptoBox(settings.app_encryption_key, settings.lookup_hmac_key),
        cast(RobokassaClient, provider),
        analytics_mode="live",
    )
    yield store
    await database.close()


async def profile_for(store: Store, telegram_id: int, *, start: bool = True) -> str:
    user = await store.ensure_user(telegram_id, "campaign")
    if start:
        await store.analytics.start(telegram_id)
    async with store.sessions() as session, session.begin():
        profile = Profile(user_id=user.id, birth_data_encrypted="encrypted", status="calculated")
        session.add(profile)
        await session.flush()
        await store.analytics.bind_profile(session, user.id, profile.id)
        return profile.id


def test_moscow_midnight_and_semantic_keys() -> None:
    assert period_since("today", datetime(2026, 9, 4, 22, 10, tzinfo=UTC)) == datetime(
        2026, 9, 4, 21, tzinfo=UTC
    )
    assert (
        form_step("ProfileForm:birth_date", {"birth_date_step": "month", "birth_year": 1990})
        == "date_month"
    )
    assert form_step("ProfileForm:birth_time", {"birth_time_step": "minute_range"}) == "time_range"
    assert callback_key("birth_date:day:1990:1:15") == "date_day"
    assert callback_key("birth_time:minute:12:30") == "time_minute"
    assert callback_key("birth_date:unknown:personal-value") is None


async def test_funnel_counts_people_once_and_keeps_one_start_cohort(analytics_store: Store) -> None:
    store = analytics_store
    steps = (
        "consent",
        "date_decade",
        "date_year",
        "date_month",
        "date_day",
        "precision",
        "city",
        "city_choice",
        "confirm",
        "calculation",
        "free",
        "offer",
    )
    for user_id in (1, 1, 2):
        await store.ensure_user(user_id)
        await store.analytics.start(user_id)
        for step in steps:
            await store.analytics.record(user_id, "step", step)
        if user_id == 1:
            await store.analytics.record(user_id, "click", "buy", actor="user")
            await store.analytics.record(user_id, "step", "paid")
            await store.analytics.record(user_id, "step", "delivered")
    funnel = await store.analytics.funnel(None, "live")
    assert funnel["total"] == 3
    assert len(funnel["people"]["start"]) == 2
    assert len(funnel["people"]["offer"]) == 2
    assert len(funnel["people"]["paid"]) == 1
    assert len(funnel["people"]["delivered"]) == 1
    # A payment now from a journey started before the period must not enter this cohort.
    async with store.sessions() as session, session.begin():
        await session.execute(update(Journey).values(created_at=utcnow() - timedelta(days=8)))
    await store.ensure_user(3)
    await store.analytics.start(3)
    await store.analytics.record(3, "step", "consent")
    recent = await store.analytics.funnel(period_since("7d"), "live")
    assert len(recent["people"]["start"]) == 1
    assert len(recent["people"]["paid"]) == 0


async def test_foreign_profile_callback_cannot_change_another_journey(
    analytics_store: Store,
) -> None:
    store = analytics_store
    profile_id = await profile_for(store, 1)
    await store.ensure_user(2)
    await store.analytics.record(2, "click", "buy", profile_id=profile_id, actor="user")
    async with store.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(JourneyEvent).where(JourneyEvent.kind == "click")
            )
            == 0
        )


async def test_latest_journey_is_not_overwritten_by_old_payment_or_background_send(
    analytics_store: Store,
) -> None:
    store = analytics_store
    profile_id = await profile_for(store, 1001)
    await store.analytics.record_profile(profile_id, "step", "free")
    await store.analytics.start(1001)
    await store.analytics.record(1001, "step", "date_month")
    await store.analytics.record_profile(profile_id, "step", "paid")
    await store.analytics.record_profile(profile_id, "step", "delivered")
    await store.analytics.record_profile(profile_id, "step", "offer", actor="automatic")
    rows = await store.analytics.current(None, "all")
    assert rows[0][1].step == "date_month"
    async with store.sessions() as session:
        previous = await session.scalar(select(Journey).where(Journey.profile_id == profile_id))
        assert previous.step == "delivered"
    history = await store.analytics.history(rows[0][0].id, None, 0)
    assert len(history["journeys"]) == 2


async def test_buttons_deduplicate_users_and_separate_automatic_progress(
    analytics_store: Store,
) -> None:
    store = analytics_store
    one = await profile_for(store, 1)
    two = await profile_for(store, 2)
    for profile_id in (one, two):
        await store.analytics.record_profile(profile_id, "step", "free")
        await store.analytics.record_profile(profile_id, "step", "free")
    await store.analytics.record(1, "click", "strength", profile_id=one, actor="user")
    await store.analytics.record(1, "click", "strength", profile_id=one, actor="user")
    await store.analytics.record_profile(one, "step", "offer")
    await store.analytics.record_profile(two, "step", "offer", actor="automatic")
    counts = (await store.analytics.buttons(None, "live"))["strength"]
    assert counts == {"sent": 2, "clicked": 1, "continued": 1, "waiting": 0, "inactive": 0}
    assert (await store.analytics.buttons(None, "test")) == {}
    # Callback with no recorded exposure is not silently promoted to an impression.
    await store.ensure_user(3)
    await store.analytics.record(3, "click", "strength", actor="user")
    assert (await store.analytics.buttons(None, "all"))["strength"]["sent"] == 2


async def test_cancel_and_back_do_not_complete_the_unanswered_step(analytics_store: Store) -> None:
    store = analytics_store
    await store.ensure_user(1)
    await store.analytics.start(1)
    for step in ("consent", "date_decade", "date_year", "date_month", "date_year", "cancelled"):
        await store.analytics.record(1, "step", step)
    funnel = await store.analytics.funnel(None, "live")
    assert len(funnel["reached"]["date_year"]) == 1
    assert not funnel["passed"]["date_month"]
    assert (await store.analytics.current(None, "all"))[0][1].step == "cancelled"


async def test_errors_are_distinct_from_inactivity_and_resolve_on_success(
    analytics_store: Store,
) -> None:
    store = analytics_store
    await store.ensure_user(1)
    await store.analytics.start(1)
    await store.analytics.record(1, "step", "confirm")
    await store.analytics.record(1, "error", "calculation")
    await store.analytics.record(1, "error", "calculation")
    rows = await store.analytics.current(None, "all")
    assert len(rows) == 1
    assert rows[0][1].error == "calculation"
    async with store.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(JourneyEvent).where(JourneyEvent.kind == "error")
            )
            == 1
        )
    await store.analytics.record(1, "step", "free")
    assert (await store.analytics.current(None, "all"))[0][1].error is None


async def test_payment_modes_and_refunds_do_not_mix(analytics_store: Store) -> None:
    store = analytics_store
    live_profile = await profile_for(store, 1)
    link = await store.create_order(
        telegram_id=1, profile_id=live_profile, email="person@example.ru", amount_minor=14900
    )
    async with store.sessions() as session:
        live_order = await session.get(Order, link.order_id)
    await store.accept_payment_callback(
        invoice_id=live_order.provider_invoice_id, amount_minor=14900, email=None
    )
    await store.accept_payment_callback(
        invoice_id=live_order.provider_invoice_id, amount_minor=14900, email=None
    )
    test_profile = await profile_for(store, 2)
    await store.create_fake_paid_order(telegram_id=2, profile_id=test_profile)
    modes = await store.analytics.finances(None)
    assert modes["live"] == {"payments": 1, "net_minor": 14900, "refunds": 0}
    assert modes["test"] == {"payments": 1, "net_minor": 0, "refunds": 0}
    async with store.sessions() as session, session.begin():
        await session.execute(
            update(Payment)
            .where(Payment.order_id == link.order_id)
            .values(refund_status="finished")
        )
    assert (await store.analytics.finances(None))["live"]["net_minor"] == 0
    async with store.sessions() as session:
        journey = await session.scalar(select(Journey).where(Journey.profile_id == live_profile))
        assert (
            await session.scalar(
                select(func.count())
                .select_from(JourneyEvent)
                .where(
                    JourneyEvent.journey_id == journey.id,
                    JourneyEvent.kind == "step",
                    JourneyEvent.key == "paid",
                )
            )
            == 1
        )


async def saved_payment(
    store: Store, number: int, *, mode: str = "unknown", provider: str = "robokassa"
) -> Order:
    profile_id = await profile_for(store, number, start=False)
    async with store.sessions() as session, session.begin():
        profile = await session.get(Profile, profile_id)
        order = Order(
            code=f"MP-HISTORY{number}",
            user_id=profile.user_id,
            profile_id=profile_id,
            provider=provider,
            provider_invoice_id=number,
            analytics_mode=mode,
            amount_minor=14900,
            receipt_email_encrypted="encrypted-email",
            status="delivered",
        )
        session.add(order)
        await session.flush()
        session.add(Payment(order_id=order.id, amount_minor=14900, currency="RUB"))
        return order


async def test_legacy_payment_modes_require_provider_evidence_and_do_not_replay(
    analytics_store: Store,
) -> None:
    store = analytics_store
    store.notifications.recipients = frozenset({101})
    orders = [await saved_payment(store, number) for number in range(1, 4)]
    await saved_payment(store, 4, mode="test")
    await saved_payment(store, 5, provider="fake")
    async with store.sessions() as session, session.begin():
        await session.execute(
            update(Payment).where(Payment.order_id == orders[0].id).values(refund_status="finished")
        )
        await session.execute(update(Journey).values(mode="unknown", complete_history=False))
        before_events = await session.scalar(select(func.count()).select_from(JourneyEvent))
        before_audit = await session.scalar(select(func.count()).select_from(Event))
    provider = cast(SimpleNamespace, store.robokassa)
    provider.operation_state = AsyncMock(
        return_value=OperationState(100, "verified", 14900, "card")
    )
    assert await store.reconcile_legacy_payment_modes() == 3
    assert await store.reconcile_legacy_payment_modes() == 0
    assert provider.operation_state.await_count == 3
    totals = await store.analytics.finances(None)
    assert totals["live"] == {"payments": 3, "refunds": 1, "net_minor": 29800}
    assert totals["test"]["payments"] == 2
    assert totals["unknown"]["payments"] == 0
    assert (await store.analytics.funnel(None, "live"))["total"] == 0
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentNotification)) == 0
        assert await session.scalar(select(func.count()).select_from(DeliveryItem)) == 0
        assert all(
            journey.mode == "unknown" and not journey.complete_history
            for journey in (await session.scalars(select(Journey))).all()
        )
        assert await session.scalar(select(func.count()).select_from(JourneyEvent)) == before_events
        assert await session.scalar(select(func.count()).select_from(Event)) == before_audit + 3


@pytest.mark.parametrize(
    "state,change",
    [
        (RobokassaError("unavailable"), {}),
        (OperationState(50, "pending", 14900, "card"), {}),
        (OperationState(100, "", 14900, "card"), {}),
        (OperationState(100, "verified", 1, "card"), {}),
        (OperationState(100, "verified", 14900, "card"), {"amount_minor": 1}),
        (OperationState(100, "verified", 14900, "card"), {"currency": "USD"}),
    ],
)
async def test_legacy_payment_modes_keep_unverified_records_unknown(
    analytics_store: Store, state: OperationState | Exception, change: dict[str, object]
) -> None:
    store = analytics_store
    order = await saved_payment(store, 1)
    provider = cast(SimpleNamespace, store.robokassa)
    provider.operation_state = AsyncMock(
        side_effect=state if isinstance(state, Exception) else None, return_value=state
    )
    if change:
        async with store.sessions() as session, session.begin():
            await session.execute(
                update(Payment).where(Payment.order_id == order.id).values(**change)
            )
    assert await store.reconcile_legacy_payment_modes() == 0
    async with store.sessions() as session:
        assert (await session.get(Order, order.id)).analytics_mode == "unknown"
    store.analytics.mode = "test"
    provider.operation_state.reset_mock()
    assert await store.reconcile_legacy_payment_modes() == 0
    provider.operation_state.assert_not_called()


async def test_legacy_verification_advances_past_unverifiable_batch(analytics_store: Store) -> None:
    store = analytics_store
    for number in range(1, 22):
        await saved_payment(store, number)
    provider = cast(SimpleNamespace, store.robokassa)
    provider.operation_state = AsyncMock(side_effect=RobokassaError("unavailable"))
    assert await store.reconcile_legacy_payment_modes() == 0
    assert provider.operation_state.await_count == 20
    assert await store.reconcile_legacy_payment_modes() == 0
    assert provider.operation_state.await_count == 21
    assert len({call.args[0] for call in provider.operation_state.call_args_list}) == 21
    await store.reconcile_legacy_payment_modes()
    assert provider.operation_state.await_count == 41


async def test_stats_shows_one_payment_count_and_no_partial_cohort(
    analytics_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = analytics_store
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    monkeypatch.setattr("money_profile_bot.bot.stats.utcnow", lambda: now)
    for number in range(1, 7):
        await saved_payment(store, number, mode="live" if number <= 5 else "test")
    await saved_payment(store, 7)
    await store.analytics.start(4)
    for step in ("date_decade", "precision", "city", "confirm", "calculation", "free", "offer"):
        await store.analytics.record(4, "step", step)
    await store.analytics.record(4, "click", "buy", actor="user")
    await store.analytics.record(4, "step", "paid")
    async with store.sessions() as session, session.begin():
        await session.execute(update(Payment).values(received_at=now))
        await session.execute(update(Journey).values(created_at=now))
        old_order = await session.scalar(select(Order).where(Order.provider_invoice_id == 5))
        await session.execute(
            update(Payment)
            .where(Payment.order_id == old_order.id)
            .values(received_at=now - timedelta(days=1), refund_status="finished")
        )
    router = Router()
    register_stats(
        router,
        store,
        Settings(
            _env_file=None,
            admin_telegram_ids="99",
            payment_mode="robokassa",
            robokassa_test_mode=False,
        ),
    )
    sent: list[str] = []

    async def capture(self: Message, text: str, **_: object) -> None:
        sent.append(text)

    monkeypatch.setattr(Message, "edit_text", capture)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    admin = TelegramUser(id=99, is_bot=False, first_name="Admin")
    callback = CallbackQuery(
        id="today",
        from_user=admin,
        chat_instance="x",
        data="report:funnel:today",
        message=Message(message_id=1, date=now, chat=Chat(id=99, type="private")),
    )
    handler = router.callback_query.handlers[0].callback
    await handler(callback)
    assert "Оплат за период: <b>4</b>" in sent[-1]
    assert "Оплатили в этой группе" not in sent[-1]
    assert "Конверсия" not in sent[-1]
    assert "полной историей" not in sent[-1]
    assert "Аватар рассчитан" in sent[-1]
    assert "Из них возвращено" not in sent[-1]
    assert "Старые оплаты на проверке: 1" in sent[-1]
    await handler(callback.model_copy(update={"data": "report:funnel:7d"}))
    assert "Оплат за период: <b>5</b>" in sent[-1]
    assert "Из них возвращено: <b>1</b>" in sent[-1]


async def test_period_steps_use_legacy_facts_without_inventing_missing_transitions(
    analytics_store: Store,
) -> None:
    store = analytics_store
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    since = period_since("today", now)
    before = since - timedelta(seconds=1)
    future = now + timedelta(seconds=1)
    orders = [await saved_payment(store, n, mode="live") for n in range(1, 5)]
    await store.delete_personal_data(4)
    async with store.sessions() as session, session.begin():
        await session.execute(update(Profile).values(created_at=since))
        await session.execute(update(Order).values(created_at=since))
        await session.execute(update(Payment).values(received_at=since))
        await session.execute(update(Journey).values(created_at=since))
        # A returning buyer started and calculated before today, but pays today.
        await session.execute(
            update(Profile).where(Profile.id == orders[1].profile_id).values(created_at=before)
        )
        await session.execute(
            update(Order).where(Order.id == orders[1].id).values(created_at=before)
        )
        # Future timestamps must not enter any of today's numbers.
        await session.execute(
            update(Profile).where(Profile.id == orders[2].profile_id).values(created_at=future)
        )
        await session.execute(
            update(Order).where(Order.id == orders[2].id).values(created_at=future)
        )
        await session.execute(
            update(Payment).where(Payment.order_id == orders[2].id).values(received_at=future)
        )
        for order, when in (
            (orders[0], since),
            (orders[0], now),
            (orders[1], before),
            (orders[2], future),
        ):
            session.add(Event(user_id=order.user_id, name="bot_started", created_at=when))
        session.add(
            StrengthOffer(
                profile_id=orders[0].profile_id, status="sent", available_at=since, sent_at=since
            )
        )
        session.add(Event(user_id=orders[0].user_id, name="offer_viewed", created_at=since))
        session.add(
            StrengthOffer(profile_id=orders[1].profile_id, status="pending", available_at=since)
        )
        journey = await session.scalar(
            select(Journey).where(Journey.profile_id == orders[1].profile_id)
        )
        session.add(JourneyEvent(journey_id=journey.id, kind="click", key="buy", created_at=now))
    assert await store.analytics.period_steps(since, now) == {
        "start": 1,
        "calculated": 1,
        "offer": 1,
        "buy": 2,
    }
    # Payment records survive personal-data deletion; anonymous payments still count.
    assert (await store.analytics.finances(since, now))["live"]["payments"] == 3
    async with store.sessions() as session:
        assert not any(j.complete_history for j in (await session.scalars(select(Journey))).all())


async def test_delete_removes_journals_and_late_events_cannot_restore_them(
    analytics_store: Store,
) -> None:
    store = analytics_store
    profile_id = await profile_for(store, 1)
    user = await store.ensure_user(1)
    await store.analytics.record_profile(profile_id, "step", "free")
    await store.delete_personal_data(1)
    await store.analytics.record_profile(profile_id, "step", "offer")
    assert await store.analytics.history(user.id, None, 0) is None
    assert await store.analytics.current(None, "all") == []
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Journey)) == 0
        assert await session.scalar(select(func.count()).select_from(JourneyEvent)) == 0


async def test_journal_retention_and_allowlist(analytics_store: Store) -> None:
    store = analytics_store
    await store.ensure_user(1)
    await store.analytics.start(1)
    with pytest.raises(ValueError, match="allowlisted"):
        await store.analytics.record(1, "action", "private@example.ru")
    async with store.sessions() as session, session.begin():
        await session.execute(update(Journey).values(updated_at=utcnow() - timedelta(days=91)))
    await store.analytics.cleanup()
    async with store.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(JourneyEvent)) == 0


async def test_legacy_fsm_import_is_partial_and_has_no_invented_clicks(
    analytics_store: Store,
) -> None:
    from money_profile_bot.bot.storage import EncryptedDatabaseStorage

    store = analytics_store
    await store.ensure_user(1)
    storage = EncryptedDatabaseStorage(store.sessions, store.crypto)
    state = FSMContext(storage, StorageKey(bot_id=42, chat_id=1, user_id=1))
    await state.set_state("ProfileForm:birth_date")
    await state.set_data({"birth_date_step": "month", "birth_year": 1990})
    await store.analytics.backfill(42)
    await store.analytics.backfill(42)
    rows = await store.analytics.current(None, "all")
    assert rows[0][1].step == "date_month"
    assert not rows[0][1].complete_history
    assert rows[0][1].last_action_at is None
    assert await store.analytics.buttons(None, "all") == {}
    assert (await store.analytics.funnel(None, "all"))["total"] == 0


async def test_form_middleware_tracks_success_and_unknown_time_without_personal_values(
    analytics_store: Store,
) -> None:
    store = analytics_store
    await store.ensure_user(1)
    await store.analytics.start(1)
    await store.analytics.record(1, "step", "precision")
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=42, chat_id=1, user_id=1))
    await state.set_state("ProfileForm:time_precision")
    callback = CallbackQuery(
        id="callback",
        from_user=TelegramUser(id=1, is_bot=False, first_name="Private"),
        chat_instance="instance",
        data="precision:unknown",
        message=Message(message_id=2, date=utcnow(), chat=Chat(id=1, type="private")),
    )

    async def handler(*_: object) -> None:
        await state.set_state("ProfileForm:city")

    await JourneyMiddleware(store.analytics)(handler, callback, {"state": state})
    rows = await store.analytics.current(None, "all")
    assert rows[0][1].step == "city"
    history = await store.analytics.history(rows[0][0].id, None, 0)
    assert any(e.kind == "skipped" for e in history["events"])
    assert all(e.key != "Private" for e in history["events"])


@pytest.mark.parametrize("test_mode", [False, True])
async def test_stats_navigation_is_admin_only_and_fits_telegram(
    analytics_store: Store, monkeypatch: pytest.MonkeyPatch, test_mode: bool
) -> None:
    store = analytics_store
    store.analytics.mode = "test" if test_mode else "live"
    for index in range(10):
        profile_id = await profile_for(store, 100 + index)
        await store.analytics.record_profile(profile_id, "step", "free")
    settings = Settings(
        _env_file=None,
        admin_telegram_ids="1",
        payment_mode="robokassa",
        robokassa_test_mode=test_mode,
    )
    store.analytics.mode = "live" if test_mode else "test"
    await store.ensure_user(999)
    await store.analytics.start(999)
    router = Router()
    register_stats(router, store, settings)
    command = router.message.handlers[0].callback
    callback_handler = router.callback_query.handlers[0].callback
    sent: list[tuple[str, object]] = []

    async def capture(self: Message, text: str, **kwargs: object) -> None:
        assert len(text) < 4096
        keyboard = kwargs["reply_markup"]
        for row in keyboard.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64
        sent.append((text, keyboard))

    monkeypatch.setattr(Message, "answer", capture)
    monkeypatch.setattr(Message, "edit_text", capture)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    admin = TelegramUser(id=1, is_bot=False, first_name="Admin")
    message = Message(
        message_id=1, date=utcnow(), chat=Chat(id=1, type="private"), from_user=admin, text="/stats"
    )
    await command(message)
    assert "Статистика · сегодня" in sent[-1][0]
    assert "Начали прохождение: <b>11</b>" in sent[-1][0]
    assert ("Тестовых оплат за период" in sent[-1][0]) == test_mode
    assert ("Оплат за период:" in sent[-1][0]) != test_mode
    assert len(sent[-1][1].inline_keyboard) == 1
    assert sent[-1][1].inline_keyboard[0][0].text == "Изменить период"
    await callback_handler(
        CallbackQuery(
            id="period",
            from_user=admin,
            chat_instance="x",
            message=message,
            data="report:period:7d",
        )
    )
    assert len(sent[-1][1].inline_keyboard) == 5
    for period in ("today", "7d", "30d", "all"):
        await callback_handler(
            CallbackQuery(
                id="period",
                from_user=admin,
                chat_instance="x",
                message=message,
                data=f"report:funnel:{period}",
            )
        )
        assert len(sent[-1][1].inline_keyboard) == 1
    user_id = (await store.ensure_user(100)).id
    routes = ["home", "funnel", "steps", "users", "buttons", "money", "sources", "filters"]
    for view in routes:
        await callback_handler(
            CallbackQuery(
                id="x",
                from_user=admin,
                chat_instance="x",
                message=message,
                data=f"report:{view}:today:unknown:all:0",
            )
        )
    await callback_handler(
        CallbackQuery(
            id="x",
            from_user=admin,
            chat_instance="x",
            message=message,
            data=f"report:user:today:unknown:{user_id}:0",
        )
    )
    assert "Начали прохождение" in sent[-1][0]
    assert "Оплатили в этой группе" not in sent[-1][0]
    assert "Telegram ID" not in sent[-1][0]
    assert "Где остановились" not in sent[-1][0]
    assert "Кнопки" not in sent[-1][0]
    assert len(sent[-1][1].inline_keyboard) == 1
    before = len(sent)
    stranger = TelegramUser(id=2, is_bot=False, first_name="Other")
    await callback_handler(
        CallbackQuery(
            id="x",
            from_user=stranger,
            chat_instance="x",
            message=message,
            data=f"report:user:all:all:{user_id}:0",
        )
    )
    assert len(sent) == before
    group_message = message.model_copy(update={"chat": Chat(id=-100, type="supergroup")})
    await callback_handler(
        CallbackQuery(
            id="x",
            from_user=admin,
            chat_instance="x",
            message=group_message,
            data=f"report:user:all:all:{user_id}:0",
        )
    )
    assert len(sent) == before
    monkeypatch.setattr(
        Message,
        "edit_text",
        AsyncMock(
            side_effect=TelegramBadRequest(
                method=EditMessageText(chat_id=1, message_id=1, text="same"),
                message="Bad Request: message is not modified",
            )
        ),
    )
    await callback_handler(
        CallbackQuery(
            id="x",
            from_user=admin,
            chat_instance="x",
            message=message,
            data="report:home:all:all:all:0",
        )
    )
    await callback_handler(
        CallbackQuery(
            id="x",
            from_user=admin,
            chat_instance="x",
            message=message,
            data="report:users:no-period:all:all:0",
        )
    )
    assert len(sent) == before
