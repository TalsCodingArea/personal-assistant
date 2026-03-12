from openai import OpenAI
import requests
from notion_client import Client
import json
import io
import requests
from urllib.parse import urlparse
import re
import os
import smtplib
from pathlib import Path
from email.message import EmailMessage
from typing import Optional, Tuple
import httpx


def send_email(to: str, subject: str, body_text: Optional[str] = None, app_password: str = None):
    """
    Send an email via SMTP.

    Args:
        smtp_host: SMTP server hostname (e.g. 'smtp.gmail.com').
        smtp_port: SMTP port (e.g. 587 for STARTTLS, 465 for SSL).
        username: SMTP username (your email address usually).
        password: SMTP password or app password.
        to: one or more recipient emails (list or tuple or single string).
        subject: email subject.
        body_text: plain text body (optional).
        body_html: HTML body (optional).
        attachments: iterable of file paths to attach (optional).
        use_tls: whether to use STARTTLS (True for port 587). If you use port 465, consider an SSL connection separately.
        debug: if True, prints SMTP protocol debug info.

    Raises:
        Exception on SMTP/auth or attachment errors.
    """

    if isinstance(to, str):
        recipients = [to]
    else:
        recipients = list(to)

    msg = EmailMessage()
    gmail_email = os.environ["GMAIL_EMAIL"]
    msg = EmailMessage()
    msg["From"] = gmail_email
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body_text or "")

    server = smtplib.SMTP("smtp.gmail.com", 587)
    try:
        server.set_debuglevel(1)  # Set to 1 for debug output
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(gmail_email, app_password)
        server.send_message(msg)
    finally:
        server.quit()

