from __future__ import annotations

import html
import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from aiohttp import web

from money_profile_bot.config import PaymentMode, Settings
from money_profile_bot.services.delivery import DeliveryWorker
from money_profile_bot.services.robokassa import RobokassaClient
from money_profile_bot.services.store import Store
from money_profile_bot.web.legal import consent_body, privacy_body, terms_body

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
        return web.Response(
            text=_page("Политика обработки персональных данных", privacy_body(settings)),
            content_type="text/html",
        )

    async def terms(_: web.Request) -> web.Response:
        return web.Response(
            text=_page("Условия использования", terms_body(settings)),
            content_type="text/html",
        )

    async def consent(_: web.Request) -> web.Response:
        return web.Response(
            text=_page("Согласие на обработку персональных данных", consent_body(settings)),
            content_type="text/html",
        )

    async def payment_result(request: web.Request) -> web.Response:
        if settings.payment_mode is not PaymentMode.ROBOKASSA:
            raise web.HTTPNotFound()
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
        if settings.payment_mode is not PaymentMode.ROBOKASSA:
            raise web.HTTPNotFound()
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
    app.router.add_get("/consent", consent)
    app.router.add_route("*", "/payments/robokassa/result", payment_result)
    app.router.add_post("/payments/robokassa/result2", payment_result2)
    app.router.add_route("*", "/payments/robokassa/success", payment_success)
    app.router.add_route("*", "/payments/robokassa/fail", payment_fail)
    return app
