from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class PrivateAccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_user_ids: frozenset[int], denial_text: str) -> None:
        self.allowed_user_ids = allowed_user_ids
        self.denial_text = denial_text

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None and user.id not in self.allowed_user_ids:
            if isinstance(event, CallbackQuery):
                await event.answer(self.denial_text, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(self.denial_text)
            return None
        return await handler(event, data)
