from __future__ import annotations

import html
import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from aiohttp import web

from money_profile_bot.config import Settings
from money_profile_bot.services.delivery import DeliveryWorker
from money_profile_bot.services.robokassa import RobokassaClient
from money_profile_bot.services.store import Store

logger = logging.getLogger(__name__)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(title)}</title><style>
:root {{ color-scheme: dark; --bg:#000; --surface:#050505; --text:#fff; --muted:#a3a3a3;
--border:#333; --accent:#d99a48; }} * {{ box-sizing:border-box }} body {{ margin:0; background:var(--bg);
color:var(--text); font:16px/1.55 Inter,Arial,sans-serif }} main {{ max-width:760px; margin:48px auto;
padding:48px; background:var(--surface); border:1px solid var(--border); border-radius:48px }}
h1,h2 {{ font-family:"Liberation Serif",Georgia,serif; letter-spacing:-.025em; line-height:1.08 }}
h1 {{ font-size:clamp(42px,8vw,72px); margin:0 0 32px }} h2 {{ margin-top:40px }} p,li {{ color:#d0d0d0 }}
a {{ color:var(--accent) }} .meta {{ color:var(--muted); font-size:14px }} @media(max-width:700px) {{
main {{ margin:16px; padding:28px 24px; border-radius:24px }} }}
</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""


def _field(data: dict[str, str], *names: str) -> str:
    lowered = {key.casefold(): value for key, value in data.items()}
    for name in names:
        if name.casefold() in lowered:
            return lowered[name.casefold()]
    return ""


def _minor(value: str) -> int:
    return int((Decimal(value.replace(",", ".")) * 100).quantize(Decimal("1"), ROUND_HALF_UP))


def create_web_app(
    settings: Settings,
    store: Store,
    robokassa: RobokassaClient,
    delivery: DeliveryWorker | None,
) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)

    async def health(_: web.Request) -> web.Response:
        healthy = await store.healthcheck()
        return web.json_response(
            {"status": "ok" if healthy else "error", "version": settings.source_commit},
            status=200 if healthy else 503,
        )

    async def source(_: web.Request) -> web.StreamResponse:
        raise web.HTTPFound(settings.source_url)

    async def privacy(_: web.Request) -> web.Response:
        operator = html.escape(settings.operator_name or "[ФИО оператора необходимо заполнить]")
        inn = html.escape(settings.operator_inn or "[ИНН необходимо заполнить]")
        email = html.escape(settings.operator_email or "[email необходимо заполнить]")
        body = f"""
<p class="meta">Версия документа: {html.escape(settings.legal_docs_version)}</p>
<p>Оператор персональных данных: {operator}, самозанятый, ИНН {inn}, контакт: {email}.</p>
<h2>Какие данные обрабатываются</h2><p>Числовой идентификатор Telegram, имя, дата, время и место рождения,
email для электронного чека, согласия, результат расчёта, оценка и технические события.</p>
<h2>Цели и основания</h2><p>Данные нужны для выполнения расчёта, заключения и исполнения договора,
приёма оплаты, выдачи результата, поддержки, безопасности и исполнения требований законодательства.
Основания: согласие пользователя, исполнение договора и обязанности оператора.</p>
<h2>Получатели</h2><p>Telegram используется как платформа общения, Robokassa — для оплаты и
автоматического формирования чека, хостинг — для работы приложения. Платёжные реквизиты карты бот не получает.</p>
<h2>Хранение и защита</h2><p>Персональные поля шифруются. Неоплаченные анкеты удаляются через
{settings.profile_draft_retention_days} дней. Резервные копии хранятся {settings.backup_retention_days} дней.
Минимальный платёжный журнал хранится {settings.payment_retention_days or "[срок не утверждён]"} дней.</p>
<h2>Права пользователя</h2><p>Удалить имя, исходные данные, результаты, карточку и текст отзыва можно
командой <code>/delete_my_data</code>. Вопросы направляются на {email} или
<a href="https://t.me/{html.escape(settings.support_username)}">@{html.escape(settings.support_username)}</a>.</p>
<p class="meta">Это проект документа. До запуска владелец или юрист должен проверить реквизиты,
правовые основания, трансграничную передачу и сроки хранения.</p>"""
        return web.Response(
            text=_page("Политика конфиденциальности", body), content_type="text/html"
        )

    async def terms(_: web.Request) -> web.Response:
        operator = html.escape(settings.operator_name or "[ФИО оператора необходимо заполнить]")
        inn = html.escape(settings.operator_inn or "[ИНН необходимо заполнить]")
        price = f"{settings.product_price_rub.quantize(Decimal('0.01'))} ₽"
        body = f"""
<p class="meta">Версия документа: {html.escape(settings.legal_docs_version)}</p>
<p>Исполнитель: {operator}, самозанятый, ИНН {inn}. Пользователь должен быть старше 18 лет.</p>
<h2>Предмет</h2><p>Сервис создаёт индивидуальную астрологическую интерпретацию «Денежный профиль»
по введённым пользователем данным. Результат состоит из шести сообщений и изображения и выдаётся в Telegram.</p>
<h2>Стоимость и оплата</h2><p>Стоимость одного профиля — {html.escape(price)}. Платёж проводится на странице
Robokassa в рублях. Электронный чек направляется на email, указанный пользователем. Результат открывается только
после серверного подтверждения платежа. Один платёж относится к одному зафиксированному набору исходных данных.</p>
<h2>Исправления и возвраты</h2><p>Если исходные данные были введены ошибочно или результат не доставлен,
обратитесь к <a href="https://t.me/{html.escape(settings.support_username)}">@{html.escape(settings.support_username)}</a>
и укажите код заказа. Возврат рассматривается исполнителем и проводится через Robokassa тем же способом оплаты.</p>
<h2>Ограничение</h2><p>Разбор предназначен для развлечения и самонаблюдения. Он не является финансовой,
инвестиционной, налоговой или юридической рекомендацией, не предсказывает доход и не гарантирует результат.</p>
<p class="meta">Это проект оферты. До запуска документ и реквизиты должен проверить владелец или юрист.</p>"""
        return web.Response(text=_page("Условия использования", body), content_type="text/html")

    async def payment_result(request: web.Request) -> web.Response:
        if request.method == "POST":
            posted = await request.post()
            data = {str(key): str(value) for key, value in posted.items()}
        else:
            data = {str(key): str(value) for key, value in request.query.items()}
        out_sum = _field(data, "OutSum")
        invoice_raw = _field(data, "InvId", "InvID", "InvoiceID")
        signature = _field(data, "SignatureValue")
        if not out_sum or not invoice_raw or not signature:
            raise web.HTTPBadRequest(text="missing payment fields")
        if not robokassa.verify_result(
            out_sum=out_sum,
            invoice_id=invoice_raw,
            signature=signature,
            user_parameters=data,
        ):
            raise web.HTTPForbidden(text="invalid signature")
        try:
            invoice_id = int(invoice_raw)
            amount_minor = _minor(out_sum)
            result = await store.accept_payment_callback(
                invoice_id=invoice_id,
                amount_minor=amount_minor,
                email=_field(data, "EMail") or None,
            )
        except (ValueError, InvalidOperation, LookupError):
            logger.warning("rejected validly signed payment notification")
            raise web.HTTPBadRequest(text="payment does not match an order") from None
        if result.newly_paid and delivery:
            delivery.notify()
        return web.Response(text=f"OK{invoice_id}", content_type="text/plain")

    async def payment_result2(_: web.Request) -> web.Response:
        # ResultURL2 is not trusted to authorize delivery. Refund data is queried through OpStateExt.
        return web.Response(status=204)

    async def payment_success(_: web.Request) -> web.Response:
        body = f"""<p>Платёжная страница закрыта. Подтверждение может занять несколько секунд.</p>
<p>Вернитесь в Telegram: результат будет отправлен автоматически. Также можно использовать
команду <code>/profile</code>.</p><p><a href="https://t.me/{html.escape(settings.bot_username)}">Вернуться в бот</a></p>"""
        return web.Response(text=_page("Платёж принят", body), content_type="text/html")

    async def payment_fail(_: web.Request) -> web.Response:
        body = f"""<p>Robokassa не подтвердила оплату. Деньги не должны быть списаны.</p>
<p>Вернитесь в бот и попробуйте снова. Если списание отображается в банке, напишите
<a href="https://t.me/{html.escape(settings.support_username)}">@{html.escape(settings.support_username)}</a>
и укажите код заказа.</p>"""
        return web.Response(text=_page("Оплата не завершена", body), content_type="text/html")

    app.router.add_get("/healthz", health)
    app.router.add_get("/source", source)
    app.router.add_get("/privacy", privacy)
    app.router.add_get("/terms", terms)
    app.router.add_route("*", "/payments/robokassa/result", payment_result)
    app.router.add_post("/payments/robokassa/result2", payment_result2)
    app.router.add_route("*", "/payments/robokassa/success", payment_success)
    app.router.add_route("*", "/payments/robokassa/fail", payment_fail)
    return app
