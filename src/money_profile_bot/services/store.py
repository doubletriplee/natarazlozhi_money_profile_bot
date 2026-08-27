from __future__ import annotations

import asyncio
import json
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from money_profile_bot.crypto import CryptoBox
from money_profile_bot.domain import BirthData, ChartFacts, GeneratedProfile
from money_profile_bot.models import (
    AdminAudit,
    AdminIdentity,
    Consent,
    DeliveryItem,
    DeliveryStatus,
    Event,
    Feedback,
    Order,
    OrderStatus,
    Payment,
    Profile,
    ProfileStatus,
    User,
    new_id,
    utcnow,
)
from money_profile_bot.services.robokassa import Invoice, RobokassaClient


def _order_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "MP-" + "".join(secrets.choice(alphabet) for _ in range(8))


def _invoice_id() -> int:
    return secrets.randbelow(2_000_000_000) + 1


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class OrderLink:
    order_id: str
    code: str
    url: str
    reused: bool


@dataclass(frozen=True, slots=True)
class CallbackResult:
    order_id: str
    newly_paid: bool


@dataclass(frozen=True, slots=True)
class AdminClaim:
    is_admin: bool
    newly_claimed: bool


@dataclass(frozen=True, slots=True)
class ProfileAccess:
    profile_id: str
    profile_status: str
    order_id: str | None
    order_status: str | None
    payment_url: str | None
    order_code: str | None


