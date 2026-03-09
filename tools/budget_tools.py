"""
tools/budget_tools.py — Monthly budget analysis and planning utilities.

All functions here are pure Python / Notion queries — no LLM calls.
LLM usage is intentionally kept in agent/budget_workflow.py (parsing user inputs only).

Key functions:
  fetch_monthly_expenses(year, month)          → List of expense dicts
  analyze_spending_patterns(lookback_months)   → Per-sub-category stats + trends
  identify_repeating_categories(analysis)      → (repeating, suggested_new)
  compute_budget_breakdown(...)                → Final allocation dict
  format_analysis_message(...)                 → Human-readable analysis string
  format_breakdown_message(...)               → Human-readable breakdown string

Persistence (budget_data/repeating_categories.json):
  load_persisted_categories()                  → (confirmed: List[Dict], excluded: Set[str])
  save_persisted_categories(confirmed, excluded_names) → writes to JSON
  merge_categories_with_persisted(...)         → merges Notion-detected with saved prefs
"""

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# A sub-category must appear in at least this many months to be "repeating"
_REPEATING_THRESHOLD = 2

# ≥ this % change between first and last month counts as a trend
_TREND_SIGNIFICANT_PCT = 0.15


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_monthly_expenses(year: int, month: int) -> List[Dict[str, Any]]:
    """
    Fetch all expenses for a given calendar month from Notion.

    Returns a list of dicts with keys:
      Description, Category (list), Sub Category (list), Date, Amount
    """
    from tools.notion_tools import get_expenses_between_dates

    start = date(year, month, 1).isoformat()
    if month == 12:
        end = (date(year + 1, 1, 1) - timedelta(days=1)).isoformat()
    else:
        end = (date(year, month + 1, 1) - timedelta(days=1)).isoformat()

    try:
        return get_expenses_between_dates.invoke({"start_date": start, "end_date": end})
    except Exception as exc:
        logger.warning("Failed to fetch expenses for %d-%02d: %s", year, month, exc)
        return []


def _get_sub_categories(expense: Dict[str, Any]) -> List[str]:
    """
    Extract sub-category names from an expense dict.
    Handles both list (multi_select) and plain string.
    Falls back to Category, then 'Uncategorized'.
    """
    raw = expense.get("Sub Category", "")
    if isinstance(raw, list) and raw:
        return [s.strip() for s in raw if s.strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]

    # Fallback to Category
    raw = expense.get("Category", "")
    if isinstance(raw, list) and raw:
        return [s.strip() for s in raw if s.strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]

    return ["Uncategorized"]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_spending_patterns(lookback_months: int = 3) -> Dict[str, Any]:
    """
    Fetch the last N complete calendar months and compute per-sub-category stats.

    Returns:
    {
      "months_analyzed": ["2026-01", "2026-02", "2026-03"],
      "by_category": {
        "Groceries 🛒": {
          "monthly_totals": [820.0, 750.0, 890.0],   # oldest → newest
          "months_present": 3,
          "avg": 820.0,
          "trend": "↑",  # "↑" | "↓" | "→"
        },
        ...
      }
    }
    """
    today = date.today()

    # Build list of (year, month) tuples, going back from the previous complete month
    months: List[Tuple[int, int]] = []
    for i in range(lookback_months, 0, -1):
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        months.append((y, m))

    month_labels = [f"{y}-{m:02d}" for y, m in months]

    # Fetch expenses per month
    monthly_expenses: List[List[Dict]] = [fetch_monthly_expenses(y, m) for y, m in months]

    # Aggregate totals by sub-category per month
    by_category: Dict[str, Dict[str, Any]] = {}

    for month_idx, expenses in enumerate(monthly_expenses):
        month_totals: Dict[str, float] = {}
        for exp in expenses:
            amount = float(exp.get("Amount") or 0)
            for cat in _get_sub_categories(exp):
                month_totals[cat] = month_totals.get(cat, 0.0) + amount

        for cat, total in month_totals.items():
            if cat not in by_category:
                by_category[cat] = {"monthly_totals": [0.0] * lookback_months}
            by_category[cat]["monthly_totals"][month_idx] = total

    # Compute derived stats for each category
    for cat, data in by_category.items():
        totals = data["monthly_totals"]
        nonzero = [t for t in totals if t > 0]
        data["months_present"] = len(nonzero)
        data["avg"] = sum(nonzero) / len(nonzero) if nonzero else 0.0

        # Trend: compare earliest vs latest month with spending
        if len(nonzero) >= 2:
            first = next(t for t in totals if t > 0)
            last = next(t for t in reversed(totals) if t > 0)
            if first > 0:
                change = (last - first) / first
                if change >= _TREND_SIGNIFICANT_PCT:
                    data["trend"] = "↑"
                elif change <= -_TREND_SIGNIFICANT_PCT:
                    data["trend"] = "↓"
                else:
                    data["trend"] = "→"
            else:
                data["trend"] = "→"
        else:
            data["trend"] = "→"

    return {
        "months_analyzed": month_labels,
        "by_category": by_category,
    }


