"""
Ideas planner tool — saves a fully brainstormed idea to the Notion IDEAS database.

The created page is structured so that an LLM (or a developer) can pick it up
and implement the idea from scratch without any additional context.

Page layout:
  - Overview        (summary paragraph)
  - The Problem     (paragraph)
  - The Solution    (paragraph)
  - Execution Path  (numbered list — step-by-step implementation)
  - Milestones      (to-do checkboxes)
  - Recommended Tools & Technologies  (bulleted list)
  - Additional Notes (paragraph, optional)

Required env var:
  IDEAS_DATABASE_ID  — Notion database ID for ideas
  NOTION_API_KEY     — already used throughout the project
"""

import logging
import os
import re
from typing import Any, Dict, List

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_NOTION_CHAR_LIMIT = 2000


# ---------------------------------------------------------------------------
# Notion block builders
# ---------------------------------------------------------------------------

def _h2(text: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _para(text: str) -> List[Dict[str, Any]]:
    """Split long text into multiple paragraph blocks respecting the 2000-char limit."""
    blocks = []
    for chunk_start in range(0, len(text), _NOTION_CHAR_LIMIT):
        chunk = text[chunk_start: chunk_start + _NOTION_CHAR_LIMIT]
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
        })
    return blocks or [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}]


def _numbered(text: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:_NOTION_CHAR_LIMIT]}}]},
    }


def _todo(text: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "to_do",
        "to_do": {
            "rich_text": [{"type": "text", "text": {"content": text[:_NOTION_CHAR_LIMIT]}}],
            "checked": False,
        },
    }


def _bullet(text: str) -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:_NOTION_CHAR_LIMIT]}}]},
    }


def _parse_list_items(text: str) -> List[str]:
    """Strip leading list markers and return individual non-empty items."""
    items = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^(\d+[.)]\s*|[-*•]\s*)", "", line).strip()
        if line:
            items.append(line)
    return items


def _build_idea_blocks(
    summary: str,
    problem: str,
    solution: str,
    execution_path: str,
    milestones: str,
    recommended_tools: str,
    additional_notes: str,
) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []

    blocks.append(_h2("Overview"))
    blocks.extend(_para(summary))

    blocks.append(_h2("The Problem"))
    blocks.extend(_para(problem))

    blocks.append(_h2("The Solution"))
    blocks.extend(_para(solution))

    blocks.append(_h2("Execution Path"))
    for item in _parse_list_items(execution_path):
        blocks.append(_numbered(item))

    blocks.append(_h2("Milestones"))
    for item in _parse_list_items(milestones):
        blocks.append(_todo(item))

    blocks.append(_h2("Recommended Tools & Technologies"))
    for item in _parse_list_items(recommended_tools):
        blocks.append(_bullet(item))

    if additional_notes and additional_notes.strip():
        blocks.append(_h2("Additional Notes"))
        blocks.extend(_para(additional_notes))

    return blocks


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@tool
def create_idea_in_notion(
    title: str,
    summary: str,
    problem: str,
    solution: str,
    execution_path: str,
    milestones: str,
    recommended_tools: str,
    additional_notes: str = "",
) -> str:
    """
    Save a fully developed idea to the Notion IDEAS database.

    Call this at the END of a brainstorming session, once the idea has been
    fleshed out with enough detail that an LLM could implement it from scratch.

    Args:
        title:             Short, clear name for the idea.
        summary:           Full 2–3 paragraph description — what it is, who it's for, why it matters.
                           Must be comprehensive enough to stand alone as a spec.
        problem:           The specific problem or pain point this idea addresses.
        solution:          How the idea solves the problem — the core mechanism and approach.
        execution_path:    Step-by-step implementation plan, one step per line.
                           Be specific: include environment setup, core features, integrations,
                           testing strategy, and deployment. Number each step.
        milestones:        Concrete, measurable checkpoints, one per line.
                           Example: "MVP working locally", "First real user test", "Production deploy".
        recommended_tools: Technologies, libraries, APIs, and services, one per line.
                           Include a brief reason for each choice when relevant.
        additional_notes:  Extra context, constraints, inspiration sources, open questions,
                           or decisions deferred to later (optional).

    Returns:
        The URL of the created Notion page, or an error message.
    """
    from notion_client import Client as NotionClient
    from notion_client.errors import APIResponseError

    db_id = os.getenv("IDEAS_DATABASE_ID", "")
    if not db_id:
        return "Error: IDEAS_DATABASE_ID is not set in .env — add it and try again."

    notion_api_key = os.getenv("NOTION_API_KEY", "")
    if not notion_api_key:
        return "Error: NOTION_API_KEY is not set in .env."

    client = NotionClient(auth=notion_api_key)

    properties = {
        "Name": {
            "title": [{"type": "text", "text": {"content": title}}]
        }
    }

    blocks = _build_idea_blocks(
        summary=summary,
        problem=problem,
        solution=solution,
        execution_path=execution_path,
        milestones=milestones,
        recommended_tools=recommended_tools,
        additional_notes=additional_notes,
    )

    try:
        # Notion API: max 100 children per create request
        page = client.pages.create(
            parent={"database_id": db_id},
            properties=properties,
            children=blocks[:100],
        )
        # Append any overflow blocks
        if len(blocks) > 100:
            page_id = page["id"]
            for i in range(100, len(blocks), 100):
                client.blocks.children.append(
                    block_id=page_id,
                    children=blocks[i: i + 100],
                )
        notion_url = page.get("url", "")
        logger.info("Idea page created: %s", notion_url)
        return notion_url or "Notion page created (URL unavailable)."
    except APIResponseError as exc:
        logger.error("Failed to create idea page: %s", exc)
        return f"Error creating Notion page: {exc}"
