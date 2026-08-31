from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from money_profile_bot.config import PaymentMode, Settings
from money_profile_bot.services.delivery import DeliveryWorker
from money_profile_bot.services.robokassa import RobokassaClient
from money_profile_bot.services.store import Store
from money_profile_bot.web.app import create_web_app


@pytest.mark.asyncio
async def test_home_page_describes_products_delivery_operator_and_legal_links() -> None:
    settings = Settings(payment_retention_days=30, _env_file=None)
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


@pytest.mark.asyncio
async def test_legal_pages_render_final_documents_without_test_notice() -> None:
    settings = Settings(
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
    assert "simonenkons@ya.ru" in privacy
    assert "simonenkons@ya.ru" in terms
    assert "simonenkons@ya.ru" in consent
    assert "Редакция от 31 августа 2026 года" in privacy
    assert "REG.RU" in privacy
    assert "Закрытый тест — оплаты нет" not in privacy
    assert "Сервис работает в закрытом тестовом режиме" not in privacy
    assert "Закрытый тест — оплаты нет" not in terms
    assert "Публичная оферта на оказание информационно-развлекательных услуг" in terms
    assert "Согласен(а)" in consent
    assert "Оно не означает принятия Публичной оферты" in consent
    assert "Закрытый тест — оплаты нет" not in consent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_path",
    ("/payments/robokassa/result", "/payments/robokassa/result2"),
)
async def test_signed_robokassa_result_authorizes_delivery_once(callback_path: str) -> None:
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

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            callback_path,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_path",
    ("/payments/robokassa/result", "/payments/robokassa/result2"),
)
async def test_robokassa_result_rejects_invalid_signature(callback_path: str) -> None:
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

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            callback_path,
            data={"OutSum": "149.00", "InvId": "123", "SignatureValue": "invalid"},
        )

    assert response.status == 403
    store.accept_payment_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_test_success_return_authorizes_delivery_and_redirects() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_password1="test-password-1",
        robokassa_test_mode=True,
        _env_file=None,
    )
    store = AsyncMock()
    store.accept_payment_callback.return_value = SimpleNamespace(newly_paid=True)
    delivery = AsyncMock(spec=DeliveryWorker)
    robokassa = RobokassaClient(settings, AsyncMock())
    app = create_web_app(
        settings,
        cast(Store, store),
        robokassa,
        delivery,
    )
    signature = hashlib.sha256(b"149.00:123:test-password-1").hexdigest()

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/payments/robokassa/success",
            params={"OutSum": "149.00", "InvId": "123", "SignatureValue": signature},
            allow_redirects=False,
        )

    assert response.status == 302
    assert response.headers["Location"] == "https://t.me/money_profile_bot"
    store.accept_payment_callback.assert_awaited_once_with(
        invoice_id=123,
        amount_minor=14900,
        email=None,
    )
    delivery.notify.assert_called_once_with()


@pytest.mark.asyncio
async def test_bound_test_success_return_authorizes_delivery_without_query() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_password1="test-password-1",
        robokassa_test_mode=True,
        _env_file=None,
    )
    store = AsyncMock()
    store.accept_payment_callback.return_value = SimpleNamespace(newly_paid=True)
    delivery = AsyncMock(spec=DeliveryWorker)
    robokassa = RobokassaClient(settings, AsyncMock())
    token = robokassa.test_success_token(invoice_id=123, amount_minor=14900)
    app = create_web_app(settings, cast(Store, store), robokassa, delivery)

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            f"/payments/robokassa/success/123/14900/{token}",
            allow_redirects=False,
        )

    assert response.status == 302
    assert response.headers["Location"] == "https://t.me/money_profile_bot"
    store.accept_payment_callback.assert_awaited_once_with(
        invoice_id=123,
        amount_minor=14900,
        email=None,
    )
    delivery.notify.assert_called_once_with()


@pytest.mark.asyncio
async def test_invalid_bound_test_success_return_only_redirects() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_password1="test-password-1",
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

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/payments/robokassa/success/123/14900/invalid",
            allow_redirects=False,
        )

    assert response.status == 302
    store.accept_payment_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_test_success_return_only_redirects() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_test_password1="test-password-1",
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

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/payments/robokassa/success",
            params={"OutSum": "149.00", "InvId": "123", "SignatureValue": "invalid"},
            allow_redirects=False,
        )

    assert response.status == 302
    store.accept_payment_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_success_return_never_authorizes_delivery() -> None:
    settings = Settings(
        payment_mode=PaymentMode.ROBOKASSA,
        robokassa_password1="live-password-1",
        robokassa_test_mode=False,
        _env_file=None,
    )
    store = AsyncMock()
    robokassa = AsyncMock(spec=RobokassaClient)
    app = create_web_app(settings, cast(Store, store), robokassa, None)
    signature = hashlib.sha256(b"149.00:123:live-password-1").hexdigest()

    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/payments/robokassa/success",
            params={"OutSum": "149.00", "InvId": "123", "SignatureValue": signature},
            allow_redirects=False,
        )

    assert response.status == 302
    assert response.headers["Location"] == "https://t.me/money_profile_bot"
    robokassa.verify_success.assert_not_called()
    store.accept_payment_callback.assert_not_awaited()

    async with TestClient(
        TestServer(create_web_app(settings, cast(Store, store), robokassa, None))
    ) as client:
        bound_response = await client.get(
            "/payments/robokassa/success/123/14900/apparently-valid-token",
            allow_redirects=False,
        )

    assert bound_response.status == 302
    robokassa.verify_test_success_token.assert_not_called()
    store.accept_payment_callback.assert_not_awaited()
