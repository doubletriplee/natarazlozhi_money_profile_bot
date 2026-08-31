from __future__ import annotations

import html
import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from aiohttp import web

from money_profile_bot.config import PaymentMode, Settings
from money_profile_bot.services.delivery import DeliveryWorker
from money_profile_bot.services.robokassa import RobokassaClient
from money_profile_bot.services.store import Store
from money_profile_bot.web.legal import (
    consent_body,
    performer_body,
    performer_bot_username,
    privacy_body,
    terms_body,
)

logger = logging.getLogger(__name__)

_SITE_STYLE = """
:root { color-scheme: dark; --bg:#000; --surface:#050505; --text:#fff; --muted:#a3a3a3;
--border:#333; --accent:#d99a48; } * { box-sizing:border-box } body { margin:0; background:var(--bg);
color:var(--text); font:16px/1.55 Inter,Arial,sans-serif }
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(title)}</title><style>
{_SITE_STYLE}main {{ max-width:760px; margin:48px auto;
padding:48px; background:var(--surface); border:1px solid var(--border); border-radius:48px }}
h1,h2 {{ font-family:"Liberation Serif",Georgia,serif; letter-spacing:-.025em; line-height:1.08 }}
h1 {{ font-size:clamp(42px,8vw,72px); margin:0 0 32px }} h2 {{ margin-top:40px }} p,li {{ color:#d0d0d0 }}
a {{ color:var(--accent) }} .meta {{ color:var(--muted); font-size:14px }} @media(max-width:700px) {{
main {{ margin:16px; padding:28px 24px; border-radius:24px }} }}
</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""


def _price(value: Decimal) -> str:
    return format(value.normalize(), "f").replace(".", ",")


def _home_page(settings: Settings) -> str:
    bot_username = html.escape(performer_bot_username(settings))
    bot_url = f"https://t.me/{bot_username}"
    first_price = _price(settings.product_price_rub)
    test_notice = ""
    if settings.payment_mode is PaymentMode.FAKE or settings.robokassa_test_mode:
        test_notice = """<aside class="test-notice" aria-label="Тестовый режим">
