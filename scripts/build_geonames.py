from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import urllib.request
import zipfile
from pathlib import Path

GEONAMES_URL = "https://download.geonames.org/export/dump/cities500.zip"
COUNTRY_URL = "https://download.geonames.org/export/dump/countryInfo.txt"
ADMIN1_URL = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed official URLs
        return response.read()


def reference_table(content: str, key_index: int, value_index: int) -> dict[str, str]:
    result = {}
    for row in csv.reader(io.StringIO(content), delimiter="\t"):
        if row and not row[0].startswith("#") and len(row) > max(key_index, value_index):
            result[row[key_index]] = row[value_index]
    return result


def build(destination: Path) -> None:
    countries = reference_table(download(COUNTRY_URL).decode("utf-8"), 0, 4)
    regions = reference_table(download(ADMIN1_URL).decode("utf-8"), 0, 1)
    archive = zipfile.ZipFile(io.BytesIO(download(GEONAMES_URL)))
    data_name = next(name for name in archive.namelist() if name.endswith(".txt"))

    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(destination)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            DROP TABLE IF EXISTS city_names;
            DROP TABLE IF EXISTS cities;
            CREATE TABLE cities (
                geoname_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                region TEXT NOT NULL,
                country_code TEXT NOT NULL,
                country_name TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                timezone TEXT NOT NULL,
                population INTEGER NOT NULL
            );
            CREATE TABLE city_names (
                normalized TEXT NOT NULL,
                geoname_id INTEGER NOT NULL REFERENCES cities(geoname_id)
            );
            CREATE INDEX ix_city_names_normalized ON city_names(normalized);
            """
        )
        from money_profile_bot.services.geonames import normalize_city_name

        with archive.open(data_name) as source:
            reader = csv.reader(io.TextIOWrapper(source, encoding="utf-8"), delimiter="\t")
            for row in reader:
                geoname_id = int(row[0])
                name, ascii_name, alternate_names = row[1], row[2], row[3]
                country_code, admin1 = row[8], row[10]
                connection.execute(
                    "INSERT INTO cities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        geoname_id,
                        name,
                        regions.get(f"{country_code}.{admin1}", admin1),
                        country_code,
                        countries.get(country_code, country_code),
                        float(row[4]),
                        float(row[5]),
                        row[17],
                        int(row[14] or 0),
                    ),
                )
                variants = {name, ascii_name, *alternate_names.split(",")}
                normalized_variants = {
                    normalized for item in variants if (normalized := normalize_city_name(item))
                }
                connection.executemany(
                    "INSERT INTO city_names VALUES (?, ?)",
                    ((variant, geoname_id) for variant in normalized_variants),
                )
        connection.commit()
        connection.execute("ANALYZE")
    finally:
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the local GeoNames cities500 index")
    parser.add_argument("destination", type=Path)
    build(parser.parse_args().destination)
