from __future__ import annotations

import asyncio
import html
import re
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
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
from money_profile_bot.models import OrderStatus, ProfileStatus
from money_profile_bot.services.astro import calculate_chart
from money_profile_bot.services.avatar import AvatarAssets
from money_profile_bot.services.delivery import DeliveryWorker
from money_profile_bot.services.geonames import CityCatalog
from money_profile_bot.services.robokassa import RobokassaError
from money_profile_bot.services.rules import generate_profile, validate_generated_profile
from money_profile_bot.services.store import Store

START_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё .'-]{0,58}[A-Za-zА-Яа-яЁё.]$")
EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@.]{1,190}\.[^\s@]{2,63}$")


def _keyboard(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data)] for text, data in rows
        ]
    )


def _price(settings: Settings) -> str:
    value = settings.product_price_rub.quantize(Decimal("0.01"))
    return f"{value:,.2f}".replace(",", " ").replace(".00", "") + " ₽"


def _offer_caption() -> str:
    return (
        "<b>Сила твоего аватара</b>\n\n"
        "Узнай, в чём его сила, что мешает раскрывать потенциал и через что тебе "
        "легче приходить к доходу.\n\n"
        "<b>Внутри:</b>\n"
        "• твоя сильная сторона\n"
        "• подходящий формат работы\n"
        "• как проявляться и продавать\n"
        "• денежная ловушка\n"
        "• эксперимент на 7 дней"
    )


def _city_label(city: City) -> str:
    parts = [city.name]
    if city.region:
        parts.append(city.region)
    parts.append(city.country_name)
    return ", ".join(parts)


async def _begin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ProfileForm.adult)
    await message.answer(
        "«Денежный потенциал» — персональная астрологическая интерпретация о стиле "
        "монетизации, работе и продажах. Сначала подтвердите, что вам исполнилось 18 лет.",
        reply_markup=_keyboard(("Мне есть 18 лет", "adult:yes"), ("Мне нет 18 лет", "adult:no")),
    )


