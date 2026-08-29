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


def city_query_candidates(value: str) -> tuple[str, ...]:
    """Accept a city alone as well as common `country, city` input."""
    normalized = normalize_city_name(value)
    if not normalized:
        return ()
    raw_parts = [normalize_city_name(part) for part in re.split(r"[,;/|]+", value)]
    words = normalized.split()[:12]
    candidates = [normalized, *(part for part in raw_parts if part)]
    # Also tolerate input without commas, such as "Россия Москва" or "New York USA".
    for size in range(min(3, len(words)), 0, -1):
        candidates.extend(
            " ".join(words[index : index + size]) for index in range(len(words) - size + 1)
        )
    return tuple(dict.fromkeys(item for item in candidates if len(item) >= 2))[:40]


class CityCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def search(self, query: str, limit: int = 5) -> list[City]:
        candidates = city_query_candidates(query)
        if not candidates or not self.path.exists():
            return []
        return await to_thread(self._search_sync, candidates, limit)

    def _search_sync(self, candidates: tuple[str, ...], limit: int) -> list[City]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            exact_placeholders = ",".join("?" for _ in candidates)
            prefix_clauses = " OR ".join("n.normalized LIKE ?" for _ in candidates)
            rows = connection.execute(
                f"""
                SELECT DISTINCT c.geoname_id, c.name, c.region, c.country_code,
                       c.country_name, c.latitude, c.longitude, c.timezone, c.population,
                       CASE WHEN n.normalized IN ({exact_placeholders}) THEN 0 ELSE 1 END AS rank,
                       length(n.normalized) AS matched_length
                  FROM city_names n
                  JOIN cities c ON c.geoname_id = n.geoname_id
                 WHERE n.normalized IN ({exact_placeholders}) OR {prefix_clauses}
                 ORDER BY rank, matched_length DESC, c.population DESC
                 LIMIT ?
                """,
                (*candidates, *candidates, *(item + "%" for item in candidates), limit),
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
