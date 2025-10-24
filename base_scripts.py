from openai import OpenAI
import requests
from notion_client import Client
import json
import io
import mimetypes
import requests
from urllib.parse import urlparse
import re



def create_notion_page(notion_client: Client, database_id: str, props: dict):
    """Create a new page in a Notion database with the given properties.
     Args:
        notion_client (Client): An instance of the Notion client.
        database_id (str): The ID of the Notion database where the page will be created.
        props (dict): A dictionary of properties to set on the new page.
    """

    for content, prop in props.items():
        if prop["type"] == "title":
            props[content] = {
                "title": [{"type": "text", "text": {"content": prop["content"]}}]
            }
        if prop["type"] == "text":
            props[content] = {
                "rich_text": [{"type": "text", "text": {"content": prop["content"]}}]
            }
        if prop["type"] == "select":
            props[content] = {
                "select": {"name": prop["content"]}
            }
        if prop["type"] == "multi_select":
            props[content] = {
                "multi_select": [{"name": tag} for tag in prop["content"]]
            }
        if prop["type"] == "number":
            props[content] = {
                "number": prop["content"]
            }
        if prop["type"] == "checkbox":
            props[content] = {
                "checkbox": prop["content"]
            }
        if prop["type"] == "date":
            props[content] = {
                "date": {"start": prop["content"]}
            }
        if prop["type"] == "file":
            props[content] = {
                "files": [
                    {
                        "name": prop["content"]["name"],
                        "type": "external",
                        "external": {"url": prop["content"]["url"]}
                    }
                ]
            }
    page = notion_client.pages.create(
        parent={"database_id": database_id},
        properties=props
    )
    return page

def get_notion_pages(notion_client: Client, database_id: str, filter: dict = None, sorts: list = []):
    """Retrieve pages from a Notion database with optional filtering.
    Args:
        notion_client (Client): An instance of the Notion client.
        database_id (str): The ID of the Notion database to query.
        filter (dict, optional): A filter object to apply to the query. Defaults to None.
        sorts (list, optional): A list of sort objects to apply to the query. Defaults to None.
    Returns:
        list: A list of pages matching the query.
    """
    query = {
        "filter": filter,
        "sorts": sorts
    }
    response = notion_client.databases.query(database_id, **{k: v for k, v in query.items() if v})
    return response.get("results", [])

def ask_openai(prompt: str, model: str = "gpt-4o", temperature: float = 0.7) -> str:
    """Send a prompt to the OpenAI API and return the response.
    Args:
        prompt (str): The prompt to send to the OpenAI API.
        model (str, optional): The model to use for the completion. Defaults to "gpt-4o".
        temperature (float, optional): The temperature for the completion. Defaults to 0.7.
    Returns:
        str: The response from the OpenAI API.
    """
    client = OpenAI()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt}
        ],
        temperature=temperature
    )
    return response.choices[0].message.content

def _guess_filename_from_url(url: str, default: str = "document.pdf") -> str:
    """
    Get a safe filename from the URL path; fall back to `default` if needed.
    """
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1] or default
    # Ensure .pdf extension
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name

def fetch_pdf_bytes(url: str, *, timeout: int = 30) -> tuple[str, bytes]:
    """
    Download a PDF from a public URL.

    Returns:
        (filename, file_bytes)

    Raises:
        requests.HTTPError on HTTP failures
        ValueError if content doesn't look like a PDF
    """
    headers = {
        "User-Agent": "Tal-PDF-Fetcher/1.0"
    }
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        # Quick content-type sanity check (some servers may return octet-stream)
        ctype = r.headers.get("Content-Type", "").lower()
        if "pdf" not in ctype and not url.lower().endswith(".pdf"):
            # As a fallback, peek at first bytes for "%PDF"
            peek = r.raw.read(5, decode_content=True)
            if peek != b"%PDF-":
                raise ValueError(f"URL doesn't appear to be a PDF (Content-Type={ctype!r}).")
            # If it *is* a PDF, we need the remainder:
            remainder = r.raw.read()
            pdf_bytes = peek + remainder
        else:
            pdf_bytes = r.content

    filename = _guess_filename_from_url(url)
    return filename, pdf_bytes

def upload_pdf_to_openai(url: str, *, client: OpenAI | None = None, purpose: str = "assistants") -> str:
    """
    Download a PDF from `url` and upload it to OpenAI Files.
    Returns the file_id you can attach to a Responses request.

    Args:
        url: Publicly accessible PDF URL
        client: Optional initialized OpenAI() client. If None, creates one from env.
        purpose: File purpose; use "assistants" for general use. (Works for Responses too.)

    Raises:
        requests.HTTPError / ValueError / openai errors
    """
    client = client or OpenAI()

    filename, pdf_bytes = fetch_pdf_bytes(url)
    # Ensure mimetype
    mime = mimetypes.guess_type(filename)[0] or "application/pdf"

    # The SDK accepts file-like objects; we provide an in-memory buffer
    buf = io.BytesIO(pdf_bytes)
    buf.name = filename  # helps servers infer filename
    uploaded = client.files.create(file=buf, purpose=purpose)
    return uploaded.id

def extract_receipt_with_openai(pdf_url: str) -> str:
    """
    Convenience function:
      - uploads the PDF
      - asks GPT-4o (or similar) to extract structured JSON with vendor, date, total, and line items
      - returns the model's text output (expected to be JSON)

    Requires OPENAI_API_KEY in env.
    """
    client = OpenAI()
    file_id = upload_pdf_to_openai(pdf_url, client=client)

    instructions = (
        "You extract receipts. Return STRICT JSON with keys: "
        "vendor (string), date (YYYY-MM-DD), currency (string), "
        "total (number), items (array of {name, qty (number|null), unit_price (number|null), line_total (number|null)}), "
        "and comment (string) with a brief evaluation of whether the spending is expensive by Israeli standards. "
        "If a value is missing, use null. Do not include extra text."
    )

    resp = client.responses.create(
        model="gpt-4o",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": instructions},
                    {"type": "input_file", "file_id": file_id},
                ],
            }
        ],
        temperature=0.2,
    )
    # The SDK provides a convenience property for plain text output:
    clean_text = re.sub(r'^```json\s*|\s*```$', '', resp.output_text.strip())
    json_data = json.loads(clean_text)
    return json_data
