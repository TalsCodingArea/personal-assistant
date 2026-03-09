import re
from langchain_core.prompts import ChatPromptTemplate

# ---------------------------------------------------------------------------
# Fast-path pattern matching (no LLM cost)
# ---------------------------------------------------------------------------

# Cancel / exit phrases — used to escape any active workflow
_CANCEL_RE = re.compile(
    r"\b(cancel|stop|exit|quit|abort|never\s*mind|forget\s*it|leave\s*it|"
    r"done\s*for\s*now|get\s*me\s*out|enough)\b",
    re.IGNORECASE,
)

# Budget planning trigger phrases → always budget
_BUDGET_RE = re.compile(
    r"\b(set|plan|start|do|run|review)\s+(my\s+)?(monthly\s+)?budget\b"
    r"|monthly\s+budget"
    r"|\bbudget\s+(review|plan|workflow|for\s+(this\s+)?month)\b"
    r"|plan\s+my\s+(finances|budget|month)",
    re.IGNORECASE,
)

# Explicit trigger phrases followed by a URL → always job_application
_JOB_URL_RE = re.compile(
    r"(^|\s)(apply|job|apply for|applying to|job application)\s+(https?://\S+)",
    re.IGNORECASE,
)

# A message that is ONLY a URL (possibly with leading/trailing whitespace)
_BARE_URL_RE = re.compile(r"^(https?://\S+)$")

# Known job board URL patterns — bare URLs from these domains are auto-classified
_JOB_DOMAINS = (
    "linkedin.com/jobs",
    "indeed.com",
    "glassdoor.com",
    "jobs.lever.co",
    "boards.greenhouse.io",
    "apply.workable.com",
    "jobs.ashbyhq.com",
)
# URL path segments that typically indicate a job listing page
_JOB_PATH_SEGMENTS = ("/jobs/", "/careers/", "/job/", "/position/")

# ---------------------------------------------------------------------------
# LLM prompt (fallback for ambiguous messages)
# ---------------------------------------------------------------------------
INTENT_PROMPT = ChatPromptTemplate.from_messages([
    (
        "system",
        (
            "Classify the user's message into ONE label: "
            "'finance', 'movies', 'job_application', 'budget', or 'general'. "
            "Use 'job_application' when the message contains a job listing URL "
            "or asks to apply for / research a job position. "
            "Use 'budget' when the user wants to plan, set, or review their monthly budget "
            "(e.g. 'set my budget for this month', 'let's do the monthly budget', "
            "'budget review', 'plan my finances for the month'). "
            "Return only the label, nothing else."
        ),
    ),
    ("human", "{text}"),
])


async def classify_intent(llm, text: str) -> str:
    """
    Classify a user message into one of: finance, movies, job_application, budget, general.

    Hardcoded regex runs first (zero LLM cost) for unambiguous patterns.
    Falls back to an LLM call for everything else.
    """
    stripped = (text or "").strip()

    # Fast path 0: budget planning trigger
    if _BUDGET_RE.search(stripped):
        return "budget"

    # Fast path 1: explicit "apply <url>" / "job <url>" pattern
    if _JOB_URL_RE.search(stripped):
        return "job_application"

    # Fast path 2: bare URL from a known job board or job-related path
    bare_match = _BARE_URL_RE.match(stripped)
    if bare_match:
        url_lower = bare_match.group(1).lower()
        if any(domain in url_lower for domain in _JOB_DOMAINS):
            return "job_application"
        if any(seg in url_lower for seg in _JOB_PATH_SEGMENTS):
            return "job_application"

    # LLM fallback
    resp = await llm.ainvoke(INTENT_PROMPT.format_messages(text=stripped))
    label = (resp.content or "").strip().lower()
    if label not in {"finance", "movies", "job_application", "budget", "general"}:
        return "general"
    return label


def extract_url_from_message(text: str) -> str:
    """
    Extract the first HTTP(S) URL from a message string.
    Returns an empty string if no URL is found.
    """
    match = re.search(r"https?://\S+", text or "")
    return match.group(0) if match else ""


def is_cancel_intent(text: str) -> bool:
    """Return True if the message is clearly asking to cancel / exit the current workflow."""
    return bool(_CANCEL_RE.search((text or "").strip()))


def is_job_url_fast(text: str) -> bool:
    """
    Return True if the message looks like a job application request without an LLM call.
    Covers explicit 'apply <url>' phrases and bare URLs from known job boards.
    """
    stripped = (text or "").strip()
    if _JOB_URL_RE.search(stripped):
        return True
    bare_match = _BARE_URL_RE.match(stripped)
    if bare_match:
        url_lower = bare_match.group(1).lower()
        if any(domain in url_lower for domain in _JOB_DOMAINS):
            return True
        if any(seg in url_lower for seg in _JOB_PATH_SEGMENTS):
            return True
    return False
