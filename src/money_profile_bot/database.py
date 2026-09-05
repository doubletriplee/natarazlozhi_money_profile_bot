from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from money_profile_bot.models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", self._sqlite_pragmas)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _sqlite_pragmas(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            if self.engine.url.drivername.startswith("sqlite"):
                columns = {
                    str(row[1])
                    for row in (
                        await connection.execute(text("PRAGMA table_info(delivery_items)"))
                    ).all()
                }
                if "available_at" not in columns:
                    await connection.execute(
                        text("ALTER TABLE delivery_items ADD COLUMN available_at DATETIME")
                    )
                order_columns = {
                    str(row[1])
                    for row in (await connection.execute(text("PRAGMA table_info(orders)"))).all()
                }
                if "analytics_mode" not in order_columns:
                    await connection.execute(
                        text(
                            "ALTER TABLE orders ADD COLUMN analytics_mode VARCHAR(16) NOT NULL DEFAULT 'unknown'"
                        )
                    )

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()