def identify_repeating_categories(
    analysis: Dict[str, Any],
    threshold_months: int = _REPEATING_THRESHOLD,
) -> Tuple[List[Dict], List[Dict]]:
    """
    Split categories into repeating (appear in ≥ threshold months)
    and suggested_new (appeared in exactly threshold-1 months).

    Each item dict: {name, avg, months_present, trend, expected_amount}
    Returns: (repeating, suggested_new)
    """
    total_months = len(analysis.get("months_analyzed", []))
    repeating: List[Dict] = []
    suggested_new: List[Dict] = []

    for cat, data in analysis["by_category"].items():
        item = {
            "name": cat,
            "avg": round(data["avg"], 2),
            "months_present": data["months_present"],
            "trend": data["trend"],
            "expected_amount": round(data["avg"]),  # default = rounded average
        }
        if data["months_present"] >= threshold_months:
            repeating.append(item)
        elif data["months_present"] == threshold_months - 1 and total_months >= 3:
            suggested_new.append(item)

    # Sort biggest spenders first
    repeating.sort(key=lambda x: x["avg"], reverse=True)
    suggested_new.sort(key=lambda x: x["avg"], reverse=True)

    return repeating, suggested_new


# ---------------------------------------------------------------------------
# Budget computation
# ---------------------------------------------------------------------------

