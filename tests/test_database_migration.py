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
        tables = {
            str(row[0])
            for row in (
                await async_connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
            ).all()
        }
    await database.close()

    assert "available_at" in columns
    assert "strength_offers" in tables
    assert "form_reminders" in tables
    assert "journeys" in tables
    assert "journey_events" in tables


@pytest.mark.asyncio
async def test_existing_orders_get_unknown_mode_without_rewriting_values(tmp_path: Path) -> None:
    path = tmp_path / "legacy_orders.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE orders (id VARCHAR(36) PRIMARY KEY, amount_minor INTEGER)")
    connection.execute("INSERT INTO orders VALUES ('legacy-order', 14900)")
    connection.commit()
    connection.close()
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    await database.initialize()
    await database.initialize()
    async with database.engine.connect() as session:
        row = (await session.execute(text("SELECT amount_minor, analytics_mode FROM orders"))).one()
        assert tuple(row) == (14900, "unknown")
    await database.close()
