from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from math import isclose
from zoneinfo import ZoneInfo

import swisseph as swe  # type: ignore[import-not-found]

from money_profile_bot.domain import AspectFact, BirthData, ChartFacts, PlanetFact, TimePrecision

ENGINE_VERSION = "1.0.0"

SIGNS = (
    "Овен",
    "Телец",
    "Близнецы",
    "Рак",
    "Лев",
    "Дева",
    "Весы",
    "Скорпион",
    "Стрелец",
    "Козерог",
    "Водолей",
    "Рыбы",
)
ELEMENTS = ("огонь", "земля", "воздух", "вода")
SIGN_RULERS = {
    "Овен": "Марс",
    "Телец": "Венера",
    "Близнецы": "Меркурий",
    "Рак": "Луна",
    "Лев": "Солнце",
    "Дева": "Меркурий",
    "Весы": "Венера",
    "Скорпион": "Марс",
    "Стрелец": "Юпитер",
    "Козерог": "Сатурн",
    "Водолей": "Сатурн",
    "Рыбы": "Юпитер",
}
PLANETS = {
    "Солнце": swe.SUN,
    "Луна": swe.MOON,
    "Меркурий": swe.MERCURY,
    "Венера": swe.VENUS,
    "Марс": swe.MARS,
    "Юпитер": swe.JUPITER,
    "Сатурн": swe.SATURN,
    "Уран": swe.URANUS,
    "Нептун": swe.NEPTUNE,
    "Плутон": swe.PLUTO,
}
ASPECTS = {
    "соединение": (0.0, 8.0, False),
    "секстиль": (60.0, 4.0, True),
    "квадрат": (90.0, 6.0, False),
    "тригон": (120.0, 6.0, True),
    "оппозиция": (180.0, 8.0, False),
}


class AmbiguousLocalTime(ValueError):
    pass


