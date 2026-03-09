"""
agent/budget_workflow.py — LangGraph-based monthly budget planning workflow.

Architecture:
  StateGraph with two nodes:
    1. "analyze"  — runs once at start: fetches Notion data, merges persisted prefs, emits first message
    2. "chat"     — handles every subsequent user message based on current phase

  The graph is compiled with interrupt_before=["chat"], so it pauses after
  emitting each bot message and waits for the next user input. State is
  persisted across Telegram messages via MemorySaver (keyed by thread_id = chat_id).

Phases (stored in state["phase"]):
  "budget_input"  → waiting for the user to enter their monthly budget
  "review"        → user adjusts recurring category amounts/membership
  "unexpected"    → user enters upcoming one-off expenses
  "carryover"     → user inputs savings carried over from last month
  "summary"       → bot shows full breakdown, user confirms
  "done"          → workflow complete

LLM usage (one call per user turn):
  _agent_turn() sends a context-rich system prompt per phase and returns:
    {"action": str, "data": {...}, "response": "<natural language reply>"}
  The "response" is shown to the user; "action" + "data" drive state changes.

Persistence:
  On "done" in the review phase, confirmed recurring categories and excluded names
  are saved to budget_data/repeating_categories.json via save_persisted_categories().
  On startup, analyze_node loads this file and merges it with fresh Notion data.

Integration with Telegram (use helpers, not graph.invoke directly):
  graph = create_budget_graph(llm)
  config = {"configurable": {"thread_id": str(chat_id)}}

  state = start_budget_workflow(graph, config)
  state = continue_budget_workflow(graph, config, user_text)   # sync
  state = await async_continue_budget_workflow(graph, config, user_text)  # async
"""

from __future__ import annotations

