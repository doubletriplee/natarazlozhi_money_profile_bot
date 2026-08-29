from datetime import date, time
from pathlib import Path

from money_profile_bot.domain import BirthData, City, TimePrecision
from money_profile_bot.services.astro import calculate_chart
from money_profile_bot.services.avatar import AvatarAssets
from money_profile_bot.services.pdf import PdfRenderer
from money_profile_bot.services.rules import generate_profile


def main() -> None:
    birth = BirthData(
        name="Наталья",
        birth_date=date(1990, 1, 15),
        time_precision=TimePrecision.EXACT,
        birth_time=time(12, 30),
        city=City(
            geoname_id=524901,
            name="Москва",
            region="Москва",
            country_code="RU",
            country_name="Россия",
            latitude=55.7522,
            longitude=37.6156,
            timezone="Europe/Moscow",
        ),
    )
    result = generate_profile(calculate_chart(birth))
    PdfRenderer(AvatarAssets(Path("assets/avatars"))).render(
        name=birth.name,
        result=result,
        destination=Path("output/pdf/sample_money_avatar.pdf"),
    )


if __name__ == "__main__":
    main()
