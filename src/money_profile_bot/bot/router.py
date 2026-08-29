from __future__ import annotations

import asyncio
import html
import re
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from aiogram import F, Router
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

from money_profile_bot.bot.states import DeleteForm, ProfileForm
from money_profile_bot.config import PaymentMode, Settings
from money_profile_bot.domain import BirthData, City, TimePrecision
from money_profile_bot.models import OrderStatus
from money_profile_bot.services.astro import calculate_chart
from money_profile_bot.services.avatar import (
    INTRO_CAPTION,
    AvatarAssets,
    sales_telegram_url,
)
from money_profile_bot.services.delivery import DeliveryWorker
from money_profile_bot.services.geonames import CityCatalog
from money_profile_bot.services.robokassa import RobokassaError
from money_profile_bot.services.rules import generate_profile, validate_generated_profile
from money_profile_bot.services.store import Store

START_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _keyboard(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data)] for text, data in rows
        ]
    )


def _city_label(city: City) -> str:
    parts = [city.name]
    if city.region:
        parts.append(city.region)
    parts.append(city.country_name)
    return ", ".join(parts)


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
                InlineKeyboardButton(text="Условия", url=f"{settings.public_base_url}/terms"),
                InlineKeyboardButton(text="Политика", url=f"{settings.public_base_url}/privacy"),
            ],
            [InlineKeyboardButton(text="Узнать свой аватар", callback_data="consent:yes")],
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
    free_insight: str,
    avatars: AvatarAssets,
) -> None:
    await message.answer_photo(
        FSInputFile(avatars.free_image(money_type)),
        caption=free_insight,
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
    await state.set_state(ProfileForm.birth_date)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Введи дату рождения в формате ДД.ММ.ГГГГ.")


def build_router(
    settings: Settings,
    store: Store,
    cities: CityCatalog,
    avatars: AvatarAssets,
    delivery: DeliveryWorker,
) -> Router:
    router = Router(name="money-profile")

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext, command: CommandObject) -> None:
        if not message.from_user:
            return
        if settings.test_access_ids and message.from_user.id not in settings.test_access_ids:
            await message.answer("Тестовый бот доступен только участникам закрытого теста.")
            return
        source = command.args if command.args and START_RE.fullmatch(command.args) else "direct"
        await store.ensure_user(message.from_user.id, source)
        newly_claimed_admin = False
        if settings.bootstrap_admin_on_first_start:
            claim = await store.claim_admin_if_unset(message.from_user.id)
            newly_claimed_admin = claim.newly_claimed
        await store.record_event(message.from_user.id, "bot_started")
        await _begin(message, state, settings, avatars)
        if newly_claimed_admin:
            await message.answer(
                "Вы назначены администратором тестового бота. Команда статистики: /stats."
            )

    @router.callback_query(ProfileForm.consent, F.data == "consent:yes")
    async def consent(callback: CallbackQuery, state: FSMContext) -> None:
        await _accept_consent(callback, state, settings, store)

    # Пользователи, остановившиеся на удалённом шаге до обновления, продолжают
    # анкету без сохранения отправленного имени.
    @router.message(ProfileForm.name)
    async def legacy_name(message: Message, state: FSMContext) -> None:
        await state.set_state(ProfileForm.birth_date)
        await message.answer("Введи дату рождения в формате ДД.ММ.ГГГГ.")

    @router.message(ProfileForm.birth_date)
    async def birth_date(message: Message, state: FSMContext) -> None:
        try:
            value = datetime.strptime((message.text or "").strip(), "%d.%m.%Y").date()
        except ValueError:
            await message.answer("Не удалось прочитать дату. Используй формат ДД.ММ.ГГГГ.")
            return
        if not _birth_date_is_plausible(value):
            await message.answer(
                "Дата рождения не может быть в будущем. Проверь, правильно ли указан год."
            )
            return
        await state.update_data(birth_date=value.isoformat())
        await state.set_state(ProfileForm.time_precision)
        await message.answer(
            "Насколько точно известно время рождения?",
            reply_markup=_keyboard(
                ("Знаю точно", "precision:exact"),
                ("Знаю примерно", "precision:approximate"),
                ("Не знаю", "precision:unknown"),
            ),
        )

    @router.callback_query(ProfileForm.time_precision, F.data.startswith("precision:"))
    async def precision(callback: CallbackQuery, state: FSMContext) -> None:
        value = (callback.data or "").split(":", 1)[1]
        if value not in {item.value for item in TimePrecision}:
            await callback.answer("Неизвестный вариант", show_alert=True)
            return
        await state.update_data(time_precision=value)
        await callback.answer()
        if value == TimePrecision.UNKNOWN:
            await state.update_data(birth_time=None)
            await state.set_state(ProfileForm.city)
            if callback.message:
                await callback.message.answer(
                    "Введи только город рождения, например: Москва. Страну писать не нужно — "
                    "если найдётся несколько городов, я покажу варианты с регионом и страной."
                )
        else:
            await state.set_state(ProfileForm.birth_time)
            if callback.message:
                warning = (
                    " В домах карты возможна неточность."
                    if value == TimePrecision.APPROXIMATE
                    else ""
                )
                await callback.message.answer(f"Введи время рождения в формате ЧЧ:ММ.{warning}")

    @router.message(ProfileForm.birth_time)
    async def birth_time(message: Message, state: FSMContext) -> None:
        try:
            value = datetime.strptime((message.text or "").strip(), "%H:%M").time()
        except ValueError:
            await message.answer("Не удалось прочитать время. Используй формат ЧЧ:ММ.")
            return
        await state.update_data(birth_time=value.isoformat())
        await state.set_state(ProfileForm.city)
        await message.answer(
            "Введи только город рождения, например: Москва. Страну писать не нужно — "
            "если найдётся несколько городов, я покажу варианты с регионом и страной."
        )

    @router.message(ProfileForm.city)
    async def city(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if len(query) > 120:
            await message.answer("Название города слишком длинное. Введи только город.")
            return
        variants = await cities.search(query)
        if not variants:
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
            return
        await state.update_data(city=selected)
        await state.set_state(ProfileForm.confirm)
        await callback.answer()
        data = await state.get_data()
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
        summary = (
            "Проверь данные:\n\n"
            f"Дата: {datetime.fromisoformat(data['birth_date']).strftime('%d.%m.%Y')}\n"
            f"Время: {time_text} ({precision_names[data['time_precision']]})\n"
            f"Место: {html.escape(_city_label(City(**selected)))}"
        )
        if callback.message:
            await callback.message.answer(
                summary,
                reply_markup=_keyboard(
                    ("Всё верно", "form:confirm"), ("Заполнить заново", "form:restart")
                ),
            )

    @router.callback_query(ProfileForm.confirm, F.data == "form:restart")
    async def form_restart(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if isinstance(callback.message, Message):
            await _begin(callback.message, state, settings, avatars)

    @router.callback_query(ProfileForm.confirm, F.data == "form:confirm")
    async def form_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(callback.message, Message):
            return
        await callback.answer("Рассчитываю профиль…")
        raw = await state.get_data()
        birth = BirthData(
            name="",
            birth_date=date.fromisoformat(raw["birth_date"]),
            time_precision=TimePrecision(raw["time_precision"]),
            birth_time=time.fromisoformat(raw["birth_time"]) if raw.get("birth_time") else None,
            city=City(**raw["city"]),
        )
        try:
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
        await state.clear()
        await store.record_event(callback.from_user.id, "profile_calculated")
        await store.schedule_strength_offer(profile_id)
        await _send_free_avatar(
            callback.message,
            profile_id=profile_id,
            money_type=result.money_type,
            free_insight=result.free_insight,
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
                "Предложение уже отправлено или недоступно.",
                show_alert=True,
            )

    @router.callback_query(F.data.startswith("buy:"))
    async def buy(callback: CallbackQuery, state: FSMContext) -> None:
        profile_id = (callback.data or "").split(":", 1)[1]
        access = await store.profile_access(callback.from_user.id)
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
        if settings.payment_mode is not PaymentMode.FAKE:
            await callback.answer(
                f"Оплата временно недоступна. Напиши @{settings.support_username}.",
                show_alert=True,
            )
            return
        await store.create_fake_paid_order(
            telegram_id=callback.from_user.id,
            profile_id=profile_id,
        )
        await state.clear()
        await callback.answer("Оплата подтверждена")
        delivery.notify()

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
            free_insight=result.free_insight,
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

    @router.message(Command("delete_my_data"))
    async def delete_my_data(message: Message, state: FSMContext) -> None:
        await state.set_state(DeleteForm.confirm)
        await message.answer(
            "Удалить данные рождения и результат расчёта? Обезличенные события и "
            "минимальный платёжный журнал сохранятся на срок из политики.",
            reply_markup=_keyboard(("Удалить мои данные", "delete:yes"), ("Отмена", "delete:no")),
        )

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
            f"Выручка: {data['revenue_rub']} ₽\n\nFirst-touch: {first}\nLast-touch: {last}"
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
            BotCommand(command="start", description="Начать расчёт"),
            BotCommand(command="profile", description="Показать сохранённый аватар"),
            BotCommand(command="support", description="Поддержка"),
            BotCommand(command="paysupport", description="Вопросы по оплате"),
            BotCommand(command="terms", description="Условия"),
            BotCommand(command="privacy", description="Конфиденциальность"),
            BotCommand(command="delete_my_data", description="Удалить мои данные"),
        ]
    )
