from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from money_profile_bot.crypto import CryptoBox
from money_profile_bot.models import (
    DeliveryItem,
    DeliveryStatus,
    Event,
    FsmRecord,
    Journey,
    JourneyEvent,
    Order,
    OrderStatus,
    Payment,
    Profile,
    StrengthOffer,
    User,
    utcnow,
)

MSK = timezone(timedelta(hours=3))
RETENTION_DAYS = 90
STEPS = {
    "consent": "Согласие",
    "date_decade": "Дата: десятилетие",
    "date_year": "Дата: год",
    "date_month": "Дата: месяц",
    "date_day": "Дата: день",
    "precision": "Точность времени",
    "time_hour": "Время: час",
    "time_range": "Время: диапазон минут",
    "time_minute": "Время: минута",
    "city": "Ввод города",
    "city_choice": "Выбор города",
    "confirm": "Подтверждение анкеты",
    "calculation": "Расчёт аватара",
    "free": "Бесплатный аватар отправлен",
    "offer": "Предложение разбора отправлено",
    "email": "Email для чека",
    "invoice": "Ссылка на оплату отправлена",
    "paid": "Оплата подтверждена, ждём выдачи",
    "delivered": "Платный разбор отправлен",
    "full_offer": "Предложение полного разбора отправлено",
    "cancelled": "Анкета отменена",
    "payment_cancelled": "Оплата отменена до создания счёта",
    "unknown": "Нет данных о текущем шаге",
}
BUTTONS = {
    "consent": "Согласен(а), продолжить",
    "date_decade": "Выбрать десятилетие",
    "date_year": "Выбрать год",
    "date_month": "Выбрать месяц",
    "date_day": "Выбрать день",
    "precision": "Выбрать точность времени",
    "time_hour": "Выбрать час",
    "time_range": "Выбрать диапазон минут",
    "time_minute": "Выбрать минуту",
    "city_choice": "Выбрать город",
    "confirm": "Всё верно",
    "restart": "Заполнить заново",
    "cancel": "Отменить анкету",
    "back": "Назад в анкете",
    "strength": "Узнать силу",
    "buy": "Раскрыть силу",
    "payment_cancel": "Отмена оплаты",
    "full": "Узнать всю денежную картину",
    "new": "Рассчитать другой аватар",
}
STEP_BUTTONS = {
    **{key: (key,) for key in BUTTONS if key in STEPS},
    "confirm": ("confirm", "restart"),
    "free": ("strength",),
    "offer": ("buy",),
    "email": ("payment_cancel",),
    "delivered": ("full", "new"),
}
ERRORS = {
    "calculation": "Ошибка расчёта",
    "capacity": "Очередь расчёта заполнена",
    "invoice": "Не удалось создать счёт",
    "payment_paused": "Создание счетов приостановлено",
    "send": "Ошибка отправки сообщения",
    "delivery": "Ошибка выдачи платного разбора",
    "offer": "Ошибка отправки предложения",
    "reminder": "Ошибка отправки напоминания",
    "handler": "Ошибка обработки действия",
}
EXTRA_KEYS = {
    "start",
    "resume",
    "profile",
    "input",
    "time_unknown",
    "payment_cancel",
    "reminder",
    "refund",
}
KINDS = {
    "action",
    "click",
    "input_rejected",
    "step",
    "passed",
    "skipped",
    "button_sent",
    "error",
    "recovered",
    "fact",
    "reminder",
}
MODES = {"live": "Реальные", "test": "Тестовые", "unknown": "Режим неизвестен", "all": "Все"}
FUNNEL = (
    ("start", "Запустили бота"),
    ("consent", "Дали согласие"),
    ("date", "Выбрали дату рождения"),
    ("time", "Указали время или «не знаю»"),
    ("city", "Выбрали город"),
    ("confirm", "Подтвердили анкету"),
    ("free", "Бесплатный аватар отправлен"),
    ("offer", "Предложение разбора отправлено"),
    ("buy", "Начали покупку"),
    ("paid", "Оплата подтверждена"),
    ("delivered", "Платный разбор отправлен"),
)
TERMINAL = {"delivered", "full_offer", "cancelled", "payment_cancelled", "unknown"}
FORM_STEPS = set(STEPS) - {"free", "offer", "email", "invoice", "paid", *TERMINAL}
ADVANCES = {
    "consent": {"date_decade"},
    "date_decade": {"date_year"},
    "date_year": {"date_month"},
    "date_month": {"date_day"},
    "date_day": {"precision"},
    "precision": {"time_hour", "city"},
    "time_hour": {"time_range"},
    "time_range": {"time_minute"},
    "time_minute": {"city"},
    "city": {"city_choice"},
    "city_choice": {"confirm"},
    "confirm": {"calculation"},
    "calculation": {"free"},
    "free": {"offer"},
    "offer": {"email", "paid"},
    "email": {"invoice", "paid"},
    "invoice": {"paid"},
    "paid": {"delivered"},
    "delivered": {"full_offer"},
}