import json
import logging
import re
from functools import partial
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from tools.budget_tools import (
    analyze_spending_patterns,
    compute_budget_breakdown,
    find_category_by_name,
    format_analysis_message,
    format_breakdown_message,
    identify_repeating_categories,
    load_persisted_categories,
    log_monthly_budget_to_notion,
    merge_categories_with_persisted,
    save_persisted_categories,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------

class BudgetState(TypedDict):
    messages: Annotated[List[BaseMessage], add_messages]
    phase: str                      # see module docstring for valid values
    monthly_budget: float
    analysis: Dict[str, Any]
    repeating_categories: List[Dict]     # [{name, expected_amount, avg, months_present, trend}]
    suggested_new_categories: List[Dict]
    excluded_categories: List[str]       # category names permanently removed by the user
    unexpected_expenses: List[Dict]      # [{description, amount}]
    carryover: float
    breakdown: Optional[Dict]


def make_initial_state(preset_budget: float = 0.0) -> Dict[str, Any]:
    """
    Return a fresh BudgetState-compatible dict to start a new workflow run.

    Args:
        preset_budget: If > 0, skip the budget input phase and use this value directly.
    """
    return {
        "messages": [],
        "phase": "init",
        "monthly_budget": preset_budget,
        "analysis": {},
        "repeating_categories": [],
        "suggested_new_categories": [],
        "excluded_categories": [],
        "unexpected_expenses": [],
        "carryover": 0.0,
        "breakdown": None,
    }


# ---------------------------------------------------------------------------
# LLM agent turn  (one call per user message → action + natural-language reply)
# ---------------------------------------------------------------------------

def _call_llm_json(llm, system: str, user_text: str) -> Dict[str, Any]:
    """Send a structured prompt to the LLM and return decoded JSON."""
    resp = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=user_text),
    ])
    raw = (resp.content or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("LLM returned non-JSON: %s", raw)
        return {}


def _agent_turn(state: BudgetState, user_text: str, llm) -> Dict[str, Any]:
    """
    Single LLM call per user turn.

    Builds a phase-specific prompt with full current state context, then asks
    the LLM to return:
      {"action": "<name>", "data": {...}, "response": "<message to show user>"}

    The "response" is the bot's natural-language reply.
    The "action" + "data" drive state changes in chat_node.
    """
    phase = state.get("phase")
    cats = state.get("repeating_categories", [])
    suggested = state.get("suggested_new_categories", [])
    budget = state.get("monthly_budget", 0.0)
    unexpected = state.get("unexpected_expenses", [])

    if phase == "budget_input":
        state_info = ""
        role = "The user needs to tell you their total monthly budget in Israeli Shekels (₪)."
        actions = """
  set_budget — user provided a valid amount
    {"action": "set_budget", "data": {"amount": <number>}, "response": "<your reply>"}
  clarify — amount unclear or missing
    {"action": "clarify", "data": {}, "response": "<your reply>"}"""

    elif phase == "review":
        cats_str = "\n".join(
            f"  • {c['name']}: ₪{c['expected_amount']:,.0f} {c.get('trend','→')}"
            for c in cats
        ) or "  (none yet)"
        sug_str = "\n".join(
            f"  • {c['name']}: ₪{c['avg']:,.0f}"
            for c in suggested
        ) or "  (none)"
        state_info = (
            f"Monthly budget: ₪{budget:,.0f}\n\n"
            f"Current recurring categories:\n{cats_str}\n\n"
            f"Suggested new categories (appeared recently):\n{sug_str}"
        )
        role = (
            "The user is reviewing their recurring monthly expense categories. "
            "They can freely adjust amounts, remove categories, add new ones, "
            "confirm suggested ones, or say they're done. "
            "If they remove a category, it will be permanently excluded from future suggestions."
        )
        actions = """
  adjust — change expected amount for an existing category
    {"action": "adjust", "data": {"category": "<name>", "amount": <number>}, "response": "<your reply>"}
  remove — permanently remove a category from recurring suggestions
    {"action": "remove", "data": {"category": "<name>"}, "response": "<your reply>"}
  add — add a brand new recurring category
    {"action": "add", "data": {"name": "<name>", "amount": <number>}, "response": "<your reply>"}
  confirm — accept a suggested category into the recurring list
    {"action": "confirm", "data": {"category": "<name>"}, "response": "<your reply>"}
  done — all categories look good, move on
    {"action": "done", "data": {}, "response": "<your reply>"}
  clarify — intent is unclear, ask a follow-up question
    {"action": "clarify", "data": {}, "response": "<your reply>"}"""

    elif phase == "unexpected":
        exp_str = "\n".join(
            f"  • {e['description']}: ₪{e['amount']:,.0f}" for e in unexpected
        ) or "  (none added yet)"
        state_info = (
            f"Monthly budget: ₪{budget:,.0f}\n\n"
            f"One-off expenses added so far:\n{exp_str}"
        )
        role = (
            "The user is listing upcoming one-off expenses for this month — "
            "non-recurring things like car service, tuition, fines, a specific purchase. "
            "Each message may add one expense or signal they are done."
        )
        actions = """
  add_expense — user named an expense with an amount
    {"action": "add_expense", "data": {"description": "<short label>", "amount": <positive number>}, "response": "<your reply>"}
  done — no more one-off expenses
    {"action": "done", "data": {}, "response": "<your reply>"}
  clarify — unclear input
    {"action": "clarify", "data": {}, "response": "<your reply>"}"""

    elif phase == "carryover":
        state_info = f"Monthly budget: ₪{budget:,.0f}"
        role = (
            "Ask if the user has savings from last month to add to this month's budget. "
            "A carryover of 0 is valid (they spent it all or it goes to a separate savings account)."
        )
        actions = """
  set_carryover — user gave a carryover amount (can be 0)
    {"action": "set_carryover", "data": {"amount": <number>}, "response": "<your reply>"}
  clarify — unclear
    {"action": "clarify", "data": {}, "response": "<your reply>"}"""

    elif phase == "summary":
        bd = state.get("breakdown") or {}
        state_info = (
            f"Monthly budget: ₪{bd.get('monthly_budget', budget):,.0f}\n"
            f"Carryover: ₪{bd.get('carryover', 0):,.0f}\n"
            f"Total available: ₪{bd.get('total_available', budget):,.0f}\n"
            f"Committed: ₪{bd.get('committed_total', 0):,.0f}\n"
            f"Discretionary: ₪{bd.get('discretionary', 0):,.0f}"
        )
        role = "The user is reviewing the final budget breakdown and deciding whether to confirm it."
        actions = """
  confirm — user approves the plan and wants to save it
    {"action": "confirm", "data": {}, "response": "<your reply>"}
  clarify — user wants to change something or is not sure
    {"action": "clarify", "data": {}, "response": "<your reply>"}"""

    else:
        return {"action": "clarify", "data": {}, "response": "I'm not sure what to do here. Please try again."}

    system = f"""You are a friendly personal finance assistant managing the user's monthly budget review.

=== Current State ===
{state_info}

=== Your role ===
{role}

=== Available actions ===
Return exactly ONE of the following as valid JSON (no markdown, no extra text):{actions}

Guidelines:
- Understand the user's message naturally — they don't need exact command syntax
- Be conversational and concise in the "response" field
- Currency is Israeli Shekels (₪ / ILS)
- If the user says something like "looks good", "all good", "move on", treat it as "done"
"""

    return _call_llm_json(llm, system, user_text)


# ---------------------------------------------------------------------------
# Graph node: analyze
# ---------------------------------------------------------------------------

def analyze_node(state: BudgetState, llm) -> Dict[str, Any]:
    """
    Fetch Notion expense data, merge with persisted category preferences,
    and emit the first bot message.  Runs exactly once at the start.
    """
    analysis = analyze_spending_patterns(lookback_months=3)
    detected_repeating, detected_suggested = identify_repeating_categories(analysis)

    # Load persisted preferences and merge with fresh Notion data
    persisted_confirmed, excluded_names = load_persisted_categories()
    repeating, suggested_new = merge_categories_with_persisted(
        detected_repeating, detected_suggested, persisted_confirmed, excluded_names
    )

    if not state.get("monthly_budget"):
        msg = (
            "💼 Budget Workflow\n\n"
            "What is your total budget for this month? (enter amount in ₪)"
        )
        return {
            "phase": "budget_input",
            "analysis": analysis,
            "repeating_categories": repeating,
            "suggested_new_categories": suggested_new,
            "excluded_categories": list(excluded_names),
            "messages": [AIMessage(content=msg)],
        }

    msg = format_analysis_message(analysis, repeating, suggested_new)
    return {
        "phase": "review",
        "analysis": analysis,
        "repeating_categories": repeating,
        "suggested_new_categories": suggested_new,
        "excluded_categories": list(excluded_names),
        "messages": [AIMessage(content=msg)],
    }


# ---------------------------------------------------------------------------
# Graph node: chat  (handles ALL user turns after the initial analysis)
# ---------------------------------------------------------------------------

def chat_node(state: BudgetState, llm) -> Dict[str, Any]:
    """
    Route each incoming user message through the LLM agent, then apply
    the returned action to update the workflow state.
    """
    last_human = next(
        (m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)),
        None,
    )
    if not last_human:
        return {}

    user_text = last_human.content.strip()
    phase = state.get("phase", "review")

    # ------------------------------------------------------------------ #
    # budget_input: user sends their monthly budget amount                #
    # ------------------------------------------------------------------ #
    if phase == "budget_input":
        parsed = _agent_turn(state, user_text, llm)
        action = parsed.get("action", "clarify")
        response = parsed.get("response", "")

        if action == "set_budget":
            budget = float(parsed.get("data", {}).get("amount", 0))
            if budget > 0:
                analysis_msg = format_analysis_message(
                    state["analysis"],
                    state["repeating_categories"],
                    state["suggested_new_categories"],
                )
                full_response = f"{response}\n\n{analysis_msg}" if response else analysis_msg
                return {
                    "monthly_budget": budget,
                    "phase": "review",
                    "messages": [AIMessage(content=full_response)],
                }

        return {"messages": [AIMessage(content=response or "Please enter a valid budget amount in ₪.")]}

    # ------------------------------------------------------------------ #
    # review: user adjusts the recurring category list                   #
    # ------------------------------------------------------------------ #
    if phase == "review":
        cats = list(state.get("repeating_categories", []))
        suggested = list(state.get("suggested_new_categories", []))
        excluded = list(state.get("excluded_categories", []))

        parsed = _agent_turn(state, user_text, llm)
        action = parsed.get("action", "clarify")
        data = parsed.get("data", {})
        response = parsed.get("response", "")

        if action == "done":
            total = sum(c.get("expected_amount", 0) for c in cats)
            # Persist confirmed categories and exclusions
            save_persisted_categories(cats, set(excluded))
            phase_msg = (
                f"Recurring categories confirmed — expected total ₪{total:,.0f}.\n\n"
                "Now, do you have any upcoming one-off expenses this month?\n"
                "(e.g. car service, tuition, a fine)\n\n"
                "Enter each one, or say 'done' / 'none' to skip."
            )
            full_response = f"{response}\n\n{phase_msg}" if response else phase_msg
            return {
                "phase": "unexpected",
                "repeating_categories": cats,
                "messages": [AIMessage(content=full_response)],
            }

        if action == "adjust":
            cat = find_category_by_name(data.get("category", ""), cats)
            amount = float(data.get("amount", 0))
            if cat and amount > 0:
                cat["expected_amount"] = round(amount)
                return {
                    "repeating_categories": cats,
                    "messages": [AIMessage(content=response or f"Updated {cat['name']} to ₪{amount:,.0f}.")],
                }

        if action == "remove":
            cat = find_category_by_name(data.get("category", ""), cats)
            if cat:
                cats.remove(cat)
                excluded.append(cat["name"])
                return {
                    "repeating_categories": cats,
                    "excluded_categories": excluded,
                    "messages": [AIMessage(content=response or f"Removed {cat['name']} and won't suggest it again.")],
                }

        if action == "add":
            name = data.get("name", "").strip()
            amount = float(data.get("amount", 0))
            if name and amount > 0:
                cats.append({
                    "name": name,
                    "expected_amount": round(amount),
                    "avg": amount,
                    "months_present": 0,
                    "trend": "→",
                })
                return {
                    "repeating_categories": cats,
                    "messages": [AIMessage(content=response or f"Added {name}: ₪{amount:,.0f}.")],
                }

        if action == "confirm":
            sug = find_category_by_name(data.get("category", ""), suggested)
            if sug:
                suggested.remove(sug)
                cats.append(sug)
                return {
                    "repeating_categories": cats,
                    "suggested_new_categories": suggested,
                    "messages": [AIMessage(content=response or f"Added {sug['name']}: ₪{sug['expected_amount']:,.0f}.")],
                }

        # clarify or unrecognised
        return {"messages": [AIMessage(content=response or (
            "Not sure I got that. You can adjust amounts, remove categories, add new ones, or say 'done'."
        ))]}

    # ------------------------------------------------------------------ #
    # unexpected: user lists one-off upcoming expenses                   #
    # ------------------------------------------------------------------ #
    if phase == "unexpected":
        parsed = _agent_turn(state, user_text, llm)
        action = parsed.get("action", "clarify")
        data = parsed.get("data", {})
        response = parsed.get("response", "")

        if action == "done":
            phase_msg = (
                "Do you have any savings from last month to carry over into this month's budget?\n"
                "(Enter ₪ amount, or 0 / 'none'.)"
            )
            full_response = f"{response}\n\n{phase_msg}" if response else phase_msg
            return {
                "phase": "carryover",
                "messages": [AIMessage(content=full_response)],
            }

        if action == "add_expense":
            description = data.get("description", "Expense").strip()
            amount = float(data.get("amount", 0))
            if amount > 0:
                expenses = list(state.get("unexpected_expenses", []))
                expenses.append({"description": description, "amount": round(amount)})
                return {
                    "unexpected_expenses": expenses,
                    "messages": [AIMessage(content=response or f"Added {description} — ₪{amount:,.0f}. Anything else?")],
                }

        return {"messages": [AIMessage(content=response or "Please enter an expense (e.g. '300 car service') or say 'done'.")]}

    # ------------------------------------------------------------------ #
    # carryover: savings from last month                                  #
    # ------------------------------------------------------------------ #
    if phase == "carryover":
        parsed = _agent_turn(state, user_text, llm)
        action = parsed.get("action", "clarify")
        data = parsed.get("data", {})
        response = parsed.get("response", "")

        if action in ("set_carryover", "clarify"):
            carryover = float(data.get("amount", 0)) if action == "set_carryover" else 0.0

            budget = state.get("monthly_budget", 0.0)
            repeating = state.get("repeating_categories", [])
            unexpected = state.get("unexpected_expenses", [])
            breakdown = compute_budget_breakdown(budget, repeating, unexpected, carryover)

            breakdown_msg = format_breakdown_message(breakdown)
            confirm_hint = "\nType 'confirm' to save this plan, or let me know if you'd like to change anything."
            full_response = f"{response}\n\n{breakdown_msg}{confirm_hint}" if response else f"{breakdown_msg}{confirm_hint}"

            return {
                "carryover": carryover,
                "phase": "summary",
                "breakdown": breakdown,
                "messages": [AIMessage(content=full_response)],
            }

        return {"messages": [AIMessage(content=response or "How much savings are you carrying over from last month? (0 if none)")]}

    # ------------------------------------------------------------------ #
    # summary: user confirms or asks for changes                         #
    # ------------------------------------------------------------------ #
    if phase == "summary":
        parsed = _agent_turn(state, user_text, llm)
        action = parsed.get("action", "clarify")
        response = parsed.get("response", "")

        if action == "confirm":
            budget = state.get("monthly_budget", 0.0)
            notion_url = ""
            try:
                notion_url = log_monthly_budget_to_notion(budget)
            except Exception as exc:
                logger.warning("Could not update Notion Budget DB: %s", exc)

            confirm_msg = response or "Budget plan confirmed! Your recurring categories have been saved for next month."
            if notion_url:
                confirm_msg += f"\n\nNotion page updated: {notion_url}"
            elif not notion_url:
                confirm_msg += "\n\n(Could not reach the Notion Budget DB — check BUDGET_DATABASE_ID in .env)"

            return {
                "phase": "done",
                "messages": [AIMessage(content=confirm_msg)],
            }

        return {"messages": [AIMessage(content=response or "Type 'confirm' to save the plan, or let me know what you'd like to adjust.")]}

    return {}


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _phase_router(state: BudgetState) -> str:
    """After each chat turn, either loop or end."""
    return END if state.get("phase") == "done" else "chat"


