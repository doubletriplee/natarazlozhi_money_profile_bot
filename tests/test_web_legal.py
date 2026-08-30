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
    settings = Settings(
        payment_mode=PaymentMode.FAKE,
        payment_retention_days=30,
        _env_file=None,
    )
    app = create_web_app(
        settings,
        cast(Store, AsyncMock()),
        cast(RobokassaClient, AsyncMock()),
        None,
    )

    async with TestClient(TestServer(app)) as client:
        privacy_response = await client.get("/privacy")
        terms_response = await client.get("/terms")
        consent_response = await client.get("/consent")
        privacy = await privacy_response.text()
        terms = await terms_response.text()
        consent = await consent_response.text()

    assert privacy_response.status == 200
    assert terms_response.status == 200
    assert consent_response.status == 200
    assert "Симоненко Наталья Сергеевна" in privacy
    assert "026108860870" in privacy
    assert "natali.nata5689@mail.ru" in privacy
    assert "Редакция от 30 августа 2026 года" in privacy
    assert "REG.RU" in privacy
    assert "Robokassa в закрытом тесте не получает данные" in privacy
    assert "Сервис работает в закрытом тестовом режиме" in privacy
    assert "деньги не списываются" in terms
    assert "покупка и обязанность оплаты не возникают" in terms
    assert "Реальная продажа через бота отключена" in terms
    assert "Публичная оферта на оказание информационно-развлекательных услуг" in terms
    assert "Согласен(а)" in consent
    assert "Оно не означает принятия Публичной оферты" in consent
    assert "деньги не списываются" in consent
