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
from aiohttp import web

from money_profile_bot.bot.router import build_router, set_commands
from money_profile_bot.bot.storage import EncryptedDatabaseStorage
from money_profile_bot.config import Settings, ensure_runtime_directories
from money_profile_bot.crypto import CryptoBox
from money_profile_bot.database import Database
from money_profile_bot.services.avatar import AvatarAssets
from money_profile_bot.services.delivery import DeliveryWorker
from money_profile_bot.services.geonames import CityCatalog
from money_profile_bot.services.pdf import PdfRenderer
from money_profile_bot.services.robokassa import RobokassaClient, RobokassaError
from money_profile_bot.services.store import Store
from money_profile_bot.web.app import create_web_app

logger = logging.getLogger(__name__)


async def maintenance(store: Store, settings: Settings) -> None:
    while True:
        try:
            await store.refresh_refunds()
            cutoff = datetime.now(UTC) - timedelta(days=settings.profile_draft_retention_days)
            await store.cleanup_expired_drafts(cutoff)
        except RobokassaError:
            logger.warning("payment maintenance request failed")
        except Exception:
            logger.exception("maintenance iteration failed")
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
    delivery: DeliveryWorker | None = None
    dispatcher: Dispatcher | None = None
    tasks: list[asyncio.Task[object]] = []
    runner: web.AppRunner | None = None

    async with aiohttp.ClientSession() as http_session:
        robokassa = RobokassaClient(settings, http_session)
        store = Store(database.sessions, crypto, robokassa)
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
                PdfRenderer(avatars),
                settings.pdf_output_directory,
                full_reading_offer_image=avatars.full_reading_offer_image(),
                full_reading_contact_url=f"https://t.me/{settings.support_username}",
            )
            storage = EncryptedDatabaseStorage(database.sessions, crypto)
            dispatcher = Dispatcher(storage=storage)
            dispatcher.include_router(
                build_router(
                    settings,
                    store,
                    CityCatalog(settings.geonames_database_path),
                    delivery,
                    avatars,
                )
            )

        app = create_web_app(settings, store, robokassa, delivery)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, settings.http_host, settings.http_port)
        await site.start()
        logger.info("public service started on configured address")

        if delivery:
            tasks.append(asyncio.create_task(delivery.run(), name="delivery-worker"))
        tasks.append(asyncio.create_task(maintenance(store, settings), name="maintenance-worker"))

        try:
            if bot and dispatcher:
                await set_commands(bot)
                await dispatcher.start_polling(
                    bot,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                )
            else:
                await asyncio.Event().wait()
        finally:
            if delivery:
                delivery.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
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
