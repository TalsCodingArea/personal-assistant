from tools.notion_tools import (
    get_income_between_dates,
    get_finance_rules,
    get_database_schema,
    get_expenses_between_dates,
    get_last_expenses,
    get_movies_data_from_notion_database,
    get_spending_habits,
    get_financial_advisor_habits,
    update_financial_advisor_habit,
)
from tools.ideas_tools import create_idea_in_notion
from tools.israeli_market_tools import (
    get_exchange_rates,
    get_tase_stock_quote,
    get_tase_index,
)


def get_tools():
    """Base tools always available to the agent."""
    return [
        get_income_between_dates,
        get_finance_rules,
        get_database_schema,
        get_expenses_between_dates,
        get_last_expenses,
        get_movies_data_from_notion_database,
        get_spending_habits,
        get_financial_advisor_habits,
        update_financial_advisor_habit,
        create_idea_in_notion,
        get_exchange_rates,
        get_tase_stock_quote,
        get_tase_index,
    ]


def get_workflow_tools(chat_id: str, budget_graph, budget_sessions: dict, pending_jobs: dict):
    """Session-bound workflow trigger tools, created once per chat_id."""
    from tools.workflow_tools import make_budget_tool, make_job_tool

    return [
        make_budget_tool(chat_id, budget_graph, budget_sessions),
        make_job_tool(chat_id, pending_jobs),
    ]
