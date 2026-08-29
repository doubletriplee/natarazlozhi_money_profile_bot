import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from money_profile_bot.database import Database


@pytest.mark.asyncio
async def test_initialize_adds_delivery_schedule_column_to_existing_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE delivery_items (id VARCHAR(36) PRIMARY KEY)")
    connection.commit()
    connection.close()

    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    await database.initialize()
    async with database.engine.connect() as async_connection:
        columns = {
            str(row[1])
            for row in (
                await async_connection.execute(text("PRAGMA table_info(delivery_items)"))
            ).all()
        }
    await database.close()

    assert "available_at" in columns
