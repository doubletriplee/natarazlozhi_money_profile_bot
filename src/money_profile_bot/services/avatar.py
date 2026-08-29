from __future__ import annotations

from pathlib import Path

AVATAR_SLUGS = {
    "Вдохновительница": "inspirer",
    "Искательница": "seeker",
    "Мастерица": "craftswoman",
    "Муза": "muse",
    "Навигатор": "navigator",
    "Проводница": "guide",
    "Рассказчица": "storyteller",
    "Создательница": "creator",
    "Хранительница": "keeper",
    "Эстетка": "aesthete",
}

# Старые значения могут встречаться в уже сохранённых результатах. Они остаются
# совместимыми, но пользователю всегда показывается один из новых аватаров.
LEGACY_AVATAR_NAMES = {
    "Коммуникатор": "Рассказчица",
    "Творец": "Создательница",
    "Эксперт": "Мастерица",
    "Стратег": "Навигатор",
    "Лидер": "Вдохновительница",
    "Организатор": "Хранительница",
    "Наставник": "Проводница",
    "Исследователь": "Искательница",
    "Визионер": "Муза",
    "Эстет": "Эстетка",
    "Первопроходец": "Вдохновительница",
    "Создатель ценности": "Мастерица",
    "Хранитель": "Хранительница",
    "Мастер": "Мастерица",
    "Партнёр": "Эстетка",
    "Трансформатор": "Навигатор",
    "Управленец": "Вдохновительница",
    "Объединитель": "Муза",
    "Инициатор": "Вдохновительница",
    "Проводник доверия": "Хранительница",
}


def display_avatar_name(value: str) -> str:
    """Return one image-backed avatar, including for legacy or dual-style results."""
    primary = value.split(" или ", 1)[0].strip()
    if primary in AVATAR_SLUGS:
        return primary
    try:
        return LEGACY_AVATAR_NAMES[primary]
    except KeyError as exc:
        raise ValueError(f"unknown money avatar: {primary}") from exc


class AvatarAssets:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def free_image(self, money_type: str) -> Path:
        return self._resolve("avatar", money_type)

    def offer_image(self, money_type: str) -> Path:
        return self._resolve("continuation", money_type)

    def full_reading_offer_image(self) -> Path:
        path = self.directory / "full_reading_offer.png"
        if not path.is_file():
            raise FileNotFoundError(f"full reading offer asset is missing: {path}")
        return path

    def _resolve(self, prefix: str, money_type: str) -> Path:
        avatar = display_avatar_name(money_type)
        path = self.directory / f"{prefix}_{AVATAR_SLUGS[avatar]}.png"
        if not path.is_file():
            raise FileNotFoundError(f"avatar asset is missing: {path}")
        return path
