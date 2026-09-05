from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select

from money_profile_bot.models import Order
from money_profile_bot.services.analytics import Analytics, callback_key, form_step


class JourneyMiddleware(BaseMiddleware):
    def __init__(self, analytics: Analytics) -> None:
        self.analytics = analytics

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        user = getattr(event, "from_user", None)
        if not state or not user:
            return await handler(event, data)
        raw = await state.get_data()
        before = form_step(await state.get_state(), raw)
        callback = event.data or "" if isinstance(event, CallbackQuery) else ""
        key = callback_key(callback) if callback else None
        text = event.text or "" if isinstance(event, Message) else ""
        command = text.split(maxsplit=1)[0].split("@")[0] if text.startswith("/") else ""
        if callback.startswith(("stats:", "report:", "delete:")) or command not in {
            "",
            "/start",
            "/profile",
        }:
            return await handler(event, data)
        if not (key or before or command):
            return await handler(event, data)
        profile_id: str | None = None
        if callback.startswith(("strength:", "buy:")):
            profile_id = callback.split(":", 1)[1]
        elif callback.startswith("full:"):
            async with self.analytics.sessions() as session:
                profile_id = await session.scalar(
                    select(Order.profile_id).where(Order.id == callback.split(":", 1)[1])
                )
            if profile_id is None:
                return await handler(event, data)
        elif before == "email" and not command and key != "new":
            profile_id = raw.get("payment_profile_id")
        if key:
            await self.analytics.record(user.id, "click", key, actor="user", profile_id=profile_id)
        elif not command and before:
            await self.analytics.record(
                user.id, "action", "input", actor="user", profile_id=profile_id
            )
        try:
            result = await handler(event, data)
        except Exception as exc:
            await self.analytics.record(
                user.id,
                "error",
                "send" if isinstance(exc, TelegramAPIError) else "handler",
                profile_id=profile_id,
            )
            raise
        after = form_step(await state.get_state(), await state.get_data())
        if isinstance(event, CallbackQuery) and not isinstance(event.message, Message):
            return result
        if after and (after != before or command == "/start" or key in {"new", "restart"}):
            if callback == "precision:unknown":
                await self.analytics.record(user.id, "skipped", "time_unknown")
            await self.analytics.record(user.id, "step", after, profile_id=profile_id)
        elif key == "cancel":
            await self.analytics.record(user.id, "step", "cancelled")
        return result