class Store:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        crypto: CryptoBox,
        robokassa: RobokassaClient,
    ) -> None:
        self.sessions = sessions
        self.crypto = crypto
        self.robokassa = robokassa
        self._payment_lock = asyncio.Lock()
        self._admin_lock = asyncio.Lock()

    async def ensure_user(self, telegram_id: int, source: str | None = None) -> User:
        digest = self.crypto.lookup(str(telegram_id), context="telegram-user")
        async with self.sessions() as session, session.begin():
            user = await session.scalar(select(User).where(User.telegram_id_hash == digest))
            if user:
                if source:
                    user.last_source = source
                return user
            user_id = new_id()
            user = User(
                id=user_id,
                telegram_id_hash=digest,
                telegram_id_encrypted=self.crypto.encrypt(
                    str(telegram_id), context=f"user.telegram_id:{user_id}"
                ),
                first_source=source,
                last_source=source,
            )
            session.add(user)
            await session.flush()
            return user

    async def claim_admin_if_unset(self, telegram_id: int) -> AdminClaim:
        """Atomically claim the single bootstrap-admin slot in a one-process deployment."""
        digest = self.crypto.lookup(str(telegram_id), context="admin-telegram-id")
        async with self._admin_lock:
            async with self.sessions() as session, session.begin():
                identity = await session.get(AdminIdentity, 1)
                if identity:
                    return AdminClaim(identity.telegram_id_hash == digest, False)
                session.add(AdminIdentity(slot=1, telegram_id_hash=digest))
                return AdminClaim(True, True)

    async def is_admin(self, telegram_id: int, configured_ids: frozenset[int]) -> bool:
        if telegram_id in configured_ids:
            return True
        digest = self.crypto.lookup(str(telegram_id), context="admin-telegram-id")
        async with self.sessions() as session:
            identity = await session.get(AdminIdentity, 1)
            return bool(identity and secrets.compare_digest(identity.telegram_id_hash, digest))

    async def record_event(
        self, telegram_id: int | None, name: str, metadata: dict[str, object] | None = None
    ) -> None:
        user = await self.ensure_user(telegram_id) if telegram_id is not None else None
        async with self.sessions() as session, session.begin():
            attached = await session.get(User, user.id) if user else None
            session.add(
                Event(
                    user_id=attached.id if attached else None,
                    name=name,
                    first_source=attached.first_source if attached else None,
                    last_source=attached.last_source if attached else None,
                    metadata_json=json.dumps(metadata or {}, separators=(",", ":")),
                )
            )

    async def save_consent(self, telegram_id: int, documents_version: str) -> None:
        user = await self.ensure_user(telegram_id)
        async with self.sessions() as session, session.begin():
            session.add(Consent(user_id=user.id, documents_version=documents_version))

    async def save_calculation(
        self,
        telegram_id: int,
        birth_data: BirthData,
        facts: ChartFacts,
        result: GeneratedProfile,
    ) -> str:
        user = await self.ensure_user(telegram_id)
        profile_id = new_id()
        async with self.sessions() as session, session.begin():
            existing = await session.scalar(
                select(Profile)
                .where(
                    Profile.user_id == user.id,
                    Profile.status.in_((ProfileStatus.PAID, ProfileStatus.CALCULATED)),
                    Profile.deleted_at.is_(None),
                )
                .order_by(Profile.created_at.desc())
            )
            if existing and existing.locked:
                return existing.id
            profile = Profile(
                id=profile_id,
                user_id=user.id,
                status=ProfileStatus.CALCULATED,
                birth_data_encrypted=self.crypto.encrypt_json(
                    birth_data.to_dict(), context=f"profile.birth:{profile_id}"
                ),
                facts_encrypted=self.crypto.encrypt_json(
                    facts.to_dict(), context=f"profile.facts:{profile_id}"
                ),
                result_encrypted=self.crypto.encrypt_json(
                    result.to_dict(), context=f"profile.result:{profile_id}"
                ),
                engine_version=result.engine_version,
                rules_version=result.rules_version,
            )
            session.add(profile)
        return profile_id

    async def get_profile_result(self, profile_id: str) -> tuple[BirthData, GeneratedProfile]:
        async with self.sessions() as session:
            profile = await session.get(Profile, profile_id)
            if not profile or not profile.result_encrypted:
                raise LookupError("profile not found")
            birth = BirthData.from_dict(
                self.crypto.decrypt_json(
                    profile.birth_data_encrypted, context=f"profile.birth:{profile.id}"
                )
            )
            result_data = self.crypto.decrypt_json(
                profile.result_encrypted, context=f"profile.result:{profile.id}"
            )
            result_data["messages"] = tuple(result_data["messages"])
            result_data["triggered_rule_ids"] = tuple(result_data["triggered_rule_ids"])
            return birth, GeneratedProfile(**result_data)

    async def latest_profile_for_user(self, telegram_id: int) -> Profile | None:
        digest = self.crypto.lookup(str(telegram_id), context="telegram-user")
        async with self.sessions() as session:
            return cast(
                Profile | None,
                await session.scalar(
                    select(Profile)
                    .join(User)
                    .where(User.telegram_id_hash == digest, Profile.deleted_at.is_(None))
                    .order_by(Profile.created_at.desc())
                ),
            )

    async def profile_access(self, telegram_id: int) -> ProfileAccess | None:
        digest = self.crypto.lookup(str(telegram_id), context="telegram-user")
        async with self.sessions() as session:
            profile = await session.scalar(
                select(Profile)
                .join(User)
                .where(User.telegram_id_hash == digest, Profile.deleted_at.is_(None))
                .order_by(Profile.created_at.desc())
            )
            if not profile:
                return None
            order = await session.scalar(
                select(Order)
                .where(Order.profile_id == profile.id)
                .order_by(Order.created_at.desc())
            )
            return ProfileAccess(
                profile_id=profile.id,
                profile_status=profile.status,
                order_id=order.id if order else None,
                order_status=order.status if order else None,
                payment_url=order.payment_url if order else None,
                order_code=order.code if order else None,
            )

    async def create_order(
        self, *, telegram_id: int, profile_id: str, email: str, amount_minor: int
    ) -> OrderLink:
        user = await self.ensure_user(telegram_id)
        now = datetime.now(UTC)
        async with self.sessions() as session:
            existing = await session.scalar(
                select(Order)
                .where(
                    Order.user_id == user.id,
                    Order.profile_id == profile_id,
                    Order.status.in_(
                        (OrderStatus.INVOICE_CREATED, OrderStatus.PAID, OrderStatus.DELIVERED)
                    ),
                )
                .order_by(Order.created_at.desc())
            )
            if existing:
                if existing.status in (OrderStatus.PAID, OrderStatus.DELIVERED):
                    return OrderLink(existing.id, existing.code, existing.payment_url or "", True)
                if existing.payment_url and (
                    not existing.expires_at or _utc(existing.expires_at) > now
                ):
                    return OrderLink(existing.id, existing.code, existing.payment_url, True)

        for _ in range(5):
            order_id = new_id()
            code = _order_code()
            invoice_id = _invoice_id()
            encrypted_email = self.crypto.encrypt(email, context=f"order.email:{order_id}")
            try:
                async with self.sessions() as session, session.begin():
                    session.add(
                        Order(
                            id=order_id,
                            code=code,
                            user_id=user.id,
                            profile_id=profile_id,
                            amount_minor=amount_minor,
                            provider_invoice_id=invoice_id,
                            receipt_email_encrypted=encrypted_email,
                            status=OrderStatus.PENDING,
                            expires_at=now + timedelta(hours=24),
                        )
                    )
                break
            except IntegrityError:
                continue
        else:
            raise RuntimeError("cannot allocate a unique payment order")

        invoice: Invoice = await self.robokassa.create_invoice(
            invoice_id=invoice_id,
            order_code=code,
            amount_minor=amount_minor,
            email=email,
        )
        if invoice.invoice_id != invoice_id:
            raise RuntimeError("Robokassa returned a different invoice id")
        async with self.sessions() as session, session.begin():
            order = await session.get(Order, order_id)
            if not order:
                raise RuntimeError("order disappeared during invoice creation")
            order.provider_invoice_uuid = invoice.invoice_uuid
            order.payment_url = invoice.payment_url
            order.status = OrderStatus.INVOICE_CREATED
        return OrderLink(order_id, code, invoice.payment_url, False)

    async def create_fake_paid_order(self, *, telegram_id: int, profile_id: str) -> OrderLink:
        """Create a zero-value test order and unlock delivery without contacting a provider."""
        async with self._payment_lock:
            user = await self.ensure_user(telegram_id)
            async with self.sessions() as session:
                existing = await session.scalar(
                    select(Order)
                    .where(
                        Order.user_id == user.id,
                        Order.profile_id == profile_id,
                        Order.provider == "fake",
                        Order.status.in_(
                            (
                                OrderStatus.PENDING,
                                OrderStatus.PAID,
                                OrderStatus.DELIVERED,
                            )
                        ),
                    )
                    .order_by(Order.created_at.desc())
                )
            if existing:
                if existing.status == OrderStatus.PENDING:
                    result = await self._accept_payment_callback_locked(
                        invoice_id=existing.provider_invoice_id,
                        amount_minor=0,
                        email=None,
                    )
                    return OrderLink(existing.id, existing.code, "", not result.newly_paid)
                return OrderLink(existing.id, existing.code, "", True)

            for _ in range(5):
                order_id = new_id()
                code = _order_code()
                invoice_id = _invoice_id()
                try:
                    async with self.sessions() as session, session.begin():
                        session.add(
                            Order(
                                id=order_id,
                                code=code,
                                user_id=user.id,
                                profile_id=profile_id,
                                amount_minor=0,
                                provider="fake",
                                provider_invoice_id=invoice_id,
                                receipt_email_encrypted=self.crypto.encrypt(
                                    "test-without-receipt", context=f"order.email:{order_id}"
                                ),
                                status=OrderStatus.PENDING,
                            )
                        )
                    break
                except IntegrityError:
                    continue
            else:
                raise RuntimeError("cannot allocate a unique test order")

            await self._accept_payment_callback_locked(
                invoice_id=invoice_id,
                amount_minor=0,
                email=None,
            )
            return OrderLink(order_id, code, "", False)

    async def accept_payment_callback(
        self, *, invoice_id: int, amount_minor: int, email: str | None
    ) -> CallbackResult:
        async with self._payment_lock:
            return await self._accept_payment_callback_locked(
                invoice_id=invoice_id, amount_minor=amount_minor, email=email
            )

    async def _accept_payment_callback_locked(
        self, *, invoice_id: int, amount_minor: int, email: str | None
    ) -> CallbackResult:
        async with self.sessions() as session, session.begin():
            order = await session.scalar(
                select(Order).where(Order.provider_invoice_id == invoice_id)
            )
            if not order:
                raise LookupError("unknown invoice")
            if order.amount_minor != amount_minor or order.currency != "RUB":
                raise ValueError("payment amount or currency does not match the order")
            if order.status in (OrderStatus.PAID, OrderStatus.DELIVERED):
                return CallbackResult(order.id, False)
            if order.status in (OrderStatus.REFUNDED, OrderStatus.FAILED):
                raise ValueError("order cannot be paid in its current state")

            payment = await session.scalar(select(Payment).where(Payment.order_id == order.id))
            if payment is None:
                payment = Payment(
                    order_id=order.id,
                    amount_minor=amount_minor,
                    currency="RUB",
                    notification_email_encrypted=(
                        self.crypto.encrypt(email, context=f"payment.email:{order.id}")
                        if email
                        else None
                    ),
                )
                session.add(payment)
            order.status = OrderStatus.PAID
            order.paid_at = utcnow()
            profile = await session.get(Profile, order.profile_id)
            if not profile:
                raise RuntimeError("profile for paid order not found")
            profile.status = ProfileStatus.PAID
            profile.locked = True
            existing_count = await session.scalar(
                select(func.count())
                .select_from(DeliveryItem)
                .where(DeliveryItem.order_id == order.id)
            )
            if not existing_count:
                kinds = [*(f"message:{index}" for index in range(6)), "card", "feedback"]
                session.add_all(
                    [
                        DeliveryItem(order_id=order.id, sequence=index, kind=kind)
                        for index, kind in enumerate(kinds, start=1)
                    ]
                )
            session.add(
                Event(
                    user_id=order.user_id,
                    name="payment_succeeded",
                    metadata_json=json.dumps(
                        {
                            "amount_minor": amount_minor,
                            "currency": "RUB",
                            "provider": order.provider,
                        }
                    ),
                )
            )
            return CallbackResult(order.id, True)

    async def delivery_context(
        self, order_id: str
    ) -> tuple[Order, int, BirthData, GeneratedProfile, list[DeliveryItem]]:
        async with self.sessions() as session:
            order = await session.get(Order, order_id)
            if not order:
                raise LookupError("order not found")
            user = await session.get(User, order.user_id)
            if not user:
                raise LookupError("user not found")
            telegram_id = int(
                self.crypto.decrypt(
                    user.telegram_id_encrypted, context=f"user.telegram_id:{user.id}"
                )
            )
            birth, result = await self.get_profile_result(order.profile_id)
            items = list(
                (
                    await session.scalars(
                        select(DeliveryItem)
                        .where(DeliveryItem.order_id == order.id)
                        .order_by(DeliveryItem.sequence)
                    )
                ).all()
            )
            return order, telegram_id, birth, result, items

    async def pending_order_ids(self) -> list[str]:
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(Order.id)
                        .join(DeliveryItem)
                        .where(
                            Order.status == OrderStatus.PAID,
                            DeliveryItem.status.in_(
                                (DeliveryStatus.PENDING, DeliveryStatus.FAILED)
                            ),
                        )
                        .distinct()
                    )
                ).all()
            )

    async def mark_delivery_item(
        self,
        item_id: str,
        *,
        status: DeliveryStatus,
        message_id: int | None = None,
        error_code: str | None = None,
    ) -> None:
        async with self.sessions() as session, session.begin():
            item = await session.get(DeliveryItem, item_id)
            if not item:
                return
            item.status = status
            item.attempts += 1
            item.telegram_message_id = message_id or item.telegram_message_id
            item.last_error_code = error_code
            if status is DeliveryStatus.SENT:
                item.sent_at = utcnow()

    async def complete_delivery_if_ready(self, order_id: str) -> bool:
        async with self.sessions() as session, session.begin():
            remaining = await session.scalar(
                select(func.count())
                .select_from(DeliveryItem)
                .where(
                    DeliveryItem.order_id == order_id,
                    DeliveryItem.status != DeliveryStatus.SENT,
                )
            )
            if remaining:
                return False
            order = await session.get(Order, order_id)
            if order and order.status == OrderStatus.PAID:
                order.status = OrderStatus.DELIVERED
                order.delivered_at = utcnow()
                session.add(Event(user_id=order.user_id, name="profile_delivered"))
            return True

    async def save_card_path(self, profile_id: str, path: str) -> None:
        async with self.sessions() as session, session.begin():
            profile = await session.get(Profile, profile_id)
            if profile:
                profile.card_path_encrypted = self.crypto.encrypt(
                    path, context=f"profile.card_path:{profile_id}"
                )

    async def save_feedback(
        self, telegram_id: int, profile_id: str, rating: int, comment: str | None = None
    ) -> None:
        if not 1 <= rating <= 5:
            raise ValueError("rating must be in 1..5")
        digest = self.crypto.lookup(str(telegram_id), context="telegram-user")
        async with self.sessions() as session, session.begin():
            profile = await session.scalar(
                select(Profile)
                .join(User)
                .where(Profile.id == profile_id, User.telegram_id_hash == digest)
            )
            if not profile:
                raise LookupError("profile not found")
            current = await session.scalar(
                select(Feedback).where(Feedback.profile_id == profile_id)
            )
            encrypted = (
                self.crypto.encrypt(comment, context=f"feedback.comment:{profile_id}")
                if comment
                else None
            )
            if current:
                current.rating = rating
                current.comment_encrypted = encrypted
            else:
                session.add(
                    Feedback(profile_id=profile_id, rating=rating, comment_encrypted=encrypted)
                )

    async def prepare_refund(self, order_code: str) -> str:
        token = f"{secrets.randbelow(1_000_000):06d}"
        async with self.sessions() as session, session.begin():
            order = await session.scalar(select(Order).where(Order.code == order_code.upper()))
            if not order:
                raise LookupError("order not found")
            if order.status not in (OrderStatus.PAID, OrderStatus.DELIVERED):
                raise ValueError("order is not refundable")
            if order.provider != "robokassa":
                raise ValueError("test orders do not have refundable payments")
            order.refund_confirmation_hash = self.crypto.lookup(
                f"{order.id}:{token}", context="refund-confirmation"
            )
            order.refund_confirmation_expires_at = utcnow() + timedelta(minutes=10)
        return token

    async def execute_refund(self, order_code: str, token: str) -> str:
        async with self.sessions() as session:
            order = await session.scalar(select(Order).where(Order.code == order_code.upper()))
            if not order:
                raise LookupError("order not found")
            expected = self.crypto.lookup(f"{order.id}:{token}", context="refund-confirmation")
            if (
                not order.refund_confirmation_hash
                or not secrets.compare_digest(expected, order.refund_confirmation_hash)
                or not order.refund_confirmation_expires_at
                or _utc(order.refund_confirmation_expires_at) < utcnow()
            ):
                raise ValueError("refund confirmation is invalid or expired")
            if order.status not in (OrderStatus.PAID, OrderStatus.DELIVERED):
                raise ValueError("order is not refundable")
            if order.provider != "robokassa":
                raise ValueError("test orders do not have refundable payments")
            payment = await session.scalar(select(Payment).where(Payment.order_id == order.id))
            if not payment:
                raise LookupError("payment journal entry not found")
            invoice_id = order.provider_invoice_id
            amount_minor = order.amount_minor
            order_id = order.id

        state = await self.robokassa.operation_state(invoice_id)
        if state.state_code != 100 or state.amount_minor != amount_minor:
            raise ValueError("Robokassa operation is not a matching successful payment")
        request_id = await self.robokassa.refund(
            operation_key=state.operation_key,
            amount_minor=amount_minor,
            order_code=order_code.upper(),
        )
        async with self.sessions() as session, session.begin():
            order = await session.get(Order, order_id)
            payment = await session.scalar(select(Payment).where(Payment.order_id == order_id))
            if not order or not payment:
                raise RuntimeError("refund state disappeared")
            payment.provider_operation_hash = self.crypto.lookup(
                state.operation_key, context="robokassa-operation"
            )
            payment.provider_operation_encrypted = self.crypto.encrypt(
                state.operation_key, context=f"payment.operation:{payment.id}"
            )
            payment.provider_payment_method = state.payment_method
            payment.refund_request_id = request_id
            payment.refund_status = "processing"
            order.refund_confirmation_hash = None
            order.refund_confirmation_expires_at = None
        return request_id

    async def refresh_refunds(self) -> int:
        async with self.sessions() as session:
            pending = list(
                (
                    await session.scalars(
                        select(Payment).where(
                            Payment.refund_status == "processing",
                            Payment.refund_request_id.is_not(None),
                        )
                    )
                ).all()
            )
            identifiers = [(item.id, item.order_id, item.refund_request_id) for item in pending]
        completed = 0
        for payment_id, order_id, request_id in identifiers:
            if not request_id:
                continue
            status = await self.robokassa.refund_state(request_id)
            async with self.sessions() as session, session.begin():
                payment = await session.get(Payment, payment_id)
                order = await session.get(Order, order_id)
                if not payment or not order:
                    continue
                payment.refund_status = status
                if status == "finished":
                    payment.refunded_at = utcnow()
                    order.status = OrderStatus.REFUNDED
                    order.refunded_at = utcnow()
                    session.add(Event(user_id=order.user_id, name="payment_refunded"))
                    completed += 1
        return completed

    async def cleanup_expired_drafts(self, older_than: datetime) -> int:
        async with self.sessions() as session, session.begin():
            profiles = list(
                (
                    await session.scalars(
                        select(Profile).where(
                            Profile.created_at < older_than,
                            Profile.status == ProfileStatus.CALCULATED,
                            Profile.locked.is_(False),
                        )
                    )
                ).all()
            )
            for profile in profiles:
                profile.birth_data_encrypted = "expired"
                profile.facts_encrypted = None
                profile.result_encrypted = None
                profile.card_path_encrypted = None
                profile.status = ProfileStatus.DELETED
                profile.deleted_at = utcnow()
            return len(profiles)

    async def delete_personal_data(self, telegram_id: int) -> list[str] | None:
        digest = self.crypto.lookup(str(telegram_id), context="telegram-user")
        async with self.sessions() as session, session.begin():
            user = await session.scalar(select(User).where(User.telegram_id_hash == digest))
            if not user:
                return None
            profiles = list(
                (await session.scalars(select(Profile).where(Profile.user_id == user.id))).all()
            )
            profile_ids = [profile.id for profile in profiles]
            card_paths = [
                self.crypto.decrypt(
                    profile.card_path_encrypted, context=f"profile.card_path:{profile.id}"
                )
                for profile in profiles
                if profile.card_path_encrypted
            ]
            if profile_ids:
                await session.execute(delete(Feedback).where(Feedback.profile_id.in_(profile_ids)))
                await session.execute(
                    update(Profile)
                    .where(Profile.id.in_(profile_ids))
                    .values(
                        birth_data_encrypted="deleted",
                        facts_encrypted=None,
                        result_encrypted=None,
                        card_path_encrypted=None,
                        status=ProfileStatus.DELETED,
                        deleted_at=utcnow(),
                    )
                )
                order_ids = select(Order.id).where(Order.profile_id.in_(profile_ids))
                await session.execute(
                    update(Payment)
                    .where(Payment.order_id.in_(order_ids))
                    .values(notification_email_encrypted=None)
                )
                await session.execute(
                    update(Order)
                    .where(Order.profile_id.in_(profile_ids))
                    .values(receipt_email_encrypted="deleted")
                )
                await session.execute(
                    update(DeliveryItem)
                    .where(DeliveryItem.order_id.in_(order_ids))
                    .values(telegram_message_id=None)
                )
            await session.execute(
                update(Event).where(Event.user_id == user.id).values(user_id=None)
            )
            await session.execute(
                delete(AdminIdentity).where(
                    AdminIdentity.telegram_id_hash
                    == self.crypto.lookup(str(telegram_id), context="admin-telegram-id")
                )
            )
            user.telegram_id_encrypted = "deleted"
            user.telegram_id_hash = self.crypto.lookup(
                f"deleted:{user.id}:{secrets.token_hex(16)}", context="telegram-user"
            )
            user.first_source = None
            user.last_source = None
            return card_paths

    async def stats(self, since: datetime | None) -> dict[str, Any]:
        async with self.sessions() as session:
            users_query = select(func.count()).select_from(User)
            profiles_query = select(func.count()).select_from(Profile)
            offers_query = (
                select(func.count()).select_from(Event).where(Event.name == "offer_viewed")
            )
            payments_query = select(func.count()).select_from(Payment)
            revenue_query = select(func.coalesce(func.sum(Payment.amount_minor), 0))
            first_query = select(User.first_source, func.count()).group_by(User.first_source)
            last_query = select(User.last_source, func.count()).group_by(User.last_source)
            if since:
                users_query = users_query.where(User.created_at >= since)
                profiles_query = profiles_query.where(Profile.created_at >= since)
                offers_query = offers_query.where(Event.created_at >= since)
                payments_query = payments_query.where(Payment.received_at >= since)
                revenue_query = revenue_query.where(Payment.received_at >= since)
                first_query = first_query.where(User.created_at >= since)
                last_query = last_query.where(User.created_at >= since)
            users = await session.scalar(users_query)
            profiles = await session.scalar(profiles_query)
            offers = await session.scalar(offers_query)
            payments = await session.scalar(payments_query)
            revenue = await session.scalar(revenue_query)
            first_sources = (
                await session.execute(first_query.order_by(func.count().desc()).limit(10))
            ).all()
            last_sources = (
                await session.execute(last_query.order_by(func.count().desc()).limit(10))
            ).all()
            conversion = (int(payments or 0) / int(offers) * 100) if offers else 0.0
            return {
                "users": int(users or 0),
                "profiles": int(profiles or 0),
                "offers": int(offers or 0),
                "payments": int(payments or 0),
                "conversion": conversion,
                "revenue_rub": Decimal(int(revenue or 0)) / 100,
                "first_sources": first_sources,
                "last_sources": last_sources,
            }

    async def audit_admin(
        self, admin_telegram_id: int, action: str, target_code: str | None = None
    ) -> None:
        async with self.sessions() as session, session.begin():
            session.add(
                AdminAudit(
                    admin_id_hash=self.crypto.lookup(
                        str(admin_telegram_id), context="admin-telegram-id"
                    ),
                    action=action,
                    target_code=target_code,
                )
            )

    async def healthcheck(self) -> bool:
        async with self.sessions() as session:
            return bool((await session.scalar(text("SELECT 1"))) == 1)
