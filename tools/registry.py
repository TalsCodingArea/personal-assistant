from tools.notion_tools import get_income_between_dates, get_finance_rules, get_database_schema, get_expenses_between_dates, get_last_expenses

def get_tools():
    return [get_income_between_dates, get_finance_rules, get_database_schema, get_expenses_between_dates, get_last_expenses]