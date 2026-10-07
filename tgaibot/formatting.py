"""Markdown tokens -> Telegram entities without an intermediate HTML parser."""

import re
from copy import copy

from markdown_it import MarkdownIt
from telethon.helpers import add_surrogate, del_surrogate, within_surrogate
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityItalic,
    MessageEntityPre,
    MessageEntitySpoiler,
    MessageEntityStrike,
    MessageEntityTextUrl,
)


def _spoiler(state, silent):
    start = state.pos
    if not state.src.startswith("||", start):
        return False
    cursor, end = start + 2, -1
    while cursor < state.posMax:
        if state.src[cursor] == "\\":
            cursor += 2
            continue
        if state.src[cursor] == "`":
            marker = re.match(r"`+", state.src[cursor:]).group()
            closing = re.search(
                r"(?<!`)" + marker + r"(?!`)", state.src[cursor + len(marker) : state.posMax]
            )
            if closing:
                cursor += len(marker) + closing.end()
                continue
            cursor += len(marker)
            continue
        if state.src.startswith("||", cursor):
            end = cursor
            break
        cursor += 1
    if end <= start + 2:
        return False
    if not silent:
        token = state.push("spoiler", "", 0)
        token.children = []
        state.md.inline.parse(state.src[start + 2 : end], state.md, state.env, token.children)
    state.pos = end + 2
    return True


MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable(["strikethrough", "table"])
MARKDOWN.inline.add_terminator_char("|")
MARKDOWN.inline.ruler.before("emphasis", "spoiler", _spoiler)
MAX_ENTITIES = 90
STYLE_TYPES = {"strong": MessageEntityBold, "em": MessageEntityItalic, "s": MessageEntityStrike}


class _Builder:
    def __init__(self):
        self.parts = []
        self.length = 0
        self.entities = []

    def append(self, text):
        self.parts.append(text)
        self.length += len(add_surrogate(text))

    def mark(self, entity_type, start, **kwargs):
        if self.length > start:
            self.entities.append(entity_type(offset=start, length=self.length - start, **kwargs))

    def inline(self, tokens):
        stack = []
        for token in tokens or []:
            kind = token.type
            if kind in {"text", "text_special", "html_inline"}:
                self.append(token.content)
            elif kind in {"softbreak", "hardbreak"}:
                self.append("\n")
            elif kind == "code_inline":
                start = self.length
                self.append(token.content)
                self.mark(MessageEntityCode, start)
            elif kind == "spoiler":
                start = self.length
                self.inline(token.children)
                self.mark(MessageEntitySpoiler, start)
            elif kind == "image":
                start = self.length
                self.append(token.content or "Изображение")
                url = token.attrGet("src")
                if url:
                    self.mark(MessageEntityTextUrl, start, url=url)
            elif kind.endswith("_open"):
                name = kind.removesuffix("_open")
                stack.append((name, self.length, token.attrGet("href")))
            elif kind.endswith("_close") and stack:
                name, start, url = stack.pop()
                if name == "link" and url:
                    self.mark(MessageEntityTextUrl, start, url=url)
                elif name in STYLE_TYPES:
                    self.mark(STYLE_TYPES[name], start)


def _table(builder, tokens):
    """Telegram has no native tables: readable labelled rows preserve cell content."""
    rows, cells = [], []
    for token in tokens:
        if token.type == "inline":
            cells.append(token.children)
        elif token.type == "tr_close":
            rows.append(cells)
            cells = []
    if not rows:
        return
    headers = []
    for cell in rows[0]:
        label = _Builder()
        label.inline(cell)
        headers.append("".join(label.parts))
    if len(rows) == 1:
        builder.append(" · ".join(headers) + "\n\n")
    for row in rows[1:]:
        for i, cell in enumerate(row):
            builder.append("• " if i == 0 else "  ")
            start = builder.length
            builder.append((headers[i] if i < len(headers) else str(i + 1)) + ":")
            builder.mark(MessageEntityBold, start)
            builder.append(" ")
            builder.inline(cell)
            builder.append("\n")
        builder.append("\n")


def _normalize(text, entities):
    """Keep legal nesting: code/pre cannot overlap other entities; spoilers stay hidden."""
    encoded = add_surrogate(text)
    protected = [e for e in entities if isinstance(e, (MessageEntitySpoiler, MessageEntityTextUrl))]
    opaque = [
        e
        for e in entities
        if isinstance(e, (MessageEntityCode, MessageEntityPre))
        and not any(
            e.offset < p.offset + p.length and p.offset < e.offset + e.length for p in protected
        )
    ]
    result, seen = [], set()
    for index, entity in enumerate(entities):
        if isinstance(entity, MessageEntityTextUrl) and any(
            isinstance(outer, MessageEntityTextUrl)
            and outer.offset <= entity.offset
            and outer.offset + outer.length >= entity.offset + entity.length
            and (outer.length > entity.length or other_index > index)
            for other_index, outer in enumerate(entities)
        ):
            continue
        if isinstance(entity, (MessageEntityCode, MessageEntityPre)) and entity not in opaque:
            continue
        ranges = [(entity.offset, entity.offset + entity.length)]
        if entity not in opaque:
            for code in opaque:
                left, right = code.offset, code.offset + code.length
                ranges = [
                    (a, b)
                    for start, end in ranges
                    for a, b in ((start, min(end, left)), (max(start, right), end))
                    if a < b
                ]
        for start, end in ranges:
            end = min(end, len(encoded))
            end -= len(encoded[start:end]) - len(encoded[start:end].rstrip())
            if end <= start:
                continue
            clone = copy(entity)
            clone.offset, clone.length = start, end - start
            identity = (
                type(clone),
                start,
                end,
                getattr(clone, "url", None),
                getattr(clone, "language", None),
            )
            if identity not in seen:
                seen.add(identity)
                result.append(clone)
    return sorted(result, key=lambda e: (e.offset, -e.length))


