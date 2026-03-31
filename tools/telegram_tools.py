import re
from typing import Any
from telegram.helpers import escape_markdown
from langchain_core.callbacks import AsyncCallbackHandler


_TOOL_STATUS: dict[str, str] = {
    "get_expenses_between_dates":        "🔍 Fetching expenses...",
    "get_income_between_dates":          "🔍 Fetching income...",
    "get_last_expenses":                 "🔍 Fetching recent expenses...",
    "get_finance_rules":                 "📋 Loading finance rules...",
    "get_database_schema":               "📋 Loading database schema...",
    "get_movies_data_from_notion_database": "🎬 Fetching movies...",
    "create_idea_in_notion":             "💡 Saving idea to Notion...",
    "get_exchange_rates":                "💱 Fetching exchange rates...",
    "get_tase_stock_quote":              "📈 Fetching stock quote...",
    "get_tase_index":                    "📊 Fetching market index...",
    "web_search":                        "🌐 Searching the web...",
}
_DEFAULT_TOOL_STATUS = "⚙️ Working on it..."


class TelegramStatusCallback(AsyncCallbackHandler):
    """Edits a Telegram status message as the agent invokes tools."""

    def __init__(self, bot, chat_id: int, message_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._current_text = ""

    async def _edit(self, text: str) -> None:
        if text == self._current_text:
            return
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text,
            )
            self._current_text = text
        except Exception:
            pass

    async def on_tool_start(self, serialized: dict, input_str: str, **kwargs) -> None:
        tool_name = serialized.get("name", "")
        await self._edit(_TOOL_STATUS.get(tool_name, _DEFAULT_TOOL_STATUS))

    async def on_tool_end(self, output: str, **kwargs) -> None:
        await self._edit("⚙️ Analysing results...")


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


def _convert_unsupported_markdown(text: str) -> str:
    """Convert markdown syntax Telegram doesn't support into MarkdownV2-compatible equivalents."""
    lines = []
    for line in text.splitlines():
        # Convert ## or ### headings → *bold*
        heading = re.match(r"^#{1,6}\s+(.+)", line)
        if heading:
            lines.append(f"*{heading.group(1).strip()}*")
            continue
        # Strip horizontal rules
        if re.match(r"^(\-{3,}|\*{3,}|_{3,})\s*$", line):
            continue
        lines.append(line)
    return "\n".join(lines)


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
    value = _convert_unsupported_markdown(str(text))
    if not preserve_formatting:
        return escape_markdown(value, version=2)
    return _sanitize_preserving_markdown(value)
