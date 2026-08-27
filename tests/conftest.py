from __future__ import annotations

from datetime import date, time

import pytest

from money_profile_bot.domain import BirthData, City, TimePrecision


@pytest.fixture
def moscow() -> City:
    return City(
        geoname_id=524901,
        name="Москва",
        region="Москва",
        country_code="RU",
        country_name="Россия",
        latitude=55.7522,
        longitude=37.6156,
        timezone="Europe/Moscow",
    )


@pytest.fixture
def birth(moscow: City) -> BirthData:
    return BirthData(
        name="Наталья",
        birth_date=date(1990, 1, 15),
        time_precision=TimePrecision.EXACT,
        birth_time=time(12, 30),
        city=moscow,
    )
