from __future__ import annotations

import asyncio
import calendar
import html
import re
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from money_profile_bot.bot.calculation_gate import CalculationAdmission, CalculationGate
from money_profile_bot.bot.states import DeleteForm, PaymentForm, ProfileForm
from money_profile_bot.config import PaymentMode, Settings
from money_profile_bot.domain import BirthData, City, TimePrecision
from money_profile_bot.models import OrderStatus
from money_profile_bot.services.astro import calculate_chart
from money_profile_bot.services.avatar import (
    INTRO_CAPTION,
    AvatarAssets,
    avatar_free_caption,
    sales_telegram_url,
)
from money_profile_bot.services.delivery import DeliveryWorker
from money_profile_bot.services.geonames import CityCatalog
from money_profile_bot.services.robokassa import RobokassaError
from money_profile_bot.services.rules import generate_profile, validate_generated_profile
from money_profile_bot.services.store import OrderLink, ReminderButtons, Store

START_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,63}$")
CONSENT_BUTTON_TEXT = "✅ Согласен(а), продолжить"
CONSENT_REMINDER_PROMPT = (
    "Нажми «Согласен(а), продолжить», чтобы подтвердить согласие и перейти к анкете."
)
BIRTH_DATE_PROMPT = "📅 Когда ты родилась?\nВыбери дату кнопками. Сначала выбери десятилетие."
TIME_PRECISION_PROMPT = "Насколько точно известно время рождения?"
CITY_PROMPT = (
    "Введи только город рождения, например: Москва. Страну писать не нужно — "
    "если найдётся несколько городов, я покажу варианты с регионом и страной."
)

MONTH_SHORT_NAMES = (
    "",
    "янв",
    "фев",
    "мар",
    "апр",
    "май",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)
MONTH_GENITIVE_NAMES = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
CANCEL_BUTTON = ("✖ Отменить", "form:cancel")


def _keyboard(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data)] for text, data in rows
        ]
    )


def _grid_keyboard(
    buttons: list[tuple[str, str]],
    *,
    columns: int,
    footer: tuple[tuple[str, str], ...] = (),
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=text, callback_data=data)
            for text, data in buttons[index : index + columns]
        ]
        for index in range(0, len(buttons), columns)
    ]
    rows.extend([InlineKeyboardButton(text=text, callback_data=data)] for text, data in footer)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _birth_year_bounds(*, today: date | None = None) -> tuple[int, int]:
    reference = today or date.today()
    return reference.year - 120, reference.year


