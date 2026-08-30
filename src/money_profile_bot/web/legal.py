from __future__ import annotations

import html
import re

from money_profile_bot.config import Settings

_DOCUMENT_FILES = {
    "privacy": "privacy_final.md",
    "terms": "terms_final.md",
    "consent": "consent_final.md",
}
_STRONG_RE = re.compile(r"\*\*(.+?)\*\*")
_URL_RE = re.compile(r"https://[^\s<]+")


def _inline(value: str) -> str:
    escaped = html.escape(value)
    with_strong = _STRONG_RE.sub(r"<strong>\1</strong>", escaped)
    return _URL_RE.sub(
        lambda match: f'<a href="{match.group(0)}">{match.group(0)}</a>',
        with_strong,
    )


def _render_markdown(source: str) -> str:
    rendered: list[str] = []
    paragraph: list[tuple[str, bool]] = []
    list_items: list[str] = []
    skipped_title = False

    def flush_paragraph() -> None:
        if not paragraph:
            return
        chunks: list[str] = []
        for index, (line, _) in enumerate(paragraph):
            if index:
                chunks.append("<br>" if paragraph[index - 1][1] else " ")
            chunks.append(_inline(line))
        rendered.append(f"<p>{''.join(chunks)}</p>")
        paragraph.clear()

    def flush_list() -> None:
        if not list_items:
            return
        items = "".join(f"<li>{_inline(item)}</li>" for item in list_items)
        rendered.append(f"<ul>{items}</ul>")
        list_items.clear()

    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_list()
            continue
        if line.startswith("# ") and not skipped_title:
            flush_paragraph()
            flush_list()
            skipped_title = True
            continue
        if line.startswith("### "):
            flush_paragraph()
            flush_list()
            rendered.append(f"<h3>{_inline(line[4:])}</h3>")
            continue
        if line.startswith("## "):
            flush_paragraph()
            flush_list()
            rendered.append(f"<h2>{_inline(line[3:])}</h2>")
            continue
        if line.startswith("— "):
            flush_paragraph()
            list_items.append(line[2:].removesuffix(";"))
            continue
        flush_list()
        paragraph.append((line, raw_line.endswith("  ")))

    flush_paragraph()
    flush_list()
    return "\n".join(rendered)


def _document_body(settings: Settings, document: str) -> str:
    filename = _DOCUMENT_FILES[document]
    path = settings.legal_documents_directory / filename
    source = path.read_text(encoding="utf-8")
    return _render_markdown(source)


def _performer_source(settings: Settings) -> str:
    filename = _DOCUMENT_FILES["terms"]
    path = settings.legal_documents_directory / filename
    lines = path.read_text(encoding="utf-8").splitlines()
    section: list[str] = []
    in_section = False
    for line in lines:
        if line.startswith("## "):
            if in_section:
                break
            in_section = line.endswith("Реквизиты Исполнителя")
            continue
        if in_section:
            section.append(line)
    if not section:
        raise ValueError("performer details are missing from the terms document")
    return "\n".join(section)


def performer_body(settings: Settings) -> str:
    return _render_markdown(_performer_source(settings))


def performer_bot_username(settings: Settings) -> str:
    match = re.search(r"Telegram-бот:\s*\*\*@([A-Za-z0-9_]+)\*\*", _performer_source(settings))
    if not match:
        raise ValueError("public Telegram bot is missing from the performer details")
    return match.group(1)


def privacy_body(settings: Settings) -> str:
    return _document_body(settings, "privacy")


def terms_body(settings: Settings) -> str:
    return _document_body(settings, "terms")


def consent_body(settings: Settings) -> str:
    return _document_body(settings, "consent")
