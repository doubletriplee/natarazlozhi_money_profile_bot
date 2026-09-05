from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from money_profile_bot.config import Environment, PaymentMode, Settings
from money_profile_bot.services.delivery import DeliveryWorker
from money_profile_bot.services.robokassa import RobokassaClient
from money_profile_bot.services.store import Store
from money_profile_bot.web.app import create_web_app


@pytest.mark.asyncio
async def test_home_page_describes_products_delivery_operator_and_legal_links() -> None:
    settings = Settings(_env_file=None)
    app = create_web_app(
        settings,
        cast(Store, AsyncMock()),
        cast(RobokassaClient, AsyncMock()),
        None,
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/")
        body = await response.text()

    assert response.status == 200
    assert "Узнай свой Денежный аватар" in body
    assert "Персональный разбор по натальной карте" in body
    assert "Разбор Денежного аватара" in body
    assert 'Получить разбор за <span class="nowrap">149 ₽</span>' in body
    assert "Полный разбор денег и реализации" in body
    assert 'Получить полный разбор за <span class="nowrap">990 ₽</span>' in body
    assert body.count('href="https://t.me/money_profile_bot"') == 4
    assert 'href="/terms"' in body
    assert 'href="/privacy"' in body
    assert 'href="/consent"' in body
    assert "Симоненко Наталья Сергеевна" in body
    assert "026108860870" in body
    assert "simonenkons@ya.ru" in body
    assert "Списание денег и покупка не происходят" in body
    assert "<form" not in body


@pytest.mark.asyncio
async def test_home_page_reads_performer_details_from_terms_document(tmp_path: Path) -> None:
    (tmp_path / "terms_final.md").write_text(
        "# Оферта\n\n## Реквизиты Исполнителя\n\n**Единый источник реквизитов**\n"
        "ИНН: **123**\nTelegram-бот: **@canonical_public_bot**\n",
        encoding="utf-8",
    )
    settings = Settings(
        bot_username="runtime_test_bot",
        legal_documents_directory=tmp_path,
        _env_file=None,
    )
    app = create_web_app(
        settings,
        cast(Store, AsyncMock()),
        cast(RobokassaClient, AsyncMock()),
        None,
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/")
        body = await response.text()

    assert response.status == 200
    assert "Единый источник реквизитов" in body
    assert "Симоненко Наталья Сергеевна" not in body
    assert body.count('href="https://t.me/canonical_public_bot"') == 4
    assert "runtime_test_bot" not in body


@pytest.mark.asyncio
async def test_home_page_hides_test_notice_for_live_robokassa() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_mode=False,
        live_payments_enabled=True,
        _env_file=None,
    )
    app = create_web_app(
        settings,
        cast(Store, AsyncMock()),
        cast(RobokassaClient, AsyncMock()),
        None,
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/")
        body = await response.text()

    assert response.status == 200
    assert "Сейчас действует тестовый режим" not in body
    assert "Оплата временно приостановлена" not in body


@pytest.mark.asyncio
async def test_home_page_discloses_paused_live_payments() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_mode=False,
        live_payments_enabled=False,
        _env_file=None,
    )
    app = create_web_app(
        settings,
        cast(Store, AsyncMock()),
        cast(RobokassaClient, AsyncMock()),
        None,
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/")
        body = await response.text()

    assert response.status == 200
    assert "Оплата временно приостановлена" in body
    assert "Новые счета не создаются" in body


@pytest.mark.asyncio
async def test_production_home_page_discloses_public_access_with_payments_paused() -> None:
    settings = Settings(
        app_env=Environment.PRODUCTION,
        bot_token="123:test",
        admin_telegram_ids="10001",
        source_commit="abcdef1",
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_merchant_login="demo",
        robokassa_password1="live-pass-1",
        robokassa_password2="live-pass-2",
        robokassa_password3="live-pass-3",
        robokassa_test_mode=False,
        live_payments_enabled=False,
        payment_platform_risk_acknowledged=True,
        methodology_approved=True,
        golden_cards_approved=True,
        app_encryption_key="test-key",
        lookup_hmac_key="test-key",
        backup_encryption_key="test-key",
        _env_file=None,
    )
    app = create_web_app(
        settings,
        cast(Store, AsyncMock()),
        cast(RobokassaClient, AsyncMock()),
        None,
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/")
        body = await response.text()

    assert response.status == 200
    assert "Оплата временно приостановлена" in body
    assert "Бот уже доступен всем" in body
    assert "Закрытый пилот" not in body


@pytest.mark.asyncio
async def test_health_fails_when_delivery_worker_is_not_running() -> None:
    settings = Settings(source_commit="abcdef1", _env_file=None)
    store = AsyncMock()
    store.healthcheck.return_value = True
    delivery = Mock(spec=DeliveryWorker)
    delivery.is_healthy.return_value = False
    app = create_web_app(
        settings,
        cast(Store, store),
        cast(RobokassaClient, AsyncMock()),
        cast(DeliveryWorker, delivery),
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/healthz")
        body = await response.json()

    assert response.status == 503
    assert body == {
        "status": "error",
        "version": "abcdef1",
        "checks": {"database": "ok", "delivery": "error", "backup": "disabled"},
    }


@pytest.mark.asyncio
async def test_health_fails_when_required_backup_status_is_missing(tmp_path: Path) -> None:
    settings = Settings(
        app_env=Environment.TEST,
        source_commit="abcdef1",
        backup_status_path=tmp_path / "missing.json",
        _env_file=None,
    )
    store = AsyncMock()
    store.healthcheck.return_value = True
    app = create_web_app(
        settings,
        cast(Store, store),
        cast(RobokassaClient, AsyncMock()),
        None,
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.get("/healthz")
        body = await response.json()

    assert response.status == 503
    assert body["checks"] == {"database": "ok", "delivery": "ok", "backup": "error"}


@pytest.mark.asyncio
async def test_legal_pages_render_final_documents_without_test_notice() -> None:
    settings = Settings(_env_file=None)
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
    assert "simonenkons@ya.ru" in privacy
    assert "simonenkons@ya.ru" in terms
    assert "simonenkons@ya.ru" in consent
    assert "Редакция от 5 сентября 2026 года" in privacy
    assert "90 дней" in privacy
    assert "технические события использования" in privacy
    assert "90 дней" in consent
    assert "не менее пяти лет" in privacy
    assert "30 дней" in privacy
    assert "REG.RU" in privacy
    assert "Закрытый тест — оплаты нет" not in privacy
    assert "Сервис работает в закрытом тестовом режиме" not in privacy
    assert "Закрытый тест — оплаты нет" not in terms
    assert "Публичная оферта на оказание информационно-развлекательных услуг" in terms
    assert "Согласен(а)" in consent
    assert "Оно не означает принятия Публичной оферты" in consent
    assert "Закрытый тест — оплаты нет" not in consent


@pytest.mark.asyncio
async def test_signed_robokassa_result_authorizes_delivery_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_password2="test-password-2",
        robokassa_test_mode=True,
        _env_file=None,
    )
    store = AsyncMock()
    store.accept_payment_callback.return_value = SimpleNamespace(newly_paid=True)
    delivery = AsyncMock(spec=DeliveryWorker)
    robokassa = RobokassaClient(settings, AsyncMock())
    app = create_web_app(settings, cast(Store, store), robokassa, delivery)
    signature = hashlib.sha256(b"149.00:123:test-password-2").hexdigest()
    caplog.set_level(logging.INFO, logger="money_profile_bot.web.app")

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/payments/robokassa/result",
            data={
                "OutSum": "149.00",
                "InvId": "123",
                "SignatureValue": signature,
                "EMail": "buyer@example.ru",
            },
        )
        body = await response.text()

    assert response.status == 200
    assert body == "OK123"
    store.accept_payment_callback.assert_awaited_once_with(
        invoice_id=123,
        amount_minor=14900,
        email="buyer@example.ru",
    )
    delivery.notify.assert_called_once_with()
    assert "received Robokassa ResultURL notification" in caplog.text
    assert "accepted signed Robokassa ResultURL notification" in caplog.text


@pytest.mark.asyncio
async def test_robokassa_result_rejects_invalid_signature(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_password2="test-password-2",
        robokassa_test_mode=True,
        _env_file=None,
    )
    store = AsyncMock()
    app = create_web_app(
        settings,
        cast(Store, store),
        RobokassaClient(settings, AsyncMock()),
        None,
    )
    caplog.set_level(logging.INFO, logger="money_profile_bot.web.app")

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/payments/robokassa/result",
            data={"OutSum": "149.00", "InvId": "123", "SignatureValue": "invalid"},
        )

    assert response.status == 403
    store.accept_payment_callback.assert_not_awaited()
    assert "invalid signature" in caplog.text


@pytest.mark.asyncio
async def test_robokassa_exposes_only_classic_post_result_callback() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_mode=True,
        _env_file=None,
    )
    app = create_web_app(
        settings,
        cast(Store, AsyncMock()),
        RobokassaClient(settings, AsyncMock()),
        None,
    )

    async with TestClient(TestServer(app)) as client:
        get_result = await client.get("/payments/robokassa/result")
        result2 = await client.post("/payments/robokassa/result2")

    assert get_result.status == 405
    assert result2.status == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("test_mode", "path"),
    (
        (True, "/payments/robokassa/success"),
        (
            True,
            "/payments/robokassa/success?OutSum=149.00&InvId=123&SignatureValue=signed",
        ),
        (False, "/payments/robokassa/success?OutSum=149.00&InvId=123"),
    ),
)
async def test_success_return_page_opens_telegram_and_never_authorizes_delivery(
    test_mode: bool, path: str
) -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_password1="live-password-1",
        robokassa_test_password1="test-password-1",
        robokassa_test_mode=test_mode,
        _env_file=None,
    )
    store = AsyncMock()
    app = create_web_app(
        settings,
        cast(Store, store),
        RobokassaClient(settings, AsyncMock()),
        None,
    )

    async with TestClient(TestServer(app)) as client:
        response = await client.get(path)
        body = await response.text()

    assert response.status == 200
    assert '<meta http-equiv="refresh" content="0;url=https://t.me/money_profile_bot">' in body
    assert '<a class="return-button" href="https://t.me/money_profile_bot">' in body
    assert "Открыть разбор в Telegram" in body
    assert "Окно Robokassa после этого можно закрыть" in body
    assert "Location" not in response.headers
    assert "<script" not in body
    store.accept_payment_callback.assert_not_awaited()
