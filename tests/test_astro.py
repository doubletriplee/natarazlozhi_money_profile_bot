from __future__ import annotations

from dataclasses import replace
from datetime import date, time

import pytest

from money_profile_bot.domain import BirthData, TimePrecision
from money_profile_bot.services import astro
from money_profile_bot.services.astro import calculate_chart, element_for_sign, sign_for
from money_profile_bot.services.rules import generate_profile


@pytest.mark.parametrize(
    ("longitude", "sign"),
    [(0, "Овен"), (29.999, "Овен"), (30, "Телец"), (180, "Весы"), (359.9, "Рыбы")],
)
def test_zodiac_boundaries(longitude: float, sign: str) -> None:
    assert sign_for(longitude) == sign


@pytest.mark.parametrize(
    ("sign", "element"),
    [("Овен", "огонь"), ("Телец", "земля"), ("Близнецы", "воздух"), ("Рак", "вода")],
)
def test_sign_elements(sign: str, element: str) -> None:
    assert element_for_sign(sign) == element


def test_exact_time_builds_placidus_profile(birth: BirthData) -> None:
    facts = calculate_chart(birth)
    assert facts.mode == "profile"
    assert facts.cusps is not None and len(facts.cusps) == 12
    assert facts.second_house_ruler_house in range(1, 13)


def test_approximate_time_keeps_houses_with_warning(birth: BirthData) -> None:
    facts = calculate_chart(replace(birth, time_precision=TimePrecision.APPROXIMATE))
    assert facts.mode == "profile"
    assert facts.warning and "примерно" in facts.warning


def test_unknown_time_uses_only_stable_daily_facts(birth: BirthData) -> None:
    facts = calculate_chart(replace(birth, time_precision=TimePrecision.UNKNOWN, birth_time=None))
    assert facts.mode == "style"
    assert facts.cusps is None
    assert "Венера" in facts.planets
    assert all(planet.house is None for planet in facts.planets.values())


@pytest.mark.parametrize(
    ("local_date", "local_time"),
    [(date(2021, 11, 7), time(1, 30)), (date(2021, 3, 14), time(2, 30))],
)
def test_dst_ambiguity_falls_back_without_houses(
    birth: BirthData, local_date: date, local_time: time
) -> None:
    new_york = replace(
        birth.city,
        geoname_id=5128581,
        name="New York",
        country_code="US",
        country_name="United States",
        timezone="America/New_York",
        latitude=40.7128,
        longitude=-74.006,
    )
    facts = calculate_chart(
        replace(birth, birth_date=local_date, birth_time=local_time, city=new_york)
    )
    assert facts.mode == "style"
    assert facts.warning and "времени" in facts.warning


def test_polar_latitude_falls_back_without_houses(birth: BirthData) -> None:
    polar = replace(
        birth.city,
        geoname_id=0,
        name="Полярный город",
        latitude=89.0,
        longitude=20.0,
        timezone="UTC",
    )
    facts = calculate_chart(replace(birth, city=polar))
    assert facts.mode == "style"
    assert facts.warning and "Плацидуса" in facts.warning


def test_unknown_time_reports_venus_transition(
    monkeypatch: pytest.MonkeyPatch, birth: BirthData
) -> None:
    calls = 0

    def fake(_: object) -> dict[str, float]:
        nonlocal calls
        calls += 1
        result = {name: float(index * 20) for index, name in enumerate(astro.PLANETS)}
        result["Венера"] = 29.9 if calls < 13 else 30.1
        return result

    monkeypatch.setattr(astro, "_planet_longitudes", fake)
    facts = calculate_chart(replace(birth, time_precision=TimePrecision.UNKNOWN, birth_time=None))
    assert facts.venus_possible_signs == ("Овен", "Телец")
    assert "Венера" not in facts.planets

    profile = generate_profile(facts)
    assert profile.money_type == "Вдохновительница или Мастерица"
    assert "style.venus_transition" in profile.triggered_rule_ids
