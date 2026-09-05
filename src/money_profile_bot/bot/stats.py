from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from money_profile_bot.config import PaymentMode
from money_profile_bot.models import utcnow
from money_profile_bot.services.analytics import MSK, period_since

if TYPE_CHECKING:
    from money_profile_bot.config import Settings
    from money_profile_bot.services.store import Store

PERIODS = {"today": "Сегодня", "7d": "7 дней", "30d": "30 дней", "all": "Всё время"}
STAGES = (
    ("start", "Начали прохождение"),
    ("calculated", "Аватар рассчитан"),
    ("offer", "Предложение отправлено"),
    ("buy", "Начали оплату"),
)


def register_stats(router: Router, store: Store, settings: Settings) -> None:
    async def render(period: str, *, choosing: bool = False) -> tuple[str, InlineKeyboardMarkup]:
        now = utcnow()
        since = period_since(period, now)
        test = settings.payment_mode is PaymentMode.FAKE or settings.robokassa_test_mode
        mode = "test" if test else "live"
        steps = await store.analytics.period_steps(since, now)
        finances = await store.analytics.finances(since, now)
        payments = finances[mode]
        dates = (
            f"{since.astimezone(MSK):%d.%m.%Y %H:%M} — {now.astimezone(MSK):%d.%m.%Y %H:%M} МСК"
            if since
            else f"По состоянию на {now.astimezone(MSK):%d.%m.%Y %H:%M} МСК"
        )
        lines = [f"<b>Статистика · {PERIODS[period].lower()}</b>", dates, ""]
        if test:
            lines += ["Тестовый режим: покупки и списания денег не происходит.", ""]
        for key, label in STAGES:
            lines.append(f"{label}: <b>{steps[key]}</b>")
        lines.append("")
        payment_label = "Тестовых оплат за период" if test else "Оплат за период"
        lines.append(f"{payment_label}: <b>{payments['payments']}</b>")
        if payments["refunds"]:
            lines.append(f"Из них возвращено: <b>{payments['refunds']}</b>")
        if not test and finances["unknown"]["payments"]:
            lines.append(f"Старые оплаты на проверке: {finances['unknown']['payments']}")
        lines += [
            "",
            "Шаги — число людей, оплаты — число платежей за период. Покупатель мог начать раньше.",
        ]
        if choosing:
            buttons = [
                [
                    InlineKeyboardButton(
                        text=("✓ " if p == period else "") + label,
                        callback_data=f"report:funnel:{p}",
                    )
                ]
                for p, label in PERIODS.items()
            ]
            buttons.append(
                [InlineKeyboardButton(text="Отмена", callback_data=f"report:funnel:{period}")]
            )
        else:
            buttons = [
                [
                    InlineKeyboardButton(
                        text="Изменить период", callback_data=f"report:period:{period}"
                    )
                ]
            ]
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not message.from_user or not await store.is_admin(
            message.from_user.id, settings.admin_ids
        ):
            return
        if message.chat.type != "private":
            await message.answer("Открой /stats в личном чате с ботом.")
            return
        text, keyboard = await render("today")
        await store.audit_admin(message.from_user.id, "stats_view")
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("report:") | F.data.startswith("stats:"))
    async def report(callback: CallbackQuery) -> None:
        if not await store.is_admin(callback.from_user.id, settings.admin_ids):
            await callback.answer()
            return
        if not isinstance(callback.message, Message) or callback.message.chat.type != "private":
            await callback.answer("Открой /stats в личном чате с ботом.", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        choosing = False
        if len(parts) == 3 and parts[0] == "report" and parts[1] in {"funnel", "period"}:
            period, choosing = parts[2], parts[1] == "period"
        elif len(parts) == 6 and parts[0] == "report":
            # Old messages lead to the new funnel; removed screens cannot be opened.
            period = parts[2]
        elif len(parts) == 2 and parts[0] == "stats":
            period = parts[1]
        else:
            period = ""
        if period not in PERIODS:
            await callback.answer("Этот отчёт недоступен. Открой /stats.", show_alert=True)
            return
        await callback.answer()
        text, keyboard = await render(period, choosing=choosing)
        await store.audit_admin(callback.from_user.id, "stats_period" if choosing else "stats_view")
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message:
                raise
