from __future__ import annotations

import argparse
from pathlib import Path

from money_profile_bot.services.card import CardRenderer

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    CardRenderer("money_profile_bot").render(
        name="Александра-Екатерина",
        money_type="Создатель ценности",
        strength="сочетание ясной структуры, эстетики и внимания к деталям",
        destination=arguments.destination,
    )