<strong>Сейчас действует тестовый режим.</strong>
Списание денег и покупка не происходят. Актуальный сценарий доступен в Telegram-боте.
</aside>"""

    operator = performer_body(settings)

    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta name="description" content="Персональные разборы Денежного аватара по натальной карте.">
<title>Денежный аватар — персональный разбор</title><style>
{_SITE_STYLE}
body {{ min-width:320px }} a {{ color:var(--accent) }} .home {{ width:min(100%,1120px); margin:0 auto;
padding:32px 24px 40px }} .panel {{ background:var(--surface); border:1px solid var(--border);
border-radius:48px; padding:clamp(32px,6vw,72px) }} .hero {{ min-height:min(720px,calc(100vh - 64px));
display:flex; flex-direction:column; justify-content:center }} .hero-copy {{ max-width:830px }}
.home h1,.home h2,.home h3 {{ font-family:"Liberation Serif",Georgia,serif; line-height:1.06;
letter-spacing:-.03em }} .home h1 {{ max-width:900px; margin:0; font-size:clamp(48px,8vw,88px) }}
.lead {{ max-width:750px; margin:28px 0 0; color:#d0d0d0; font-size:clamp(18px,2vw,22px);
line-height:1.5 }} .cta-wrap {{ display:flex; flex-direction:column; align-items:flex-start; gap:12px;
margin-top:36px }} .button {{ display:inline-flex; align-items:center; justify-content:center; min-height:56px;
padding:14px 24px; border:1px solid var(--accent); border-radius:16px; background:var(--accent);
color:#111; font-weight:700; line-height:1.25; text-align:center; text-decoration:none }}
.button:hover {{ background:#e3aa60 }} .button:focus-visible,.home a:focus-visible {{ outline:3px solid #fff;
outline-offset:4px }} .nowrap {{ white-space:nowrap }} .hero-note {{ color:var(--muted); font-size:14px }}
.test-notice {{ max-width:750px;
margin-top:28px; padding:16px 20px; border:1px solid var(--border); border-radius:16px;
background:#0a0a0a; color:#d0d0d0 }} .test-notice strong {{ color:var(--text) }}
.section {{ margin-top:24px }} .section h2 {{ margin:0 0 28px; font-size:clamp(36px,5vw,56px) }}
.product-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:24px }}
.product-card {{ display:flex; flex-direction:column; min-width:0; padding:32px; border:1px solid var(--border);
border-radius:32px; background:#0a0a0a }} .product-card h3 {{ margin:0; font-size:clamp(28px,3vw,36px) }}
.price {{ margin:16px 0 0; color:var(--accent); font-size:32px; font-weight:700; line-height:1.15 }}
.product-card ul {{ margin:24px 0 32px; padding-left:22px; color:#d0d0d0 }} .product-card li {{ margin:10px 0 }}
.product-card li::marker {{ color:var(--accent) }} .product-card .button {{ width:100%; margin-top:auto }}
.steps {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; margin:0; padding:0;
list-style:none; counter-reset:steps }} .steps li {{ position:relative; min-height:132px; padding:56px 24px 24px;
border:1px solid var(--border); border-radius:24px; background:#0a0a0a; color:#d0d0d0; counter-increment:steps }}
.steps li::before {{ content:counter(steps); position:absolute; top:20px; left:24px; color:var(--accent);
font-weight:700 }} .important {{ max-width:750px; margin:0; color:#d0d0d0; font-size:18px;
line-height:1.55 }} .footer {{ padding-bottom:40px }} .footer-grid {{ display:grid;
grid-template-columns:minmax(0,1fr) minmax(260px,.8fr); gap:40px }} .footer h2 {{ margin-bottom:20px }}
.footer h3 {{ margin:0 0 16px; font-size:24px }} .footer p {{ margin:0; color:#d0d0d0 }}
.legal-links {{ display:flex; flex-direction:column; align-items:flex-start; gap:12px }}
.telegram-link {{ display:inline-block; margin-top:24px }}
@media(max-width:760px) {{ .home {{ padding:16px }} .panel {{ border-radius:24px; padding:28px 24px }}
.hero {{ min-height:calc(100svh - 32px) }} .home h1 {{ font-size:clamp(44px,14vw,64px) }}
.lead {{ margin-top:24px }} .cta-wrap {{ align-items:stretch; margin-top:28px }} .button {{ width:100% }}
.product-grid,.steps,.footer-grid {{ grid-template-columns:1fr }} .product-card {{ padding:24px;
border-radius:24px }} .section h2 {{ margin-bottom:24px }} .steps li {{ min-height:0 }} }}
@media(prefers-reduced-motion:reduce) {{ * {{ scroll-behavior:auto!important }} }}
</style></head><body><main class="home">
<section class="panel hero" aria-labelledby="hero-title"><div class="hero-copy">
<h1 id="hero-title">Узнай свой Денежный аватар</h1>
<p class="lead">Персональный разбор по натальной карте, который помогает увидеть сильные стороны,
подходящие направления реализации и особенности взаимодействия с деньгами.</p>
<div class="cta-wrap"><a class="button" href="{bot_url}">Узнать свой Денежный аватар</a>
<span class="hero-note">Разбор проходит в Telegram-боте</span></div>{test_notice}
</div></section>

<section class="panel section" aria-labelledby="services-title"><h2 id="services-title">Услуги</h2>
<div class="product-grid">
<article class="product-card"><h3>Разбор Денежного аватара</h3><p class="price">{first_price} ₽</p>
<ul><li>подходящие профессии</li><li>комфортный формат работы</li>
<li>как лучше проявляться и подавать себя</li><li>денежная ловушка</li>
<li>денежный шаг уже сегодня</li></ul>
<a class="button" href="{bot_url}"><span>Получить разбор за <span class="nowrap">{first_price} ₽</span></span></a></article>
<article class="product-card"><h3>Полный разбор денег и реализации</h3><p class="price">990 ₽</p>
<ul><li>через что легче приходить к доходу</li><li>подходящие профессии и формат работы</li>
<li>денежные сценарии и ограничения</li><li>как проявляться, продавать и называть цену</li>
<li>персональный план действий</li></ul>
<a class="button" href="{bot_url}"><span>Получить полный разбор за <span class="nowrap">990 ₽</span></span></a></article>
</div></section>

<section class="panel section" aria-labelledby="steps-title"><h2 id="steps-title">Как это работает</h2>
<ol class="steps"><li>Перейти в Telegram-бот.</li><li>Ввести дату, точное время и место рождения.</li>
<li>Получить расчёт Денежного аватара.</li>
<li>При желании выбрать подробный платный разбор.</li></ol></section>

<section class="panel section" aria-labelledby="important-title"><h2 id="important-title">Важно</h2>
<p class="important">Разбор носит информационно-развлекательный характер и не является финансовой,
инвестиционной, медицинской или психологической консультацией. Результат не гарантирует увеличение дохода.</p>
</section>

<footer class="panel section footer"><div class="footer-grid"><div><h2>Исполнитель</h2>{operator}
<a class="telegram-link" href="{bot_url}">Telegram: @{bot_username}</a></div>
<nav aria-label="Юридические документы"><h3>Документы</h3><div class="legal-links">
<a href="/terms">Публичная оферта</a>
<a href="/privacy">Политика обработки персональных данных</a>
<a href="/consent">Согласие на обработку персональных данных</a>
</div></nav></div></footer>
</main></body></html>"""


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

    async def home(_: web.Request) -> web.Response:
        return web.Response(text=_home_page(settings), content_type="text/html")

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
            text=_page(
                "Политика в отношении обработки персональных данных", privacy_body(settings)
            ),
            content_type="text/html",
        )

    async def terms(_: web.Request) -> web.Response:
        return web.Response(
            text=_page(
                "Публичная оферта на оказание информационно-развлекательных услуг",
                terms_body(settings),
            ),
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
        callback_kind = "result2" if request.path.endswith("/result2") else "result"
        logger.info(
            "accepted signed Robokassa %s notification; newly_paid=%s",
            callback_kind,
            result.newly_paid,
        )
        if result.newly_paid and delivery:
            delivery.notify()
        return web.Response(text=f"OK{invoice_id}", content_type="text/plain")

    async def payment_success(_: web.Request) -> web.Response:
        raise web.HTTPFound(location=f"https://t.me/{settings.bot_username}")

    async def payment_fail(_: web.Request) -> web.Response:
        body = f"""<p>Robokassa не подтвердила оплату. Деньги не должны быть списаны.</p>
<p>Вернитесь в бот и попробуйте снова. Если списание отображается в банке, напишите
<a href="https://t.me/{html.escape(settings.support_username)}">@{html.escape(settings.support_username)}</a>
и укажите код заказа.</p>"""
        return web.Response(text=_page("Оплата не завершена", body), content_type="text/html")

    app.router.add_get("/", home)
    app.router.add_get("/healthz", health)
    app.router.add_get("/source", source)
    app.router.add_get("/privacy", privacy)
    app.router.add_get("/terms", terms)
    app.router.add_get("/consent", consent)
    app.router.add_route("*", "/payments/robokassa/result", payment_result)
    app.router.add_post("/payments/robokassa/result2", payment_result)
    app.router.add_route(
        "*",
        "/payments/robokassa/success/{invoice_id}/{amount_minor}/{token}",
        payment_success,
    )
    app.router.add_route("*", "/payments/robokassa/success", payment_success)
    app.router.add_route("*", "/payments/robokassa/fail", payment_fail)
    return app