def _birth_date_picker(
    data: dict[str, Any], *, today: date | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    reference = today or date.today()
    first_year, last_year = _birth_year_bounds(today=reference)
    step = data.get("birth_date_step", "decade")

    if step == "year" and isinstance(data.get("birth_decade"), int):
        decade = data["birth_decade"]
        years = range(max(decade, first_year), min(decade + 9, last_year) + 1)
        buttons = [(str(year), f"birth_date:year:{year}") for year in years]
        return (
            "📅 Выбери год рождения:",
            _grid_keyboard(
                buttons,
                columns=5,
                footer=(("‹ К десятилетиям", "birth_date:back:decades"), CANCEL_BUTTON),
            ),
        )

    if step == "month" and isinstance(data.get("birth_year"), int):
        year = data["birth_year"]
        last_month = reference.month if year == reference.year else 12
        buttons = [
            (MONTH_SHORT_NAMES[month], f"birth_date:month:{year}:{month}")
            for month in range(1, last_month + 1)
        ]
        return (
            f"📅 Год: {year} — выбери месяц:",
            _grid_keyboard(
                buttons,
                columns=4,
                footer=(("‹ Год", f"birth_date:back:years:{year // 10 * 10}"), CANCEL_BUTTON),
            ),
        )

    if (
        step == "day"
        and isinstance(data.get("birth_year"), int)
        and isinstance(data.get("birth_month"), int)
    ):
        year = data["birth_year"]
        month = data["birth_month"]
        last_day = calendar.monthrange(year, month)[1]
        if year == reference.year and month == reference.month:
            last_day = min(last_day, reference.day)
        buttons = [
            (str(day), f"birth_date:day:{year}:{month}:{day}") for day in range(1, last_day + 1)
        ]
        return (
            "📅 Выбери день:",
            _grid_keyboard(
                buttons,
                columns=7,
                footer=(("‹ Месяц", f"birth_date:back:months:{year}"), CANCEL_BUTTON),
            ),
        )

    first_decade = first_year // 10 * 10
    last_decade = last_year // 10 * 10
    buttons = [
        (f"{decade}-е", f"birth_date:decade:{decade}")
        for decade in range(first_decade, last_decade + 1, 10)
    ]
    return (
        BIRTH_DATE_PROMPT,
        _grid_keyboard(buttons, columns=3, footer=(CANCEL_BUTTON,)),
    )


def _time_precision_keyboard() -> InlineKeyboardMarkup:
    return _grid_keyboard(
        [
            ("Знаю точно", "precision:exact"),
            ("Знаю примерно", "precision:approximate"),
            ("Не знаю", "precision:unknown"),
        ],
        columns=1,
        footer=(CANCEL_BUTTON,),
    )


def _birth_time_picker(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    step = data.get("birth_time_step", "hour")
    precision = data.get("time_precision")
    warning = (
        "\nВ домах карты возможна неточность."
        if precision == TimePrecision.APPROXIMATE.value
        else ""
    )

    if step == "minute_range" and isinstance(data.get("birth_hour"), int):
        hour = data["birth_hour"]
        buttons = [
            (f"{hour:02d}:{start:02d}–{start + 9:02d}", f"birth_time:range:{hour}:{start}")
            for start in range(0, 60, 10)
        ]
        return (
            f"🕐 Час: {hour:02d} — выбери диапазон минут:",
            _grid_keyboard(
                buttons,
                columns=2,
                footer=(("‹ Час", "birth_time:back:hours"), CANCEL_BUTTON),
            ),
        )

    if (
        step == "minute"
        and isinstance(data.get("birth_hour"), int)
        and isinstance(data.get("birth_minute_start"), int)
    ):
        hour = data["birth_hour"]
        minute_start = data["birth_minute_start"]
        buttons = [
            (f"{hour:02d}:{minute:02d}", f"birth_time:minute:{hour}:{minute}")
            for minute in range(minute_start, minute_start + 10)
        ]
        return (
            f"🕐 {hour:02d}:{minute_start:02d}–{minute_start + 9:02d} — выбери точную минуту:",
            _grid_keyboard(
                buttons,
                columns=5,
                footer=(
                    ("‹ К диапазонам минут", f"birth_time:back:ranges:{hour}"),
                    CANCEL_BUTTON,
                ),
            ),
        )

    buttons = [(f"{hour:02d}", f"birth_time:hour:{hour}") for hour in range(24)]
    return (
        "🕐 Во сколько ты родилась? Выбери час.\n"
        "Точное время может быть на бирке из роддома или его знает мама."
        f"{warning}",
        _grid_keyboard(
            buttons,
            columns=6,
            footer=(("‹ К точности времени", "birth_time:back:precision"), CANCEL_BUTTON),
        ),
    )


def _reminder_buttons(
    reply_markup: InlineKeyboardMarkup | None,
) -> tuple[tuple[tuple[str, str], ...], ...]:
    if reply_markup is None:
        return ()
    rows = tuple(
        tuple(
            (button.text, button.callback_data)
            for button in row
            if button.callback_data is not None
        )
        for row in reply_markup.inline_keyboard
    )
    return tuple(row for row in rows if row)


async def _schedule_form_reminder(
    store: Store,
    telegram_id: int,
    *,
    state: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    await store.schedule_form_reminder(
        telegram_id,
        state=state,
        text=text,
        buttons=_reminder_buttons(reply_markup),
    )


def _city_label(city: City) -> str:
    parts = [city.name]
    if city.region:
        parts.append(city.region)
    parts.append(city.country_name)
    return ", ".join(parts)


def _confirmation_text(data: dict[str, Any], selected: dict[str, Any]) -> str:
    precision_names = {
        "exact": "точное",
        "approximate": "примерное",
        "unknown": "неизвестно",
    }
    time_text = (
        time.fromisoformat(data["birth_time"]).strftime("%H:%M")
        if data.get("birth_time")
        else "не указано"
    )
    return (
        "Проверь данные:\n\n"
        f"Дата: {datetime.fromisoformat(data['birth_date']).strftime('%d.%m.%Y')}\n"
        f"Время: {time_text} ({precision_names[data['time_precision']]})\n"
        f"Место: {html.escape(_city_label(City(**selected)))}"
    )


def form_reminder_payload(state: str, data: dict[str, Any]) -> tuple[str, ReminderButtons] | None:
    if state == ProfileForm.consent.state:
        return (
            CONSENT_REMINDER_PROMPT,
            (((CONSENT_BUTTON_TEXT, "consent:yes"),),),
        )
    if state in {ProfileForm.name.state, ProfileForm.birth_date.state}:
        text, keyboard = _birth_date_picker(data)
        return text, _reminder_buttons(keyboard)
    if state == ProfileForm.time_precision.state:
        keyboard = _time_precision_keyboard()
        return TIME_PRECISION_PROMPT, _reminder_buttons(keyboard)
    if state == ProfileForm.birth_time.state:
        text, keyboard = _birth_time_picker(data)
        return text, _reminder_buttons(keyboard)
    if state == ProfileForm.city.state:
        return CITY_PROMPT, ()
    if state == ProfileForm.city_choice.state:
        options = data.get("city_options")
        if not isinstance(options, list):
            return CITY_PROMPT, ()
        rows = tuple(
            ((_city_label(City(**option)), f"city:{index}"),)
            for index, option in enumerate(options)
            if isinstance(option, dict)
        )
        return ("Выбери подходящий вариант:", rows) if rows else (CITY_PROMPT, ())
    if state == ProfileForm.confirm.state:
        selected = data.get("city")
        if not isinstance(selected, dict):
            return None
        return (
            _confirmation_text(data, selected),
            (
                (("Всё верно", "form:confirm"),),
                (("Заполнить заново", "form:restart"),),
            ),
        )
    return None


def _birth_date_is_plausible(value: date, *, today: date | None = None) -> bool:
    reference = today or date.today()
    age = (
        reference.year - value.year - ((reference.month, reference.day) < (value.month, value.day))
    )
    return value <= reference and age <= 120


def _intro_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔒 Политика обработки данных",
                    url=f"{settings.public_base_url}/privacy",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📜 Публичная оферта",
                    url=f"{settings.public_base_url}/terms",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✍️ Согласие на обработку",
                    url=f"{settings.public_base_url}/consent",
                )
            ],
            [InlineKeyboardButton(text=CONSENT_BUTTON_TEXT, callback_data="consent:yes")],
        ]
    )


