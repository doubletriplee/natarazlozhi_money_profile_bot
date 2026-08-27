from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from money_profile_bot.services.card import CardRenderer
from money_profile_bot.services.geonames import CityCatalog, normalize_city_name


def test_city_normalization_handles_yo_and_punctuation() -> None:
    assert normalize_city_name("Орёл, Россия") == "орел россия"


@pytest.mark.asyncio
async def test_city_catalog_returns_ambiguous_results_by_population(tmp_path: Path) -> None:
    database = tmp_path / "cities.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE cities (geoname_id INTEGER PRIMARY KEY, name TEXT, region TEXT,
        country_code TEXT, country_name TEXT, latitude REAL, longitude REAL, timezone TEXT,
        population INTEGER);
        CREATE TABLE city_names (normalized TEXT, geoname_id INTEGER);
        """
    )
    connection.executemany(
        "INSERT INTO cities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "Киров", "Кировская область", "RU", "Россия", 58.6, 49.6, "Europe/Kirov", 500000),
            (2, "Киров", "Калужская область", "RU", "Россия", 54.0, 34.3, "Europe/Moscow", 30000),
        ],
    )
    connection.executemany("INSERT INTO city_names VALUES ('киров', ?)", [(1,), (2,)])
    connection.commit()
    connection.close()
    results = await CityCatalog(database).search("Киров")
    assert [result.geoname_id for result in results] == [1, 2]


@pytest.mark.parametrize(
    ("name", "money_type"),
    [
        ("Наталья", "Управленец"),
        ("Александра-Екатерина Константинопольская", "Создатель ценности и Коммуникатор"),
    ],
)
def test_card_is_exact_size_for_short_and_long_text(
    tmp_path: Path, name: str, money_type: str
) -> None:
    output = tmp_path / "card.png"
    CardRenderer("money_profile_bot").render(
        name=name,
        money_type=money_type,
        strength="сочетание ясной структуры, эстетики и внимания к деталям",
        destination=output,
    )
    with Image.open(output) as image:
        assert image.size == (1080, 1350)
        assert image.mode == "RGB"
