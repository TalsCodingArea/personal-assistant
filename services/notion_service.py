import os
from dotenv import load_dotenv
from notion_client import AsyncClient

load_dotenv()

class NotionService:
    def __init__(self):
        token = os.getenv("NOTION_API_KEY")
        if not token:
            raise ValueError("Missing NOTION_API_KEY in .env")

        timeout_ms = int(os.getenv("NOTION_TIMEOUT_MS", "15000"))  # 15s default

        # notion_client supports options including timeout_ms
        self.client = AsyncClient(auth=token, timeout_ms=timeout_ms)

    async def query_database(self, database_id: str, query_kwargs: dict | None = None) -> dict:
        query_kwargs = query_kwargs or {}
        return await self.client.databases.query(database_id=database_id, **query_kwargs)

    async def create_page(self, parent_database_id: str, properties: dict) -> dict:
        return await self.client.pages.create(
            parent={"database_id": parent_database_id},
            properties=properties,
        )