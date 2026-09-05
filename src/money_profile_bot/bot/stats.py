from __future__ import annotations

import hashlib
import html
from collections import Counter
from datetime import timedelta
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from money_profile_bot.models import Journey, JourneyEvent, User, utcnow
from money_profile_bot.services.analytics import (
    BUTTONS,
    ERRORS,
    FORM_STEPS,
    FUNNEL,
    MODES,
    MSK,
    STEPS,
    TERMINAL,
    as_utc,
    period_since,
)

if TYPE_CHECKING:
    from money_profile_bot.config import Settings
    from money_profile_bot.services.store import Store

PERIODS = {"today": "Сегодня", "7d": "7 дней", "30d": "30 дней", "all": "Всё время"}
PAGE_SIZE = 8


def idle(journey: Journey | None) -> timedelta:
    if journey is None:
        return timedelta(0)
    reference = max(as_utc(journey.step_at), as_utc(journey.last_action_at or journey.step_at))
    return utcnow() - reference


def duration(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes >= 1440:
        return f"{minutes // 1440} д. {minutes % 1440 // 60} ч."
    if minutes >= 60:
        return f"{minutes // 60} ч. {minutes % 60} мин."
    return f"{minutes} мин."


def waiting(journey: Journey | None) -> bool:
    return bool(journey and journey.step not in TERMINAL and not journey.error)


def source_key(source: str) -> str:
    return "x" + hashlib.sha256(source.encode()).hexdigest()[:12]


def selected(
    rows: list[tuple[User, Journey | None]], value: str
) -> list[tuple[User, Journey | None]]:
    return [
        (u, j)
        for u, j in rows
        if (
            value == "all"
            or value == "hour"
            and waiting(j)
            and idle(j) >= timedelta(hours=1)
            or value == "day"
            and waiting(j)
            and idle(j) >= timedelta(days=1)
            or value == "form"
            and j is not None
            and j.step in FORM_STEPS
            or value == "error"
            and j is not None
            and j.error is not None
            or value.startswith("s-")
            and (j.step if j else "unknown") == value[2:]
            or value.startswith("x")
            and source_key((j.source if j else u.last_source) or "direct") == value
        )
    ]


def event_label(kind: str, key: str, actor: str) -> str:
    if kind == "click":
        return f"Нажал «{BUTTONS.get(key, key)}»"
    if kind == "step":
        suffix = " · автоматически" if actor == "automatic" else ""
        return STEPS.get(key, key) + suffix
    if kind == "passed":
        return f"✓ Пройден шаг: {STEPS.get(key, key)}"
    if kind == "input_rejected":
        return f"Ввод не принят: {STEPS.get(key, key)}"
    if kind == "error":
        return f"⚠ {ERRORS.get(key, key)}"
    if kind == "recovered":
        return f"✓ Устранено: {ERRORS.get(key, key)}"
    if kind == "skipped":
        return "Время неизвестно — ввод времени пропущен по выбору пользователя"
    if kind == "reminder":
        return "Напоминание отправлено автоматически"
    return {
        "start": "Начато новое прохождение",
        "input": "Отправлен ответ боту",
        "profile": "Повторный запрос /profile",
        "calculation": "Аватар рассчитан",
        "refund": "Возврат завершён",
        "payment_cancel": "Оплата отменена",
    }.get(key, f"Подтверждённый факт: {STEPS.get(key, key)}")


def register_stats(router: Router, store: Store, settings: Settings) -> None:
    async def render(
        view: str, period: str, mode: str, arg: str, page: int
    ) -> tuple[str, InlineKeyboardMarkup]:
        since = period_since(period)
        buttons: list[list[InlineKeyboardButton]] = []

        def button(
            label: str, target: str, value: str = "all", number: int = 0
        ) -> InlineKeyboardButton:
            data = f"report:{target}:{period}:{mode}:{value}:{number}"
            if len(data.encode()) > 64:
                raise ValueError("stats callback exceeds Telegram limit")
            return InlineKeyboardButton(text=label, callback_data=data)

        def add(label: str, target: str, value: str = "all", number: int = 0) -> None:
            buttons.append([button(label, target, value, number)])

        title = f"<b>Статистика · {PERIODS[period]} · {MODES[mode]}</b>\nВремя: МСК\n"
        lines = [title]
        rows = (
            await store.analytics.current(since, mode)
            if view in {"home", "steps", "users", "sources", "filters"}
            else []
        )
        if view == "home":
            new = sum(since is None or as_utc(u.created_at) >= since for u, _ in rows)
            returning = sum(
                since is not None
                and as_utc(u.created_at) < since
                and j is not None
                and j.last_action_at is not None
                and as_utc(j.last_action_at) >= since
                for u, j in rows
            )
            lines += [
                f"Пользователи с данными за период: {len(rows)}",
                f"Из них новые: {new} · вернувшиеся: {returning}",
                f"На шагах анкеты: {len(selected(rows, 'form'))}",
                f"Без продолжения ≥ 1 ч.: {len(selected(rows, 'hour'))}",
                f"Без продолжения ≥ 24 ч.: {len(selected(rows, 'day'))}",
                f"С неустранённой ошибкой: {len(selected(rows, 'error'))}",
                f"История неполная / отсутствует: {sum(not j or not j.complete_history for _, j in rows)}",
            ]
            funnel = await store.analytics.funnel(since, mode)
            people = funnel["people"]
            lines += [
                "",
                f"<b>Общая воронка: {len(people['start'])} человек</b>",
                f"Аватар отправлен: {len(people['free'])}",
                f"Начали покупку: {len(people['buy'])}",
                f"Оплата / тест подтверждены: {len(people['paid'])}",
                f"Разбор отправлен: {len(people['delivered'])}",
                "\nОстановки — по последнему прохождению каждого пользователя. "
                "Воронка — по людям, начавшим прохождение в периоде; каждый человек считается один раз.",
            ]
            add("Общая воронка", "funnel")
            add("Где остановились", "steps")
            add("Пользователи", "users")
            add("Без продолжения ≥ 24 ч.", "users", "day")
            add("Ошибки бота", "users", "error")
            add("Кнопки", "buttons")
            add("Оплаты", "money")
            add("Источники", "sources")
        elif view == "funnel":
            funnel = await store.analytics.funnel(since, mode)
            people = funnel["people"]
            total = len(people["start"])
            lines += [
                "<b>Общая воронка · уникальные люди</b>",
                "Группа: начавшие прохождение в выбранном периоде и режиме. Повторный расчёт человека не дублирует.\n",
            ]
            previous = total
            for key, label in FUNNEL:
                count = len(people[key])
                if key == "paid" and mode == "test":
                    label = "Завершили тест без покупки и списания"
                if key == "start":
                    lines.append(f"<b>{label}: {count}</b>")
                else:
                    rate = f"{count / previous * 100:.1f}%" if previous else "—"
                    lines.append(
                        f"<b>{label}: {count}</b>\nС прошлого шага: {rate} · не перешли: {previous - count}"
                    )
                previous = count
            conversion = f"{len(people['paid']) / total * 100:.1f}%" if total else "—"
            lines += [
                f"\n<b>Старт → подтверждение оплаты: {conversion}</b>",
                f"Прохождений у этой группы: {funnel['total']}.",
                "Оплаты учитываются после старта этой же группы, даже если произошли позднее выбранного периода. "
                "Неполная старая история исключена; её можно посмотреть в «Пользователях». "
                "«Не перешли» — состояние на сейчас, человек ещё может продолжить.",
            ]
            if mode == "all":
                lines.append(
                    "Все режимы включают тесты. Для фактических покупок выбери «Реальные»."
                )
            add("Места остановки", "steps")
        elif view == "steps":
            funnel = await store.analytics.funnel(since, mode)
            lines += [
                "<b>Где находятся пользователи сейчас</b>",
                "На кнопках: сейчас здесь / без продолжения ≥ 24 ч.\n",
                "Воронка ниже: дошли / прошли дальше. Только полная история; "
                "каждое прохождение считается один раз на шаге.",
            ]
            for key, label in STEPS.items():
                members = selected(rows, "s-" + key)
                inactive = sum(waiting(j) and idle(j) >= timedelta(days=1) for _, j in members)
                add(f"{label}: {len(members)} / {inactive}", "users", "s-" + key)
                reached = len(funnel["reached"][key])
                passed = len(funnel["passed"][key] & funnel["reached"][key])
                if reached:
                    lines.append(f"{label}: {reached} / {passed}")
            lines.append(
                "\n«Не знаю время» — разрешённая ветка. Отмена и завершённый разбор не считаются зависанием."
            )
        elif view == "users":
            members = selected(rows, arg)
            members.sort(
                key=lambda row: (row[1] is not None and row[1].error is not None, idle(row[1])),
                reverse=True,
            )
            page = min(page, max(0, (len(members) - 1) // PAGE_SIZE))
            lines += [
                f"<b>Пользователи: {len(members)}</b> · страница {page + 1}",
                "Порядок: ошибки, затем длительность остановки. Открой карточку кнопкой.\n",
            ]
            for user, journey in members[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
                step = STEPS[journey.step] if journey else STEPS["unknown"]
                details = (
                    ERRORS[journey.error]
                    if journey and journey.error
                    else duration(idle(journey))
                    if journey
                    else "нет данных"
                )
                code = "U-" + user.id[:8]
                lines.append(
                    f"<b>{code}</b> · {html.escape(step)}\n{html.escape(details)} · {html.escape((journey.source if journey else user.last_source) or 'direct')}"
                )
                add(f"{code} · {step}", "user", user.id)
            nav = []
            if page:
                nav.append(button("←", view, arg, page - 1))
            if (page + 1) * PAGE_SIZE < len(members):
                nav.append(button("→", view, arg, page + 1))
            if nav:
                buttons.append(nav)
            add("Фильтры пользователей", "filters")
        elif view == "filters":
            lines.append("<b>Фильтр пользователей</b>")
            for label, value in [
                ("Все", "all"),
                ("Заполняют анкету", "form"),
                ("Без действий ≥ 1 ч.", "hour"),
                ("Без действий ≥ 24 ч.", "day"),
                ("Ошибки", "error"),
            ]:
                add(label, "users", value)
            add("По шагу", "steps")
            add("По источнику", "sources")
        elif view in {"user", "journey"}:
            journey_id = int(arg) if view == "journey" else None
            if journey_id:
                async with store.sessions() as session:
                    chosen = await session.get(Journey, journey_id)
                    user_id = chosen.user_id if chosen else ""
            else:
                user_id = arg
            history = await store.analytics.history(user_id, journey_id, page)
            if not history:
                lines.append("Пользователь удалён или история недоступна.")
            else:
                user, journey = history["user"], history["journey"]
                lines += [
                    f"<b>U-{user.id[:8]}</b> · Telegram ID: <code>{html.escape(history['telegram_id'])}</code>",
                    f"Первый источник: {html.escape(user.first_source or 'direct')}",
                    f"Последний источник: {html.escape(user.last_source or 'direct')}",
                ]
                if journey:
                    lines += [
                        f"\n<b>Прохождение #{journey.id}</b> · {MODES[journey.mode]}",
                        f"Начало: {as_utc(journey.created_at).astimezone(MSK):%d.%m.%Y %H:%M}",
                        f"Текущий шаг: {STEPS[journey.step]}",
                        f"Последнее действие: {as_utc(journey.last_action_at).astimezone(MSK).strftime('%d.%m %H:%M') if journey.last_action_at else 'нет данных'}",
                    ]
                    if journey.error:
                        lines.append(f"⚠ {ERRORS[journey.error]}")
                    expected = {
                        "consent": "Нажать «Согласен(а), продолжить»",
                        "city": "Отправить название города",
                        "city_choice": "Выбрать подходящий город",
                        "confirm": "Нажать «Всё верно»",
                        "calculation": "Дождаться расчёта или повторить после ошибки",
                        "free": "Нажать «Узнать силу» или дождаться предложения через час",
                        "offer": "Нажать «Раскрыть силу»",
                        "email": "Отправить корректный email для чека",
                        "invoice": "Завершить оплату в Robokassa",
                        "paid": "Дождаться выдачи оплаченного разбора",
                        "delivered": "Разбор выдан; продолжение необязательно",
                        "full_offer": "Предложение отправлено; продолжение необязательно",
                        "cancelled": "Для нового расчёта отправить /start",
                        "payment_cancelled": "Для возврата к покупке отправить /profile",
                        "unknown": "Нет данных",
                    }.get(journey.step, BUTTONS.get(journey.step, STEPS[journey.step]))
                    lines.append(f"Далее: {expected}")
                    if journey.step == "invoice":
                        lines.append(
                            "Ссылка отправлена. Переход неизвестен; подтверждение оплаты ещё не получено."
                        )
                    if not journey.complete_history:
                        lines.append("История неполная: прошлые нажатия неизвестны.")
                    lines.append("\n<b>История · сначала последние события</b>")
                    for event in history["events"]:
                        stamp = as_utc(event.created_at).astimezone(MSK).strftime("%d.%m %H:%M")
                        lines.append(
                            f"{stamp} · {html.escape(event_label(event.kind, event.key, event.actor))}"
                        )
                    nav = []
                    if page:
                        nav.append(button("← Новее", "journey", str(journey.id), page - 1))
                    if history["more"]:
                        nav.append(button("Старее →", "journey", str(journey.id), page + 1))
                    if nav:
                        buttons.append(nav)
                    journeys = history["journeys"]
                    index = next(i for i, item in enumerate(journeys) if item.id == journey.id)
                    if index + 1 < len(journeys):
                        add("Предыдущее прохождение", "journey", str(journeys[index + 1].id))
                    if index:
                        add("Следующее прохождение", "journey", str(journeys[index - 1].id))
                    # Only observed exposures can be classified as unclicked.
                    async with store.sessions() as session:
                        events = list(
                            (
                                await session.scalars(
                                    select(JourneyEvent).where(
                                        JourneyEvent.journey_id == journey.id
                                    )
                                )
                            ).all()
                        )
                    sent = {event.key for event in events if event.kind == "button_sent"}
                    clicked = {event.key for event in events if event.kind == "click"}
                    missing = [BUTTONS[key] for key in BUTTONS if key in sent - clicked]
                    if missing:
                        lines.append(
                            "\nБез зарегистрированного нажатия: "
                            + "; ".join(missing)
                            + ". Необязательные кнопки не означают остановку."
                        )
                else:
                    lines.append("\nИстория шагов пока отсутствует. Прошлые нажатия неизвестны.")
                add("К списку пользователей", "users")
        elif view == "buttons":
            metrics = await store.analytics.buttons(since, mode)
            lines += [
                "<b>Кнопки · уникальные пользователи</b>",
                "Когорта: кому сообщение с кнопкой отправлено в периоде.",
                "Отправлено / нажали / продолжили иначе / ждём / без действий ≥ 24 ч.\n",
            ]
            entries = [(key, label) for key, label in BUTTONS.items() if key in metrics]
            for key, label in entries[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
                row = metrics[key]
                lines.append(
                    f"<b>{label}</b>\n{row['sent']} / {row['clicked']} / {row['continued']} / {row['waiting']} / {row['inactive']}"
                )
            if not entries:
                lines.append("Нет зарегистрированных отправок за период.")
            nav = []
            if page:
                nav.append(button("←", view, arg, page - 1))
            if (page + 1) * PAGE_SIZE < len(entries):
                nav.append(button("→", view, arg, page + 1))
            if nav:
                buttons.append(nav)
            lines.append(
                "\nОтправлено ≠ прочитано. URL-кнопки оплаты, документов и связи: переход неизвестен. "
                "Автоотправка предложения не считается нажатием «Узнать силу»."
            )
        elif view == "money":
            finances = await store.analytics.finances(since)
            lines.append("<b>Оплаты по дате подтверждения</b>")
            for key, row in finances.items():
                if mode != "all" and key != mode:
                    continue
                lines += [
                    f"\n<b>{MODES[key]}</b>",
                    f"Подтверждено: {row['payments']} · полных возвратов: {row['refunds']}",
                ]
                if key == "live":
                    lines.append(f"Чистая выручка: {row['net_minor'] / 100:.2f} ₽")
                elif key == "test":
                    lines.append(
                        "Тестовые прохождения: покупки и списания денег нет; в выручку не входят."
                    )
                else:
                    lines.append(
                        f"Историческая сумма после возвратов: {row['net_minor'] / 100:.2f} ₽; режим не был записан, в реальную выручку не включена."
                    )
        elif view == "sources":
            sources = Counter((j.source if j else u.last_source) or "direct" for u, j in rows)
            lines.append("<b>Источники последнего прохождения</b> · пользователи\n")
            source_entries = sources.most_common()
            for source, count in source_entries[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
                lines.append(f"{html.escape(source)}: {count}")
                add(f"{source}: {count}", "users", source_key(source))
            if page:
                add("←", view, arg, page - 1)
            if (page + 1) * PAGE_SIZE < len(source_entries):
                add("→", view, arg, page + 1)
            first = Counter(u.first_source or "direct" for u, _ in rows)
            lines.append(
                "\nПервые источники этой группы: "
                + ", ".join(f"{html.escape(k)}: {v}" for k, v in first.most_common(10))
            )
        else:
            raise ValueError("unknown stats view")
        if view not in {"user", "journey"}:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=("✓ " if p == period else "") + label,
                        callback_data=f"report:{view}:{p}:{mode}:{arg}:0",
                    )
                    for p, label in PERIODS.items()
                ]
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=("✓ " if m == mode else "") + label,
                        callback_data=f"report:{view}:{period}:{m}:{arg}:0",
                    )
                    for m, label in MODES.items()
                ]
            )
        if view != "home":
            add("← Сводка", "home")
        add("Обновить", view, arg, page)
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
        text, keyboard = await render("home", "all", "all", "all", 0)
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
        try:
            raw = callback.data or ""
            if raw.startswith("stats:"):
                view, period, mode, arg, page = "home", raw.split(":")[1], "all", "all", 0
            else:
                _, view, period, mode, arg, page_raw = raw.split(":")
                page = int(page_raw)
            if period not in PERIODS or mode not in MODES or not 0 <= page <= 100000:
                raise ValueError
            await callback.answer()
            text, keyboard = await render(view, period, mode, arg, page)
        except (ValueError, KeyError):
            await callback.answer("Этот отчёт недоступен. Открой /stats.", show_alert=True)
            return
        await store.audit_admin(callback.from_user.id, "stats_" + view, arg[:16])
        if isinstance(callback.message, Message):
            if callback.message.text != text or callback.message.reply_markup != keyboard:
                try:
                    await callback.message.edit_text(text, reply_markup=keyboard)
                except TelegramBadRequest as exc:
                    if "message is not modified" not in exc.message:
                        raise