def create_notion_page(notion_client: Client, database_id: str, props: dict, file: dict=None):
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
    if file:
        headers = {
            "Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }
        payload = {
            "properties": {
                "Invoice": {
                    "type": "files",
                    "files": [
                        {
                            "type": "file_upload",
                            "file_upload": { "id": file["file_upload_id"]},
                            "name": file['filename']
                        }
                    ]
                }
            }
        }

        r = requests.patch(f"https://api.notion.com/v1/pages/{page["id"]}", headers=headers, json=payload)
        r.raise_for_status()
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

def ask_openai(prompt: str, model: str = "gpt-4o", temperature: float = 0.7, system_message: str = "You are a helpful assistant.") -> str:
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

def fetch_slack_pdf_bytes(file_obj: dict, timeout: float = 60.0, bot_token: str = "") -> Tuple[str, bytes, str]:
    """
    Fetch raw bytes for a Slack-hosted file (e.g., uploaded PDF).

    Accepts either:
      - a Slack `event` dict that contains `files: [...]`, or
      - a single Slack `file` dict (as found in event["files"][i])

    Returns:
      (filename, content_bytes, mimetype)

    Requires the `files:read` scope. Uses `url_private_download` and your bot token.
    """

    if not isinstance(file_obj, dict):
        raise ValueError("fetch_slack_pdf_bytes: expected a Slack event or file dict.")

    # Prefer the direct download URL; fall back to the private URL
    url = file_obj.get("url_private_download") or file_obj.get("url_private")
    if not url:
        raise ValueError("fetch_slack_pdf_bytes: no url_private_download/url_private found on the file object.")

    # Best-effort filename & mimetype
    filename = file_obj.get("name") or file_obj.get("title") or "file"
    mimetype = file_obj.get("mimetype") or "application/octet-stream"

    # Download with auth
    headers = {"Authorization": f"Bearer {bot_token}"}
    # Some Slack file URLs redirect; follow them.
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()

        # If Slack provides a better filename via Content-Disposition, use it.
        cd = resp.headers.get("content-disposition", "")
        # e.g., 'attachment; filename="Invoice_0021.pdf"'
        if "filename=" in cd:
            try:
                # crude but robust extraction
                filename = cd.split("filename=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass

        # If Slack provides a better mimetype, prefer it
        ct = resp.headers.get("content-type")
        if ct:
            mimetype = ct.split(";")[0].strip() or mimetype

        if not resp.content.startswith(b"%PDF-"):
            pdf_bytes = text_html_to_application_pdf_bytes(resp.content)
            return filename, pdf_bytes[1], "application/pdf"

        return filename, resp.content, mimetype

def upload_pdf_to_openai(filename: str, pdf_bytes: bytes, client: OpenAI | None = None, purpose: str = "assistants") -> str:
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

def fetch_pdf_bytes_from_slack_file_dict(file_dict: dict, token) -> tuple[str, bytes]:
    """
    Given a Slack file dictionary (from events/files.* payloads), download the PDF bytes.

    Expected keys in file_dict (Slack Web API files schema):
      - "mimetype": should be "application/pdf" (or "filetype" == "pdf")
      - "name": original filename (fallback to "<id>.pdf" if missing)
      - "url_private_download" or "url_private": authenticated download URL

    Args:
        file_dict: The Slack file object received in an event or from files.info.
        bot_token: Optional Slack Bot token. If omitted, falls back to env var SLACK_BOT_TOKEN.

    Returns:
        (filename, pdf_bytes)

    Raises:
        ValueError: if the file is not a PDF or required fields/URL are missing.
    """
    if not isinstance(file_dict, dict):
        raise ValueError("file_dict must be a dict of the Slack file object.")

    # Validate PDF mimetype / type
    mimetype = file_dict.get("mimetype") or ""
    filetype = file_dict.get("filetype") or ""
    if not (mimetype.startswith("application/pdf") or filetype == "pdf"):
        raise ValueError(f"Unsupported file type: {mimetype or filetype}. Please upload a PDF file.")

    # Resolve filename
    filename = file_dict.get("name")
    if not filename:
        fid = file_dict.get("id", "receipt")
        filename = f"{fid}.pdf"

    # Resolve authenticated URL
    url = file_dict.get("url_private_download") or file_dict.get("url_private")
    if not url:
        raise ValueError("Slack file object missing 'url_private_download'/'url_private'.")


    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=60)
    if resp.status_code != 200:
        raise ValueError(f"Failed to download file from Slack (HTTP {resp.status_code}).")

    return filename, resp.content

def text_html_to_application_pdf_bytes(html_text: bytes, timeout: int = 30) -> tuple[str, bytes]:
    """
    Convert HTML text to PDF bytes using an external API (pdfcrowd.com).

    Returns:
        (filename, file_bytes)

    Raises:
        requests.HTTPError on HTTP failures
        ValueError on conversion errors
    """
    json_serializable = {"html": html_text.decode("utf-8", errors="ignore")}
    url = "https://api.pdfendpoint.com/v1/convert"
    pdf_endpoint_token = os.getenv("PDF_ENDPOINT_ACCESS_TOKEN")
    if not pdf_endpoint_token:
        raise ValueError("PDF_ENDPOINT_ACCESS_TOKEN environment variable is not set.")
    headers = {
        "Authorization": f"Bearer {pdf_endpoint_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "input": json_serializable,
        "output_format": "pdf"
    }
    with requests.post(url, headers=headers, json=payload, timeout=timeout) as r:
        r.raise_for_status()
        pdf_bytes = r.content
    # sanity checks
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("Converted content is not a PDF (missing %PDF- header).")
    return "converted_document.pdf", pdf_bytes

def notion_response_simplifier(entries: list, exclude: list = []) -> str:
    """Takes a notion response, and simplifies it to a more readable format."""
    simplified = []
    for entry in entries:
        simplified_entry = {}
        props = entry['properties']
        for prop_name, prop_content in props.items():
            if prop_name in exclude:
                continue
            if prop_content.get("type") == "relation":
                simplified_entry[prop_name] = prop_content.get("relation")
            elif prop_content.get("type") == "select" and prop_content.get("select") is not None:
                simplified_entry[prop_name] = prop_content.get("select", {}).get("name")
            elif prop_content.get("type") == "multi_select":
                simplified_entry[prop_name] = [item['name'] for item in prop_content.get("multi_select", [])]
            elif prop_content.get("type") == "title":
                title_parts = prop_content.get("title", [])
                title_text = " ".join(part.get("plain_text", "") for part in title_parts)
                simplified_entry[prop_name] = title_text
            elif prop_content.get("type") == "rich_text":
                text_parts = prop_content.get("rich_text", [])
                text_content = " ".join(part.get("plain_text", "") for part in text_parts)
                simplified_entry[prop_name] = text_content
            elif prop_content.get("type") == "number":
                simplified_entry[prop_name] = prop_content.get("number")
            elif prop_content.get("type") == "date":
                simplified_entry[prop_name] = prop_content.get("date", {}).get("start")
            elif prop_content.get("type") == "checkbox":
                simplified_entry[prop_name] = prop_content.get("checkbox")
            elif prop_content.get("type") == "files":
                file_urls = []
                for file_item in prop_content.get("files", []):
                    if file_item.get("type") == "external":
                        file_urls.append(file_item.get("external", {}).get("url"))
                    elif file_item.get("type") == "file":
                        file_urls.append(file_item.get("file", {}).get("url"))
                simplified_entry[prop_name] = file_urls
        simplified.append(simplified_entry)
    return simplified

def download_slack_file(url_private_download: str) -> bytes:
    r = requests.get(
        url_private_download,
        headers={"Authorization": f"Bearer {os.environ["SLACK_BOT_TOKEN"]}"},
        timeout=60,
    )
    r.raise_for_status()
    return r.content

def create_notion_file_upload():
    r = requests.post(
        "https://api.notion.com/v1/file_uploads",
        headers={
            "Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
            "Notion-Version": "2025-09-03",
            "Content-Type": "application/json",
        },
        json={"mode": "single_part"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()  # contains "id"

def upload_file_bytes(file_upload_id, pdf_bytes, filename = "receipt.pdf"):
    headers = {
        "Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
        "Notion-Version": "2022-06-28"
    }
    files = {
        "file": (
            filename,
            pdf_bytes,
            "application/pdf"
        )
    }
    response = requests.post(f"https://api.notion.com/v1/file_uploads/{file_upload_id}/send", headers=headers, files=files)
    response.raise_for_status()
    return response.json()