def step_buttons(key: str) -> set[str]:
    buttons = set(STEP_BUTTONS.get(key, ()))
    if key.startswith(("date_", "time_")) or key == "precision":
        buttons.add("cancel")
    if key.startswith(("date_", "time_")) and key != "date_decade":
        buttons.add("back")
    return buttons


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def period_since(period: str, now: datetime | None = None) -> datetime | None:
    now = as_utc(now or utcnow())
    if period == "today":
        return (
            now.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        )
    if period in {"7d", "30d"}:
        return now - timedelta(days=int(period[:-1]))
    if period == "all":
        return None
    raise ValueError("unknown stats period")


def form_step(state: str | None, data: dict[str, Any]) -> str | None:
    if state == "ProfileForm:birth_date":
        suffix = data.get("birth_date_step") or "decade"
        return f"date_{suffix}" if suffix in {"decade", "year", "month", "day"} else "date_decade"
    if state == "ProfileForm:birth_time":
        suffix = {"hour": "hour", "minute_range": "range", "minute": "minute"}.get(
            str(data.get("birth_time_step")), "hour"
        )
        return f"time_{suffix}"
    return {
        "ProfileForm:consent": "consent",
        "ProfileForm:name": "date_decade",
        "ProfileForm:time_precision": "precision",
        "ProfileForm:city": "city",
        "ProfileForm:city_choice": "city_choice",
        "ProfileForm:confirm": "confirm",
        "PaymentForm:email": "email",
    }.get(state or "")


def callback_key(value: str) -> str | None:
    # Never store callback bodies: they contain dates, times, city indices and identifiers.
    parts = value.split(":")
    if parts[0] in {"birth_date", "birth_time"} and len(parts) > 1:
        if parts[1] == "back":
            return "back"
        prefix = "date" if parts[0] == "birth_date" else "time"
        key = f"{prefix}_{parts[1]}"
        return key if key in BUTTONS else None
    return {
        "consent:yes": "consent",
        "form:confirm": "confirm",
        "form:restart": "restart",
        "form:cancel": "cancel",
        "payment:cancel": "payment_cancel",
        "profile:new": "new",
    }.get(value) or {
        "precision": "precision",
        "city": "city_choice",
        "strength": "strength",
        "buy": "buy",
        "full": "full",
    }.get(parts[0])


