from __future__ import annotations

import base64
import hashlib
import hmac
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

import aiohttp

from money_profile_bot.config import Settings

INVOICE_API = "https://services.robokassa.ru/InvoiceServiceWebApi/api"
REFUND_API = "https://services.robokassa.ru/RefundService/Refund"
OP_STATE_URL = "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt"


class RobokassaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Invoice:
    invoice_uuid: str
    invoice_id: int
    payment_url: str


@dataclass(frozen=True, slots=True)
class OperationState:
    state_code: int
    operation_key: str
    amount_minor: int
    payment_method: str


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _amount_rub(amount_minor: int) -> Decimal:
    return (Decimal(amount_minor) / 100).quantize(Decimal("0.00"))


def _minor(value: str | Decimal | float) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class RobokassaClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self.session = session

    def _digest(self, value: bytes, *, password: str | None = None) -> bytes:
        algorithm = self.settings.robokassa_hash_algorithm
        if password is None:
            return hashlib.new(algorithm, value).digest()
        return hmac.new(password.encode(), value, algorithm).digest()

    def _jwt(self, payload: dict[str, Any], password: str, *, refund: bool = False) -> str:
        algorithm = (
            "HS256"
            if refund
            else {
                "md5": "MD5",
                "sha1": "HS1",
                "sha256": "HS256",
                "sha384": "HS384",
                "sha512": "HS512",
            }[self.settings.robokassa_hash_algorithm]
        )
        header = _base64url(_json_bytes({"typ": "JWT", "alg": algorithm}))
        body = _base64url(_json_bytes(payload))
        signing_input = f"{header}.{body}".encode()
        if refund:
            signature = hmac.new(password.encode(), signing_input, hashlib.sha256).digest()
        else:
            key = f"{self.settings.robokassa_merchant_login}:{password}".encode()
            signature = hmac.new(
                key, signing_input, self.settings.robokassa_hash_algorithm
            ).digest()
        return f"{header}.{body}.{_base64url(signature)}"

    async def _post_jwt(
        self, endpoint: str, payload: dict[str, Any], password: str, *, refund: bool = False
    ) -> dict[str, Any]:
        token = self._jwt(payload, password, refund=refund)
        try:
            async with self.session.post(
                endpoint, json=token, timeout=aiohttp.ClientTimeout(total=20)
            ) as response:
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise RobokassaError("Robokassa request failed") from exc
        if response.status >= 400:
            raise RobokassaError(f"Robokassa HTTP status {response.status}")
        return cast(dict[str, Any], data)

    async def create_invoice(
        self,
        *,
        invoice_id: int,
        order_code: str,
        amount_minor: int,
        email: str,
    ) -> Invoice:
        if (
            not self.settings.robokassa_merchant_login
            or not self.settings.active_robokassa_password1
        ):
            raise RobokassaError("Robokassa credentials are not configured")
        amount = float(_amount_rub(amount_minor))
        expires = datetime.now(UTC) + timedelta(hours=24)
        additional = {
            "Email": email,
            "ResultURL2": f"{self.settings.public_base_url}/payments/robokassa/result2",
        }
        if self.settings.robokassa_test_mode:
            additional["IsTest"] = "1"
        payload = {
            "MerchantLogin": self.settings.robokassa_merchant_login,
            "InvId": invoice_id,
            "InvoiceType": "OneTime",
            "Culture": "ru",
            "OutSum": amount,
            "ExpirationDate": expires.isoformat(),
            "Description": f"Денежный потенциал, заказ {order_code}",
            "MerchantComments": "Выдать результат только после ResultURL",
            "UserFields": {"order_code": order_code},
            "InvoiceItems": [
                {
                    "Name": "Индивидуальный разбор «Денежный потенциал»",
                    "Quantity": 1,
                    "Cost": amount,
                    "Tax": "none",
                    "PaymentMethod": "full_payment",
                    "PaymentObject": "service",
                }
            ],
            "Aliases": ["BankCard", "SBP"],
            "SuccessUrl2Data": {
                "Url": f"{self.settings.public_base_url}/payments/robokassa/success",
                "Method": "GET",
            },
            "FailUrl2Data": {
                "Url": f"{self.settings.public_base_url}/payments/robokassa/fail",
                "Method": "GET",
            },
            "AdditionalParameters": additional,
        }
        data = await self._post_jwt(
            f"{INVOICE_API}/CreateInvoice", payload, self.settings.active_robokassa_password1
        )
        if not data.get("isSuccess"):
            raise RobokassaError(str(data.get("message") or "invoice was rejected"))
        return Invoice(str(data["id"]), int(data["invId"]), str(data["url"]))

    def verify_result(
        self,
        *,
        out_sum: str,
        invoice_id: str,
        signature: str,
        user_parameters: dict[str, str] | None = None,
    ) -> bool:
        parts = [out_sum, invoice_id, self.settings.active_robokassa_password2]
        for key, value in sorted((user_parameters or {}).items()):
            if key.startswith("Shp_"):
                parts.append(f"{key}={value}")
        expected = hashlib.new(
            self.settings.robokassa_hash_algorithm, ":".join(parts).encode()
        ).hexdigest()
        return hmac.compare_digest(expected.casefold(), signature.casefold())

    async def operation_state(self, invoice_id: int) -> OperationState:
        if self.settings.robokassa_test_mode:
            raise RobokassaError("OpStateExt is unavailable for test payments")
        source = (
            f"{self.settings.robokassa_merchant_login}:{invoice_id}:"
            f"{self.settings.robokassa_password2}"
        )
        signature = hashlib.new(self.settings.robokassa_hash_algorithm, source.encode()).hexdigest()
        params = {
            "MerchantLogin": self.settings.robokassa_merchant_login,
            "InvoiceID": str(invoice_id),
            "Signature": signature,
        }
        try:
            async with self.session.get(
                OP_STATE_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)
            ) as response:
                body = await response.text()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise RobokassaError("cannot query operation state") from exc
        root = ET.fromstring(body)
        namespace = {"r": "http://merchant.roboxchange.com/WebService/"}
        code = int(root.findtext("r:Result/r:Code", default="1000", namespaces=namespace))
        if code != 0:
            raise RobokassaError(f"OpStateExt returned code {code}")
        state_code = int(root.findtext("r:State/r:Code", default="0", namespaces=namespace))
        operation_key = root.findtext("r:Info/r:OpKey", default="", namespaces=namespace)
        out_sum = root.findtext("r:Info/r:OutSum", default="0", namespaces=namespace)
        method = root.findtext("r:Info/r:PaymentMethod/r:Code", default="", namespaces=namespace)
        return OperationState(state_code, operation_key, _minor(out_sum), method)

    async def refund(self, *, operation_key: str, amount_minor: int, order_code: str) -> str:
        if not self.settings.robokassa_password3:
            raise RobokassaError("ROBOKASSA_PASSWORD3 is not configured")
        amount = float(_amount_rub(amount_minor))
        payload = {
            "OpKey": operation_key,
            "RefundSum": amount,
            "InvoiceItems": [
                {
                    "Name": f"Денежный потенциал, заказ {order_code}",
                    "Quantity": 1,
                    "Cost": amount,
                    "Tax": "none",
                    "PaymentMethod": "full_payment",
                    "PaymentObject": "service",
                }
            ],
        }
        data = await self._post_jwt(
            f"{REFUND_API}/Create",
            payload,
            self.settings.robokassa_password3,
            refund=True,
        )
        if not data.get("success"):
            raise RobokassaError(str(data.get("message") or "refund was rejected"))
        return str(data["requestId"])

    async def refund_state(self, request_id: str) -> str:
        try:
            async with self.session.get(
                f"{REFUND_API}/GetState",
                params={"id": request_id},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise RobokassaError("cannot query refund state") from exc
        if response.status >= 400 or "label" not in data:
            raise RobokassaError(str(data.get("message") or "invalid refund state response"))
        return str(data["label"])
