from __future__ import annotations

from pathlib import Path

import pytest
from aiogram.fsm.storage.base import StorageKey
from sqlalchemy import func, select

from money_profile_bot.bot.storage import EncryptedDatabaseStorage
from money_profile_bot.config import Settings
from money_profile_bot.crypto import CryptoBox
from money_profile_bot.database import Database
from money_profile_bot.models import FsmRecord


def storage_key() -> StorageKey:
    return StorageKey(bot_id=1, chat_id=2, user_id=3)


@pytest.mark.asyncio
async def test_clearing_empty_state_does_not_delete_transient_record(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'fsm.sqlite3').as_posix()}")
    await database.initialize()
    storage = EncryptedDatabaseStorage(
        database.sessions,
        CryptoBox(settings.app_encryption_key, settings.lookup_hmac_key),
    )

    await storage.set_state(storage_key(), None)

    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(FsmRecord)) == 0
    await database.close()


@pytest.mark.asyncio
async def test_state_and_data_are_removed_only_when_both_are_empty(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'fsm.sqlite3').as_posix()}")
    await database.initialize()
    storage = EncryptedDatabaseStorage(
        database.sessions,
        CryptoBox(settings.app_encryption_key, settings.lookup_hmac_key),
    )
    key = storage_key()

    await storage.set_data(key, {"name": "Наталья"})
    await storage.set_state(key, "profile:adult")
    await storage.set_state(key, None)
    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {"name": "Наталья"}

    await storage.set_data(key, {})
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(FsmRecord)) == 0
    await database.close()