def compute_budget_breakdown(
    monthly_budget: float,
    repeating_categories: List[Dict],
    unexpected_expenses: List[Dict],
    carryover: float = 0.0,
) -> Dict[str, Any]:
    """
    Compute the full monthly budget breakdown.

    Args:
        monthly_budget:       Total budget for the month.
        repeating_categories: List of {name, expected_amount, ...}.
        unexpected_expenses:  List of {description, amount}.
        carryover:            Savings carried over from last month.

    Returns dict with:
      monthly_budget, carryover, total_available,
      repeating_total, unexpected_total, committed_total, discretionary,
      categories_breakdown, unexpected_breakdown
    """
    total_available = monthly_budget + carryover
    repeating_total = sum(float(c.get("expected_amount", 0)) for c in repeating_categories)
    unexpected_total = sum(float(e.get("amount", 0)) for e in unexpected_expenses)
    committed_total = repeating_total + unexpected_total
    discretionary = total_available - committed_total

    return {
        "monthly_budget": monthly_budget,
        "carryover": carryover,
        "total_available": total_available,
        "repeating_total": repeating_total,
        "unexpected_total": unexpected_total,
        "committed_total": committed_total,
        "discretionary": discretionary,
        "categories_breakdown": list(repeating_categories),
        "unexpected_breakdown": list(unexpected_expenses),
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_analysis_message(
    analysis: Dict[str, Any],
    repeating: List[Dict],
    suggested_new: List[Dict],
) -> str:
    """Format the spending analysis as a readable message for the user."""
    months = analysis.get("months_analyzed", [])
    months_str = ", ".join(months) if months else "recent months"
    total_repeating = sum(c["expected_amount"] for c in repeating)

    lines = [
        f"📊 Spending Analysis ({months_str})\n",
        f"Recurring categories — expected total: ₪{total_repeating:,.0f}",
    ]
    for cat in repeating:
        lines.append(
            f"  • {cat['name']}: ₪{cat['expected_amount']:,.0f} {cat['trend']} "
            f"(avg over {cat['months_present']} months)"
        )

    if suggested_new:
        lines.append("\nNewly detected (appeared recently — add to recurring?):")
        for cat in suggested_new:
            lines.append(
                f"  • {cat['name']}: ₪{cat['avg']:,.0f} {cat['trend']}"
            )

    lines += [
        "",
        "Review and adjust. Commands:",
        "  set <category> <amount>   — change expected amount",
        "  remove <category>         — remove from recurring",
        "  add <name> <amount>       — add a new recurring category",
        "  confirm <category>        — accept a suggested category",
        "  done                      — proceed to next step",
    ]
    return "\n".join(lines)


def format_breakdown_message(breakdown: Dict[str, Any]) -> str:
    """Format the final budget breakdown as a readable message."""
    lines = [
        "💰 Monthly Budget Breakdown\n",
        f"Monthly budget:    ₪{breakdown['monthly_budget']:,.0f}",
    ]
    if breakdown["carryover"]:
        lines.append(f"Carryover savings: ₪{breakdown['carryover']:,.0f}")
    lines.append(f"Total available:   ₪{breakdown['total_available']:,.0f}\n")

    lines.append("Recurring expenses:")
    for cat in breakdown["categories_breakdown"]:
        lines.append(f"  • {cat['name']}: ₪{cat['expected_amount']:,.0f}")
    lines.append(f"  Subtotal: ₪{breakdown['repeating_total']:,.0f}\n")

    if breakdown["unexpected_breakdown"]:
        lines.append("Upcoming one-off expenses:")
        for exp in breakdown["unexpected_breakdown"]:
            lines.append(f"  • {exp['description']}: ₪{exp['amount']:,.0f}")
        lines.append(f"  Subtotal: ₪{breakdown['unexpected_total']:,.0f}\n")

    disc = breakdown["discretionary"]
    disc_sign = "+" if disc >= 0 else ""
    status = "OK" if disc >= 0 else "OVER BUDGET"
    lines.append(f"Committed total:   ₪{breakdown['committed_total']:,.0f}")
    lines.append(f"Discretionary:     {disc_sign}₪{disc:,.0f}  [{status}]")

    if disc < 0:
        lines.append("\nYou're over budget. Consider reducing some categories.")
    else:
        lines.append("\nThis is what's left for unplanned spending and savings.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Category name matching (used by the workflow to apply user adjustments)
# ---------------------------------------------------------------------------

def _strip_emoji(text: str) -> str:
    """Remove emoji and punctuation for fuzzy matching."""
    return re.sub(r"[^\w\s]", "", text).strip().lower()


def find_category_by_name(name: str, categories: List[Dict]) -> Optional[Dict]:
    """
    Case-insensitive, emoji-tolerant search for a category in a list.
    Returns the matching dict or None.
    """
    query = _strip_emoji(name)
    for cat in categories:
        if query in _strip_emoji(cat["name"]) or _strip_emoji(cat["name"]) in query:
            return cat
    return None


# ---------------------------------------------------------------------------
# Persistent category preferences  (budget_data/repeating_categories.json)
# ---------------------------------------------------------------------------

_BUDGET_DATA_DIR = Path(__file__).parent.parent / "budget_data"
_PERSISTED_CATEGORIES_FILE = _BUDGET_DATA_DIR / "repeating_categories.json"


def load_persisted_categories() -> Tuple[List[Dict], Set[str]]:
    """
    Load the user's saved category preferences.

    Returns:
        confirmed  — List of {name, expected_amount} the user has confirmed as recurring.
        excluded   — Set of category names the user has explicitly removed (never suggest again).
    """
    if not _PERSISTED_CATEGORIES_FILE.exists():
        return [], set()
    try:
        data = json.loads(_PERSISTED_CATEGORIES_FILE.read_text(encoding="utf-8"))
        confirmed = data.get("confirmed", [])
        excluded = set(data.get("excluded", []))
        return confirmed, excluded
    except Exception as exc:
        logger.warning("Failed to load persisted categories: %s", exc)
        return [], set()


def save_persisted_categories(confirmed: List[Dict], excluded_names: Set[str]) -> None:
    """
    Save confirmed recurring categories and the excluded list to disk.

    Args:
        confirmed:      List of {name, expected_amount} to persist.
        excluded_names: Set of category names to never suggest as recurring.
    """
    _BUDGET_DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Only store name + expected_amount (keep the file lean)
    slim_confirmed = [{"name": c["name"], "expected_amount": c.get("expected_amount", 0)} for c in confirmed]
    data = {"confirmed": slim_confirmed, "excluded": sorted(excluded_names)}
    try:
        _PERSISTED_CATEGORIES_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Persisted %d recurring categories, %d excluded.", len(slim_confirmed), len(excluded_names))
    except Exception as exc:
        logger.warning("Failed to save persisted categories: %s", exc)


def merge_categories_with_persisted(
    detected_repeating: List[Dict],
    detected_suggested: List[Dict],
    persisted_confirmed: List[Dict],
    excluded_names: Set[str],
) -> Tuple[List[Dict], List[Dict]]:
    """
    Merge Notion-detected categories with the user's saved preferences.

    Rules:
    - Persisted confirmed categories are always included in repeating.
      If Notion data has a newer average, update expected_amount toward it (but keep user's last set value).
    - Categories in excluded_names are never shown (repeating or suggested).
    - Newly detected categories not in persisted and not excluded go to suggested_new.

    Returns: (final_repeating, final_suggested)
    """
    # Index persisted by stripped name for matching
    persisted_index = {_strip_emoji(c["name"]): c for c in persisted_confirmed}
    # Index detected by stripped name for amount updates
    detected_index = {_strip_emoji(c["name"]): c for c in detected_repeating}

    final_repeating: List[Dict] = []
    seen_names: Set[str] = set()

    # 1. Start with persisted confirmed — always included, update avg if Notion has fresher data
    for p in persisted_confirmed:
        key = _strip_emoji(p["name"])
        if p["name"] in excluded_names or key in {_strip_emoji(e) for e in excluded_names}:
            continue  # user later excluded this — respect the exclusion
        entry = dict(p)
        if key in detected_index:
            d = detected_index[key]
            entry.setdefault("avg", d["avg"])
            entry.setdefault("months_present", d["months_present"])
            entry.setdefault("trend", d["trend"])
            # Update expected_amount if user hasn't manually set it
            # (we detect this if expected_amount == rounded avg from last save)
            if abs(entry["expected_amount"] - round(p["expected_amount"])) < 1:
                entry["expected_amount"] = round(d["avg"])
        else:
            entry.setdefault("avg", entry["expected_amount"])
            entry.setdefault("months_present", 0)
            entry.setdefault("trend", "→")
        final_repeating.append(entry)
        seen_names.add(key)

    # 2. Add newly detected that aren't persisted and aren't excluded
    excluded_stripped = {_strip_emoji(e) for e in excluded_names}
    for d in detected_repeating:
        key = _strip_emoji(d["name"])
        if key in seen_names or key in excluded_stripped:
            continue
        final_repeating.append(dict(d))
        seen_names.add(key)

    # 3. Suggested new: detected suggested, not excluded, not already repeating
    final_suggested: List[Dict] = []
    for s in detected_suggested:
        key = _strip_emoji(s["name"])
        if key in seen_names or key in excluded_stripped:
            continue
        final_suggested.append(dict(s))

    # Sort repeating biggest first
    final_repeating.sort(key=lambda x: x.get("expected_amount", 0), reverse=True)

    return final_repeating, final_suggested


# ---------------------------------------------------------------------------
# Notion Budget DB logging
# ---------------------------------------------------------------------------

def log_monthly_budget_to_notion(monthly_budget: float) -> str:
    """
    Create or update the current month's entry in the Notion Budget database.

    Behaviour:
    - Retrieves the DB schema to discover the title property name dynamically.
    - Queries all pages and looks for one whose title contains the current month
      label (e.g. "March 2026") OR whose date property falls in the current month.
    - If a matching page is found: updates its "Budget" (number) property.
    - If none is found: creates a new page with the month label as title.

    Required env var: BUDGET_DATABASE_ID
    Required DB property: Budget (number)

    Returns:
        The Notion page URL of the created/updated page, or "" on failure.
    """
    import os
    from notion_client import Client as NotionClient

    db_id = os.getenv("BUDGET_DATABASE_ID", "")
    if not db_id:
        raise ValueError("Missing BUDGET_DATABASE_ID environment variable.")

    notion_api_key = os.getenv("NOTION_API_KEY", "")
    if not notion_api_key:
        raise ValueError("Missing NOTION_API_KEY environment variable.")

    client = NotionClient(auth=notion_api_key)
    today = date.today()
    month_label = today.strftime("%B %Y")   # e.g. "March 2026"
    year_month = today.strftime("%Y-%m")    # e.g. "2026-03"

    # --- Discover title property name ---
    try:
        db_info = client.databases.retrieve(database_id=db_id)
        title_prop = next(
            (name for name, prop in db_info["properties"].items() if prop["type"] == "title"),
            "Name",
        )
    except Exception as exc:
        logger.warning("Could not retrieve Budget DB schema: %s", exc)
        title_prop = "Name"

    # --- Search for existing page for this month ---
    existing_page = None
    try:
        all_pages = client.databases.query(database_id=db_id).get("results", [])
        for page in all_pages:
            props = page.get("properties", {})

            # Match by title containing the month label
            title_data = props.get(title_prop, {}).get("title", [])
            title_text = "".join(t.get("plain_text", "") for t in title_data)
            if month_label.lower() in title_text.lower():
                existing_page = page
                break

            # Match by any date property falling in the current month
            for prop_data in props.values():
                if prop_data.get("type") == "date":
                    start = (prop_data.get("date") or {}).get("start", "")
                    if start[:7] == year_month:
                        existing_page = page
                        break
            if existing_page:
                break
    except Exception as exc:
        logger.warning("Could not query Budget DB: %s", exc)

    # --- Create or update ---
    budget_property = {"Budget": {"number": monthly_budget}}

    try:
        if existing_page:
            page = client.pages.update(
                page_id=existing_page["id"],
                properties=budget_property,
            )
            logger.info("Updated Budget DB page for %s: ₪%s", month_label, monthly_budget)
        else:
            page = client.pages.create(
                parent={"database_id": db_id},
                properties={
                    title_prop: {
                        "title": [{"type": "text", "text": {"content": month_label}}]
                    },
                    **budget_property,
                },
            )
            logger.info("Created Budget DB page for %s: ₪%s", month_label, monthly_budget)

        return page.get("url", "")
    except Exception as exc:
        logger.error("Failed to write to Budget DB: %s", exc)
        raise RuntimeError(f"Notion Budget DB update failed: {exc}") from exc
