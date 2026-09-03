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
from money_profile_bot.services.robokassa import (
    RobokassaClient,
    RobokassaError,
    RobokassaTransportError,
)


class FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body

    async def text(self) -> str:
        return self.body


class FakeRequest:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> FakeResponse:
        return self.response

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    def post(self, *_: object, **__: object) -> FakeRequest:
        return FakeRequest(self.response)


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


def response_client(status: int, body: str) -> RobokassaClient:
    return RobokassaClient(
        settings(),
        cast(ClientSession, FakeSession(FakeResponse(status, body))),
    )


def test_result_signature_is_accepted_case_insensitively() -> None:
    source = "149.00:123:pass2"
    signature = hashlib.sha256(source.encode()).hexdigest().upper()
    assert client().verify_result(out_sum="149.00", invoice_id="123", signature=signature)


def test_result_signature_rejects_wrong_amount() -> None:
    signature = hashlib.sha256(b"149.00:123:pass2").hexdigest()
    assert not client().verify_result(out_sum="1.00", invoice_id="123", signature=signature)


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
async def test_post_jwt_treats_explicit_client_rejection_as_retryable() -> None:
    robokassa = response_client(400, "{}")

    with pytest.raises(RobokassaError) as captured:
        await robokassa._post_jwt("https://example.test", {}, "pass3", refund=True)

    assert not isinstance(captured.value, RobokassaTransportError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body"),
    [(500, "{}"), (200, "not-json")],
)
async def test_post_jwt_marks_ambiguous_provider_response_as_uncertain(
    status: int,
    body: str,
) -> None:
    robokassa = response_client(status, body)

    with pytest.raises(RobokassaTransportError):
        await robokassa._post_jwt("https://example.test", {}, "pass3", refund=True)


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
    assert payload["AdditionalParameters"] == {"Email": "buyer@example.ru"}
    assert "UserFields" not in payload
    assert post_jwt.await_args.args[2] == "pass1"


@pytest.mark.asyncio
async def test_test_invoice_uses_plain_success_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert payload["SuccessUrl2Data"] == {
        "Url": f"{value.public_base_url}/payments/robokassa/success",
        "Method": "GET",
    }
    assert payload["FailUrl2Data"] == {
        "Url": f"{value.public_base_url}/payments/robokassa/fail",
        "Method": "GET",
    }
    assert payload["AdditionalParameters"] == {
        "Email": "buyer@example.ru",
        "IsTest": "1",
    }
    assert post_jwt.await_args.args[2] == "test1"


@pytest.mark.asyncio
async def test_refund_uses_money_avatar_name(monkeypatch: pytest.MonkeyPatch) -> None:
    robokassa = client()
    post_jwt = AsyncMock(return_value={"success": True, "requestId": "refund-id"})
    monkeypatch.setattr(robokassa, "_post_jwt", post_jwt)

    await robokassa.refund(operation_key="operation-key", amount_minor=14900, order_code="ORDER-42")

    payload = post_jwt.await_args.args[1]
    assert payload["InvoiceItems"][0]["Name"] == "Денежный аватар, заказ ORDER-42"


@pytest.mark.asyncio
async def test_refund_requires_request_id_in_success_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    robokassa = client()
    monkeypatch.setattr(robokassa, "_post_jwt", AsyncMock(return_value={"success": True}))

    with pytest.raises(RobokassaTransportError, match="request ID"):
        await robokassa.refund(
            operation_key="operation-key",
            amount_minor=14900,
            order_code="ORDER-42",
        )
