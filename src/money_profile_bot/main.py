from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiohttp import web

from money_profile_bot.bot.access import PrivateAccessMiddleware
from money_profile_bot.bot.rate_limit import RateLimitMiddleware
from money_profile_bot.bot.router import build_router, form_reminder_payload, set_commands
from money_profile_bot.bot.storage import EncryptedDatabaseStorage
from money_profile_bot.config import Environment, PaymentMode, Settings, ensure_runtime_directories
from money_profile_bot.crypto import CryptoBox
from money_profile_bot.database import Database
from money_profile_bot.services.avatar import AvatarAssets
from money_profile_bot.services.delivery import DeliveryWorker
from money_profile_bot.services.geonames import CityCatalog
from money_profile_bot.services.robokassa import RobokassaClient, RobokassaError
from money_profile_bot.services.store import Store
from money_profile_bot.web.app import create_web_app

logger = logging.getLogger(__name__)


def years_before(value: datetime, years: int) -> datetime:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


async def maintenance(store: Store, settings: Settings) -> None:
    while True:
        try:
            now = datetime.now(UTC)
            await store.analytics.cleanup()
            await store.notifications.cleanup()
            draft_cutoff = now - timedelta(days=settings.profile_draft_retention_days)
            await store.cleanup_expired_form_data(draft_cutoff)
            await store.cleanup_expired_drafts(draft_cutoff)
            payment_contact_cutoff = now - timedelta(days=settings.payment_contact_retention_days)
            await store.cleanup_expired_unpaid_orders(payment_contact_cutoff)
            await store.cleanup_expired_payment_contacts(payment_contact_cutoff)
            if settings.payment_mode is PaymentMode.FAKE or settings.robokassa_test_mode:
                await store.cleanup_expired_test_payments(payment_contact_cutoff)
            else:
                payment_record_cutoff = years_before(now, settings.payment_record_retention_years)
                await store.cleanup_expired_anonymized_payment_records(payment_record_cutoff)
        except Exception:
            logger.exception("data retention maintenance failed")
        try:
            await store.refresh_refunds()
        except RobokassaError:
            logger.warning("payment maintenance request failed")
        except Exception:
            logger.exception("refund maintenance failed")
        await asyncio.sleep(3600)


async def serve() -> None:
    settings = Settings()
    ensure_runtime_directories(settings)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database = Database(settings.database_url)
    await database.initialize()
    crypto = CryptoBox(settings.app_encryption_key, settings.lookup_hmac_key)

    bot: Bot | None = None
    dispatcher: Dispatcher | None = None
    delivery: DeliveryWorker | None = None
    tasks: list[asyncio.Task[object]] = []
    runner: web.AppRunner | None = None
    delivery_task: asyncio.Task[None] | None = None

    async with aiohttp.ClientSession() as http_session:
        robokassa = RobokassaClient(settings, http_session)
        store = Store(
            database.sessions,
            crypto,
            robokassa,
            payment_notification_ids=settings.payment_notification_ids,
            payment_notifications_include_test=settings.payment_notifications_include_test,
            analytics_mode=(
                "test"
                if settings.payment_mode is PaymentMode.FAKE or settings.robokassa_test_mode
                else "live"
            ),
        )
        if not settings.web_only:
            if not settings.bot_token:
                raise ValueError("BOT_TOKEN is required when WEB_ONLY=false")
            bot = Bot(
                settings.bot_token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
                session=AiohttpSession(proxy=settings.telegram_proxy_url or None),
            )
            avatars = AvatarAssets(settings.avatar_asset_directory)
            delivery = DeliveryWorker(
                bot,
                store,
                avatars,
                sales_telegram_username=settings.support_username,
                product_price_rub=settings.product_price_rub,
                payment_mode=settings.payment_mode,
                robokassa_test_mode=settings.robokassa_test_mode,
            )
            backfilled_reminders = await store.backfill_form_reminders(
                bot.id,
                form_reminder_payload,
            )
            if backfilled_reminders:
                logger.info(
                    "backfilled form reminders",
                    extra={"count": backfilled_reminders},
                )
            storage = EncryptedDatabaseStorage(database.sessions, crypto)
            await store.analytics.backfill(bot.id)
            dispatcher = Dispatcher(storage=storage, events_isolation=SimpleEventIsolation())
            if settings.app_env in {Environment.STAGING, Environment.PILOT}:
                if settings.app_env is Environment.STAGING:
                    allowed_ids = settings.test_access_ids
                    denial_text = "Тестовый бот закрыт. Доступ предоставляется владельцем."
                else:
                    allowed_ids = settings.pilot_access_ids
                    denial_text = "Закрытый пилот недоступен. Доступ предоставляется владельцем."
                access = PrivateAccessMiddleware(allowed_ids, denial_text)
                dispatcher.message.outer_middleware(access)
                dispatcher.callback_query.outer_middleware(access)
            rate_limit = RateLimitMiddleware()
            dispatcher.message.outer_middleware(rate_limit)
            dispatcher.callback_query.outer_middleware(rate_limit)
            dispatcher.include_router(
                build_router(
                    settings,
                    store,
                    CityCatalog(settings.geonames_database_path),
                    avatars,
                    delivery,
                )
            )

        app = create_web_app(settings, store, robokassa, delivery)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, settings.http_host, settings.http_port)
        await site.start()
        logger.info("public service started on configured address")

        tasks.append(asyncio.create_task(maintenance(store, settings), name="maintenance-worker"))
        if delivery:
            delivery_task = asyncio.create_task(delivery.run(), name="delivery-worker")
            tasks.append(delivery_task)

        try:
            if bot and dispatcher:
                await set_commands(bot)
                polling_task = asyncio.create_task(
                    dispatcher.start_polling(
                        bot,
                        allowed_updates=dispatcher.resolve_used_update_types(),
                    ),
                    name="telegram-polling",
                )
                tasks.append(polling_task)
                critical_tasks = {polling_task}
                if delivery_task:
                    critical_tasks.add(delivery_task)
                done, _ = await asyncio.wait(
                    critical_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if delivery_task and delivery_task in done:
                    await delivery_task
                    raise RuntimeError("delivery worker stopped unexpectedly")
                await polling_task
            else:
                await asyncio.Event().wait()
        finally:
            if delivery:
                delivery.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task
            if runner:
                await runner.cleanup()
            if bot:
                await bot.session.close()
            await database.close()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
