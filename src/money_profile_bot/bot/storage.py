from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from money_profile_bot.crypto import CryptoBox
from money_profile_bot.models import FsmRecord


def _storage_key(key: StorageKey) -> str:
    return ":".join(
        str(value or 0)
        for value in (
            key.bot_id,
            key.chat_id,
            key.user_id,
            getattr(key, "thread_id", None),
            getattr(key, "business_connection_id", None),
            getattr(key, "destiny", None),
        )
    )


class EncryptedDatabaseStorage(BaseStorage):
    def __init__(self, sessions: async_sessionmaker[AsyncSession], crypto: CryptoBox) -> None:
        self.sessions = sessions
        self.crypto = crypto

    def _digest(self, key: StorageKey) -> str:
        return self.crypto.lookup(_storage_key(key), context="fsm-key")

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        digest = self._digest(key)
        state_value = state.state if isinstance(state, State) else state
        async with self.sessions() as session, session.begin():
            record = await session.get(FsmRecord, digest)
            if record is None:
                if state_value is None:
                    return
                session.add(FsmRecord(key_hash=digest, state=state_value))
                return
            record.state = state_value
            if record.state is None and record.data_encrypted is None:
                await session.delete(record)

    async def get_state(self, key: StorageKey) -> str | None:
        async with self.sessions() as session:
            record = await session.get(FsmRecord, self._digest(key))
            return record.state if record else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        digest = self._digest(key)
        async with self.sessions() as session, session.begin():
            record = await session.get(FsmRecord, digest)
            if not data:
                if record:
                    record.data_encrypted = None
                    if record.state is None:
                        await session.delete(record)
                return
            encrypted = self.crypto.encrypt_json(dict(data), context=f"fsm.data:{digest}")
            if record is None:
                session.add(FsmRecord(key_hash=digest, data_encrypted=encrypted))
            else:
                record.data_encrypted = encrypted

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self.sessions() as session:
            record = await session.get(FsmRecord, self._digest(key))
            if not record or not record.data_encrypted:
                return {}
            value = self.crypto.decrypt_json(
                record.data_encrypted, context=f"fsm.data:{record.key_hash}"
            )
            if not isinstance(value, dict):
                raise TypeError("FSM data must be a dictionary")
            return value

    async def close(self) -> None:
        return None
