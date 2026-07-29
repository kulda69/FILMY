"""Mala markdown render a sanitizacni vrstva pro textove bloky."""

from __future__ import annotations

import html
import re

from markupsafe import Markup


_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")


def render_user_markdown(value: str | None) -> Markup:
    """Render a safe, small Markdown subset for local user notes.

    The app only needs lightweight formatting for personal ratings, not a full
    Markdown engine. Raw HTML is always escaped; supported syntax is paragraphs,
    line breaks, bullet/numbered lists, links, bold, italic and inline code.
    """

    text = (value or "").strip()
    if not text:
        return Markup("")

    blocks: list[str] = []
    paragraph: list[str] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            _flush_paragraph(blocks, paragraph)
            index += 1
            continue

        unordered = re.match(r"^\s*[-*]\s+(.+)$", line)
        if unordered:
            _flush_paragraph(blocks, paragraph)
            items: list[str] = []
            while index < len(lines):
                match = re.match(r"^\s*[-*]\s+(.+)$", lines[index])
                if not match:
                    break
                items.append(f"<li>{_render_inline(match.group(1))}</li>")
                index += 1
            blocks.append("<ul class=\"mb-0 ps-3\">" + "".join(items) + "</ul>")
            continue

        ordered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if ordered:
            _flush_paragraph(blocks, paragraph)
            items = []
            while index < len(lines):
                match = re.match(r"^\s*\d+[.)]\s+(.+)$", lines[index])
                if not match:
                    break
                items.append(f"<li>{_render_inline(match.group(1))}</li>")
                index += 1
            blocks.append("<ol class=\"mb-0 ps-3\">" + "".join(items) + "</ol>")
            continue

        heading = re.match(r"^\s{0,3}#{1,4}\s+(.+)$", line)
        if heading:
            _flush_paragraph(blocks, paragraph)
            blocks.append(f"<div class=\"fw-semibold mb-2\">{_render_inline(heading.group(1))}</div>")
            index += 1
            continue

        paragraph.append(line.strip())
        index += 1

    _flush_paragraph(blocks, paragraph)
    return Markup("\n".join(blocks))


def _flush_paragraph(blocks: list[str], paragraph: list[str]) -> None:
    """Uzavri nasbirane radky jako jeden HTML odstavec."""

    if not paragraph:
        return
    rendered_lines = [_render_inline(line) for line in paragraph]
    blocks.append("<p class=\"mb-0\">" + "<br>".join(rendered_lines) + "</p>")
    paragraph.clear()


def _render_inline(value: str) -> str:
    """Vyrenderuj inline markdown vcetne code segmentu mezi backticky."""

    parts = value.split("`")
    rendered: list[str] = []
    for index, part in enumerate(parts):
        escaped = html.escape(part, quote=True)
        if index % 2 == 1:
            rendered.append(f"<code>{escaped}</code>")
        else:
            rendered.append(_render_inline_without_code(escaped))
    return "".join(rendered)


def _render_inline_without_code(value: str) -> str:
    """Vyrenderuj odkazy, tucne a kurzivu v uz escapovanem textu."""

    value = _LINK_RE.sub(r'<a class="link-light" href="\2" rel="noreferrer">\1</a>', value)
    value = _BOLD_RE.sub(r"<strong>\1</strong>", value)
    value = _ITALIC_RE.sub(r"<em>\1</em>", value)
    return value
