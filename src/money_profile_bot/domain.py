from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, time
from enum import StrEnum
from typing import Any


class TimePrecision(StrEnum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class City:
    geoname_id: int
    name: str
    region: str
    country_code: str
    country_name: str
    latitude: float
    longitude: float
    timezone: str


@dataclass(frozen=True, slots=True)
class BirthData:
    name: str
    birth_date: date
    time_precision: TimePrecision
    birth_time: time | None
    city: City

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["birth_date"] = self.birth_date.isoformat()
        result["birth_time"] = self.birth_time.isoformat() if self.birth_time else None
        result["time_precision"] = self.time_precision.value
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BirthData:
        return cls(
            name=value["name"],
            birth_date=date.fromisoformat(value["birth_date"]),
            time_precision=TimePrecision(value["time_precision"]),
            birth_time=time.fromisoformat(value["birth_time"]) if value["birth_time"] else None,
            city=City(**value["city"]),
        )


@dataclass(frozen=True, slots=True)
class PlanetFact:
    name: str
    longitude: float
    sign: str
    element: str
    house: int | None


@dataclass(frozen=True, slots=True)
class AspectFact:
    first: str
    second: str
    kind: str
    orb: float
    harmonious: bool


@dataclass(frozen=True, slots=True)
class ChartFacts:
    mode: str
    warning: str | None
    planets: dict[str, PlanetFact]
    aspects: tuple[AspectFact, ...]
    cusps: tuple[float, ...] | None
    cusp_signs: tuple[str, ...] | None
    second_house_ruler: str | None
    second_house_ruler_house: int | None
    venus_possible_signs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GeneratedProfile:
    title: str
    money_type: str
    strength: str
    trap: str
    free_insight: str
    messages: tuple[str, ...]
    triggered_rule_ids: tuple[str, ...]
    engine_version: str
    rules_version: str
    disclaimer: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
