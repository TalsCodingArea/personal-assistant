import os
from typing import Any, Dict, List, Optional
from pathlib import Path
import requests
from langchain_core.tools import tool
from notion_client import Client
from notion_client.errors import APIResponseError

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"


def _build_notion_client() -> Client:
    """Create a Notion client from `NOTION_API_KEY`."""
    notion_api_key = os.getenv("NOTION_API_KEY")
    if not notion_api_key:
        raise ValueError("Missing NOTION_API_KEY environment variable.")
    return Client(auth=notion_api_key)


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _format_rich_text(content: Any) -> List[Dict[str, Any]]:
    if not _is_non_empty_string(content):
        raise ValueError("Text content must be a non-empty string.")
    return [{"type": "text", "text": {"content": content}}]


def _normalize_page_properties(raw_properties: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Normalize a simplified property payload into Notion API property format.

    Expected input per property:
    {
      "Property Name": {"type": "<type>", "content": <value>}
    }
    """
    if not isinstance(raw_properties, dict) or not raw_properties:
        raise ValueError("`properties` must be a non-empty dictionary.")

    normalized: Dict[str, Dict[str, Any]] = {}
    for property_name, descriptor in raw_properties.items():
        if not _is_non_empty_string(property_name):
            raise ValueError("Each property name must be a non-empty string.")
        if not isinstance(descriptor, dict):
            raise ValueError(f"Property '{property_name}' must be an object.")

        prop_type = descriptor.get("type")
        content = descriptor.get("content")
        if not _is_non_empty_string(prop_type):
            raise ValueError(f"Property '{property_name}' must include a non-empty 'type'.")

        prop_type = prop_type.strip()
        if prop_type == "title":
            normalized[property_name] = {"title": _format_rich_text(content)}
        elif prop_type in ("text", "rich_text"):
            normalized[property_name] = {"rich_text": _format_rich_text(content)}
        elif prop_type == "select":
            if not _is_non_empty_string(content):
                raise ValueError(f"Property '{property_name}' select content must be a non-empty string.")
            normalized[property_name] = {"select": {"name": content}}
        elif prop_type == "multi_select":
            if not isinstance(content, list):
                raise ValueError(f"Property '{property_name}' multi_select content must be a list of strings.")
            names = [item for item in content if _is_non_empty_string(item)]
            normalized[property_name] = {"multi_select": [{"name": name} for name in names]}
        elif prop_type == "number":
            if not isinstance(content, (int, float)):
                raise ValueError(f"Property '{property_name}' number content must be int or float.")
            normalized[property_name] = {"number": content}
        elif prop_type == "checkbox":
            if not isinstance(content, bool):
                raise ValueError(f"Property '{property_name}' checkbox content must be boolean.")
            normalized[property_name] = {"checkbox": content}
        elif prop_type == "date":
            if isinstance(content, dict):
                if not _is_non_empty_string(content.get("start")):
                    raise ValueError(
                        f"Property '{property_name}' date dictionary must include a non-empty 'start'."
                    )
                date_value = {
                    "start": content["start"],
                    "end": content.get("end"),
                    "time_zone": content.get("time_zone"),
                }
            elif _is_non_empty_string(content):
                date_value = {"start": content}
            else:
                raise ValueError(
                    f"Property '{property_name}' date content must be ISO date string or date object."
                )
            normalized[property_name] = {"date": date_value}
        elif prop_type == "url":
            if not _is_non_empty_string(content):
                raise ValueError(f"Property '{property_name}' url content must be a non-empty string.")
            normalized[property_name] = {"url": content}
        elif prop_type == "email":
            if not _is_non_empty_string(content):
                raise ValueError(f"Property '{property_name}' email content must be a non-empty string.")
            normalized[property_name] = {"email": content}
        elif prop_type == "phone_number":
            if not _is_non_empty_string(content):
                raise ValueError(f"Property '{property_name}' phone_number content must be a non-empty string.")
            normalized[property_name] = {"phone_number": content}
        elif prop_type == "file":
            if not isinstance(content, dict):
                raise ValueError(
                    f"Property '{property_name}' file content must be an object with 'name' and 'url'."
                )
            file_name = content.get("name")
            file_url = content.get("url")
            if not _is_non_empty_string(file_name) or not _is_non_empty_string(file_url):
                raise ValueError(
                    f"Property '{property_name}' file content must include non-empty 'name' and 'url'."
                )
            normalized[property_name] = {
                "files": [
                    {
                        "name": file_name,
                        "type": "external",
                        "external": {"url": file_url},
                    }
                ]
            }
        else:
            raise ValueError(
                f"Unsupported property type '{prop_type}' for '{property_name}'. "
                "Supported types: title, text, rich_text, select, multi_select, number, "
                "checkbox, date, url, email, phone_number, file."
            )

    return normalized


@tool
def notion_create_database_page(
    database_id: str,
    properties: Dict[str, Dict[str, Any]],
    file_property_name: Optional[str] = None,
    file_upload_id: Optional[str] = None,
    file_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a new Notion page in a database from a simplified property schema.

    Use this tool when the agent needs to insert a new row/page into a Notion database.
    It accepts human-friendly property definitions and converts them into the Notion API format.

    Args:
        database_id: Target Notion database ID.
        properties: Dictionary keyed by property name. Each value must contain:
            - `type`: Notion-like property type.
            - `content`: Value for that property.
            Example:
            {
              "Description": {"type": "title", "content": "Coffee"},
              "Amount": {"type": "number", "content": 21.5},
              "Category": {"type": "select", "content": "Food"},
              "Date": {"type": "date", "content": "2026-02-19"}
            }
        file_property_name: Optional files property name to attach an uploaded Notion file.
        file_upload_id: Optional Notion `file_upload_id` created via `notion_create_file_upload`.
        file_name: Optional display name for the uploaded file in Notion.

    Returns:
        A dictionary with page metadata, including:
        - `ok`: success flag
        - `page_id`: created page id
        - `url`: page url
        - `created_time`: page creation timestamp

    Raises:
        ValueError: On invalid input.
        RuntimeError: On Notion API failures.
    """
    if not _is_non_empty_string(database_id):
        raise ValueError("`database_id` must be a non-empty string.")

    notion_client = _build_notion_client()
    notion_properties = _normalize_page_properties(properties)

    try:
        page = notion_client.pages.create(parent={"database_id": database_id}, properties=notion_properties)
    except APIResponseError as exc:
        raise RuntimeError(f"Failed to create Notion page: {exc}") from exc

    if file_property_name or file_upload_id or file_name:
        if not all(
            [
                _is_non_empty_string(file_property_name),
                _is_non_empty_string(file_upload_id),
                _is_non_empty_string(file_name),
            ]
        ):
            raise ValueError(
                "To attach an uploaded file, provide all of: `file_property_name`, `file_upload_id`, `file_name`."
            )
        try:
            notion_client.pages.update(
                page_id=page["id"],
                properties={
                    file_property_name: {
                        "files": [
                            {
                                "type": "file_upload",
                                "file_upload": {"id": file_upload_id},
                                "name": file_name,
                            }
                        ]
                    }
                },
            )
        except APIResponseError as exc:
            raise RuntimeError(f"Page created, but attaching file failed: {exc}") from exc

    return {
        "ok": True,
        "page_id": page["id"],
        "url": page.get("url"),
        "created_time": page.get("created_time"),
    }


@tool
def notion_create_file_upload(mode: str = "single_part") -> Dict[str, Any]:
    """
    Create a Notion file-upload object and return its upload ID.

    Use this before attaching a hosted file to a page property with type `files`.
    Typical flow:
    1. Create file upload via this tool (get `file_upload_id`).
    2. Send bytes to Notion upload endpoint: `/v1/file_uploads/{id}/send`.
    3. Reference the upload id in a page `files` property (`type: file_upload`).

    Args:
        mode: Upload mode accepted by Notion. Default is `single_part`.

    Returns:
        Dictionary containing:
        - `ok`: success flag
        - `file_upload_id`: created upload id
        - `status`: upload status from Notion response (if present)
        - `raw`: full API response for advanced workflows

    Raises:
        ValueError: On invalid input or missing API key.
        RuntimeError: On Notion API request failures.
    """
    if not _is_non_empty_string(mode):
        raise ValueError("`mode` must be a non-empty string.")

    notion_api_key = os.getenv("NOTION_API_KEY")
    if not notion_api_key:
        raise ValueError("Missing NOTION_API_KEY environment variable.")

    try:
        response = requests.post(
            f"{NOTION_API_BASE}/file_uploads",
            headers={
                "Authorization": f"Bearer {notion_api_key}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            json={"mode": mode},
            timeout=60,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to create Notion file upload: {exc}") from exc

    payload = response.json()
    upload_id = payload.get("id")
    if not _is_non_empty_string(upload_id):
        raise RuntimeError("Notion file upload was created but response does not include an `id`.")

    return {
        "ok": True,
        "file_upload_id": upload_id,
        "status": payload.get("status"),
        "raw": payload,
    }


@tool
def attach_file_to_notion_file_upload(file_upload_id: str, file_path: str, file_name: str = None) -> Dict[str, Any]:
    """
    Send file bytes to Notion's file upload endpoint to complete the upload process.

    Args:
        file_upload_id: The ID of the Notion file upload object.
        file_path: The path to the file to be uploaded.
        file_name: Optional name for the file in Notion.

    Returns:
        Dictionary containing:
        - `ok`: success flag
        - `status`: upload status from Notion response (if present)
        - `raw`: full API response for advanced workflows
    Raises:
        ValueError: On invalid input or missing API key.
        RuntimeError: On Notion API request failures.
    """
    if not _is_non_empty_string(file_upload_id):
        raise ValueError("`file_upload_id` must be a non-empty string.")
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("`file_path` must be a non-empty string.")

    file_bytes = Path(file_path).read_bytes()
    file_name = file_name or Path(file_path).name

    notion_api_key = os.getenv("NOTION_API_KEY")
    if not notion_api_key:
        raise ValueError("Missing NOTION_API_KEY environment variable.")
    try:
        response = requests.post(
            f"{NOTION_API_BASE}/file_uploads/{file_upload_id}/send",
            headers={
                "Authorization": f"Bearer {notion_api_key}",
                "Notion-Version": NOTION_VERSION,
            },
            files = {
                "file": (file_name, file_bytes, "application/pdf")
            },
            timeout=120,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to send file bytes to Notion: {exc}") from exc


@tool
def notion_get_database_pages(
    database_id: str,
    filter: Optional[Dict[str, Any]] = None,
    sorts: Optional[List[Dict[str, Any]]] = None,
    page_size: int = 100,
    max_results: int = 200,
) -> Dict[str, Any]:
    """
    Retrieve pages from a Notion database with optional filter/sort and safe pagination.

    Use this tool when the agent needs to read records from Notion.
    It automatically paginates until there are no more pages or `max_results` is reached.

    Args:
        database_id: Notion database ID to query.
        filter: Optional Notion database filter object.
        sorts: Optional list of Notion sort objects.
        page_size: Notion page size per request (1-100).
        max_results: Upper bound on total records returned.

    Returns:
        Dictionary containing:
        - `ok`: success flag
        - `count`: number of returned pages
        - `results`: list of raw Notion page objects

    Raises:
        ValueError: On invalid arguments.
        RuntimeError: On Notion API failures.
    """
    if not _is_non_empty_string(database_id):
        raise ValueError("`database_id` must be a non-empty string.")
    if not isinstance(page_size, int) or not (1 <= page_size <= 100):
        raise ValueError("`page_size` must be an integer in the range 1..100.")
    if not isinstance(max_results, int) or max_results < 1:
        raise ValueError("`max_results` must be a positive integer.")
    if filter is not None and not isinstance(filter, dict):
        raise ValueError("`filter` must be a dictionary when provided.")
    if sorts is not None and not isinstance(sorts, list):
        raise ValueError("`sorts` must be a list when provided.")

    notion_client = _build_notion_client()
    query: Dict[str, Any] = {"page_size": page_size}
    if filter:
        query["filter"] = filter
    if sorts:
        query["sorts"] = sorts

    results: List[Dict[str, Any]] = []
    start_cursor: Optional[str] = None

    try:
        while len(results) < max_results:
            if start_cursor:
                query["start_cursor"] = start_cursor
            response = notion_client.databases.query(database_id=database_id, **query)
            batch = response.get("results", [])
            results.extend(batch)

            if not response.get("has_more"):
                break
            start_cursor = response.get("next_cursor")
            if not start_cursor:
                break
    except APIResponseError as exc:
        raise RuntimeError(f"Failed to query Notion database: {exc}") from exc

    return {
        "ok": True,
        "count": min(len(results), max_results),
        "results": results[:max_results],
    }