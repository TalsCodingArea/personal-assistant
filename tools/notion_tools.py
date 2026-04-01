import os
import json
from typing import Any, Dict, List, Optional
from pathlib import Path
from datetime import datetime, timedelta
import calendar
import re
import requests
from langchain_core.tools import tool
from notion_client import Client
from notion_client.errors import APIResponseError
import dotenv
from notion_config.loader import NotionConfigLoader
from services.notion_service import NotionService

dotenv.load_dotenv()
_loader = NotionConfigLoader()
_notion = NotionService()


@tool
def notion_query(database: str, query_kwargs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Query a Notion database by logical name.

    database: logical database name ("expenses" or "income")
    query_kwargs: kwargs passed to notion_client.databases.query, e.g.
      {
        "page_size": 5,
        "sorts": [{"property": "Date", "direction": "descending"}],
        "filter": {...}
      }

    Returns: raw Notion API response JSON.
    """
    cfg = _loader.get_database_config(database)
    return _notion.query_database(cfg["database_id"], query_kwargs)


@tool
def notion_create_page(database: str, properties: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a page in a Notion database by logical name.

    database: logical database name ("expenses" or "income")
    properties: Notion 'properties' object (formatted per Notion API)

    Returns: raw Notion API response JSON.
    """
    cfg = _loader.get_database_config(database)
    return _notion.create_page(cfg["database_id"], properties)


@tool
def get_finance_rules() -> Dict[str, Any]:
    """Return finance allocation rules from local config."""
    return _loader.get_finance_rules()


@tool
def get_database_schema(database: str) -> Dict[str, Any]:
    """Return the properties schema for a logical database name."""
    cfg = _loader.get_database_config(database)
    return cfg["properties"]



NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"


def _extract_notion_property_content(property_data: Dict[str, Any]) -> Any:
    """Extract the content value from a Notion property based on its type."""
    if "title" in property_data:
        return "".join([part["text"]["content"] for part in property_data["title"]])
    elif "rich_text" in property_data:
        return "".join([part["text"]["content"] for part in property_data["rich_text"]])
    elif "select" in property_data:
        return property_data["select"]["name"] if property_data["select"] else None
    elif "multi_select" in property_data:
        return [item["name"] for item in property_data["multi_select"]]
    elif "number" in property_data:
        return property_data["number"]
    elif "checkbox" in property_data:
        return property_data["checkbox"]
    elif "date" in property_data:
        date_info = property_data["date"]
        if date_info is None:
            return None
        start = date_info.get("start")
        end = date_info.get("end")
        if end:
            return {"start": start, "end": end}
        return start
    elif "url" in property_data:
        return property_data["url"]
    elif "email" in property_data:
        return property_data["email"]
    elif "phone_number" in property_data:
        return property_data["phone_number"]
    elif "files" in property_data:
        return [file.get("name") for file in property_data["files"]]
    elif "formula" in property_data:
        formula = property_data["formula"]
        if formula["type"] == "string":
            return formula.get("string")
        elif formula["type"] == "number":
            return formula.get("number")
        elif formula["type"] == "boolean":
            return formula.get("boolean")
        elif formula["type"] == "date":
            date_info = formula.get("date")
            if date_info is None:
                return None
            start = date_info.get("start")
            end = date_info.get("end")
            if end:
                return {"start": start, "end": end}
            return start
    else:
        return None

def _raw_notion_response_to_dict(propeties_names: List[str], response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert Notion API response to a list of simplified dicts with only specified properties."""
    results = []
    for page in response.get("results", []):
        page_data = {"id": page.get("id"), "url": page.get("url")}
        props = page.get("properties", {})
        for prop_name in propeties_names:
            page_data[prop_name] = _extract_notion_property_content(props.get(prop_name, {}))
        results.append(page_data)
    return results


def _build_notion_client() -> Client:
    """Create a Notion client from `NOTION_API_KEY`."""
    notion_api_key = os.getenv("NOTION_API_KEY")
    if not notion_api_key:
        raise ValueError("Missing NOTION_API_KEY environment variable.")
    return Client(auth=notion_api_key)


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _parse_iso_date_with_clamp(raw_date: str, field_name: str) -> datetime.date:
    """
    Parse YYYY-MM-DD and clamp out-of-range day values to the month's last valid day.
    Example: 2026-02-29 -> 2026-02-28
    """
    if not _is_non_empty_string(raw_date):
        raise ValueError(f"`{field_name}` must be a non-empty ISO date string (YYYY-MM-DD).")

    text = raw_date.strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if not match:
            raise ValueError(f"`{field_name}` must be an ISO date string (YYYY-MM-DD).")
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        if month < 1 or month > 12:
            raise ValueError(f"`{field_name}` has invalid month: {month}.")
        if day < 1:
            day = 1
        last_day = calendar.monthrange(year, month)[1]
        day = min(day, last_day)
        return datetime(year, month, day).date()


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


def notion_properties_from_receipt(receipt_json: Dict[str, object]) -> Dict[str, Dict[str, object]]:
    vendor = receipt_json.get("vendor")
    total_amount = receipt_json.get("total_amount")
    category = receipt_json.get("category")
    date = receipt_json.get("date")
    properties: Dict[str, Dict[str, object]] = {}

    if isinstance(vendor, str) and vendor.strip():
        properties["Description"] = {"type": "title", "content": vendor.strip()}
    if isinstance(total_amount, (int, float)):
        properties["Amount"] = {"type": "number", "content": float(total_amount)}
    if isinstance(category, str) and category.strip():
        if category.strip() == "Uncategorized":
            properties["Category"] = {"type": "multi_select", "content": ["Uncategorized"]}
        elif category.strip() == "Groceries":
            properties["Category"] = {"type": "multi_select", "content": ["Home 🏡"]}
            properties["Sub Category"] = {"type": "multi_select", "content": ["Groceries 🛒"]}
        elif category.strip() == "EV":
            properties["Category"] = {"type": "multi_select", "content": ["Car 🚗"]}
            properties["Sub Category"] = {"type": "multi_select", "content": ["Electric 🔋"]}
        elif category.strip() == "Bills":
            properties["Category"] = {"type": "multi_select", "content": ["Home 🏡"]}
            properties["Sub Category"] = {"type": "multi_select", "content": ["Bills 🧾"]}
    if isinstance(date, str):
        properties["Date"] = {"type": "date", "content": {"start": date}}
    properties["Tag"] = {"type": "multi_select", "content": ["Tal 👨🏻"]}
    properties["Type"] = {"type": "select", "content": "Need"}
    properties["Payment Method"] = {"type": "select", "content": "Credit"}
    return properties


@tool
def get_expenses_between_dates(start_date: str, end_date: str) -> Dict[str, Any]:
    """
    Fetches expenses from a Notion database between two dates and returns a structured summary.

    The return value contains:
    - period: the queried date range
    - total: pre-computed sum of all expense amounts (ILS)
    - count: number of expense records
    - by_category: total amount per category (use this for category comparisons)
    - by_subcategory: total amount per sub-category (use this for sub-category comparisons)
    - records: individual expense records, each with: date, description, amount, category,
               sub_category, and url (Notion page link — use this when the user asks for
               a link to a specific expense)

    Use the pre-computed totals (total, by_category, by_subcategory) directly.
    Do NOT re-sum the records yourself.

    Args:
        start_date: The start date in ISO format (YYYY-MM-DD).
        end_date: The end date in ISO format (YYYY-MM-DD).
    """
    start_dt = _parse_iso_date_with_clamp(start_date, "start_date")
    end_dt = _parse_iso_date_with_clamp(end_date, "end_date")

    if end_dt < start_dt:
        raise ValueError("`end_date` must be on or after `start_date`.")

    filter_dict = {
        "and": [
            {"property": "Date", "date": {"on_or_after": start_dt.isoformat()}},
            {"property": "Date", "date": {"before": (end_dt + timedelta(days=1)).isoformat()}},
            {"property": "Tag", "multi_select": {"contains": "Tal 👨🏻"}}
        ]
    }
    expenses_database_id = os.getenv("EXPENSES_DATABASE_ID")
    if not _is_non_empty_string(expenses_database_id):
        raise ValueError("Missing EXPENSES_DATABASE_ID environment variable.")

    raw = notion_get_database_pages.invoke({"database_id": expenses_database_id, "filter": filter_dict})
    rows = _raw_notion_response_to_dict(["Description", "Final", "Category", "Sub Category", "Date"], raw)

    records = []
    by_category: Dict[str, float] = {}
    by_subcategory: Dict[str, float] = {}
    total = 0.0

    for row in rows:
        amount = row.get("Final") or 0
        if not isinstance(amount, (int, float)):
            amount = 0.0
        total += amount

        categories = row.get("Category") or []
        if isinstance(categories, str):
            categories = [categories]
        subcategories = row.get("Sub Category") or []
        if isinstance(subcategories, str):
            subcategories = [subcategories]

        for cat in categories:
            if cat:
                by_category[cat] = round(by_category.get(cat, 0.0) + amount, 2)
        for sub in subcategories:
            if sub:
                by_subcategory[sub] = round(by_subcategory.get(sub, 0.0) + amount, 2)

        records.append({
            "date": row.get("Date"),
            "description": row.get("Description"),
            "amount": amount,
            "category": categories,
            "sub_category": subcategories,
            "url": row.get("url"),
        })

    print(f"Total expenses from {start_dt} to {end_dt}: {round(total, 2)}")
    return {
        "period": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "total": round(total, 2),
        "count": len(records),
        "by_category": by_category,
        "by_subcategory": by_subcategory,
        "records": records,
    }



@tool
def get_income_between_dates(start_date: str, end_date: str) -> Dict[str, Any]:
    """
    Fetches income from a Notion database between two dates.
    Use this tool to retrieve income records for a given date range.
    Args:
        start_date: The start date in ISO format (YYYY-MM-DD).
        end_date: The end date in ISO format (YYYY-MM-DD).
    Returns:     A dictionary containing the raw Notion API response with income data.
    """
    start_dt = _parse_iso_date_with_clamp(start_date, "start_date")
    end_dt = _parse_iso_date_with_clamp(end_date, "end_date")

    if end_dt < start_dt:
        raise ValueError("`end_date` must be on or after `start_date`.")

    # Inclusive date-range for day-level queries:
    # Date >= start_date and Date < (end_date + 1 day)
    filter_dict = {
        "and": [
            {
                "property": "Date",
                "date": {
                    "on_or_after": start_dt.isoformat(),
                }
            },
            {
                "property": "Date",
                "date": {
                    "before": (end_dt + timedelta(days=1)).isoformat(),
                }
            }
        ]
    }
    expenses_database_id = os.getenv("INCOME_DATABASE_ID")
    if not _is_non_empty_string(expenses_database_id):
        raise ValueError("Missing INCOME_DATABASE_ID environment variable.")
    expenses_data = notion_get_database_pages.invoke(
        {"database_id": expenses_database_id, "filter": filter_dict}
    )
    expenses_data = _raw_notion_response_to_dict(["Description", "Amount", "Category", "Sub Category", "Date"], expenses_data)
    return expenses_data



@tool
def get_last_expenses(n: int = 5) -> Dict[str, Any]:
    """
    Fetches the last n expenses from a Notion database.
    Use this tool to retrieve the most recent expense records.
    Args:
        n: The number of recent expenses to fetch (default is 5).
    Returns:     A dictionary containing the raw Notion API response with expenses data.
    """
    if not isinstance(n, int) or n < 1:
        raise ValueError("`n` must be a positive integer.")
    expenses_database_id = os.getenv("EXPENSES_DATABASE_ID")
    if not _is_non_empty_string(expenses_database_id):
        raise ValueError("Missing EXPENSES_DATABASE_ID environment variable.")
    expenses_data = notion_get_database_pages.invoke(
        {
            "database_id": expenses_database_id,
            "sorts": [{"property": "Date", "direction": "descending"}],
            "max_results": n,
            "filter": {
                "property": "Tag",
                "multi_select": {
                    "contains": "Tal 👨🏻"
                }
            }
        }
    )
    expenses_data = _raw_notion_response_to_dict(["Description", "Final", "Category", "Sub Category", "Date"], expenses_data)
    for expense in expenses_data:
        expense["Amount"] = expense.get("Final", 0)
        expense.pop("Final", None)
    return expenses_data

@tool
def get_movies_data_from_notion_database() -> Dict[str, Any]:
    """
    Fetches movie data from a Notion database.
    Use this tool to retrieve movie records for a given date range.
    Returns:     A dictionary containing the raw Notion API response with movies data.
    """
    movies_database_id = os.getenv("MOVIES_DATABASE_ID")
    if not _is_non_empty_string(movies_database_id):
        raise ValueError("Missing MOVIES_DATABASE_ID environment variable.")
    movies_data = notion_get_database_pages.invoke({"database_id": movies_database_id})
    movies_data = _raw_notion_response_to_dict(["Title", "Genre", "Rating", "Mood", "Last Watched"], movies_data)
    return movies_data


@tool
def update_movie_property(movie_page_id: str, properties: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Update properties of a movie page in Notion.
    Use this tool to update movie records with new information.
    Args:
        movie_page_id: The ID of the Notion page representing the movie.
        properties: A dictionary of properties to update.
    Returns:     A dictionary containing the raw Notion API response with the updated page data.
    """
    notion_client = _build_notion_client()

    try:
        updated_page = notion_client.pages.update(page_id=movie_page_id, properties=properties)
    except APIResponseError as exc:
        raise RuntimeError(f"Failed to update Notion page: {exc}") from exc

    return updated_page



@tool
def notion_get_database_pages(
    database_id: str,
    filter: Optional[Dict[str, Any]] = None,
    sorts: Optional[List[Dict[str, Any]]] = None,
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
    if not isinstance(max_results, int) or max_results < 1:
        raise ValueError("`max_results` must be a positive integer.")
    if filter is not None and not isinstance(filter, dict):
        raise ValueError("`filter` must be a dictionary when provided.")
    if sorts is not None and not isinstance(sorts, list):
        raise ValueError("`sorts` must be a list when provided.")

    notion_client = _build_notion_client()
    query: Dict[str, Any] = {}
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
            response = notion_client.databases.query(database_id=database_id, **{k: v for k, v in query.items() if v is not None})
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


_BUDGET_DATA_DIR = Path(__file__).parent.parent / "budget_data"
_SPENDING_HABITS_EMPTY = {"last_updated": None, "months_tracked": 0, "by_subcategory": {}}
_ADVISOR_HABITS_EMPTY = {"last_updated": None, "rules": []}


def _read_json_or_default(path: Path, default: dict) -> dict:
    """Read a JSON file, returning default if the file is missing or empty."""
    if not path.exists():
        return dict(default)
    try:
        content = path.read_text(encoding="utf-8").strip()
        return json.loads(content) if content else dict(default)
    except (json.JSONDecodeError, OSError):
        return dict(default)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@tool
def get_spending_habits() -> Dict[str, Any]:
    """
    Returns Tal's historical spending habits per subcategory, built from monthly expense data.

    Each subcategory entry contains:
    - avg: rolling average monthly spend (ILS)
    - min: lowest month on record
    - max: highest month on record
    - last: last month's spend

    Use this BEFORE any financial recap to understand what is normal for Tal.
    A subcategory spending close to its avg is NOT a problem — do not flag it.
    Only flag subcategories that deviate significantly (>20%) above their avg.
    If months_tracked is 0, no history exists yet — skip habit comparison entirely.
    """
    return _read_json_or_default(_BUDGET_DATA_DIR / "spending_habits.json", _SPENDING_HABITS_EMPTY)


@tool
def get_financial_advisor_habits() -> Dict[str, Any]:
    """
    Returns the financial rules and targets Tal has explicitly stated he wants to follow.

    These are personal commitments (e.g. "keep restaurants under 500 ILS/month").
    Use these as hard constraints when evaluating spending — flag any rule that is breached.
    If rules list is empty, skip advisor rule checks.
    """
    return _read_json_or_default(_BUDGET_DATA_DIR / "financial_advisor_habits.json", _ADVISOR_HABITS_EMPTY)


@tool
def update_financial_advisor_habit(rule: str) -> Dict[str, Any]:
    """
    Saves a new financial rule or target that Tal wants to follow.

    Call this whenever Tal states a spending intention or financial goal in conversation,
    such as:
    - "I want to keep restaurants under 500 ILS a month"
    - "From now on, save at least 15% of my income"
    - "Stop spending more than 200 ILS on takeout"

    Args:
        rule: A plain-text description of the financial rule or target.
    """
    if not _is_non_empty_string(rule):
        raise ValueError("`rule` must be a non-empty string.")

    path = _BUDGET_DATA_DIR / "financial_advisor_habits.json"
    habits = _read_json_or_default(path, _ADVISOR_HABITS_EMPTY)
    habits["rules"].append({"rule": rule.strip(), "added": datetime.now().strftime("%Y-%m-%d")})
    habits["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    _write_json(path, habits)
    return {"ok": True, "total_rules": len(habits["rules"])}