def render(text):
    builder = _Builder()
    lists, heading_starts = [], []
    quote_depth, quote_start = 0, 0
    tokens = MARKDOWN.parse(text)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        kind = token.type
        if kind == "table_open":
            end = next(i for i in range(index + 1, len(tokens)) if tokens[i].type == "table_close")
            _table(builder, tokens[index + 1 : end])
            index = end
        elif kind == "inline":
            children = token.children or []
            if lists and children and children[0].type == "text":
                first = copy(children[0])
                first.content = re.sub(
                    r"^\[([ xX])\]\s+", lambda m: "☐ " if m[1] == " " else "☑ ", first.content
                )
                children = [first, *children[1:]]
            builder.inline(children)
        elif kind in {"fence", "code_block"}:
            start = builder.length
            builder.append(token.content)
            builder.mark(MessageEntityPre, start, language=token.info.strip().split(" ")[0][:30])
            builder.append("\n")
        elif kind == "heading_open":
            heading_starts.append(builder.length)
        elif kind == "heading_close":
            builder.mark(MessageEntityBold, heading_starts.pop())
            builder.append("\n\n")
        elif kind == "paragraph_close":
            builder.append("\n" if lists else "\n\n")
        elif kind == "bullet_list_open":
            lists.append(None)
        elif kind == "ordered_list_open":
            lists.append(int(token.attrGet("start") or 1))
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
            builder.append("\n")
        elif kind == "list_item_open":
            builder.append(
                "  " * (len(lists) - 1) + ("• " if lists[-1] is None else f"{lists[-1]}. ")
            )
            if lists[-1] is not None:
                lists[-1] += 1
        elif kind == "blockquote_open":
            if quote_depth == 0:
                quote_start = builder.length
            quote_depth += 1
        elif kind == "blockquote_close":
            quote_depth -= 1
            if quote_depth == 0:
                builder.mark(MessageEntityBlockquote, quote_start)
            builder.append("\n")
        elif kind == "hr":
            builder.append("────────\n")
        index += 1
    plain = "".join(builder.parts).rstrip()
    return plain, _normalize(plain, builder.entities)


def _clip(entities, start, end, prefix_size, encoded):
    spans = []
    for entity in entities:
        left = max(entity.offset, start)
        right = min(entity.offset + entity.length, end)
        right -= len(encoded[left:right]) - len(encoded[left:right].rstrip())
        if left < right:
            part = copy(entity)
            part.offset = left - start + prefix_size
            part.length = right - left
            spans.append(part)
    return spans


def formatted_chunks(text, prefix="", limit=3500):
    prefix_size = len(add_surrogate(prefix))
    if limit > 4096 or limit - prefix_size < 2:
        raise ValueError("Chunk limit must fit prefix and Unicode text, and be <=4096")
    try:
        plain, entities = render(text)
    except Exception:
        plain, entities = text, []
    encoded = add_surrogate(plain)
    # One UTF-16 unit replaces one unit, so Telegram entity offsets stay valid.
    protected = [
        e
        for e in entities
        if isinstance(
            e, (MessageEntityCode, MessageEntityPre, MessageEntityBlockquote, MessageEntityTextUrl)
        )
    ]
    encoded = "".join(
        "-"
        if char in "—–" and not any(e.offset <= i < e.offset + e.length for e in protected)
        else char
        for i, char in enumerate(encoded)
    )
    start = 0
    while start < len(encoded):
        end = min(len(encoded), start + limit - prefix_size)
        if within_surrogate(encoded, end):
            end -= 1
        if end < len(encoded):
            newline = encoded.rfind("\n", start + (end - start) // 2, end)
            if newline > start:
                end = newline + 1
        spans = _clip(entities, start, end, prefix_size, encoded)
        if len(spans) > MAX_ENTITIES:
            boundary = spans[MAX_ENTITIES].offset - prefix_size + start
            if boundary > start:
                end = boundary
                spans = _clip(entities, start, end, prefix_size, encoded)
        yield prefix + del_surrogate(encoded[start:end]), spans[:MAX_ENTITIES]
        start = end


def spoken_markdown(text):
    """Do not read Markdown punctuation, code listings or hidden spoilers aloud."""
    # Be forgiving of model output with a fence starting mid-sentence.
    text = re.sub(
        r"(`{3,}|~{3,})[^\n]*\n.*?\1",
        "Фрагмент кода приведён в текстовом ответе.",
        text,
        flags=re.S,
    )
    plain, entities = render(text)
    encoded = add_surrogate(plain)
    replacements = []
    for entity in entities:
        if isinstance(entity, MessageEntityPre):
            replacements.append(
                (
                    entity.offset,
                    entity.offset + entity.length,
                    "Фрагмент кода приведён в текстовом ответе.",
                )
            )
        elif isinstance(entity, MessageEntitySpoiler):
            replacements.append(
                (
                    entity.offset,
                    entity.offset + entity.length,
                    "Скрытый фрагмент можно открыть в текстовом ответе.",
                )
            )
    for start, end, replacement in sorted(replacements, reverse=True):
        encoded = encoded[:start] + replacement + encoded[end:]
    return del_surrogate(encoded).strip()


def plain_fallback(text, entities):
    """Entity rejection must never turn a hidden spoiler into visible plaintext."""
    encoded = add_surrogate(text)
    for entity in sorted(entities, key=lambda e: e.offset, reverse=True):
        if isinstance(entity, MessageEntitySpoiler):
            encoded = (
                encoded[: entity.offset]
                + "[Скрытый фрагмент]"
                + encoded[entity.offset + entity.length :]
            )
    return del_surrogate(encoded)
