from __future__ import annotations

import base64
import secrets

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from money_profile_bot.config import Environment, PaymentMode, Settings
from money_profile_bot.crypto import CryptoBox


def key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def test_crypto_round_trip() -> None:
    box = CryptoBox(key(), key())
    token = box.encrypt_json({"имя": "Наталья", "value": 42}, context="test")
    assert box.decrypt_json(token, context="test") == {"имя": "Наталья", "value": 42}


def test_crypto_context_is_authenticated() -> None:
    box = CryptoBox(key(), key())
    token = box.encrypt("secret", context="first")
    with pytest.raises(InvalidTag):
        box.decrypt(token, context="second")


def test_lookup_is_stable_but_context_specific() -> None:
    box = CryptoBox(key(), key())
    assert box.lookup("123", context="a") == box.lookup("123", context="a")
    assert box.lookup("123", context="a") != box.lookup("123", context="b")


def test_development_settings_generate_keys() -> None:
    settings = Settings(_env_file=None)
    assert settings.product_price_minor == 14900
    assert settings.payment_mode is PaymentMode.FAKE
    assert settings.payment_retention_days == 30
    assert settings.legal_docs_version == "2026-08-30.1"
    assert settings.operator_name == "Симоненко Наталья Сергеевна"
    assert settings.operator_inn == "026108860870"
    assert settings.operator_email == "natali.nata5689@mail.ru"
    assert settings.methodology_approved is True
    assert settings.app_encryption_key
    assert settings.lookup_hmac_key


def test_production_rejects_incomplete_configuration() -> None:
    with pytest.raises(ValidationError):
        Settings(app_env=Environment.PRODUCTION, _env_file=None)


def test_invalid_robokassa_algorithm_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(robokassa_hash_algorithm="crc32", _env_file=None)


def test_production_rejects_fake_payment_mode() -> None:
    with pytest.raises(ValidationError, match="PAYMENT_MODE=robokassa"):
        Settings(
            app_env=Environment.PRODUCTION,
            payment_mode=PaymentMode.FAKE,
            _env_file=None,
        )


def test_public_test_requires_fake_payment() -> None:
    with pytest.raises(ValidationError, match="PAYMENT_MODE=fake"):
        Settings(
            app_env=Environment.TEST,
            payment_mode=PaymentMode.ROBOKASSA,
            _env_file=None,
        )

    settings = Settings(
        app_env=Environment.TEST,
        payment_mode=PaymentMode.FAKE,
        _env_file=None,
    )
    assert settings.app_env is Environment.TEST


def test_staging_requires_test_robokassa_and_private_access() -> None:
    required = {
        "app_env": Environment.STAGING,
        "bot_token": "123:test",
        "admin_telegram_ids": "10001",
        "test_access_telegram_ids": "10001, 20002",
        "bootstrap_admin_on_first_start": False,
        "source_commit": "abcdef1",
        "payment_mode": PaymentMode.ROBOKASSA,
        "robokassa_merchant_login": "demo",
        "robokassa_test_password1": "test-pass-1",
        "robokassa_test_password2": "test-pass-2",
        "robokassa_test_mode": True,
        "app_encryption_key": key(),
        "lookup_hmac_key": key(),
        "backup_encryption_key": key(),
        "_env_file": None,
    }
    settings = Settings(**required)

    assert settings.app_env is Environment.STAGING
    assert settings.payment_mode is PaymentMode.ROBOKASSA
    assert settings.test_access_ids == frozenset({10001, 20002})
    assert settings.active_robokassa_password1 == "test-pass-1"
    assert settings.active_robokassa_password2 == "test-pass-2"

    with pytest.raises(ValidationError, match="TEST_ACCESS_TELEGRAM_IDS"):
        Settings(**(required | {"test_access_telegram_ids": ""}))
    with pytest.raises(ValidationError, match="PAYMENT_MODE=robokassa"):
        Settings(**(required | {"payment_mode": PaymentMode.FAKE}))
    with pytest.raises(ValidationError, match="ROBOKASSA_TEST_MODE=true"):
        Settings(**(required | {"robokassa_test_mode": False}))
