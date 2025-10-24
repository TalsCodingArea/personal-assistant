from openai import OpenAI
import requests
from notion_client import Client
import json
import io
import mimetypes
import requests
from urllib.parse import urlparse
import re
import os



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
    headers = {"User-Agent": "Tal-PDF-Fetcher/2.0"}
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        chunks = []
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                chunks.append(chunk)
        pdf_bytes = b"".join(chunks)
    # sanity checks
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("Downloaded content is not a PDF (missing %PDF- header).")
    # optional: some PDFs omit %%EOF; not fatal
    return _guess_filename_from_url(url), pdf_bytes

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

    # ensure a sane .pdf basename
    base = os.path.basename(filename if filename.lower().endswith(".pdf") else filename + ".pdf")

    buf = io.BytesIO(pdf_bytes)
    buf.seek(0)
    buf.name = base  # helps servers infer filename

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

# --- OCR helpers for image-only PDFs ---

def render_pdf_to_images(pdf_bytes: bytes, dpi: int = 200, max_pages: int = 2) -> list[bytes]:
    """
    Render the first `max_pages` pages of a PDF (provided as bytes) to PNG bytes using PyMuPDF.
    Returns a list of PNG byte strings, one per rendered page.

    Requirements:
        pip install pymupdf

    Raises:
        ImportError: if PyMuPDF (fitz) is not installed.
        ValueError: if the PDF cannot be opened/rendered.
    """
    try:
        import fitz
    except Exception as e:
        raise ImportError("PyMuPDF (fitz) is required for OCR fallback. Install with: pip install pymupdf") from e

    images: list[bytes] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise ValueError("Could not open PDF bytes for rendering.") from e

    zoom = dpi / 72.0  # 72 dpi is PDF default
    mat = fitz.Matrix(zoom, zoom)

    for page_index in range(min(len(doc), max_pages)):
        page = doc.load_page(page_index)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        images.append(pix.tobytes("png"))

    doc.close()
    return images
