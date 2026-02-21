from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List

from langchain_core.messages import BaseMessage
from langchain_core.chat_history import InMemoryChatMessageHistory


@dataclass
class MemoryStore:
    """
    Simple in-memory store keyed by an external session id (e.g., Telegram chat_id).
    Later we can swap to Redis/SQLite without changing agent code if needed.
    """
    _store: Dict[str, InMemoryChatMessageHistory]

    def __init__(self):
        self._store = {}

    def get_history(self, session_id: str) -> InMemoryChatMessageHistory:
        if session_id not in self._store:
            self._store[session_id] = InMemoryChatMessageHistory()
        return self._store[session_id]