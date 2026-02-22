from typing import Any
from telegram.helpers import escape_markdown


def markdown_v2_safe(text: Any) -> str:
    """Return text escaped for Telegram MarkdownV2 parsing."""
    if text is None:
        return ""
    return escape_markdown(str(text), version=2)

