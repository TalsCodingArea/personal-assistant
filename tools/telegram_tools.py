import re
from typing import Any
from telegram.helpers import escape_markdown


_MDV2_ENTITY_RE = re.compile(
    r"(?P<pre>```[\s\S]*?```)"
    r"|(?P<code>`[^`\n]+`)"
    r"|(?P<link>\[[^\]\n]+\]\([^)]+\))"
    r"|(?P<underline>__(?:[^_\n]|_(?!_))+__)"
    r"|(?P<bold>\*(?:[^*\n]|\\\*)+\*)"
    r"|(?P<italic>_(?:[^_\n]|\\_)+_)"
    r"|(?P<strike>~(?:[^~\n]|\\~)+~)"
    r"|(?P<spoiler>\|\|(?:[^|\n]|\\\|)+\|\|)"
)


def _sanitize_preserving_markdown(text: str) -> str:
    """Escape MarkdownV2-sensitive chars while preserving common style entities."""
    result: list[str] = []
    cursor = 0

    for match in _MDV2_ENTITY_RE.finditer(text):
        start, end = match.span()
        if start > cursor:
            result.append(escape_markdown(text[cursor:start], version=2))
        token = match.group(0)
        kind = match.lastgroup or ""

        if kind == "pre":
            result.append("```" + escape_markdown(token[3:-3], version=2, entity_type="pre") + "```")
        elif kind == "code":
            result.append("`" + escape_markdown(token[1:-1], version=2, entity_type="code") + "`")
        elif kind == "link":
            label, url = token[1:].split("](", 1)
            result.append(
                f"[{_sanitize_preserving_markdown(label)}]"
                f"({escape_markdown(url[:-1], version=2, entity_type='text_link')})"
            )
        else:
            delimiter = {
                "underline": "__",
                "bold": "*",
                "italic": "_",
                "strike": "~",
                "spoiler": "||",
            }[kind]
            inner = token[len(delimiter): -len(delimiter)]
            if inner.strip():
                result.append(f"{delimiter}{_sanitize_preserving_markdown(inner)}{delimiter}")
            else:
                result.append(escape_markdown(token, version=2))

        cursor = end

    if cursor < len(text):
        result.append(escape_markdown(text[cursor:], version=2))
    return "".join(result)


def markdown_v2_safe(text: Any, preserve_formatting: bool = False) -> str:
    """Return text sanitized for Telegram MarkdownV2 parsing.

    If ``preserve_formatting`` is True, supported MarkdownV2 entities (bold,
    italic, underline, strikethrough, spoiler, inline code, fenced code blocks,
    and links) are preserved while unsafe characters are escaped.
    """
    if text is None:
        return ""
    value = str(text)
    if not preserve_formatting:
        return escape_markdown(value, version=2)
    return _sanitize_preserving_markdown(value)
