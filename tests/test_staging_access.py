from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import TelegramObject

from money_profile_bot.bot.access import StagingAccessMiddleware


@pytest.mark.asyncio
async def test_staging_access_allows_only_configured_users() -> None:
    middleware = StagingAccessMiddleware(frozenset({10001}))
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
