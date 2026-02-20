import base64
import io
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests
from langchain_core.tools import tool
from openai import OpenAI


def _openai_client() -> OpenAI:
    """Build an OpenAI client from environment configuration."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("Missing OPENAI_API_KEY environment variable.")
    return OpenAI(api_key=api_key)


def _is_pdf_bytes(data: bytes) -> bool:
    return isinstance(data, (bytes, bytearray)) and bytes(data).startswith(b"%PDF-")


def _read_pdf_from_path(pdf_path: str) -> Tuple[str, bytes]:
    path = Path(pdf_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ValueError(f"File does not exist: {pdf_path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file, got: {path.name}")
    pdf_bytes = path.read_bytes()
    if not _is_pdf_bytes(pdf_bytes):
        raise ValueError("File content is not a valid PDF (missing %PDF- header).")
    return path.name, pdf_bytes


def _read_pdf_from_url(pdf_url: str, timeout: int = 60) -> Tuple[str, bytes]:
    if not isinstance(pdf_url, str) or not pdf_url.strip():
        raise ValueError("`pdf_url` must be a non-empty string.")
    response = requests.get(pdf_url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    pdf_bytes = response.content
    if not _is_pdf_bytes(pdf_bytes):
        raise ValueError("Downloaded content is not a valid PDF.")
    filename = Path(pdf_url.split("?", 1)[0]).name or "receipt.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    return filename, pdf_bytes


def _detect_pdf_content_type(pdf_bytes: bytes, max_pages: int = 2, min_chars: int = 20) -> Dict[str, Any]:
    """
    Detect whether a PDF has selectable text or appears scanned/image-only.

    Returns:
        {
          "has_selectable_text": bool,
          "content_type": "text_pdf" | "scanned_pdf",
          "sample_text": str
        }
    """
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise ImportError("PyMuPDF is required. Install with: pip install pymupdf") from exc

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError("Could not open PDF bytes.") from exc

    extracted: List[str] = []
    for page_index in range(min(max_pages, len(doc))):
        page = doc.load_page(page_index)
        text = (page.get_text("text") or "").strip()
        if text:
            extracted.append(text)
    doc.close()

    sample_text = "\n".join(extracted).strip()
    has_selectable_text = len(sample_text) >= min_chars
    return {
        "has_selectable_text": has_selectable_text,
        "content_type": "text_pdf" if has_selectable_text else "scanned_pdf",
        "sample_text": sample_text[:500],
    }


def _render_pdf_to_png_data_urls(pdf_bytes: bytes, dpi: int = 220, max_pages: int = 2) -> List[str]:
    try:
        import fitz  # type: ignore
    except Exception as exc:
        raise ImportError("PyMuPDF is required. Install with: pip install pymupdf") from exc

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError("Could not open PDF bytes for rendering.") from exc

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    images: List[str] = []
    for page_index in range(min(max_pages, len(doc))):
        page = doc.load_page(page_index)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        png_bytes = pix.tobytes("png")
        images.append("data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii"))
    doc.close()
    return images


def _upload_pdf_to_openai(client: OpenAI, pdf_bytes: bytes, filename: str) -> str:
    safe_name = filename if filename.lower().endswith(".pdf") else f"{filename}.pdf"
    buf = io.BytesIO(pdf_bytes)
    buf.name = safe_name
    buf.seek(0)
    uploaded = client.files.create(file=buf, purpose="assistants")
    return uploaded.id


def _parse_model_json(raw_text: str) -> Dict[str, Any]:
    text = (raw_text or "").strip()
    text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ValueError("Model output is not valid JSON.")
        return json.loads(match.group(0))


def _build_receipt_prompt(category_options: List[str]) -> str:
    cleaned = [c.strip() for c in category_options if isinstance(c, str) and c.strip()]
    if not cleaned:
        raise ValueError("`category_options` must contain at least one non-empty category.")

    categories_list = ", ".join(cleaned)
    return (
        "Extract receipt data from the provided document. "
        "The receipt can be in Hebrew (RTL) or English. "
        "Return STRICT JSON only, no markdown and no extra text, with this schema: "
        "{"
        "\"vendor\": string|null, "
        "\"total_amount\": number|null, "
        "\"currency\": string|null, "
        "\"category\": string, "
        "\"language\": \"he\"|\"en\"|\"unknown\", "
        "\"confidence\": number, "
        "\"reasoning\": string"
        "\"date\": string|null (ISO 8601 format) "
        "}. "
        f"The `category` must be one of: [{categories_list}, Unrecognized]. "
        "Choose the best-fitting category based on vendor/items/context. "
        "If data is unreadable, set unknown fields to null and use category='Unrecognized'. "
        "Use a decimal point for numeric values."
    )


def _normalize_category(category: Any, category_options: List[str]) -> str:
    allowed = [c.strip() for c in category_options if isinstance(c, str) and c.strip()]
    allowed_lower = {c.lower(): c for c in allowed}
    if isinstance(category, str) and category.strip():
        canonical = allowed_lower.get(category.strip().lower())
        if canonical:
            return canonical
    return "Unrecognized"


def _extract_with_openai(
    pdf_bytes: bytes,
    filename: str,
    category_options: List[str],
    model: str = "gpt-4o",
    max_pages_for_ocr: int = 2,
) -> Dict[str, Any]:
    client = _openai_client()
    content_probe = _detect_pdf_content_type(pdf_bytes, max_pages=max_pages_for_ocr)
    prompt = _build_receipt_prompt(category_options)

    if content_probe["has_selectable_text"]:
        file_id = _upload_pdf_to_openai(client, pdf_bytes, filename)
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_file", "file_id": file_id},
                    ],
                }
            ],
            temperature=0,
        )
    else:
        image_urls = _render_pdf_to_png_data_urls(pdf_bytes, max_pages=max_pages_for_ocr)
        image_inputs = [{"type": "input_image", "image_url": url} for url in image_urls]
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}, *image_inputs],
                }
            ],
            temperature=0,
        )

    parsed = _parse_model_json(response.output_text)
    parsed["category"] = _normalize_category(parsed.get("category"), category_options)
    parsed["source_pdf_type"] = content_probe["content_type"]
    parsed["text_probe_sample"] = content_probe["sample_text"]
    return parsed


@tool
def receipt_detect_pdf_content_type(pdf_path: str, max_pages: int = 2) -> Dict[str, Any]:
    """
    Detect whether a local receipt PDF contains selectable text or is image-only (scanned).

    Use this when the agent must decide if OCR is needed before extraction.

    Args:
        pdf_path: Absolute or relative filesystem path to the receipt PDF.
        max_pages: Number of initial pages to inspect for text (default: 2).

    Returns:
        A dictionary with:
        - `has_selectable_text` (bool)
        - `content_type` (`text_pdf` or `scanned_pdf`)
        - `sample_text` (short extracted snippet, if available)
    """
    _, pdf_bytes = _read_pdf_from_path(pdf_path)
    return _detect_pdf_content_type(pdf_bytes, max_pages=max_pages)


@tool
def receipt_extract_summary_from_pdf(
    pdf_path: str,
    category_options: List[str],
    model: str = "gpt-4o",
    max_pages_for_ocr: int = 2,
) -> Dict[str, Any]:
    """
    Process a local receipt PDF and extract vendor, total amount, and best-fit category.

    This tool handles both text-based PDFs and scanned/image receipts. If no text layer
    is detected, it renders pages and performs OCR-capable vision extraction.
    Hebrew and English receipts are supported.

    Args:
        pdf_path: Absolute or relative path to a local `.pdf` receipt file.
        category_options: List of allowed category names to classify into.
        model: OpenAI model for extraction/classification (default: `gpt-4o`).
        max_pages_for_ocr: Max pages rendered for OCR when scanned (default: 2).

    Returns:
        JSON-compatible dictionary:
        - `vendor`
        - `total_amount`
        - `currency`
        - `category`
        - `language`
        - `confidence`
        - `reasoning`
        - `source_pdf_type` (`text_pdf` or `scanned_pdf`)
        - `text_probe_sample`
        - `date`
    """
    filename, pdf_bytes = _read_pdf_from_path(pdf_path)
    return _extract_with_openai(
        pdf_bytes=pdf_bytes,
        filename=filename,
        category_options=category_options,
        model=model,
        max_pages_for_ocr=max_pages_for_ocr,
    )


@tool
def receipt_extract_summary_from_pdf_url(
    pdf_url: str,
    category_options: List[str],
    model: str = "gpt-4o",
    max_pages_for_ocr: int = 2,
) -> Dict[str, Any]:
    """
    Process a receipt PDF from a URL and extract vendor, total amount, and category.

    This tool first downloads the PDF, then runs the same extraction pipeline used for local files:
    text-layer detection followed by OCR fallback for scanned receipts.

    Args:
        pdf_url: Public URL pointing to a receipt PDF.
        category_options: Allowed category labels to classify into.
        model: OpenAI model for extraction/classification (default: `gpt-4o`).
        max_pages_for_ocr: Max pages rendered for OCR fallback (default: 2).

    Returns:
        JSON-compatible dictionary with extraction and classification fields.
    """
    filename, pdf_bytes = _read_pdf_from_url(pdf_url)
    return _extract_with_openai(
        pdf_bytes=pdf_bytes,
        filename=filename,
        category_options=category_options,
        model=model,
        max_pages_for_ocr=max_pages_for_ocr,
    )
