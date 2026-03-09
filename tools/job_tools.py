"""
Job Application Workflow (tools/job_tools.py)
=============================================

A 6-step hardcoded pipeline that turns a job listing URL into a complete
application package and logs it to Notion.

Pipeline (executed in order by run_job_application_workflow):
  Step 1 — scrape_job_listing(url)
            Fetches raw HTML and extracts cleaned page text.
            Returns: JobData dict (title, company, description, url, location)

  Step 2 — research_company(company_name, llm)
            DuckDuckGo web search → LLM synthesizes a 2-3 sentence summary.
            Returns: str

  Step 3 — generate_documents(job_data, company_summary, user_profile, llm)
            3a. _tailor_resume_summary  → short tailored summary paragraph (LLM)
            3b. _generate_cover_letter_text → 3-4 paragraph cover letter (LLM)
            3c. _render_resume_to_pdf   → PDF at temp path
            3d. _render_cover_letter_to_pdf → PDF at temp path
            Returns: (resume_path: Path, cover_letter_path: Path)

  Step 4 — generate_personal_note(job_data, llm)
            Loads .txt examples from personal_notes_examples/ and generates
            a short note in the same style (LLM, few-shot).
            Returns: str

  Step 5 — log_job_to_notion(job_data, resume_path)
            Creates a Notion page in the Jobs database with all properties
            and appends the full job description as page body blocks.
            Returns: dict (page_id, notion_url)

  (Step 6 — Telegram sending is handled in app.py after this function returns.)

LLM is called ONLY in steps 2, 3a, 3b, 4 — everything else is deterministic.

Notion DB expected properties (create these in your Jobs database):
  - "Job Title"      → title
  - "Company"        → rich_text
  - "Application URL"→ url
  - "Company URL"    → url
  - "Status"         → select  (options: Applied, Interview, Offer, Rejected, Withdrawn)
  - "Next Action"    → rich_text
  - "Date Applied"   → date

Environment variables required:
  JOBS_DATABASE_ID   → Notion database ID for job applications
  NOTION_API_KEY     → already used throughout the project
  OPENAI_API_KEY     → already used throughout the project (for LLM)

WeasyPrint system dependencies (Raspberry Pi / Debian):
  sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
                   libgdk-pixbuf2.0-0 libffi-dev shared-mime-info

Playwright fallback for JS-rendered pages (e.g. Ultipro, Greenhouse, Lever):
  pip install playwright
  playwright install chromium
  # On Raspberry Pi also run:
  playwright install-deps chromium
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from langchain_core.messages import HumanMessage, SystemMessage
from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).parent.parent
_RESUME_DATA_DIR = _PROJECT_ROOT / "resume_data"
_USER_PROFILE_PATH = _RESUME_DATA_DIR / "user_profile.json"
_RESUME_TEMPLATE_PATH = _RESUME_DATA_DIR / "resume_template.html"
_COVER_LETTER_TEMPLATE_PATH = _RESUME_DATA_DIR / "cover_letter_template.html"
_PERSONAL_NOTES_DIR = _PROJECT_ROOT / "personal_notes_examples"

# Notion API
_NOTION_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2025-09-03"

# Limits to keep LLM token usage minimal
_MAX_JOB_DESCRIPTION_LLM_CHARS = 4000   # fed to LLM for parsing / cover letter
_MAX_COMPANY_SEARCH_SNIPPETS = 4         # DuckDuckGo result snippets to use
_NOTION_BLOCK_CHAR_LIMIT = 1900          # safe margin under Notion's 2000 limit

# Minimum characters for a scrape result to be considered "useful".
# Below this, we assume the page is JS-rendered and try the Playwright fallback.
_MIN_USEFUL_TEXT_LENGTH = 200

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
JobData = Dict[str, Any]
ProgressCallback = Optional[Callable[[str], Coroutine]]


# ===========================================================================
# STEP 1 — Job Scraping
# ===========================================================================

def scrape_job_listing(url: str) -> JobData:
    """
    Fetch a job listing page and extract clean text for LLM parsing.

    Strategy (two-tier):
      1. requests + BeautifulSoup — fast, zero dependencies beyond what's listed.
      2. Playwright headless Chromium fallback — used automatically when the
         requests result is too short (< _MIN_USEFUL_TEXT_LENGTH chars), which
         indicates a JS-rendered SPA (Ultipro, Greenhouse, Lever, LinkedIn, etc.).
         Playwright must be installed separately:
           pip install playwright && playwright install chromium

    Args:
        url: Public job listing URL.

    Returns:
        JobData dict with keys:
          url, raw_text, title (best-effort from meta), company (best-effort),
          description (empty — filled by _parse_job_with_llm), location (empty),
          scrape_method ("requests" | "playwright")
    """
    result = _scrape_with_requests(url)

    if len(result["raw_text"]) < _MIN_USEFUL_TEXT_LENGTH:
        logger.info(
            "requests scrape returned only %d chars — trying Playwright fallback",
            len(result["raw_text"]),
        )
        playwright_result = _scrape_with_playwright(url)
        if playwright_result and len(playwright_result["raw_text"]) > len(result["raw_text"]):
            logger.info("Playwright returned %d chars — using it", len(playwright_result["raw_text"]))
            return playwright_result
        # Playwright failed or returned no better result — return requests result as-is
        logger.warning(
            "Playwright fallback did not improve scrape result. "
            "The page may require login or is otherwise inaccessible. "
            "Consider pasting the job description text manually via TEST_JOB_TEXT."
        )

    return result


def _scrape_with_requests(url: str) -> JobData:
    """Attempt to scrape the page with requests + BeautifulSoup (static HTML only)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not fetch job listing: {exc}") from exc

    return _html_to_job_data(url, html, method="requests")


