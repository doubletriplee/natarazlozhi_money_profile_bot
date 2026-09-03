from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import TelegramObject

from money_profile_bot.bot.access import PrivateAccessMiddleware


@pytest.mark.asyncio
async def test_staging_access_allows_only_configured_users() -> None:
    middleware = PrivateAccessMiddleware(
        frozenset({10001}),
        "Тестовый бот закрыт. Доступ предоставляется владельцем.",
    )
    handler = AsyncMock(return_value="handled")
    event = TelegramObject()

    allowed = await middleware(
        handler,
        event,
        {"event_from_user": SimpleNamespace(id=10001)},
    )
    denied = await middleware(
        handler,
        event,
        {"event_from_user": SimpleNamespace(id=99999)},
    )

    assert allowed == "handled"
    assert denied is None
    handler.assert_awaited_once_with(event, {"event_from_user": SimpleNamespace(id=10001)})


@pytest.mark.asyncio
async def test_private_access_uses_environment_specific_denial_text() -> None:
    middleware = PrivateAccessMiddleware(frozenset({10001}), "Закрытый пилот недоступен.")
    handler = AsyncMock()
    event = TelegramObject()

    result = await middleware(
        handler,
        event,
        {"event_from_user": SimpleNamespace(id=99999)},
    )

    assert result is None
    handler.assert_not_awaited()
