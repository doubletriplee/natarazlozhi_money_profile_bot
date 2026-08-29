from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from money_profile_bot.config import PaymentMode, Settings
from money_profile_bot.services.robokassa import RobokassaClient
from money_profile_bot.services.store import Store
from money_profile_bot.web.app import create_web_app


@pytest.mark.asyncio
async def test_fake_mode_legal_pages_disclose_that_no_purchase_occurs() -> None:
    settings = Settings(payment_mode=PaymentMode.FAKE, _env_file=None)
    app = create_web_app(
        settings,
        cast(Store, AsyncMock()),
        cast(RobokassaClient, AsyncMock()),
        None,
    )

    async with TestClient(TestServer(app)) as client:
        privacy_response = await client.get("/privacy")
        terms_response = await client.get("/terms")
        privacy = await privacy_response.text()
        terms = await terms_response.text()

    assert "списание денег не производится" in privacy
    assert "Платёжный оператор в тестовом сценарии не используется" in privacy
    assert "не сохраняет\nподтверждение совершеннолетия" in privacy
    assert "деньги не списываются" in terms
    assert "покупка и обязанность оплаты не возникают" in terms
    assert "Публичная продажа через бота пока отключена" in terms
