from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select

from money_profile_bot.bot.router import build_router
from money_profile_bot.config import Settings
from money_profile_bot.crypto import CryptoBox
from money_profile_bot.database import Database
from money_profile_bot.domain import City
from money_profile_bot.models import JourneyEvent, utcnow
from money_profile_bot.services.avatar import AvatarAssets
from money_profile_bot.services.delivery import DeliveryWorker
from money_profile_bot.services.robokassa import RobokassaClient
from money_profile_bot.services.store import Store


class TelegramSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Message] = []

    async def close(self) -> None:
        pass

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        if method.__api_method__ == "getMe":
            return User(id=42, is_bot=True, first_name="Bot", username="test_bot")
        if method.__api_method__ in {"sendPhoto", "sendMessage", "editMessageText"}:
            message = Message(
                message_id=len(self.sent) + 1,
                date=utcnow(),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=User(id=42, is_bot=True, first_name="Bot"),
                text=getattr(method, "text", None),
                reply_markup=method.reply_markup,
            )
            self.sent.append(message)
            return message
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""


async def test_real_router_flow_produces_unique_people_funnel(tmp_path: Path, moscow: City) -> None:
    settings = Settings(_env_file=None, admin_telegram_ids="100")
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'flow.sqlite3').as_posix()}")
    await database.initialize()
    store = Store(
        database.sessions,
        CryptoBox(settings.app_encryption_key, settings.lookup_hmac_key),
        cast(RobokassaClient, AsyncMock()),
        analytics_mode="test",
        payment_notification_ids=frozenset({200}),
        payment_notifications_include_test=True,
    )
    session = TelegramSession()
    bot = Bot("42:abcdefghijklmnopqrstuvwxzy0123456789", session=session)
    avatars = AvatarAssets(Path("assets/avatars"))
    delivery = DeliveryWorker(
        bot,
        store,
        avatars,
        sales_telegram_username="support",
        product_price_rub=settings.product_price_rub,
    )
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    cities = SimpleNamespace(search=AsyncMock(return_value=[moscow]))
    dispatcher.include_router(build_router(settings, store, cities, avatars, delivery))
    user = User(id=100, is_bot=False, first_name="Private Name")
    update_id = 0

    async def send(text: str) -> None:
        nonlocal update_id
        update_id += 1
        await dispatcher.feed_update(
            bot,
            Update(
                update_id=update_id,
                message=Message(
                    message_id=update_id + 100,
                    date=utcnow(),
                    chat=Chat(id=100, type="private"),
                    from_user=user,
                    text=text,
                ),
            ),
        )

    async def click(value: str) -> None:
        nonlocal update_id
        update_id += 1
        # Every click comes from a keyboard actually delivered by the router.
        assert any(
            button.callback_data == value
            for row in session.sent[-1].reply_markup.inline_keyboard
            for button in row
        )
        await dispatcher.feed_update(
            bot,
            Update(
                update_id=update_id,
                callback_query=CallbackQuery(
                    id=str(update_id),
                    from_user=user,
                    chat_instance="test",
                    message=session.sent[-1],
                    data=value,
                ),
            ),
        )

    try:
        await send("/start campaign")
        await click("consent:yes")
        await click("birth_date:decade:1990")
        # An admin can inspect stats while filling out the form; it must not eat the date input.
        prior_prompt = session.sent[-1]
        await send("/myid")
        assert "Telegram ID: <code>100</code>" in session.sent[-1].text
        await send("/stats")
        assert "Воронка с полной историей" in session.sent[-1].text
        session.sent.append(prior_prompt)
        await click("birth_date:year:1990")
        await click("birth_date:month:1990:1")
        await click("birth_date:day:1990:1:15")
        await click("precision:unknown")
        await send("Москва")
        await click("city:0")
        await click("form:confirm")
        profile_id = (await store.profile_access(100)).profile_id
        await click(f"strength:{profile_id}")
        await click(f"buy:{profile_id}")
        await delivery._deliver_pending_cycle()
        notifications = [message for message in session.sent if message.chat.id == 200]
        assert len(notifications) == 1
        assert "Покупки и списания денег не произошло" in notifications[0].text
        funnel = await store.analytics.funnel(None, "test")
        assert all(len(people) == 1 for people in funnel["people"].values())
        assert len(funnel["people"]["paid"]) == 1
        assert len(funnel["people"]["delivered"]) == 1
        assert (await store.analytics.current(None, "all"))[0][1].step == "delivered"
        async with store.sessions() as connection:
            events = list((await connection.scalars(select(JourneyEvent))).all())
        assert not any(
            "1990" in event.key or "Москва" in event.key or "Private" in event.key
            for event in events
        )
        assert any(event.kind == "skipped" for event in events)
    finally:
        await dispatcher.storage.close()
        await dispatcher.fsm.events_isolation.close()
        await bot.session.close()
        await database.close()
