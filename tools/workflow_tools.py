"""
Tools that trigger multi-step workflows (budget planning, job application).

These are created per-chat-session via factory functions so they hold
a reference to the shared app-level session state without circular imports.
"""
import time

from langchain_core.messages import AIMessage
from langchain_core.tools import tool


def make_budget_tool(chat_id: str, budget_graph, budget_sessions: dict):
    """Return a start_budget_planning tool bound to this chat session."""

    @tool
    async def start_budget_planning() -> str:
        """
        Start the interactive monthly budget planning workflow.
        Use this when the user asks to plan, review, or set their monthly budget
        for the month. The workflow will guide them through recurring expenses,
        one-off costs, carryover savings, and produce a final breakdown.
        Relay the returned message directly to the user — it contains their first prompt.
        """
        from agent.budget_workflow import async_start_budget_workflow

        thread_id = f"budget_{chat_id}_{int(time.time())}"
        config = {"configurable": {"thread_id": thread_id}}
        state = await async_start_budget_workflow(budget_graph, config)
        budget_sessions[chat_id] = thread_id

        msgs = state.get("messages", [])
        last_ai = next((m for m in reversed(msgs) if isinstance(m, AIMessage)), None)
        return last_ai.content if last_ai else "Budget workflow started. What's your monthly budget?"

    return start_budget_planning


def make_job_tool(chat_id: str, pending_jobs: dict):
    """Return an apply_for_job tool that queues the pipeline for app.py to execute."""

    @tool
    def apply_for_job(url: str) -> str:
        """
        Trigger the job application pipeline for a given job listing URL.
        Use this when the user provides a job listing URL or asks to apply for a job.
        The pipeline scrapes the listing, tailors the resume, writes a cover letter,
        generates a personal note, and logs the application to Notion.
        """
        pending_jobs[chat_id] = url
        return "Job application pipeline started — I'll send the documents shortly."

    return apply_for_job