def _sales_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Хочу денежный разбор",
                    url=sales_telegram_url(settings.support_username),
                )
            ]
        ]
    )


def _receipt_email_is_valid(value: str) -> bool:
    return len(value) <= 254 and EMAIL_RE.fullmatch(value) is not None


def _payment_email_prompt(settings: Settings) -> tuple[str, InlineKeyboardMarkup]:
    mode = (
        "Сейчас используется тестовая Robokassa: деньги не спишутся. "
        if settings.robokassa_test_mode
        else f"После этого я создам счёт на {settings.product_price_rub:.0f} ₽ в Robokassa. "
    )
    text = (
        "Пришли email одним сообщением — он нужен для электронного чека. "
        f"{mode}Перед продолжением можно ещё раз открыть условия и политику."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📜 Условия", url=f"{settings.public_base_url}/terms"),
                InlineKeyboardButton(
                    text="🔒 Конфиденциальность",
                    url=f"{settings.public_base_url}/privacy",
                ),
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="payment:cancel")],
        ]
    )
    return text, keyboard


def _payment_link_message(settings: Settings, link: OrderLink) -> tuple[str, InlineKeyboardMarkup]:
    if settings.robokassa_test_mode:
        title = "<b>Тестовый счёт Robokassa готов</b>"
        notice = "Деньги не списываются. Пройди платёжный сценарий до конца."
        button_text = "Перейти к тестовой оплате"
    else:
        title = "<b>Счёт Robokassa готов</b>"
        notice = "Результат будет отправлен после подтверждения платежа."
        button_text = f"Оплатить {settings.product_price_rub:.0f} ₽"
    text = (
        f"{title}\n\nСумма: {settings.product_price_rub:.2f} ₽\n"
        f"Код заказа: <code>{html.escape(link.code)}</code>\n\n{notice}"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button_text, url=link.url)],
            [
                InlineKeyboardButton(text="📜 Условия", url=f"{settings.public_base_url}/terms"),
                InlineKeyboardButton(
                    text="🔒 Конфиденциальность",
                    url=f"{settings.public_base_url}/privacy",
                ),
            ],
        ]
    )
    return text, keyboard


async def _begin(
    message: Message,
    state: FSMContext,
    settings: Settings,
    avatars: AvatarAssets,
) -> None:
    await state.clear()
    await state.set_state(ProfileForm.consent)
    await message.answer_photo(
        FSInputFile(avatars.first_message_image()),
        caption=INTRO_CAPTION,
        reply_markup=_intro_keyboard(settings),
    )


async def _delete_start_command(message: Message) -> None:
    with suppress(TelegramAPIError):
        await message.delete()


def _strength_trigger_keyboard(profile_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Узнать силу",
                    callback_data=f"strength:{profile_id}",
                )
            ]
        ]
    )


async def _send_free_avatar(
    message: Message,
    *,
    profile_id: str,
    money_type: str,
    avatars: AvatarAssets,
) -> None:
    await message.answer_photo(
        FSInputFile(avatars.free_image(money_type)),
        caption=avatar_free_caption(money_type),
        reply_markup=_strength_trigger_keyboard(profile_id),
    )


async def _accept_consent(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    store: Store,
) -> None:
    if not callback.from_user:
        return
    await store.save_consent(callback.from_user.id, settings.legal_docs_version)
    await state.update_data(
        birth_date_step="decade",
        birth_decade=None,
        birth_year=None,
        birth_month=None,
    )
    await state.set_state(ProfileForm.birth_date)
    await callback.answer()
    if callback.message:
        prompt, keyboard = _birth_date_picker({})
        await _schedule_form_reminder(
            store,
            callback.from_user.id,
            state="birth_date",
            text=prompt,
            reply_markup=keyboard,
        )
        await callback.message.answer(prompt, reply_markup=keyboard)


async def _request_data_deletion(message: Message, state: FSMContext) -> None:
    await state.set_state(DeleteForm.confirm)
    await message.answer(
        "Удалить данные рождения и результат расчёта? Обезличенные события и "
        "минимальный платёжный журнал сохранятся на срок из политики.",
        reply_markup=_keyboard(("Удалить мои данные", "delete:yes"), ("Отмена", "delete:no")),
    )