def sign_for(longitude: float) -> str:
    return SIGNS[int(longitude % 360 // 30)]


def element_for_sign(sign: str) -> str:
    return ELEMENTS[SIGNS.index(sign) % 4]


def _julian_day(moment: datetime) -> float:
    utc = moment.astimezone(UTC)
    hour = utc.hour + utc.minute / 60 + utc.second / 3600
    return float(swe.julday(utc.year, utc.month, utc.day, hour, swe.GREG_CAL))


def _planet_longitudes(moment: datetime) -> dict[str, float]:
    jd = _julian_day(moment)
    flags = swe.FLG_SWIEPH | swe.FLG_SPEED
    return {
        name: float(swe.calc_ut(jd, identifier, flags)[0][0]) % 360
        for name, identifier in PLANETS.items()
    }


def _localize(value: datetime, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    first = value.replace(tzinfo=zone, fold=0)
    second = value.replace(tzinfo=zone, fold=1)
    first_valid = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == value
    second_valid = second.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == value
    if not first_valid and not second_valid:
        raise AmbiguousLocalTime("local time does not exist because of a clock change")
    if first_valid and second_valid and first.utcoffset() != second.utcoffset():
        raise AmbiguousLocalTime("local time occurs twice because of a clock change")
    return first if first_valid else second


def _house_for(longitude: float, cusps: tuple[float, ...]) -> int:
    for index, start in enumerate(cusps):
        end = cusps[(index + 1) % 12]
        span = (end - start) % 360
        position = (longitude - start) % 360
        if position < span or isclose(position, 0.0, abs_tol=1e-9):
            return index + 1
    return 12


def _aspects(longitudes: dict[str, float]) -> tuple[AspectFact, ...]:
    result: list[AspectFact] = []
    names = list(longitudes)
    for index, first in enumerate(names):
        for second in names[index + 1 :]:
            distance = abs(longitudes[first] - longitudes[second]) % 360
            distance = min(distance, 360 - distance)
            matches = []
            for kind, (angle, max_orb, harmonious) in ASPECTS.items():
                orb = abs(distance - angle)
                if orb <= max_orb:
                    matches.append((orb, kind, harmonious))
            if matches:
                orb, kind, harmonious = min(matches)
                result.append(AspectFact(first, second, kind, round(orb, 3), harmonious))
    return tuple(sorted(result, key=lambda item: item.orb))


def _without_houses(
    moment: datetime,
    *,
    warning: str,
    venus_possible_signs: tuple[str, ...] = (),
) -> ChartFacts:
    longitudes = _planet_longitudes(moment)
    planets = {
        name: PlanetFact(name, value, sign_for(value), element_for_sign(sign_for(value)), None)
        for name, value in longitudes.items()
    }
    return ChartFacts(
        mode="style",
        warning=warning,
        planets=planets,
        aspects=_aspects(longitudes),
        cusps=None,
        cusp_signs=None,
        second_house_ruler=None,
        second_house_ruler_house=None,
        venus_possible_signs=venus_possible_signs,
    )


def _unknown_time(data: BirthData) -> ChartFacts:
    local_start = datetime.combine(data.birth_date, time.min)
    zone = ZoneInfo(data.city.timezone)
    samples: list[dict[str, float]] = []
    for hour in range(25):
        naive = local_start + timedelta(hours=hour)
        samples.append(_planet_longitudes(naive.replace(tzinfo=zone).astimezone(UTC)))

    stable_names = {
        name for name in PLANETS if len({sign_for(sample[name]) for sample in samples}) == 1
    }
    midpoint_values = samples[12]
    planets = {
        name: PlanetFact(
            name,
            midpoint_values[name],
            sign_for(midpoint_values[name]),
            element_for_sign(sign_for(midpoint_values[name])),
            None,
        )
        for name in stable_names
    }
    stable_aspects = []
    for aspect in _aspects(midpoint_values):
        if aspect.first not in stable_names or aspect.second not in stable_names:
            continue
        if all(
            any(
                candidate.first == aspect.first
                and candidate.second == aspect.second
                and candidate.kind == aspect.kind
                for candidate in _aspects(sample)
            )
            for sample in samples
        ):
            stable_aspects.append(aspect)
    venus_signs = tuple(dict.fromkeys(sign_for(sample["Венера"]) for sample in samples))
    return ChartFacts(
        mode="style",
        warning=(
            "Время рождения неизвестно: используются только показатели, стабильные в течение суток, "
            "без домов карты."
        ),
        planets=planets,
        aspects=tuple(stable_aspects),
        cusps=None,
        cusp_signs=None,
        second_house_ruler=None,
        second_house_ruler_house=None,
        venus_possible_signs=venus_signs,
    )


def calculate_chart(data: BirthData) -> ChartFacts:
    if data.time_precision is TimePrecision.UNKNOWN or data.birth_time is None:
        return _unknown_time(data)

    naive = datetime.combine(data.birth_date, data.birth_time)
    try:
        moment = _localize(naive, data.city.timezone)
    except AmbiguousLocalTime:
        fallback = naive.replace(tzinfo=ZoneInfo(data.city.timezone)).astimezone(UTC)
        return _without_houses(
            fallback,
            warning=(
                "Перевод местного времени в этот день неоднозначен, поэтому разбор построен "
                "без домов карты."
            ),
        )

    longitudes = _planet_longitudes(moment)
    try:
        cusps_raw, _ = swe.houses_ex(
            _julian_day(moment), data.city.latitude, data.city.longitude, b"P"
        )
        cusps = tuple(float(value) % 360 for value in cusps_raw)
        if len(cusps) != 12:
            raise ValueError("Swiss Ephemeris returned an invalid number of cusps")
    except Exception:
        return _without_houses(
            moment,
            warning="Для указанной широты дома Плацидуса не определены; используется разбор без домов.",
        )

    planets = {
        name: PlanetFact(
            name,
            value,
            sign_for(value),
            element_for_sign(sign_for(value)),
            _house_for(value, cusps),
        )
        for name, value in longitudes.items()
    }
    cusp_signs = tuple(sign_for(value) for value in cusps)
    ruler = SIGN_RULERS[cusp_signs[1]]
    warning = None
    if data.time_precision is TimePrecision.APPROXIMATE:
        warning = (
            "Время указано примерно: положения планет рассчитаны по введённому времени, "
            "но дома карты могут быть неточными."
        )
    return ChartFacts(
        mode="profile",
        warning=warning,
        planets=planets,
        aspects=_aspects(longitudes),
        cusps=cusps,
        cusp_signs=cusp_signs,
        second_house_ruler=ruler,
        second_house_ruler_house=planets[ruler].house,
        venus_possible_signs=(planets["Венера"].sign,),
    )
