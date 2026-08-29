from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from money_profile_bot.domain import GeneratedProfile
from money_profile_bot.models import DeliveryStatus
from money_profile_bot.services.avatar import FULL_READING_CAPTION, display_avatar_name
from money_profile_bot.services.pdf import PdfRenderer
from money_profile_bot.services.store import Store

logger = logging.getLogger(__name__)

class DeliveryWorker:
    def __init__(
        self,
        bot: Bot,
        store: Store,
        renderer: PdfRenderer,
        pdfs_dir: Path,
        *,
        full_reading_offer_image: Path | None = None,
        full_reading_contact_url: str = "https://t.me/simnatali",
    ) -> None:
        self.bot = bot
        self.store = store
        self.renderer = renderer
        self.pdfs_dir = pdfs_dir
        self.full_reading_offer_image = full_reading_offer_image
        self.full_reading_contact_url = full_reading_contact_url
        self._wake = asyncio.Event()
        self._stopping = False

    def notify(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while not self._stopping:
            for order_id in await self.store.pending_order_ids():
                await self.deliver(order_id)
            self._wake.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=10)

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def deliver(self, order_id: str) -> None:
        try:
            order, telegram_id, birth, result, items = await self.store.delivery_context(order_id)
        except Exception:
            logger.exception("delivery context failed", extra={"order_id": order_id})
            return
        has_pdf_item = any(item.kind == "pdf" for item in items)
        legacy_pdf_sent = False
        for item in items:
            if item.status in (DeliveryStatus.SCHEDULED, DeliveryStatus.SENT):
                continue
            try:
                if item.kind == "pdf":
                    pdf_path = await self._ensure_pdf(order.profile_id, birth.name, result)
                    sent = await self.bot.send_document(
                        telegram_id,
                        FSInputFile(
                            pdf_path,
                            filename=(
                                f"Денежный_потенциал_{display_avatar_name(result.money_type)}.pdf"
                            ),
                        ),
                    )
                elif item.kind.startswith("message:") or item.kind == "card":
                    # Очереди ранней тестовой версии могли содержать шесть сообщений и
                    # карточку. При первом оставшемся элементе заменяем их единым PDF.
                    if not has_pdf_item and not legacy_pdf_sent:
                        pdf_path = await self._ensure_pdf(order.profile_id, birth.name, result)
                        sent = await self.bot.send_document(
                            telegram_id,
                            FSInputFile(pdf_path, filename="Денежный_потенциал.pdf"),
                        )
                        legacy_pdf_sent = True
                    else:
                        await self.store.mark_delivery_item(item.id, status=DeliveryStatus.SENT)
                        continue
                elif item.kind == "feedback":
                    await self.store.mark_delivery_item(item.id, status=DeliveryStatus.SENT)
                    continue
                elif item.kind == "full_reading_offer":
                    if self.full_reading_offer_image is None:
                        raise RuntimeError("full reading offer image is not configured")
                    sent = await self.bot.send_photo(
                        telegram_id,
                        FSInputFile(self.full_reading_offer_image),
                        caption=FULL_READING_CAPTION,
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="Хочу денежный разбор",
                                        url=self.full_reading_contact_url,
                                    )
                                ]
                            ]
                        ),
                    )
                else:
                    raise ValueError("unknown delivery item kind")
            except (TelegramAPIError, OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "delivery item failed",
                    extra={"order_id": order_id, "kind": item.kind, "error": type(exc).__name__},
                )
                await self.store.mark_delivery_item(
                    item.id, status=DeliveryStatus.FAILED, error_code=type(exc).__name__
                )
                return
            await self.store.mark_delivery_item(
                item.id, status=DeliveryStatus.SENT, message_id=sent.message_id
            )
        await self.store.complete_delivery_if_ready(order_id)

    async def send_copy(self, order_id: str) -> None:
        order, telegram_id, birth, result, _ = await self.store.delivery_context(order_id)
        pdf_path = await self._ensure_pdf(order.profile_id, birth.name, result)
        await self.bot.send_document(
            telegram_id,
            FSInputFile(
                pdf_path,
                filename=f"Денежный_потенциал_{display_avatar_name(result.money_type)}.pdf",
            ),
        )

    async def _ensure_pdf(self, profile_id: str, name: str, result: GeneratedProfile) -> Path:
        pdf_path = self.pdfs_dir / f"{profile_id}.pdf"
        await asyncio.to_thread(
            self.renderer.render,
            name=name,
            result=result,
            destination=pdf_path,
        )
        await self.store.save_card_path(profile_id, str(pdf_path))
        return pdf_path