def _scrape_with_playwright(url: str) -> Optional[JobData]:
    """
    Scrape a JS-rendered page using Playwright headless Chromium.

    Returns None if Playwright is not installed or if the fetch fails.
    Install with: pip install playwright && playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # type: ignore
    except ImportError:
        logger.warning(
            "Playwright is not installed. Install it to support JS-rendered job pages:\n"
            "  pip install playwright && playwright install chromium"
        )
        return None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            )
            page.goto(url, wait_until="networkidle", timeout=30_000)
            # Extra wait for slow SPAs that render after networkidle
            try:
                page.wait_for_timeout(2000)
            except PWTimeout:
                pass
            html = page.content()
            browser.close()
        return _html_to_job_data(url, html, method="playwright")
    except Exception as exc:
        logger.warning("Playwright scrape failed: %s", exc)
        return None


def _html_to_job_data(url: str, html: str, method: str = "requests") -> JobData:
    """Parse raw HTML into a JobData dict using BeautifulSoup."""
    soup = BeautifulSoup(html, "lxml")

    # Remove boilerplate tags that pollute the text
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "noscript", "iframe", "svg"]):
        tag.decompose()

    # Best-effort metadata extraction from <meta> and <title>
    title_from_meta = (
        (soup.find("meta", property="og:title") or {}).get("content", "")
        or (soup.find("title") or {}).get_text("", strip=True)
        or ""
    )
    company_from_meta = (
        (soup.find("meta", property="og:site_name") or {}).get("content", "")
        or ""
    )

    # Prefer main content containers; fall back to full body text
    main_containers = soup.find_all(["main", "article", "section"], limit=5)
    if main_containers:
        raw_text = "\n\n".join(
            c.get_text(separator="\n", strip=True) for c in main_containers
        )
    else:
        raw_text = soup.get_text(separator="\n", strip=True)

    # Collapse runs of blank lines
    raw_text = re.sub(r"\n{3,}", "\n\n", raw_text).strip()

    return {
        "url": url,
        "raw_text": raw_text[:12000],
        "title": title_from_meta.strip(),
        "company": company_from_meta.strip(),
        "description": "",
        "location": "",
        "scrape_method": method,
    }


def _parse_job_with_llm(raw_data: JobData, llm) -> JobData:
    """
    Extract structured job fields from raw scraped text using the LLM.

    This is the ONLY LLM call in step 1 (and only if needed).
    Returns the same dict enriched with title, company, description, location.
    """
    system = (
        "You are a data extraction assistant. "
        "Extract job information from the following web page text and return STRICT JSON. "
        "Keys: title (string), company (string), description (string), location (string). "
        "If a field cannot be determined, use an empty string. "
        "The description should be the COMPLETE job description text, not a summary. "
        "Return only JSON, no markdown fences."
    )
    text_snippet = raw_data["raw_text"][:_MAX_JOB_DESCRIPTION_LLM_CHARS]
    hint = ""
    if raw_data.get("title"):
        hint += f"\nHint — page title: {raw_data['title']}"
    if raw_data.get("company"):
        hint += f"\nHint — site name: {raw_data['company']}"

    human = f"Extract job data from this text:{hint}\n\n---\n{text_snippet}"
    response = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
    try:
        text = (response.content or "").strip()
        text = re.sub(r"^```json\s*|```$", "", text, flags=re.IGNORECASE).strip()
        parsed = json.loads(text)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("LLM job parsing returned non-JSON; using raw text fallback")
        parsed = {}

    return {
        **raw_data,
        "title": parsed.get("title") or raw_data.get("title") or "Unknown Position",
        "company": parsed.get("company") or raw_data.get("company") or "Unknown Company",
        "description": parsed.get("description") or raw_data.get("raw_text", "")[:3000],
        "location": parsed.get("location") or "",
    }


# ===========================================================================
# STEP 2 — Company Research
# ===========================================================================

def research_company(company_name: str, llm) -> str:
    """
    Search DuckDuckGo for the company and synthesize a short summary via LLM.

    Args:
        company_name: Company name to research.
        llm: LangChain LLM instance.

    Returns:
        2-3 sentence plain-text company summary.
    """
    snippets = _duckduckgo_search(company_name)
    if not snippets:
        return f"{company_name} — no additional information found via web search."

    context = "\n\n".join(
        f"[{r['title']}]\n{r['body']}" for r in snippets
    )
    system = (
        "You are a research assistant. Given search result snippets about a company, "
        "write 2-3 concise sentences summarizing what the company does, its industry, "
        "and any notable facts. Be factual and neutral. Plain text only."
    )
    human = (
        f"Company: {company_name}\n\n"
        f"Search snippets:\n{context}\n\n"
        "Write a 2-3 sentence company summary."
    )
    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
    return (resp.content or "").strip()


def _duckduckgo_search(query: str) -> List[Dict[str, str]]:
    """Return up to _MAX_COMPANY_SEARCH_SNIPPETS DuckDuckGo results for query.

    Supports both the legacy duckduckgo_search package and the newer ddgs package.
    """
    # Try new package name first (ddgs), fall back to legacy (duckduckgo_search)
    DDGS = None
    try:
        from ddgs import DDGS  # type: ignore  # newer package name
    except ImportError:
        try:
            from duckduckgo_search import DDGS  # type: ignore  # legacy package name
        except ImportError:
            logger.warning(
                "DuckDuckGo search package not installed; skipping company research. "
                "Install with: pip install ddgs"
            )
            return []
    try:
        results = list(
            DDGS().text(
                f"{query} company overview",
                max_results=_MAX_COMPANY_SEARCH_SNIPPETS,
            )
        )
        return results
    except Exception as exc:
        logger.warning("DuckDuckGo search failed: %s", exc)
        return []


# ===========================================================================
# STEP 3 — Document Generation (Resume + Cover Letter)
# ===========================================================================

def generate_documents(
    job_data: JobData,
    company_summary: str,
    llm,
) -> Tuple[Path, Path]:
    """
    Generate tailored resume PDF and cover letter PDF for the given job.

    Loads user_profile.json, calls LLM for tailored content, renders both
    PDFs via WeasyPrint + Jinja2 HTML templates.

    Args:
        job_data: Parsed job data dict from scrape step.
        company_summary: 2-3 sentence summary from research step.
        llm: LangChain LLM instance.

    Returns:
        (resume_pdf_path, cover_letter_pdf_path) — temp files (caller should clean up).

    Raises:
        FileNotFoundError: If user_profile.json or templates are missing.
        RuntimeError: If WeasyPrint or Jinja2 is not installed.
    """
    user_profile = _load_user_profile()

    tailored_summary = _tailor_resume_summary(job_data, company_summary, user_profile, llm)
    cover_letter_text = _generate_cover_letter_text(job_data, company_summary, user_profile, llm)

    resume_path = _render_resume_to_pdf(user_profile, tailored_summary, job_data)
    cover_letter_path = _render_cover_letter_to_pdf(cover_letter_text, user_profile, job_data)

    return resume_path, cover_letter_path


def _load_user_profile() -> Dict[str, Any]:
    """Load and return the user profile from resume_data/user_profile.json."""
    if not _USER_PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"User profile not found at {_USER_PROFILE_PATH}. "
            "Copy resume_data/user_profile.json and fill in your details."
        )
    return json.loads(_USER_PROFILE_PATH.read_text(encoding="utf-8"))


def _tailor_resume_summary(
    job_data: JobData,
    company_summary: str,
    user_profile: Dict[str, Any],
    llm,
) -> str:
    """
    Generate a 2-3 sentence professional summary tailored to this specific job.

    The rest of the resume (experience, education, skills) stays unchanged.
    Only the professional summary paragraph is customized per application.
    """
    system = (
        "You are a professional resume writer. "
        "Write a 2-3 sentence professional summary for a job application. "
        "Be specific, confident, and match keywords from the job description. "
        "Do NOT use first-person (no 'I'). Plain text only, no bullet points."
    )
    job_desc_snippet = job_data.get("description", "")[:_MAX_JOB_DESCRIPTION_LLM_CHARS]
    human = (
        f"Job Title: {job_data.get('title')}\n"
        f"Company: {job_data.get('company')}\n"
        f"Company context: {company_summary}\n\n"
        f"Job description excerpt:\n{job_desc_snippet}\n\n"
        f"Candidate's current summary:\n{user_profile.get('professional_summary', '')}\n"
        f"Candidate's skills: {', '.join(user_profile.get('skills', []))}\n\n"
        "Write a tailored professional summary for this specific application."
    )
    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
    return (resp.content or "").strip()


def _generate_cover_letter_text(
    job_data: JobData,
    company_summary: str,
    user_profile: Dict[str, Any],
    llm,
) -> str:
    """
    Generate a 3-4 paragraph cover letter body as plain text.

    Paragraphs:
      1. Opening — express interest, mention position and company
      2. Why them — reference company summary, show genuine interest
      3. Why you — connect top skills/experience to the job requirements
      4. Closing — call to action, professional sign-off line
    """
    system = (
        "You are a professional cover letter writer. "
        "Write a 3-4 paragraph cover letter body. "
        "Be genuine, specific, and professional. "
        "Do NOT include date, address lines, or 'Dear...' — just the body paragraphs. "
        "Separate paragraphs with a blank line. Plain text only."
    )
    job_desc_snippet = job_data.get("description", "")[:_MAX_JOB_DESCRIPTION_LLM_CHARS]
    experience_lines = "\n".join(
        f"- {e.get('title')} at {e.get('company')} ({e.get('dates', '')})"
        for e in user_profile.get("experience", [])[:3]
    )
    human = (
        f"Position: {job_data.get('title')}\n"
        f"Company: {job_data.get('company')}\n"
        f"Company context: {company_summary}\n\n"
        f"Job description:\n{job_desc_snippet}\n\n"
        f"Applicant name: {user_profile.get('name')}\n"
        f"Applicant experience:\n{experience_lines}\n"
        f"Key skills: {', '.join(user_profile.get('skills', [])[:10])}\n\n"
        "Write a compelling 3-4 paragraph cover letter body."
    )
    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
    return (resp.content or "").strip()


def _render_resume_to_pdf(
    user_profile: Dict[str, Any],
    tailored_summary: str,
    job_data: JobData,
) -> Path:
    """Render the HTML resume template to a PDF using WeasyPrint + Jinja2."""
    _check_render_deps()
    from jinja2 import Template  # type: ignore
    from weasyprint import HTML  # type: ignore

    template_text = _RESUME_TEMPLATE_PATH.read_text(encoding="utf-8")
    template = Template(template_text)
    profile_vars = {k: v for k, v in user_profile.items() if k != "professional_summary"}
    html_content = template.render(
        **profile_vars,
        professional_summary=tailored_summary,
        job_title_applied=job_data.get("title", ""),
        company_applied=job_data.get("company", ""),
        render_date=datetime.now().strftime("%B %Y"),
    )

    tmp = tempfile.NamedTemporaryFile(
        suffix=".pdf",
        prefix=f"resume_{_safe_filename(job_data.get('company', 'company'))}_",
        delete=False,
    )
    tmp.close()
    HTML(string=html_content, base_url=str(_RESUME_DATA_DIR)).write_pdf(tmp.name)
    logger.info("Resume PDF rendered: %s", tmp.name)
    return Path(tmp.name)


def _render_cover_letter_to_pdf(
    cover_letter_text: str,
    user_profile: Dict[str, Any],
    job_data: JobData,
) -> Path:
    """Render the HTML cover letter template to a PDF using WeasyPrint + Jinja2."""
    _check_render_deps()
    from jinja2 import Template  # type: ignore
    from weasyprint import HTML  # type: ignore

    # Convert plain text paragraphs → HTML <p> tags
    paragraphs_html = "\n".join(
        f"<p>{para.strip()}</p>"
        for para in cover_letter_text.split("\n\n")
        if para.strip()
    )
    template_text = _COVER_LETTER_TEMPLATE_PATH.read_text(encoding="utf-8")
    template = Template(template_text)
    profile_vars = {k: v for k, v in user_profile.items() if k != "professional_summary"}
    html_content = template.render(
        **profile_vars,
        job_title=job_data.get("title", ""),
        company_name=job_data.get("company", ""),
        cover_letter_body=paragraphs_html,
        letter_date=datetime.now().strftime("%B %d, %Y"),
    )

    tmp = tempfile.NamedTemporaryFile(
        suffix=".pdf",
        prefix=f"cover_letter_{_safe_filename(job_data.get('company', 'company'))}_",
        delete=False,
    )
    tmp.close()
    HTML(string=html_content, base_url=str(_RESUME_DATA_DIR)).write_pdf(tmp.name)
    logger.info("Cover letter PDF rendered: %s", tmp.name)
    return Path(tmp.name)


def _check_render_deps() -> None:
    """Raise a clear error if WeasyPrint or Jinja2 are not installed."""
    missing = []
    try:
        import weasyprint  # noqa: F401
    except ImportError:
        missing.append("weasyprint")
    try:
        import jinja2  # noqa: F401
    except ImportError:
        missing.append("Jinja2")
    if missing:
        raise RuntimeError(
            f"Missing PDF rendering dependencies: {', '.join(missing)}. "
            "Install with: pip install weasyprint Jinja2\n"
            "WeasyPrint also needs system libs — see tools/job_tools.py header."
        )


# ===========================================================================
# STEP 4 — Personal Note
# ===========================================================================

def generate_personal_note(job_data: JobData, llm) -> str:
    """
    Generate a short, personal-style note for the job application.

    Loads all .txt files from personal_notes_examples/ as few-shot style examples.
    The LLM writes a new note matching the user's voice and style.

    Args:
        job_data: Parsed job data dict.
        llm: LangChain LLM instance.

    Returns:
        Short personal note as plain text (3-6 sentences).
    """
    examples = _load_personal_note_examples()

    if not examples:
        logger.warning(
            "No personal note examples found in %s. "
            "Add .txt files to that folder to improve note quality.",
            _PERSONAL_NOTES_DIR,
        )
        examples_block = "(No examples provided — write in a warm, genuine professional tone.)"
    else:
        examples_block = "\n\n---\n\n".join(
            f"Example {i + 1}:\n{ex}" for i, ex in enumerate(examples)
        )

    system = (
        "You are a writing assistant helping someone craft a personal note to send "
        "alongside a job application. Match the style, tone, and length of the examples exactly. "
        "The note should be genuine, warm, and specific to this role and company. "
        "Keep it to 3-6 sentences. Plain text only."
    )
    human = (
        f"Role: {job_data.get('title')}\n"
        f"Company: {job_data.get('company')}\n\n"
        f"Style examples from this person's past notes:\n\n{examples_block}\n\n"
        "Write a personal note for this application."
    )
    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
    return (resp.content or "").strip()


def _load_personal_note_examples() -> List[str]:
    """Load all .txt files from personal_notes_examples/ as a list of strings."""
    if not _PERSONAL_NOTES_DIR.exists():
        return []
    examples = []
    for path in sorted(_PERSONAL_NOTES_DIR.glob("*.txt")):
        text = path.read_text(encoding="utf-8").strip()
        if text:
            examples.append(text)
    return examples


# ===========================================================================
# STEP 5 — Notion Logging
# ===========================================================================

def log_job_to_notion(job_data: JobData) -> Dict[str, str]:
    """
    Create a Notion page in the Jobs database to log this application.

    Creates the page with all properties and appends the full job description
    as paragraph blocks in the page body (supports descriptions > 2000 chars).

    Required env var: JOBS_DATABASE_ID

    Notion DB must have these properties:
      Company (title)           — company name
      Position (rich_text)      — job position/title
      Status (multi_select)     — always set to "Applied"
      Application Date (date)   — today's date
      Next Action (multi_select)— always set to "Waiting"
      Website (url)             — company website (derived from job URL domain)
      Reference Link (url)      — original job listing URL
      City (select)             — city of the position

    Args:
        job_data: Dict with keys: title, company, url, description, location.

    Returns:
        Dict with page_id and notion_url.
    """
    db_id = os.getenv("JOBS_DATABASE_ID", "")
    if not db_id:
        raise ValueError("Missing JOBS_DATABASE_ID environment variable.")

    notion_api_key = os.getenv("NOTION_API_KEY", "")
    if not notion_api_key:
        raise ValueError("Missing NOTION_API_KEY environment variable.")

    client = NotionClient(auth=notion_api_key)
    today = datetime.now().strftime("%Y-%m-%d")

    # Derive company website from job URL (scheme + netloc only)
    job_url = job_data.get("url") or ""
    company_website: Optional[str] = None
    if job_url:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(job_url)
            company_website = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            company_website = None

    # City: use only the city portion if location is "City, Country" style
    raw_location = job_data.get("location", "") or ""
    city = raw_location.split(",")[0].strip() if raw_location else ""

    properties: Dict[str, Any] = {
        # Title property — must be the "title" type in Notion
        "Company": {
            "title": [{"type": "text", "text": {"content": job_data.get("company", "Unknown Company")}}]
        },
        "Position": {
            "rich_text": [{"type": "text", "text": {"content": job_data.get("title", "")}}]
        },
        "Status": {
            "multi_select": [{"name": "Applied"}]
        },
        "Application Date": {
            "date": {"start": today}
        },
        "Next Action": {
            "multi_select": [{"name": "Waiting"}]
        },
        "Reference Link": {
            "url": job_url or None
        },
    }

    if company_website:
        properties["Website"] = {"url": company_website}

    if city:
        properties["City"] = {"select": {"name": city}}

    # Build page body blocks from full job description
    description = job_data.get("description", "")
    body_blocks = _build_notion_text_blocks(description) if description else []

    try:
        page = client.pages.create(
            parent={"database_id": db_id},
            properties=properties,
            children=body_blocks,
        )
    except APIResponseError as exc:
        raise RuntimeError(f"Failed to create Notion job page: {exc}") from exc

    page_id = page.get("id", "")
    notion_url = page.get("url", "")
    logger.info("Notion job page created: %s", notion_url)
    return {"page_id": page_id, "notion_url": notion_url}


def _build_notion_text_blocks(text: str) -> List[Dict[str, Any]]:
    """
    Split text into Notion paragraph blocks, respecting the 2000-char API limit.

    Each block contains at most _NOTION_BLOCK_CHAR_LIMIT characters.
    Adds a header block first so the description is visually separated.
    """
    blocks: List[Dict[str, Any]] = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "Job Description"}}]
            },
        }
    ]
    # Chunk by paragraphs first, then by character limit
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        # Split large paragraphs into chunks
        for chunk_start in range(0, len(paragraph), _NOTION_BLOCK_CHAR_LIMIT):
            chunk = paragraph[chunk_start: chunk_start + _NOTION_BLOCK_CHAR_LIMIT]
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": chunk}}]
                    },
                }
            )
    return blocks


# ===========================================================================
# Utilities
# ===========================================================================

def _safe_filename(name: str) -> str:
    """Convert a string to a safe filename component."""
    return re.sub(r"[^\w\-]", "_", name)[:30]


# ===========================================================================
# MAIN ORCHESTRATOR
# ===========================================================================

async def run_job_application_workflow(
    url: str,
    llm,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    """
    Execute the full 5-step job application pipeline.

    Step 6 (Telegram sending) is handled by the caller (app.py) using the
    file paths and text returned here.

    Args:
        url: Job listing URL provided by the user.
        llm: LangChain LLM instance (e.g. ChatOpenAI).
        progress_callback: Optional async callable(message: str) for step updates.

    Returns:
        Dict with keys:
          job_data       — parsed job fields
          company_summary — 2-3 sentence company description
          resume_path    — Path to generated resume PDF (temp file)
          cover_letter_path — Path to generated cover letter PDF (temp file)
          personal_note  — Short personal note string
          notion_url     — URL of the created Notion page

    Raises:
        RuntimeError: On scraping, PDF rendering, or Notion failures.
    """
    import asyncio

    async def _progress(msg: str) -> None:
        if progress_callback:
            await progress_callback(msg)
        logger.info("[job workflow] %s", msg)

    # ------------------------------------------------------------------
    # Step 1 — Scrape
    # ------------------------------------------------------------------
    await _progress("Step 1/5 — Scraping job listing...")
    raw_data = await asyncio.to_thread(scrape_job_listing, url)

    await _progress("Step 1/5 — Parsing job data (LLM)...")
    job_data = await asyncio.to_thread(_parse_job_with_llm, raw_data, llm)

    title = job_data.get("title", "Unknown Position")
    company = job_data.get("company", "Unknown Company")
    await _progress(f"Scraped: *{title}* at *{company}*")

    # ------------------------------------------------------------------
    # Step 2 — Company Research
    # ------------------------------------------------------------------
    await _progress("Step 2/5 — Researching company...")
    company_summary = await asyncio.to_thread(research_company, company, llm)
    await _progress(f"Research done.")

    # ------------------------------------------------------------------
    # Step 3 — Generate Documents
    # ------------------------------------------------------------------
    await _progress("Step 3/5 — Generating resume & cover letter...")
    resume_path, cover_letter_path = await asyncio.to_thread(
        generate_documents, job_data, company_summary, llm
    )
    await _progress("Documents generated.")

    # ------------------------------------------------------------------
    # Step 4 — Personal Note
    # ------------------------------------------------------------------
    await _progress("Step 4/5 — Writing personal note...")
    personal_note = await asyncio.to_thread(generate_personal_note, job_data, llm)
    await _progress("Personal note ready.")

    # ------------------------------------------------------------------
    # Step 5 — Log to Notion
    # ------------------------------------------------------------------
    await _progress("Step 5/5 — Logging to Notion...")
    notion_result = await asyncio.to_thread(log_job_to_notion, job_data)
    await _progress(f"Notion page created.")

    return {
        "job_data": job_data,
        "company_summary": company_summary,
        "resume_path": resume_path,
        "cover_letter_path": cover_letter_path,
        "personal_note": personal_note,
        "notion_url": notion_result.get("notion_url", ""),
    }
