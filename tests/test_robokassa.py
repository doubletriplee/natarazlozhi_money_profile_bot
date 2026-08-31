from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession

from money_profile_bot.config import Settings
from money_profile_bot.services.robokassa import RobokassaClient


def settings() -> Settings:
    return Settings(
        robokassa_merchant_login="demo",
        robokassa_password1="pass1",
        robokassa_password2="pass2",
        robokassa_password3="pass3",
        robokassa_test_password1="test1",
        robokassa_test_password2="test2",
        robokassa_test_mode=False,
        robokassa_hash_algorithm="sha256",
        _env_file=None,
    )


def client() -> RobokassaClient:
    return RobokassaClient(settings(), cast(ClientSession, None))


def test_result_signature_is_accepted_case_insensitively() -> None:
    source = "149.00:123:pass2"
    signature = hashlib.sha256(source.encode()).hexdigest().upper()
    assert client().verify_result(out_sum="149.00", invoice_id="123", signature=signature)


def test_result_signature_rejects_wrong_amount() -> None:
    signature = hashlib.sha256(b"149.00:123:pass2").hexdigest()
    assert not client().verify_result(out_sum="1.00", invoice_id="123", signature=signature)


def test_success_signature_uses_password1() -> None:
    signature = hashlib.sha256(b"149.00:123:pass1").hexdigest()
    assert client().verify_success(out_sum="149.00", invoice_id="123", signature=signature)


def test_success_signature_rejects_password2() -> None:
    signature = hashlib.sha256(b"149.00:123:pass2").hexdigest()
    assert not client().verify_success(out_sum="149.00", invoice_id="123", signature=signature)


def test_invoice_jwt_uses_merchant_and_password_as_hmac_key() -> None:
    value = client()._jwt({"MerchantLogin": "demo", "InvId": 123}, "pass1")
    header, payload, signature = value.split(".")
    decoded_header = json.loads(base64.urlsafe_b64decode(header + "=="))
    assert decoded_header["alg"] == "HS256"
    expected = hmac.new(b"demo:pass1", f"{header}.{payload}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64encode(expected).rstrip(b"=").decode() == signature


def test_refund_jwt_uses_password3_directly() -> None:
    value = client()._jwt({"OpKey": "abc"}, "pass3", refund=True)
    header, payload, signature = value.split(".")
    expected = hmac.new(b"pass3", f"{header}.{payload}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64encode(expected).rstrip(b"=").decode() == signature


@pytest.mark.asyncio
async def test_invoice_uses_money_avatar_name(monkeypatch: pytest.MonkeyPatch) -> None:
    robokassa = client()
    post_jwt = AsyncMock(return_value={"isSuccess": True, "id": "id", "invId": 42, "url": "url"})
    monkeypatch.setattr(robokassa, "_post_jwt", post_jwt)

    await robokassa.create_invoice(
        invoice_id=42,
        order_code="ORDER-42",
        amount_minor=14900,
        email="buyer@example.ru",
    )

    payload = post_jwt.await_args.args[1]
    assert payload["Description"] == "Денежный аватар, заказ ORDER-42"
    assert payload["InvoiceItems"][0]["Name"] == "Индивидуальный разбор «Денежный аватар»"


@pytest.mark.asyncio
async def test_test_invoice_uses_bound_success_return(monkeypatch: pytest.MonkeyPatch) -> None:
    value = settings().model_copy(update={"robokassa_test_mode": True})
    robokassa = RobokassaClient(value, cast(ClientSession, None))
    post_jwt = AsyncMock(return_value={"isSuccess": True, "id": "id", "invId": 42, "url": "url"})
    monkeypatch.setattr(robokassa, "_post_jwt", post_jwt)

    await robokassa.create_invoice(
        invoice_id=42,
        order_code="ORDER-42",
        amount_minor=14900,
        email="buyer@example.ru",
    )

    payload = post_jwt.await_args.args[1]
    token = robokassa.test_success_token(invoice_id=42, amount_minor=14900)
    assert payload["SuccessUrl2Data"] == {
        "Url": f"{value.public_base_url}/payments/robokassa/success/42/14900/{token}",
        "Method": "GET",
    }


def test_bound_test_success_token_rejects_changed_order_data() -> None:
    value = settings().model_copy(update={"robokassa_test_mode": True})
    robokassa = RobokassaClient(value, cast(ClientSession, None))
    token = robokassa.test_success_token(invoice_id=42, amount_minor=14900)

    assert robokassa.verify_test_success_token(invoice_id=42, amount_minor=14900, token=token)
    assert not robokassa.verify_test_success_token(invoice_id=43, amount_minor=14900, token=token)
    assert not robokassa.verify_test_success_token(invoice_id=42, amount_minor=1, token=token)


@pytest.mark.asyncio
async def test_refund_uses_money_avatar_name(monkeypatch: pytest.MonkeyPatch) -> None:
    robokassa = client()
    post_jwt = AsyncMock(return_value={"success": True, "requestId": "refund-id"})
    monkeypatch.setattr(robokassa, "_post_jwt", post_jwt)

    await robokassa.refund(operation_key="operation-key", amount_minor=14900, order_code="ORDER-42")

    payload = post_jwt.await_args.args[1]
    assert payload["InvoiceItems"][0]["Name"] == "Денежный аватар, заказ ORDER-42"
