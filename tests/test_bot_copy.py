from money_profile_bot.bot.router import OFFER_DELAY_SECONDS, _offer_caption, _price
from money_profile_bot.config import Settings


def test_avatar_offer_copy_and_delay() -> None:
    settings = Settings(_env_file=None)
    assert OFFER_DELAY_SECONDS == 5
    assert _price(settings) == "149₽"
    caption = _offer_caption()
    assert "Твоя сила уже есть" in caption
    assert "ближайшие 7 дней" in caption
    assert "твоя инструкция" in caption