class Analytics:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], crypto: CryptoBox, mode: str
    ) -> None:
        self.sessions = sessions
        self.crypto = crypto
        self.mode = mode

    async def _journey(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        profile_id: str | None = None,
        new: bool = False,
    ) -> Journey | None:
        user = await session.get(User, user_id)
        if not user or user.telegram_id_encrypted == "deleted":
            return None
        query = select(Journey).where(Journey.user_id == user_id)
        if profile_id:
            profile = await session.get(Profile, profile_id)
            if not profile or profile.user_id != user_id or profile.deleted_at:
                return None
            query = query.where(Journey.profile_id == profile_id)
        journey = None if new else await session.scalar(query.order_by(Journey.id.desc()).limit(1))
        if journey is None:
            journey = Journey(
                user_id=user_id,
                profile_id=profile_id,
                mode=self.mode if new else "unknown",
                source=user.last_source,
                complete_history=new,
                step="unknown",
            )
            session.add(journey)
            await session.flush()
        return journey

    async def start(self, telegram_id: int) -> None:
        async with self.sessions() as session, session.begin():
            user_id = await self._user_id(session, telegram_id)
            if user_id:
                journey = await self._journey(session, user_id, new=True)
                if journey:
                    self._append(session, journey, "action", "start", "user")

    async def _user_id(self, session: AsyncSession, telegram_id: int) -> str | None:
        return cast(
            str | None,
            await session.scalar(
                select(User.id).where(
                    User.telegram_id_hash
                    == self.crypto.lookup(str(telegram_id), context="telegram-user")
                )
            ),
        )

    async def bind_profile(self, session: AsyncSession, user_id: str, profile_id: str) -> None:
        journey = await self._journey(session, user_id)
        if journey:
            if journey.profile_id:
                journey = await self._journey(session, user_id, profile_id=profile_id)
            if journey:
                journey.profile_id = profile_id
                self._append(session, journey, "fact", "calculation", "system")

    @staticmethod
    def _append(session: AsyncSession, journey: Journey, kind: str, key: str, actor: str) -> None:
        if kind not in KINDS or key not in (
            STEPS.keys() | BUTTONS.keys() | ERRORS.keys() | EXTRA_KEYS
        ):
            raise ValueError("analytics accepts only semantic allowlisted events")
        if actor not in {"user", "system", "automatic"}:
            raise ValueError("invalid analytics actor")
        now = utcnow()
        if kind == "reminder" and journey.error == "reminder":
            session.add(JourneyEvent(journey_id=journey.id, kind="recovered", key="reminder"))
            journey.error = None
        if kind == "error":
            if journey.error == key:
                return  # Retry storms must not fill the journal or reset inactivity.
            journey.error = key
        if actor == "user":
            journey.last_action_at = now
        if kind == "step":
            if journey.step == key and not journey.error:
                return
            journey.step = key
            journey.step_at = now
            if journey.error:
                session.add(
                    JourneyEvent(journey_id=journey.id, kind="recovered", key=journey.error)
                )
            journey.error = None
        journey.updated_at = now
        session.add(JourneyEvent(journey_id=journey.id, kind=kind, key=key, actor=actor))

    async def add(
        self,
        session: AsyncSession,
        user_id: str,
        kind: str,
        key: str,
        *,
        profile_id: str | None = None,
        actor: str = "system",
        advance: bool = False,
    ) -> None:
        journey = await self._journey(session, user_id, profile_id=profile_id)
        if not journey:
            return
        if kind == "step" or advance:
            previous = journey.step
            # Background sends and old profile buttons cannot undo a confirmed payment.
            if profile_id and (
                (
                    previous in {"paid", "delivered", "full_offer"}
                    and key in {"free", "offer", "email", "invoice"}
                )
                or (previous in {"email", "invoice"} and key in {"free", "offer"})
            ):
                self._append(session, journey, "fact", key, actor)
                return
            if key in ADVANCES.get(previous, set()):
                self._append(session, journey, "passed", previous, actor)
        self._append(session, journey, kind, key, actor)
        if advance:
            self._append(session, journey, "step", key, actor)
        if kind == "step" or advance:
            for button in sorted(step_buttons(key)):
                self._append(session, journey, "button_sent", button, actor)

    async def record(
        self,
        telegram_id: int,
        kind: str,
        key: str,
        *,
        profile_id: str | None = None,
        actor: str = "system",
        advance: bool = False,
    ) -> None:
        async with self.sessions() as session, session.begin():
            user_id = await self._user_id(session, telegram_id)
            if user_id:
                await self.add(
                    session, user_id, kind, key, profile_id=profile_id, actor=actor, advance=advance
                )

    async def record_profile(
        self,
        profile_id: str,
        kind: str,
        key: str,
        *,
        actor: str = "system",
        advance: bool = False,
    ) -> None:
        async with self.sessions() as session, session.begin():
            profile = await session.get(Profile, profile_id)
            if profile:
                await self.add(
                    session,
                    profile.user_id,
                    kind,
                    key,
                    profile_id=profile_id,
                    actor=actor,
                    advance=advance,
                )

    async def current(self, since: datetime | None, mode: str) -> list[tuple[User, Journey | None]]:
        # Select latest first, then filter: a previous paid journey cannot hide a new unfinished one.
        latest = select(func.max(Journey.id)).group_by(Journey.user_id)
        query = (
            select(User, Journey)
            .outerjoin(Journey, (Journey.user_id == User.id) & Journey.id.in_(latest))
            .where(User.telegram_id_encrypted != "deleted")
        )
        async with self.sessions() as session:
            rows = (await session.execute(query)).all()
        return [
            (user, journey)
            for user, journey in rows
            if (mode == "all" or (journey.mode if journey else "unknown") == mode)
            and (
                since is None or as_utc(journey.updated_at if journey else user.created_at) >= since
            )
        ]

    async def funnel(self, since: datetime | None, mode: str) -> dict[str, Any]:
        query = select(Journey).where(Journey.complete_history.is_(True))
        if since:
            query = query.where(Journey.created_at >= since)
        if mode != "all":
            query = query.where(Journey.mode == mode)
        async with self.sessions() as session:
            journeys = list((await session.scalars(query)).all())
            ids = [j.id for j in journeys]
            events = (
                list(
                    (
                        await session.scalars(
                            select(JourneyEvent).where(
                                JourneyEvent.journey_id.in_(ids),
                                JourneyEvent.kind.in_(("step", "passed", "click")),
                            )
                        )
                    ).all()
                )
                if ids
                else []
            )
        reached: dict[str, set[int]] = defaultdict(set)
        passed: dict[str, set[int]] = defaultdict(set)
        user_by_journey = {journey.id: journey.user_id for journey in journeys}
        people: dict[str, set[str]] = defaultdict(set)
        people["start"] = set(user_by_journey.values())
        milestone = {
            "date_decade": "consent",
            "precision": "date",
            "city": "time",
            "confirm": "city",
            "calculation": "confirm",
            "free": "free",
            "offer": "offer",
            "paid": "paid",
            "delivered": "delivered",
        }
        for event in events:
            if event.kind in {"step", "passed"}:
                (reached if event.kind == "step" else passed)[event.key].add(event.journey_id)
            if event.kind == "step" and event.key in milestone:
                people[milestone[event.key]].add(user_by_journey[event.journey_id])
            if event.kind == "click" and event.key == "buy":
                people["buy"].add(user_by_journey[event.journey_id])
        previous = people["start"]
        for key, _ in FUNNEL[1:]:
            people[key] &= previous
            previous = people[key]
        return {"total": len(journeys), "reached": reached, "passed": passed, "people": people}

    async def buttons(self, since: datetime | None, mode: str) -> dict[str, dict[str, int]]:
        query = select(JourneyEvent, Journey).join(Journey).order_by(JourneyEvent.id)
        if mode != "all":
            query = query.where(Journey.mode == mode)
        # Exposure cohort: include later clicks even if a message was sent before a repeat visit.
        async with self.sessions() as session:
            rows = (await session.execute(query)).all()
        exposed: dict[str, dict[str, list[tuple[Journey, int]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        clicks: set[tuple[int, str]] = set()
        advanced: set[tuple[int, str]] = set()
        pending: dict[int, set[str]] = defaultdict(set)
        for event, journey in rows:
            if event.kind == "button_sent":
                if since is None or as_utc(event.created_at) >= since:
                    exposed[event.key][journey.user_id].append((journey, event.id))
                    pending[journey.id].add(event.key)
            elif event.kind == "click":
                if event.key in pending[journey.id]:
                    clicks.add((journey.id, event.key))
            elif event.kind == "step":
                current_buttons = step_buttons(event.key)
                for key in pending[journey.id] - current_buttons:
                    advanced.add((journey.id, key))
        result = {}
        latest_ids: dict[str, int] = {}
        for _, journey in rows:
            latest_ids[journey.user_id] = max(latest_ids.get(journey.user_id, 0), journey.id)
        for key, users in exposed.items():
            counts = {"sent": len(users), "clicked": 0, "continued": 0, "waiting": 0, "inactive": 0}
            for exposures in users.values():
                if any((j.id, key) in clicks for j, _ in exposures):
                    counts["clicked"] += 1
                elif any(
                    (j.id, key) in advanced or latest_ids[j.user_id] > j.id for j, _ in exposures
                ):
                    counts["continued"] += 1
                else:
                    latest = max((j for j, _ in exposures), key=lambda j: j.id)
                    counts[
                        "inactive"
                        if utcnow()
                        - max(
                            as_utc(latest.last_action_at or latest.step_at), as_utc(latest.step_at)
                        )
                        >= timedelta(days=1)
                        else "waiting"
                    ] += 1
            result[key] = counts
        return result

    async def history(
        self, user_id: str, journey_id: int | None, page: int
    ) -> dict[str, Any] | None:
        async with self.sessions() as session:
            user = await session.get(User, user_id)
            if not user or user.telegram_id_encrypted == "deleted":
                return None
            journeys = list(
                (
                    await session.scalars(
                        select(Journey)
                        .where(Journey.user_id == user_id)
                        .order_by(Journey.id.desc())
                    )
                ).all()
            )
            journey = (
                next((j for j in journeys if j.id == journey_id), None)
                if journey_id
                else next(iter(journeys), None)
            )
            events = (
                list(
                    (
                        await session.scalars(
                            select(JourneyEvent)
                            .where(
                                JourneyEvent.journey_id == journey.id,
                                JourneyEvent.kind != "button_sent",
                            )
                            .order_by(JourneyEvent.id.desc())
                            .offset(page * 15)
                            .limit(16)
                        )
                    ).all()
                )
                if journey
                else []
            )
            telegram_id = self.crypto.decrypt(
                user.telegram_id_encrypted, context=f"user.telegram_id:{user.id}"
            )
            return {
                "user": user,
                "telegram_id": telegram_id,
                "journeys": journeys,
                "journey": journey,
                "events": events[:15],
                "more": len(events) > 15,
            }

    async def period_steps(self, since: datetime | None, until: datetime) -> dict[str, int]:
        """Unique people per observed milestone date, including legacy facts, without cohort gating."""

        def in_period(column: Any) -> Any:
            return (column <= until) & (column >= since if since else True)

        queries = {
            "start": select(Event.user_id)
            .where(Event.name == "bot_started", in_period(Event.created_at))
            .union(
                select(Journey.user_id).where(
                    Journey.complete_history.is_(True), in_period(Journey.created_at)
                )
            ),
            "calculated": select(Profile.user_id).where(
                Profile.deleted_at.is_(None),
                Profile.status.in_(("calculated", "paid")),
                in_period(Profile.created_at),
            ),
            "offer": select(Profile.user_id)
            .join(StrengthOffer)
            .where(
                Profile.deleted_at.is_(None),
                StrengthOffer.status == DeliveryStatus.SENT,
                in_period(StrengthOffer.sent_at),
            )
            .union(
                select(Event.user_id).where(
                    Event.name == "offer_viewed", in_period(Event.created_at)
                )
            ),
            "buy": select(Order.user_id)
            .where(in_period(Order.created_at))
            .union(
                select(Journey.user_id)
                .join(JourneyEvent)
                .where(
                    JourneyEvent.kind == "click",
                    JourneyEvent.key == "buy",
                    in_period(JourneyEvent.created_at),
                )
            ),
        }
        result = {}
        async with self.sessions() as session:
            for key, query in queries.items():
                result[key] = int(
                    await session.scalar(
                        select(func.count(User.id)).where(
                            User.telegram_id_encrypted != "deleted", User.id.in_(query)
                        )
                    )
                    or 0
                )
        return result

    async def finances(
        self, since: datetime | None, until: datetime | None = None
    ) -> dict[str, dict[str, int]]:
        query = select(Order, Payment).join(Payment)
        if since:
            query = query.where(Payment.received_at >= since)
        if until:
            query = query.where(Payment.received_at <= until)
        async with self.sessions() as session:
            rows = (await session.execute(query)).all()
        result = {
            mode: {"payments": 0, "net_minor": 0, "refunds": 0}
            for mode in ("live", "test", "unknown")
        }
        for order, payment in rows:
            mode = "test" if order.provider == "fake" else order.analytics_mode
            row = result[mode if mode in result else "unknown"]
            row["payments"] += 1
            if payment.refund_status == "finished":
                row["refunds"] += 1
            else:
                row["net_minor"] += payment.amount_minor
        return result

    async def cleanup(self) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                delete(Journey).where(
                    Journey.updated_at < utcnow() - timedelta(days=RETENTION_DAYS)
                )
            )

    async def backfill(self, bot_id: int) -> None:
        """Import only persisted facts, without inventing exposures, clicks or activity times."""
        async with self.sessions() as session, session.begin():
            users = list(
                (
                    await session.scalars(
                        select(User).where(
                            User.telegram_id_encrypted != "deleted",
                            ~User.id.in_(select(Journey.user_id)),
                        )
                    )
                ).all()
            )
            for user in users:
                profiles = list(
                    (
                        await session.scalars(
                            select(Profile)
                            .where(
                                Profile.user_id == user.id,
                                Profile.deleted_at.is_(None),
                            )
                            .order_by(Profile.created_at)
                        )
                    ).all()
                )
                for profile in profiles:
                    journey = Journey(
                        user_id=user.id,
                        profile_id=profile.id,
                        mode="unknown",
                        source=user.last_source,
                        complete_history=False,
                        step="unknown",
                        created_at=profile.created_at,
                        step_at=profile.created_at,
                        updated_at=profile.created_at,
                    )
                    session.add(journey)
                    await session.flush()
                    session.add(
                        JourneyEvent(
                            journey_id=journey.id,
                            kind="fact",
                            key="calculation",
                            created_at=profile.created_at,
                        )
                    )
                    offer = await session.scalar(
                        select(StrengthOffer).where(StrengthOffer.profile_id == profile.id)
                    )
                    if offer and offer.sent_at:
                        journey.step, journey.step_at = "offer", offer.sent_at
                    order = await session.scalar(
                        select(Order)
                        .where(Order.profile_id == profile.id)
                        .order_by(Order.created_at.desc())
                        .limit(1)
                    )
                    if order:
                        journey.mode = "test" if order.provider == "fake" else order.analytics_mode
                        if order.status in {
                            OrderStatus.PAID,
                            OrderStatus.DELIVERED,
                            OrderStatus.REFUNDED,
                        }:
                            journey.step, journey.step_at = (
                                "paid",
                                order.paid_at or order.created_at,
                            )
                        items = list(
                            (
                                await session.scalars(
                                    select(DeliveryItem)
                                    .where(
                                        DeliveryItem.order_id == order.id,
                                        DeliveryItem.status == DeliveryStatus.SENT,
                                    )
                                    .order_by(DeliveryItem.sequence)
                                )
                            ).all()
                        )
                        for item in items:
                            if (
                                item.kind in {"avatar_result", "full_reading_offer"}
                                and item.sent_at
                            ):
                                journey.step = (
                                    "delivered" if item.kind == "avatar_result" else "full_offer"
                                )
                                journey.step_at = item.sent_at
                    journey.updated_at = journey.step_at
                    if journey.step != "unknown":
                        session.add(
                            JourneyEvent(
                                journey_id=journey.id,
                                kind="fact",
                                key=journey.step,
                                created_at=journey.step_at,
                            )
                        )
                telegram_id = self.crypto.decrypt(
                    user.telegram_id_encrypted, context=f"user.telegram_id:{user.id}"
                )
                key_hash = self.crypto.lookup(
                    f"{bot_id}:{telegram_id}:{telegram_id}:0:0:default", context="fsm-key"
                )
                record = await session.get(FsmRecord, key_hash)
                if record and record.state:
                    raw = (
                        self.crypto.decrypt_json(
                            record.data_encrypted, context=f"fsm.data:{key_hash}"
                        )
                        if record.data_encrypted
                        else {}
                    )
                    step = form_step(record.state, raw if isinstance(raw, dict) else {})
                    if step:
                        profile_id = raw.get("payment_profile_id") if step == "email" else None
                        existing = (
                            await session.scalar(
                                select(Journey).where(
                                    Journey.profile_id == profile_id, Journey.user_id == user.id
                                )
                            )
                            if profile_id
                            else None
                        )
                        if existing:
                            existing.step = step
                            existing.step_at = record.updated_at
                            existing.updated_at = record.updated_at
                        else:
                            session.add(
                                Journey(
                                    user_id=user.id,
                                    mode="unknown",
                                    source=user.last_source,
                                    complete_history=False,
                                    step=step,
                                    created_at=record.updated_at,
                                    step_at=record.updated_at,
                                    updated_at=record.updated_at,
                                )
                            )
