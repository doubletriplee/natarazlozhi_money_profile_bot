from __future__ import annotations

import pytest

from money_profile_bot.domain import AspectFact, ChartFacts, PlanetFact
from money_profile_bot.services.astro import ELEMENTS, SIGNS, element_for_sign
from money_profile_bot.services.rules import (
    HOUSE_TYPES,
    UNKNOWN_TYPES,
    generate_profile,
    validate_generated_profile,
)


def planet(name: str, sign: str, house: int | None) -> PlanetFact:
    return PlanetFact(name, SIGNS.index(sign) * 30 + 10, sign, element_for_sign(sign), house)


def house_facts(house: int) -> ChartFacts:
    cusps = tuple(index * 30.0 for index in range(12))
    return ChartFacts(
        mode="profile",
        warning=None,
        planets={
            "Венера": planet("Венера", "Козерог", house),
            "Меркурий": planet("Меркурий", "Дева", 9),
            "Сатурн": planet("Сатурн", "Телец", 2),
        },
        aspects=(AspectFact("Венера", "Сатурн", "тригон", 1.5, True),),
        cusps=cusps,
        cusp_signs=tuple(SIGNS),
        second_house_ruler="Венера",
        second_house_ruler_house=house,
        venus_possible_signs=("Козерог",),
    )


@pytest.mark.parametrize("house", range(1, 13))
def test_all_twelve_house_types_are_deterministic(house: int) -> None:
    profile = generate_profile(house_facts(house))
    assert profile.money_type == HOUSE_TYPES[house][0]
    assert len(profile.messages) == 6
    assert validate_generated_profile(profile) == []
    assert "Это короткий ориентир по карте" not in profile.free_insight
    assert profile.free_insight.endswith("<b>Узнать больше ↓</b>")


@pytest.mark.parametrize("element", ELEMENTS)
def test_unknown_time_types_cover_four_elements(element: str) -> None:
    sign = next(sign for sign in SIGNS if element_for_sign(sign) == element)
    facts = ChartFacts(
        mode="style",
        warning="без времени",
        planets={
            "Венера": planet("Венера", sign, None),
            "Меркурий": planet("Меркурий", sign, None),
        },
        aspects=(),
        cusps=None,
        cusp_signs=None,
        second_house_ruler=None,
        second_house_ruler_house=None,
        venus_possible_signs=(sign,),
    )
    profile = generate_profile(facts)
    assert profile.money_type == UNKNOWN_TYPES[element][0]
    assert validate_generated_profile(profile) == []


def test_venus_transition_returns_two_styles() -> None:
    facts = ChartFacts(
        mode="style",
        warning="переход",
        planets={"Венера": planet("Венера", "Овен", None)},
        aspects=(),
        cusps=None,
        cusp_signs=None,
        second_house_ruler=None,
        second_house_ruler_house=None,
        venus_possible_signs=("Овен", "Телец"),
    )
    profile = generate_profile(facts)
    assert "Вдохновительница" in profile.money_type
    assert "Мастерица" in profile.money_type
    assert "style.venus_transition" in profile.triggered_rule_ids


def test_tense_aspect_drives_single_trap() -> None:
    facts = house_facts(10)
    facts = ChartFacts(
        **{
            **facts.to_dict(),
            "planets": facts.planets,
            "aspects": (AspectFact("Венера", "Нептун", "квадрат", 0.2, False),),
        }
    )
    profile = generate_profile(facts)
    assert "неясные договорённости" in profile.trap
    assert len(profile.trap) < 120