def build_router(
    settings: Settings,
    store: Store,
    cities: CityCatalog,
    avatars: AvatarAssets,
    delivery: DeliveryWorker,
) -> Router:
    router = Router(name="money-profile")
    calculation_gate = CalculationGate()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext, command: CommandObject) -> None:
        if not message.from_user:
            return
        await _delete_start_command(message)
        source = command.args if command.args and START_RE.fullmatch(command.args) else "direct"
        await store.ensure_user(message.from_user.id, source)
        await store.cancel_form_reminder(message.from_user.id)
        newly_claimed_admin = False
        if settings.bootstrap_admin_on_first_start:
            claim = await store.claim_admin_if_unset(message.from_user.id)
            newly_claimed_admin = claim.newly_claimed
        await store.record_event(message.from_user.id, "bot_started")
        await _schedule_form_reminder(
            store,
            message.from_user.id,
            state="consent",
            text=CONSENT_REMINDER_PROMPT,
            reply_markup=_intro_keyboard(settings),
        )
        await _begin(message, state, settings, avatars)
        if newly_claimed_admin:
            await message.answer(
                "Вы назначены администратором тестового бота. Команда статистики: /stats."
            )

    # Команда удаления должна обрабатываться раньше ответов на шаги анкеты,
    # иначе Telegram-команда может быть ошибочно прочитана как дата, время или город.
    @router.message(Command("delete_my_data"))
    async def delete_my_data(message: Message, state: FSMContext) -> None:
        if message.from_user:
            await store.cancel_form_reminder(message.from_user.id)
        await _request_data_deletion(message, state)

    @router.callback_query(ProfileForm.consent, F.data == "consent:yes")
    async def consent(callback: CallbackQuery, state: FSMContext) -> None:
        await _accept_consent(callback, state, settings, store)

    # Пользователи, остановившиеся на удалённом шаге до обновления, продолжают
    # анкету без сохранения отправленного имени.
    @router.message(ProfileForm.name)
    async def legacy_name(message: Message, state: FSMContext) -> None:
        await state.update_data(
            birth_date_step="decade",
            birth_decade=None,
            birth_year=None,
            birth_month=None,
        )
        await state.set_state(ProfileForm.birth_date)
        prompt, keyboard = _birth_date_picker({})
        if message.from_user:
            await _schedule_form_reminder(
                store,
                message.from_user.id,
                state="birth_date",
                text=prompt,
                reply_markup=keyboard,
            )
        await message.answer(prompt, reply_markup=keyboard)

    @router.callback_query(ProfileForm.birth_date, F.data == "form:cancel")
    @router.callback_query(ProfileForm.time_precision, F.data == "form:cancel")
    @router.callback_query(ProfileForm.birth_time, F.data == "form:cancel")
    async def cancel_form(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await store.cancel_form_reminder(callback.from_user.id)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "Анкета отменена. Чтобы начать заново, отправь /start."
            )

    @router.message(ProfileForm.birth_date)
    async def birth_date(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        prompt, keyboard = _birth_date_picker(data)
        if message.from_user:
            await _schedule_form_reminder(
                store,
                message.from_user.id,
                state="birth_date",
                text=prompt,
                reply_markup=keyboard,
            )
        await message.answer(
            "Дату не нужно вводить вручную — выбери её кнопками ниже.\n\n" + prompt,
            reply_markup=keyboard,
        )

    @router.callback_query(ProfileForm.birth_date, F.data.startswith("birth_date:"))
    async def birth_date_choice(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":")
        first_year, last_year = _birth_year_bounds()
        try:
            action = parts[1]
            if action == "decade":
                decade = int(parts[2])
                if decade % 10 or not (first_year // 10 * 10 <= decade <= last_year // 10 * 10):
                    raise ValueError
                await state.update_data(
                    birth_date_step="year",
                    birth_decade=decade,
                    birth_year=None,
                    birth_month=None,
                )
            elif action == "year":
                year = int(parts[2])
                if not first_year <= year <= last_year:
                    raise ValueError
                await state.update_data(
                    birth_date_step="month",
                    birth_decade=year // 10 * 10,
                    birth_year=year,
                    birth_month=None,
                )
            elif action == "month":
                year, month = map(int, parts[2:4])
                if not first_year <= year <= last_year or not 1 <= month <= 12:
                    raise ValueError
                if year == date.today().year and month > date.today().month:
                    raise ValueError
                await state.update_data(
                    birth_date_step="day",
                    birth_decade=year // 10 * 10,
                    birth_year=year,
                    birth_month=month,
                )
            elif action == "day":
                year, month, day = map(int, parts[2:5])
                value = date(year, month, day)
                if not _birth_date_is_plausible(value):
                    raise ValueError
                await state.update_data(
                    birth_date=value.isoformat(),
                    birth_date_step=None,
                    birth_decade=None,
                    birth_year=None,
                    birth_month=None,
                )
                await state.set_state(ProfileForm.time_precision)
                keyboard = _time_precision_keyboard()
                await _schedule_form_reminder(
                    store,
                    callback.from_user.id,
                    state="time_precision",
                    text=TIME_PRECISION_PROMPT,
                    reply_markup=keyboard,
                )
                await callback.answer()
                if isinstance(callback.message, Message):
                    selected_date = f"{day} {MONTH_GENITIVE_NAMES[month]} {year}"
                    await callback.message.edit_text(
                        f"📅 Дата рождения: {selected_date}\n\n{TIME_PRECISION_PROMPT}",
                        reply_markup=keyboard,
                    )
                return
            elif action == "back" and parts[2] == "decades":
                await state.update_data(
                    birth_date_step="decade",
                    birth_decade=None,
                    birth_year=None,
                    birth_month=None,
                )
            elif action == "back" and parts[2] == "years":
                decade = int(parts[3])
                await state.update_data(
                    birth_date_step="year",
                    birth_decade=decade,
                    birth_year=None,
                    birth_month=None,
                )
            elif action == "back" and parts[2] == "months":
                year = int(parts[3])
                await state.update_data(
                    birth_date_step="month",
                    birth_decade=year // 10 * 10,
                    birth_year=year,
                    birth_month=None,
                )
            else:
                raise ValueError
        except (IndexError, TypeError, ValueError):
            await callback.answer("Вариант устарел. Выбери дату ещё раз.", show_alert=True)
            return

        data = await state.get_data()
        prompt, keyboard = _birth_date_picker(data)
        await _schedule_form_reminder(
            store,
            callback.from_user.id,
            state="birth_date",
            text=prompt,
            reply_markup=keyboard,
        )
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(prompt, reply_markup=keyboard)

    @router.callback_query(ProfileForm.time_precision, F.data.startswith("precision:"))
    async def precision(callback: CallbackQuery, state: FSMContext) -> None:
        value = (callback.data or "").split(":", 1)[1]
        if value not in {item.value for item in TimePrecision}:
            keyboard = _time_precision_keyboard()
            await _schedule_form_reminder(
                store,
                callback.from_user.id,
                state="time_precision",
                text=TIME_PRECISION_PROMPT,
                reply_markup=keyboard,
            )
            await callback.answer("Неизвестный вариант", show_alert=True)
            return
        await state.update_data(time_precision=value)
        await callback.answer()
        if value == TimePrecision.UNKNOWN.value:
            await state.update_data(birth_time=None)
            await state.set_state(ProfileForm.city)
            await _schedule_form_reminder(
                store,
                callback.from_user.id,
                state="city",
                text=CITY_PROMPT,
            )
            if isinstance(callback.message, Message):
                await callback.message.edit_text("🕐 Время рождения: не указано")
                await callback.message.answer(CITY_PROMPT)
        else:
            await state.update_data(
                birth_time_step="hour",
                birth_hour=None,
                birth_minute_start=None,
            )
            await state.set_state(ProfileForm.birth_time)
            data = await state.get_data()
            prompt, keyboard = _birth_time_picker(data)
            await _schedule_form_reminder(
                store,
                callback.from_user.id,
                state="birth_time",
                text=prompt,
                reply_markup=keyboard,
            )
            if isinstance(callback.message, Message):
                await callback.message.edit_text(prompt, reply_markup=keyboard)

    @router.message(ProfileForm.birth_time)
    async def birth_time(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        prompt, keyboard = _birth_time_picker(data)
        if message.from_user:
            await _schedule_form_reminder(
                store,
                message.from_user.id,
                state="birth_time",
                text=prompt,
                reply_markup=keyboard,
            )
        await message.answer(
            "Время не нужно вводить вручную — выбери его кнопками ниже.\n\n" + prompt,
            reply_markup=keyboard,
        )

    @router.callback_query(ProfileForm.birth_time, F.data.startswith("birth_time:"))
    async def birth_time_choice(callback: CallbackQuery, state: FSMContext) -> None:
        parts = (callback.data or "").split(":")
        try:
            action = parts[1]
            if action == "hour":
                hour = int(parts[2])
                if not 0 <= hour <= 23:
                    raise ValueError
                await state.update_data(
                    birth_time_step="minute_range",
                    birth_hour=hour,
                    birth_minute_start=None,
                )
            elif action == "range":
                hour, minute_start = map(int, parts[2:4])
                if not 0 <= hour <= 23 or minute_start not in range(0, 60, 10):
                    raise ValueError
                await state.update_data(
                    birth_time_step="minute",
                    birth_hour=hour,
                    birth_minute_start=minute_start,
                )
            elif action == "minute":
                hour, minute = map(int, parts[2:4])
                value = time(hour, minute)
                await state.update_data(
                    birth_time=value.isoformat(),
                    birth_time_step=None,
                    birth_hour=None,
                    birth_minute_start=None,
                )
                await state.set_state(ProfileForm.city)
                await _schedule_form_reminder(
                    store,
                    callback.from_user.id,
                    state="city",
                    text=CITY_PROMPT,
                )
                await callback.answer()
                if isinstance(callback.message, Message):
                    await callback.message.edit_text(f"🕐 Время рождения: {hour:02d}:{minute:02d}")
                    await callback.message.answer(CITY_PROMPT)
                return
            elif action == "back" and parts[2] == "precision":
                await state.set_state(ProfileForm.time_precision)
                keyboard = _time_precision_keyboard()
                await _schedule_form_reminder(
                    store,
                    callback.from_user.id,
                    state="time_precision",
                    text=TIME_PRECISION_PROMPT,
                    reply_markup=keyboard,
                )
                await callback.answer()
                if isinstance(callback.message, Message):
                    await callback.message.edit_text(
                        TIME_PRECISION_PROMPT,
                        reply_markup=keyboard,
                    )
                return
            elif action == "back" and parts[2] == "hours":
                await state.update_data(
                    birth_time_step="hour",
                    birth_hour=None,
                    birth_minute_start=None,
                )
            elif action == "back" and parts[2] == "ranges":
                hour = int(parts[3])
                if not 0 <= hour <= 23:
                    raise ValueError
                await state.update_data(
                    birth_time_step="minute_range",
                    birth_hour=hour,
                    birth_minute_start=None,
                )
            else:
                raise ValueError
        except (IndexError, TypeError, ValueError):
            await callback.answer("Вариант устарел. Выбери время ещё раз.", show_alert=True)
            return

        data = await state.get_data()
        prompt, keyboard = _birth_time_picker(data)
        await _schedule_form_reminder(
            store,
            callback.from_user.id,
            state="birth_time",
            text=prompt,
            reply_markup=keyboard,
        )
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(prompt, reply_markup=keyboard)

    @router.message(ProfileForm.city)
    async def city(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if len(query) > 120:
            if message.from_user:
                await _schedule_form_reminder(
                    store,
                    message.from_user.id,
                    state="city",
                    text=CITY_PROMPT,
                )
            await message.answer("Название города слишком длинное. Введи только город.")
            return
        variants = await cities.search(query)
        if not variants:
            if message.from_user:
                await _schedule_form_reminder(
                    store,
                    message.from_user.id,
                    state="city",
                    text=CITY_PROMPT,
                )
            await message.answer(
                "Не получилось найти город. Введи только его название без страны, например: "
                f"Москва. Можно указать ближайший крупный город или написать @{settings.support_username}."
            )
            return
        await state.update_data(city_options=[asdict(item) for item in variants])
        await state.set_state(ProfileForm.city_choice)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=_city_label(item), callback_data=f"city:{index}")]
                for index, item in enumerate(variants)
            ]
        )
        if message.from_user:
            await _schedule_form_reminder(
                store,
                message.from_user.id,
                state="city_choice",
                text="Выбери подходящий вариант:",
                reply_markup=keyboard,
            )
        await message.answer("Выбери подходящий вариант:", reply_markup=keyboard)

    @router.callback_query(ProfileForm.city_choice, F.data.startswith("city:"))
    async def city_choice(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        try:
            index = int((callback.data or "").split(":", 1)[1])
            selected = data["city_options"][index]
        except (ValueError, IndexError, KeyError):
            await callback.answer("Вариант устарел. Введи город ещё раз.", show_alert=True)
            await state.set_state(ProfileForm.city)
            await _schedule_form_reminder(
                store,
                callback.from_user.id,
                state="city",
                text=CITY_PROMPT,
            )
            if callback.message:
                await callback.message.answer(CITY_PROMPT)
            return
        await state.update_data(city=selected)
        await state.set_state(ProfileForm.confirm)
        await callback.answer()
        data = await state.get_data()
        summary = _confirmation_text(data, selected)
        if callback.message:
            keyboard = _keyboard(
                ("Всё верно", "form:confirm"), ("Заполнить заново", "form:restart")
            )
            await _schedule_form_reminder(
                store,
                callback.from_user.id,
                state="confirm",
                text=summary,
                reply_markup=keyboard,
            )
            await callback.message.answer(
                summary,
                reply_markup=keyboard,
            )

    @router.callback_query(ProfileForm.confirm, F.data == "form:restart")
    async def form_restart(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await store.cancel_form_reminder(callback.from_user.id)
        if isinstance(callback.message, Message):
            await _schedule_form_reminder(
                store,
                callback.from_user.id,
                state="consent",
                text=CONSENT_REMINDER_PROMPT,
                reply_markup=_intro_keyboard(settings),
            )
            await _begin(callback.message, state, settings, avatars)

    @router.callback_query(ProfileForm.confirm, F.data == "form:confirm")
    async def form_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(callback.message, Message):
            return
        telegram_id = callback.from_user.id
        admission = await calculation_gate.acquire(telegram_id)
        if admission is CalculationAdmission.DUPLICATE:
            await callback.answer("Профиль уже рассчитывается…")
            return
        if admission is CalculationAdmission.BUSY:
            await callback.answer(
                "Сейчас много расчётов. Попробуй ещё раз через минуту.",
                show_alert=True,
            )
            return
        try:
            await callback.answer("Рассчитываю профиль…")
            raw = await state.get_data()
            birth = BirthData(
                name="",
                birth_date=date.fromisoformat(raw["birth_date"]),
                time_precision=TimePrecision(raw["time_precision"]),
                birth_time=(
                    time.fromisoformat(raw["birth_time"]) if raw.get("birth_time") else None
                ),
                city=City(**raw["city"]),
            )
            facts = await asyncio.to_thread(calculate_chart, birth)
            result = generate_profile(facts)
            issues = validate_generated_profile(result)
            if issues:
                raise RuntimeError("generated content failed validation: " + "; ".join(issues))
            profile_id = await store.save_calculation(callback.from_user.id, birth, facts, result)
        except Exception:
            await callback.message.answer(
                f"Расчёт временно не завершён. Попробуй позже или напиши @{settings.support_username}."
            )
            return
        finally:
            await calculation_gate.release(telegram_id)
        await state.clear()
        await store.cancel_form_reminder(callback.from_user.id)
        await store.record_event(callback.from_user.id, "profile_calculated")
        await store.schedule_strength_offer(profile_id)
        await _send_free_avatar(
            callback.message,
            profile_id=profile_id,
            money_type=result.money_type,
            avatars=avatars,
        )
        delivery.notify()

    @router.callback_query(F.data.startswith("strength:"))
    async def strength_offer(callback: CallbackQuery) -> None:
        profile_id = (callback.data or "").split(":", 1)[1]
        delivered = await delivery.deliver_strength_offer(
            profile_id,
            telegram_id=callback.from_user.id,
            force=True,
        )
        if delivered:
            await callback.answer("Разбор силы уже ниже")
        else:
            await callback.answer(
                "Эта кнопка уже неактивна. Для нового аватара отправь /start.",
                show_alert=True,
            )

    @router.callback_query(F.data.startswith("buy:"))
    async def buy(callback: CallbackQuery, state: FSMContext) -> None:
        profile_id = (callback.data or "").split(":", 1)[1]
        access = await store.profile_access(callback.from_user.id, profile_id=profile_id)
        if not access or access.profile_id != profile_id:
            await callback.answer("Профиль не найден. Начни с /start.", show_alert=True)
            return
        if access.order_id and access.order_status in (OrderStatus.PAID, OrderStatus.DELIVERED):
            await callback.answer("Результат уже доступен")
            if access.order_status == OrderStatus.DELIVERED:
                await delivery.send_copy(access.order_id)
            else:
                delivery.notify()
            return
        if settings.payment_mode is PaymentMode.ROBOKASSA:
            if not settings.robokassa_invoice_creation_enabled:
                await state.clear()
                await callback.answer(
                    "Оплата временно приостановлена. Новые счета не создаются.",
                    show_alert=True,
                )
                return
            if (
                access.order_status == OrderStatus.INVOICE_CREATED
                and access.payment_url
                and access.order_id
                and access.order_code
            ):
                await callback.answer("Ссылка на оплату уже создана")
                if callback.message:
                    text, keyboard = _payment_link_message(
                        settings,
                        OrderLink(
                            order_id=access.order_id,
                            code=access.order_code,
                            url=access.payment_url,
                            reused=True,
                        ),
                    )
                    await callback.message.answer(text, reply_markup=keyboard)
                return
            await state.update_data(payment_profile_id=profile_id)
            await state.set_state(PaymentForm.email)
            await callback.answer()
            if callback.message:
                text, keyboard = _payment_email_prompt(settings)
                await callback.message.answer(text, reply_markup=keyboard)
            return
        await store.create_fake_paid_order(
            telegram_id=callback.from_user.id,
            profile_id=profile_id,
        )
        await state.clear()
        await store.cancel_form_reminder(callback.from_user.id)
        await callback.answer("Тестовый разбор открыт — деньги не списаны")
        delivery.notify()

    @router.callback_query(PaymentForm.email, F.data == "payment:cancel")
    async def cancel_payment(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Оплата отменена")
        if callback.message:
            await callback.message.answer(
                "Счёт не создан. Вернуться к покупке можно через /profile."
            )

    @router.message(PaymentForm.email)
    async def payment_email(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return
        if settings.payment_mode is not PaymentMode.ROBOKASSA:
            await state.clear()
            await message.answer(
                "Создание счёта сейчас недоступно. Вернись к покупке через /profile."
            )
            return
        if not settings.robokassa_invoice_creation_enabled:
            await state.clear()
            await message.answer(
                f"Оплата временно приостановлена. Новые счета не создаются. "
                f"Попробуй позже или напиши @{settings.support_username}."
            )
            return
        email = (message.text or "").strip()
        if not _receipt_email_is_valid(email):
            await message.answer(
                "Не получилось распознать email. Пришли адрес в формате name@example.ru."
            )
            return
        raw = await state.get_data()
        profile_id = raw.get("payment_profile_id")
        access = (
            await store.profile_access(message.from_user.id, profile_id=profile_id)
            if isinstance(profile_id, str)
            else None
        )
        if not isinstance(profile_id, str) or not access or access.profile_id != profile_id:
            await state.clear()
            await message.answer("Профиль не найден. Начни заново с /start.")
            return
        try:
            link = await store.create_order(
                telegram_id=message.from_user.id,
                profile_id=profile_id,
                email=email,
                amount_minor=settings.product_price_minor,
            )
        except (RobokassaError, RuntimeError):
            await message.answer(
                f"Robokassa пока не создала счёт. Попробуй ещё раз или напиши "
                f"@{settings.support_username}."
            )
            return
        await state.clear()
        text, keyboard = _payment_link_message(settings, link)
        await message.answer(text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith("full:"))
    async def full_reading_offer(callback: CallbackQuery) -> None:
        order_id = (callback.data or "").split(":", 1)[1]
        revealed = await store.reveal_full_reading_offer(callback.from_user.id, order_id)
        if not revealed:
            await callback.answer(
                "Предложение уже отправлено или недоступно.",
                show_alert=True,
            )
            return
        await callback.answer("Полный разбор уже ниже")
        await delivery.deliver(order_id)

    @router.callback_query(F.data == "profile:new")
    async def new_profile(callback: CallbackQuery, state: FSMContext) -> None:
        await store.cancel_form_reminder(callback.from_user.id)
        await _schedule_form_reminder(
            store,
            callback.from_user.id,
            state="consent",
            text=CONSENT_REMINDER_PROMPT,
            reply_markup=_intro_keyboard(settings),
        )
        await callback.answer("Начинаем новый расчёт")
        if isinstance(callback.message, Message):
            await _begin(callback.message, state, settings, avatars)

    @router.message(Command("profile"))
    async def profile(message: Message) -> None:
        if not message.from_user:
            return
        access = await store.profile_access(message.from_user.id)
        if not access:
            await message.answer("Денежный аватар ещё не рассчитан. Начни с /start.")
            return
        if access.order_id and access.order_status in (OrderStatus.PAID, OrderStatus.DELIVERED):
            if access.order_status == OrderStatus.DELIVERED:
                await delivery.send_copy(access.order_id)
            else:
                delivery.notify()
            return
        _, result = await store.get_profile_result(access.profile_id)
        await store.schedule_strength_offer(access.profile_id)
        await _send_free_avatar(
            message,
            profile_id=access.profile_id,
            money_type=result.money_type,
            avatars=avatars,
        )
        delivery.notify()

    @router.message(Command("support"))
    @router.message(Command("paysupport"))
    async def support(message: Message) -> None:
        await message.answer(
            f"Поддержка: @{settings.support_username}. Не присылай данные карты, "
            "пароли или коды из SMS."
        )

    @router.message(Command("terms"))
    async def terms(message: Message) -> None:
        await message.answer(f"Условия использования: {settings.public_base_url}/terms")

    @router.message(Command("privacy"))
    async def privacy(message: Message) -> None:
        await message.answer(f"Политика конфиденциальности: {settings.public_base_url}/privacy")

    @router.message(Command("consent"))
    async def consent_text(message: Message) -> None:
        await message.answer(f"Согласие на обработку данных: {settings.public_base_url}/consent")

    @router.callback_query(DeleteForm.confirm, F.data == "delete:no")
    async def delete_no(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Удаление отменено")

    @router.callback_query(DeleteForm.confirm, F.data == "delete:yes")
    async def delete_yes(callback: CallbackQuery, state: FSMContext) -> None:
        paths = await store.delete_personal_data(callback.from_user.id)
        await state.clear()
        for raw_path in paths or []:
            await asyncio.to_thread(Path(raw_path).unlink, missing_ok=True)
        await callback.answer("Данные удалены", show_alert=True)
        if callback.message:
            await callback.message.answer(
                "Персональные данные удалены. Новый расчёт можно начать с /start."
            )

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not message.from_user or not await store.is_admin(
            message.from_user.id, settings.admin_ids
        ):
            return
        await message.answer(
            "Выберите период:",
            reply_markup=_keyboard(
                ("Сегодня", "stats:today"),
                ("7 дней", "stats:7d"),
                ("30 дней", "stats:30d"),
                ("Всё время", "stats:all"),
            ),
        )

    @router.callback_query(F.data.startswith("stats:"))
    async def stats_period(callback: CallbackQuery) -> None:
        if not await store.is_admin(callback.from_user.id, settings.admin_ids):
            await callback.answer()
            return
        period = (callback.data or "").split(":", 1)[1]
        now = datetime.now(UTC)
        since = {
            "today": datetime(now.year, now.month, now.day, tzinfo=UTC),
            "7d": now - timedelta(days=7),
            "30d": now - timedelta(days=30),
            "all": None,
        }.get(period)
        if period not in {"today", "7d", "30d", "all"}:
            await callback.answer("Неизвестный период", show_alert=True)
            return
        data = await store.stats(since)
        first = (
            ", ".join(f"{source or 'direct'}: {count}" for source, count in data["first_sources"])
            or "—"
        )
        last = (
            ", ".join(f"{source or 'direct'}: {count}" for source, count in data["last_sources"])
            or "—"
        )
        text = (
            f"Пользователи: {data['users']}\nАнкеты: {data['profiles']}\n"
            f"Офферы: {data['offers']}\nОплаты: {data['payments']}\n"
            f"Конверсия оффер → оплата: {data['conversion']:.1f}%\n"
            f"Чистая выручка после возвратов: {data['revenue_rub']} ₽\n\n"
            f"First-touch: {first}\nLast-touch: {last}"
        )
        await callback.answer()
        if callback.message:
            await callback.message.answer(text)

    @router.message(Command("refund"))
    async def refund(message: Message, command: CommandObject) -> None:
        if not message.from_user or not await store.is_admin(
            message.from_user.id, settings.admin_ids
        ):
            return
        parts = (command.args or "").split()
        if len(parts) not in (1, 2):
            await message.answer("Формат: /refund MP-XXXXXXXX [код подтверждения]")
            return
        order_code = parts[0].upper()
        try:
            if len(parts) == 1:
                token = await store.prepare_refund(order_code)
                await store.audit_admin(message.from_user.id, "refund_prepared", order_code)
                await message.answer(
                    f"Подтвердите возврат в течение 10 минут:\n/refund {order_code} {token}"
                )
            else:
                request_id = await store.execute_refund(order_code, parts[1])
                await store.audit_admin(message.from_user.id, "refund_requested", order_code)
                await message.answer(
                    f"Robokassa приняла запрос на возврат. Request ID: {request_id}. "
                    "Статус будет проверен автоматически."
                )
        except (LookupError, ValueError, RobokassaError) as exc:
            await message.answer(f"Возврат не выполнен: {html.escape(str(exc))}")

    return router


async def set_commands(bot: Any) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Рассчитать новый аватар"),
            BotCommand(command="profile", description="Показать сохранённый аватар"),
            BotCommand(command="support", description="Поддержка"),
            BotCommand(command="paysupport", description="Вопросы по оплате"),
            BotCommand(command="terms", description="Условия"),
            BotCommand(command="privacy", description="Конфиденциальность"),
            BotCommand(command="consent", description="Согласие на обработку данных"),
            BotCommand(command="delete_my_data", description="Удалить мои данные"),
        ]
    )
