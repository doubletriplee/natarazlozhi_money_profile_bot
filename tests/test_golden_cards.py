from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from money_profile_bot.domain import BirthData
from money_profile_bot.services.astro import calculate_chart
from money_profile_bot.services.avatar import (
    AvatarAssets,
    avatar_free_caption,
    avatar_paid_caption_parts,
)
from money_profile_bot.services.rules import generate_profile, validate_generated_profile

GOLDEN_CARDS = Path("tests/fixtures/golden_cards.json")
AVATAR_ASSETS = AvatarAssets(Path("assets/avatars"))


def _normalized(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    return value


def _digest(value: Any) -> str:
    payload = json.dumps(
        _normalized(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _cases() -> list[dict[str, Any]]:
    return json.loads(GOLDEN_CARDS.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["id"])
def test_approved_golden_card_stays_unchanged(case: dict[str, Any]) -> None:
    birth = BirthData.from_dict(case["input"])
    expected = case["expected"]

    facts = calculate_chart(birth)
    profile = generate_profile(facts)

    assert facts.mode == expected["mode"]
    assert facts.warning == expected["warning"]
    assert list(facts.venus_possible_signs) == expected["venus_possible_signs"]
    actual_cusp_signs = list(facts.cusp_signs) if facts.cusp_signs else None
    assert actual_cusp_signs == expected["cusp_signs"]
    assert facts.second_house_ruler == expected["second_house_ruler"]
    assert facts.second_house_ruler_house == expected["second_house_ruler_house"]
    assert profile.money_type == expected["money_type"]
    assert profile.strength == expected["strength"]
    assert profile.trap == expected["trap"]
    assert list(profile.triggered_rule_ids) == expected["triggered_rule_ids"]
    assert profile.engine_version == expected["engine_version"]
    assert profile.rules_version == expected["rules_version"]
    assert validate_generated_profile(profile) == []

    assert _digest(facts.to_dict()) == expected["chart_sha256"]
    assert _digest(profile.to_dict()) == expected["profile_sha256"]
    assert _digest(avatar_free_caption(profile.money_type)) == expected["free_caption_sha256"]
    assert _digest(avatar_paid_caption_parts(profile.money_type)) == expected["paid_caption_sha256"]

    avatar_path = AVATAR_ASSETS.free_image(profile.money_type)
    assert avatar_path.name == expected["avatar_asset"]
    assert hashlib.sha256(avatar_path.read_bytes()).hexdigest() == expected["avatar_sha256"]
