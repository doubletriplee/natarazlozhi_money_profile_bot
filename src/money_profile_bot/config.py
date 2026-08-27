from __future__ import annotations

import base64
import hashlib
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class PaymentMode(StrEnum):
    ROBOKASSA = "robokassa"
    FAKE = "fake"


def _development_key(label: str) -> str:
    digest = hashlib.sha256(f"money-profile:{label}:development-only".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Environment = Environment.DEVELOPMENT
    bot_token: str = ""
    bot_username: str = "money_profile_bot"
    support_username: str = "simnatali"
    admin_telegram_ids: str = ""
    test_access_telegram_ids: str = ""
    bootstrap_admin_on_first_start: bool = False
    product_price_rub: Decimal = Field(default=Decimal("149.00"), gt=0)
    payment_mode: PaymentMode = PaymentMode.ROBOKASSA

    robokassa_merchant_login: str = ""
    robokassa_password1: str = ""
    robokassa_password2: str = ""
    robokassa_password3: str = ""
    robokassa_test_password1: str = ""
    robokassa_test_password2: str = ""
    robokassa_test_mode: bool = True
    robokassa_hash_algorithm: str = "sha256"
    payment_platform_risk_acknowledged: bool = False

    database_url: str = "sqlite+aiosqlite:///./runtime/money_profile.sqlite3"
    geonames_database_path: Path = Path("data/cities.sqlite3")
    card_output_directory: Path = Path("runtime/cards")

    public_base_url: str = "https://money.natarazlozhi.ru"
    source_repository_url: str = "https://github.com/doubletriplee/natarazlozhi_money_profile_bot"
    source_commit: str = "development"

    legal_docs_version: str = "DRAFT"
    methodology_approved: bool = False
    golden_cards_approved: bool = False
    operator_name: str = ""
    operator_inn: str = ""
    operator_email: str = ""
    payment_retention_days: int = Field(default=0, ge=0)
    profile_draft_retention_days: int = Field(default=30, ge=1)
    backup_retention_days: int = Field(default=14, ge=1)

    app_encryption_key: str = ""
    lookup_hmac_key: str = ""
    backup_encryption_key: str = ""

    web_only: bool = False
    http_host: str = "0.0.0.0"
    http_port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"

    @field_validator("bot_username", "support_username")
    @classmethod
    def strip_at_sign(cls, value: str) -> str:
        return value.strip().removeprefix("@")

    @field_validator("robokassa_hash_algorithm")
    @classmethod
    def validate_hash_algorithm(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"md5", "sha1", "sha256", "sha384", "sha512"}:
            raise ValueError("unsupported Robokassa hash algorithm")
        return normalized

    @model_validator(mode="after")
    def validate_runtime_safety(self) -> Settings:
        if self.app_env is not Environment.PRODUCTION:
            self.app_encryption_key = self.app_encryption_key or _development_key("data")
            self.lookup_hmac_key = self.lookup_hmac_key or _development_key("lookup")
            self.backup_encryption_key = self.backup_encryption_key or _development_key("backup")
            return self

        missing: list[str] = []
        if not self.web_only and not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.admin_ids:
            missing.append("ADMIN_TELEGRAM_IDS")
        if self.bootstrap_admin_on_first_start:
            missing.append("BOOTSTRAP_ADMIN_ON_FIRST_START=false")
        if self.legal_docs_version.upper() == "DRAFT":
            missing.append("LEGAL_DOCS_VERSION")
        if not self.operator_name:
            missing.append("OPERATOR_NAME")
        if not self.operator_inn:
            missing.append("OPERATOR_INN")
        if not self.operator_email:
            missing.append("OPERATOR_EMAIL")
        if self.source_commit == "development" or len(self.source_commit) < 7:
            missing.append("SOURCE_COMMIT")
        if self.payment_retention_days <= 0:
            missing.append("PAYMENT_RETENTION_DAYS")
        if not self.methodology_approved:
            missing.append("METHODOLOGY_APPROVED")
        if not self.golden_cards_approved:
            missing.append("GOLDEN_CARDS_APPROVED")
        if not self.robokassa_merchant_login:
            missing.append("ROBOKASSA_MERCHANT_LOGIN")
        if not self.robokassa_password1:
            missing.append("ROBOKASSA_PASSWORD1")
        if not self.robokassa_password2:
            missing.append("ROBOKASSA_PASSWORD2")
        if not self.robokassa_password3:
            missing.append("ROBOKASSA_PASSWORD3")
        if self.robokassa_test_mode:
            missing.append("ROBOKASSA_TEST_MODE=false")
        if self.payment_mode is not PaymentMode.ROBOKASSA:
            missing.append("PAYMENT_MODE=robokassa")
        if not self.payment_platform_risk_acknowledged:
            missing.append("PAYMENT_PLATFORM_RISK_ACKNOWLEDGED")
        if not self.app_encryption_key:
            missing.append("APP_ENCRYPTION_KEY")
        if not self.lookup_hmac_key:
            missing.append("LOOKUP_HMAC_KEY")
        if not self.backup_encryption_key:
            missing.append("BACKUP_ENCRYPTION_KEY")
        if missing:
            raise ValueError("production configuration is incomplete: " + ", ".join(missing))
        return self

    @property
    def admin_ids(self) -> frozenset[int]:
        return self._parse_id_list(self.admin_telegram_ids)

    @property
    def test_access_ids(self) -> frozenset[int]:
        return self._parse_id_list(self.test_access_telegram_ids)

    @staticmethod
    def _parse_id_list(raw: str) -> frozenset[int]:
        result: set[int] = set()
        for item in raw.split(","):
            item = item.strip()
            if item:
                result.add(int(item))
        return frozenset(result)

    @property
    def product_price_minor(self) -> int:
        return int((self.product_price_rub * 100).quantize(Decimal("1")))

    @property
    def active_robokassa_password1(self) -> str:
        if self.robokassa_test_mode:
            return self.robokassa_test_password1
        return self.robokassa_password1

    @property
    def active_robokassa_password2(self) -> str:
        if self.robokassa_test_mode:
            return self.robokassa_test_password2
        return self.robokassa_password2

    @property
    def source_url(self) -> str:
        if self.source_commit and self.source_commit != "development":
            return f"{self.source_repository_url}/tree/{self.source_commit}"
        return self.source_repository_url


def ensure_runtime_directories(settings: Settings) -> None:
    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.rsplit("///", 1)[-1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    settings.geonames_database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.card_output_directory.mkdir(parents=True, exist_ok=True)
