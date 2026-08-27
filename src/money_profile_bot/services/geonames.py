from __future__ import annotations

import re
import sqlite3
import unicodedata
from asyncio import to_thread
from pathlib import Path

from money_profile_bot.domain import City


def normalize_city_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold().replace("ё", "е"))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-zа-я0-9]+", " ", normalized).strip()


class CityCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def search(self, query: str, limit: int = 5) -> list[City]:
        normalized = normalize_city_name(query)
        if len(normalized) < 2 or not self.path.exists():
            return []
        return await to_thread(self._search_sync, normalized, limit)

    def _search_sync(self, normalized: str, limit: int) -> list[City]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT DISTINCT c.geoname_id, c.name, c.region, c.country_code,
                       c.country_name, c.latitude, c.longitude, c.timezone, c.population,
                       CASE WHEN n.normalized = ? THEN 0 ELSE 1 END AS rank
                  FROM city_names n
                  JOIN cities c ON c.geoname_id = n.geoname_id
                 WHERE n.normalized = ? OR n.normalized LIKE ?
                 ORDER BY rank, c.population DESC
                 LIMIT ?
                """,
                (normalized, normalized, normalized + "%", limit),
            ).fetchall()
            return [
                City(
                    geoname_id=row["geoname_id"],
                    name=row["name"],
                    region=row["region"],
                    country_code=row["country_code"],
                    country_name=row["country_name"],
                    latitude=row["latitude"],
                    longitude=row["longitude"],
                    timezone=row["timezone"],
                )
                for row in rows
            ]
        finally:
            connection.close()