def create_budget_graph(llm):
    """
    Build and compile the LangGraph budget workflow.

    Returns a compiled graph. Use the helpers below to drive it — do NOT
    call graph.invoke directly, as the resume pattern requires update_state first.
    """
    graph = StateGraph(BudgetState)

    graph.add_node("analyze", partial(analyze_node, llm=llm))
    graph.add_node("chat", partial(chat_node, llm=llm))

    graph.set_entry_point("analyze")
    graph.add_edge("analyze", "chat")
    graph.add_conditional_edges("chat", _phase_router)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer, interrupt_before=["chat"])


# ---------------------------------------------------------------------------
# Invocation helpers  (always use these — do NOT call graph.invoke directly)
# ---------------------------------------------------------------------------

async def async_start_budget_workflow(graph, config: dict, preset_budget: float = 0.0) -> dict:
    """Async variant of start_budget_workflow for use inside Telegram handlers."""
    return await graph.ainvoke(make_initial_state(preset_budget=preset_budget), config)


def start_budget_workflow(graph, config: dict, preset_budget: float = 0.0) -> dict:
    """
    Start a fresh budget workflow run.

    Args:
        graph:         Compiled graph from create_budget_graph().
        config:        {"configurable": {"thread_id": <unique session id>}}.
        preset_budget: If > 0, skip the budget-input phase.

    Returns the current graph state; check state["messages"] for the bot reply.
    """
    return graph.invoke(make_initial_state(preset_budget=preset_budget), config)


def continue_budget_workflow(graph, config: dict, user_text: str) -> dict:
    """
    Feed the user's next message into the workflow and advance to the next step.

    Uses update_state + invoke(None) to correctly resume from an interrupt_before
    checkpoint (rather than restarting the graph from the entry point).
    """
    graph.update_state(config, {"messages": [HumanMessage(content=user_text)]})
    return graph.invoke(None, config)


async def async_continue_budget_workflow(graph, config: dict, user_text: str) -> dict:
    """Async variant of continue_budget_workflow for use inside Telegram handlers."""
    await graph.aupdate_state(config, {"messages": [HumanMessage(content=user_text)]})
    return await graph.ainvoke(None, config)
