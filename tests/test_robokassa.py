from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import cast

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