def build_router(
    settings: Settings,
    store: Store,
    cities: CityCatalog,
    delivery: DeliveryWorker,
    avatars: AvatarAssets,
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
        if settings.bootstrap_admin_on_first_start:
            claim = await store.claim_admin_if_unset(message.from_user.id)
            if claim.newly_claimed:
                await message.answer(
                    "Вы назначены администратором тестового бота. Команда статистики: /stats."
                )
        await store.record_event(message.from_user.id, "bot_started")
        access = await store.profile_access(message.from_user.id)
        if access and access.profile_status == ProfileStatus.PAID:
            await message.answer("Ваш оплаченный профиль уже готов. Используйте /profile.")
            return
        await _begin(message, state)

    @router.callback_query(ProfileForm.adult, F.data == "adult:no")
    async def adult_no(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        if callback.message:
            await callback.message.answer("Продукт доступен только пользователям старше 18 лет.")

    @router.callback_query(ProfileForm.adult, F.data == "adult:yes")
    async def adult_yes(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ProfileForm.consent)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Для расчёта нужны имя, дата, время и место рождения. Нажимая «Согласен», вы "
                "принимаете условия и даёте согласие на обработку этих данных. Разбор носит "
                "развлекательный характер.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Условия", url=f"{settings.public_base_url}/terms"
                            ),
                            InlineKeyboardButton(
                                text="Политика", url=f"{settings.public_base_url}/privacy"
                            ),
                        ],
                        [InlineKeyboardButton(text="Согласен", callback_data="consent:yes")],
                    ]
                ),
            )

    @router.callback_query(ProfileForm.consent, F.data == "consent:yes")
    async def consent(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user:
            return
        await store.save_consent(callback.from_user.id, settings.legal_docs_version)
        await state.set_state(ProfileForm.name)
        await callback.answer()
        if callback.message:
            await callback.message.answer("Как вас называть в разборе? Введите имя.")

    @router.message(ProfileForm.name)
    async def name(message: Message, state: FSMContext) -> None:
        value = (message.text or "").strip()
        if len(value) > 60 or len(value) < 2 or not NAME_RE.fullmatch(value):
            await message.answer("Введите имя длиной 2–60 символов, без цифр и служебных знаков.")
            return
        await state.update_data(name=value)
        await state.set_state(ProfileForm.birth_date)
        await message.answer("Введите дату рождения в формате ДД.ММ.ГГГГ.")

    @router.message(ProfileForm.birth_date)
    async def birth_date(message: Message, state: FSMContext) -> None:
        try:
            value = datetime.strptime((message.text or "").strip(), "%d.%m.%Y").date()
        except ValueError:
            await message.answer("Не удалось прочитать дату. Используйте формат ДД.ММ.ГГГГ.")
            return
        today = date.today()
        age = today.year - value.year - ((today.month, today.day) < (value.month, value.day))
        if age < 18 or age > 120:
            await message.answer("Для продукта нужен возраст от 18 до 120 лет. Проверьте дату.")
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
                    "Введите только город рождения, например: Москва. Страну писать не нужно — "
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
                await callback.message.answer(f"Введите время рождения в формате ЧЧ:ММ.{warning}")

    @router.message(ProfileForm.birth_time)
    async def birth_time(message: Message, state: FSMContext) -> None:
        try:
            value = datetime.strptime((message.text or "").strip(), "%H:%M").time()
        except ValueError:
            await message.answer("Не удалось прочитать время. Используйте формат ЧЧ:ММ.")
            return
        await state.update_data(birth_time=value.isoformat())
        await state.set_state(ProfileForm.city)
        await message.answer(
            "Введите только город рождения, например: Москва. Страну писать не нужно — "
            "если найдётся несколько городов, я покажу варианты с регионом и страной."
        )

    @router.message(ProfileForm.city)
    async def city(message: Message, state: FSMContext) -> None:
        query = (message.text or "").strip()
        if len(query) > 120:
            await message.answer("Название города слишком длинное. Введите только город.")
            return
        variants = await cities.search(query)
        if not variants:
            await message.answer(
                "Не получилось найти город. Введите только его название без страны, например: "
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
        await message.answer("Выберите подходящий вариант:", reply_markup=keyboard)

    @router.callback_query(ProfileForm.city_choice, F.data.startswith("city:"))
    async def city_choice(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        try:
            index = int((callback.data or "").split(":", 1)[1])
            selected = data["city_options"][index]
        except (ValueError, IndexError, KeyError):
            await callback.answer("Вариант устарел. Введите город ещё раз.", show_alert=True)
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
        time_text = data.get("birth_time") or "не указано"
        summary = (
            f"Проверьте данные:\n\nИмя: {html.escape(data['name'])}\n"
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
            await _begin(callback.message, state)

    @router.callback_query(ProfileForm.confirm, F.data == "form:confirm")
    async def form_confirm(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.message:
            return
        await callback.answer("Рассчитываю профиль…")
        raw = await state.get_data()
        birth = BirthData(
            name=raw["name"],
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
            avatar_image = avatars.free_image(result.money_type)
            offer_image = avatars.offer_image(result.money_type)
            profile_id = await store.save_calculation(callback.from_user.id, birth, facts, result)
        except Exception:
            await callback.message.answer(
                f"Расчёт временно не завершён. Попробуйте позже или напишите @{settings.support_username}."
            )
            return
        await state.clear()
        await store.record_event(callback.from_user.id, "profile_calculated")
        if facts.warning:
            await callback.message.answer(f"Важно: {facts.warning}")
        await callback.message.answer_photo(
            FSInputFile(avatar_image),
            caption=result.free_insight,
        )
        await store.record_event(callback.from_user.id, "offer_viewed")
        button = (f"Раскрыть силу — {_price(settings)}", f"buy:{profile_id}")
        await callback.message.answer_photo(
            FSInputFile(offer_image),
            caption=_offer_caption(),
            reply_markup=_keyboard(button),
        )

    @router.callback_query(F.data.startswith("buy:"))
    async def buy(callback: CallbackQuery, state: FSMContext) -> None:
        profile_id = (callback.data or "").split(":", 1)[1]
        access = await store.profile_access(callback.from_user.id)
        if not access or access.profile_id != profile_id:
            await callback.answer("Профиль не найден", show_alert=True)
            return
        if access.order_status in (OrderStatus.PAID, OrderStatus.DELIVERED):
            await callback.answer("Оплата уже получена. Откройте /profile.", show_alert=True)
            return
        if settings.payment_mode is PaymentMode.FAKE:
            try:
                order = await store.create_fake_paid_order(
                    telegram_id=callback.from_user.id,
                    profile_id=profile_id,
                )
            except RuntimeError:
                await callback.answer("Не удалось открыть тестовый результат", show_alert=True)
                return
            await state.clear()
            await store.record_event(callback.from_user.id, "fake_payment_succeeded")
            delivery.notify()
            await callback.answer("Тестовый доступ открыт")
            if callback.message:
                await callback.message.answer(
                    f"Тестовый заказ {order.code} подтверждён без списания денег. "
                    "Отправляю полный результат."
                )
            return
        await state.set_state(ProfileForm.email)
        await state.update_data(payment_profile_id=profile_id)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Введите email, на который Robokassa отправит чек. Мы не используем его для рассылок."
            )

    @router.message(ProfileForm.email)
    async def payment_email(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return
        email = (message.text or "").strip().casefold()
        if len(email) > 254 or not EMAIL_RE.fullmatch(email):
            await message.answer("Проверьте email и введите его в формате name@example.ru.")
            return
        raw = await state.get_data()
        profile_id = raw.get("payment_profile_id")
        if not profile_id:
            await state.clear()
            await message.answer("Сессия устарела. Откройте /profile и повторите оплату.")
            return
        try:
            order = await store.create_order(
                telegram_id=message.from_user.id,
                profile_id=profile_id,
                email=email,
                amount_minor=settings.product_price_minor,
            )
        except (RobokassaError, RuntimeError):
            await message.answer(
                f"Не удалось создать ссылку на оплату. Попробуйте позже или напишите @{settings.support_username}."
            )
            return
        await state.clear()
        await store.record_event(message.from_user.id, "payment_link_created")
        await message.answer(
            f"Заказ {order.code}. После нажатия откроется защищённая страница Robokassa. "
            "Чек придёт на указанный email. Результат будет отправлен сюда только после подтверждения оплаты.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=f"Оплатить {_price(settings)}", url=order.url)],
                    [
                        InlineKeyboardButton(
                            text="Проверить оплату", callback_data=f"check:{order.order_id}"
                        )
                    ],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("check:"))
    async def check_payment(callback: CallbackQuery) -> None:
        access = await store.profile_access(callback.from_user.id)
        order_id = (callback.data or "").split(":", 1)[1]
        if not access or access.order_id != order_id:
            await callback.answer("Заказ не найден", show_alert=True)
        elif access.order_status in (OrderStatus.PAID, OrderStatus.DELIVERED):
            delivery.notify()
            await callback.answer("Оплата подтверждена. Результат отправляется.", show_alert=True)
        else:
            await callback.answer("Подтверждение от Robokassa ещё не получено.", show_alert=True)

    @router.message(Command("profile"))
    async def profile(message: Message) -> None:
        if not message.from_user:
            return
        access = await store.profile_access(message.from_user.id)
        if not access:
            await message.answer("Денежный аватар ещё не рассчитан. Начните с /start.")
            return
        if access.order_status == OrderStatus.DELIVERED and access.order_id:
            await delivery.send_copy(access.order_id)
        elif access.order_status == OrderStatus.PAID and access.order_id:
            delivery.notify()
            await message.answer("Оплата подтверждена. Завершаю выдачу результата.")
        elif access.payment_url:
            await message.answer(
                f"Оплата заказа {access.order_code} ещё не подтверждена.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"Оплатить {_price(settings)}", url=access.payment_url
                            )
                        ]
                    ]
                ),
            )
        else:
            _, result = await store.get_profile_result(access.profile_id)
            await message.answer_photo(
                FSInputFile(avatars.offer_image(result.money_type)),
                caption=_offer_caption(),
                reply_markup=_keyboard(
                    (f"Раскрыть силу — {_price(settings)}", f"buy:{access.profile_id}")
                ),
            )

    @router.callback_query(F.data.startswith("rating:"))
    async def rating(callback: CallbackQuery) -> None:
        try:
            _, profile_id, raw_rating = (callback.data or "").split(":", 2)
            await store.save_feedback(callback.from_user.id, profile_id, int(raw_rating))
        except (ValueError, LookupError):
            await callback.answer("Не удалось сохранить оценку", show_alert=True)
            return
        await callback.answer("Спасибо! Оценка сохранена.", show_alert=True)
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(Command("support"))
    @router.message(Command("paysupport"))
    async def support(message: Message) -> None:
        await message.answer(
            f"Поддержка: @{settings.support_username}. По оплате укажите код заказа, но не "
            "присылайте данные карты, пароли или коды из SMS."
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
            "Удалить имя, данные рождения, результат, PDF и отзыв? Обезличенные события и "
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
            BotCommand(command="profile", description="Получить сохранённый результат"),
            BotCommand(command="support", description="Поддержка"),
            BotCommand(command="paysupport", description="Вопросы по оплате"),
            BotCommand(command="terms", description="Условия"),
            BotCommand(command="privacy", description="Конфиденциальность"),
            BotCommand(command="delete_my_data", description="Удалить мои данные"),
        ]
    )
