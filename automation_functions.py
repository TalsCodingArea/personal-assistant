from base_scripts import *
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
import json
from pathlib import Path
load_dotenv()

_BUDGET_DATA_DIR = Path(__file__).parent / "budget_data"

GMAIL_SMTP_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
notion_client = Client(auth=os.environ["NOTION_API_KEY"])
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

def morning_summary():
    month_ago_date = datetime.now() - timedelta(days=90)
    filter_dict = {
        "and": [
            {
                "property": "Date",
                "date": {
                    "on_or_after": month_ago_date.strftime("%Y-%m-%d")
                }
            }
        ]
    }

    notion_client = Client(auth=os.environ["NOTION_API_KEY"])
    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    last_90_days_data = get_notion_pages(notion_client, database_id=os.environ["DAY_RATING_DATABASE_ID"], filter=filter_dict)
    last_90_days_scores = [(entry['properties']["Day's Rating"]['formula']["number"], entry['properties']["Date"]["date"]["start"]) for entry in last_90_days_data]
    filter_dict = {
        "and": [
            {
                "property": "Date",
                "date": {
                    "on_or_after": month_ago_date.strftime("%Y-%m-%d")
                }
            },
            {
                "property": "Name",
                "title": {
                    "contains": "Workout"
                }
            }
        ]
    }
    last_90_days_workouts = get_notion_pages(notion_client, database_id=os.environ["PERSONAL_GROWTH_ENTRIES_DATABASE_ID"], filter=filter_dict)
    last_90_days_workouts = [entry['properties']['Date']['date']['start'] for entry in last_90_days_workouts]
    prompt = f"""Each day I log a day score that is affected by how many tasks I've managed to complete and my workout streaks.
    The number of tasks I completed is multiplied by the percent of tasks completed that day and it's added to the current workout streak count (if I worked out that day)
    Based on the following data from the last 90 days, provide a cheerful summary of my performance yesterday in comparison to the previous days, and highlight my current workout streak, so I can reflect on it this morning:
    Scores and Dates: {last_90_days_scores}
    Workout Dates: {last_90_days_workouts}
    """

    answer = ask_openai(prompt)
    return answer.replace("**", "*")

def get_weekly_spending_summary(category: str=""):

    """Fetches and summarizes weekly spending from a Notion database."""
    notion_client = Client(auth=os.environ["NOTION_API_KEY"])
    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    last_sunday = datetime.now() - timedelta(days=datetime.now().weekday() + 1)
    filter_dict = {
        "and": [
            {
                "property": "Date",
                "date": {
                    "on_or_after": last_sunday.strftime("%Y-%m-%d")
                }
            }
        ]
    }
    if category:
        filter_dict["and"].append({
            "property": "Category",
            "select": {
                "equals": category
            }
        })

    expenses_data = get_notion_pages(notion_client, database_id="your_expenses_database_id", filter=filter_dict)
    expenses_list = [(entry['properties']['Amount']['number'], entry['properties']['Category']['select']['name'], entry['properties']['Date']['date']['start']) for entry in expenses_data]

    prompt = f"""Provide a summary of my spending over the last week based on the following data:
    Expenses (Amount, Category, Date): {expenses_list}
    """

    answer = ask_openai(prompt)
    return answer.replace("**", "*")

def evaluate_expense(last_expense: str):
    """Logs a new expense into the Notion database."""
    current_month_expenses = get_notion_pages(notion_client, database_id=os.environ["EXPENSES_DATABASE_ID"], filter={
        "and": [
            {
                "property": "Date",
                "date": {
                    "on_or_after": datetime.now().replace(day=1).strftime("%Y-%m-%d")
                }
            },
            {
                "property": "Tag",
                "multi_select": {
                    "contains": "Tal 👨🏻"
                }
            }
        ]
    })
    exclude_props = ["Yearly Finance Vacation 🏖️", "Yearly Finance Lifestyle 🏞️", "Yearly Finance Car 🚗",
                     "Invoice", "Academic Yearly Finance", "Budget", "Yearly Finance Spendings 📦",
                     "Yearly Finance Subscription ♻️", "Payment Method", "Shiri Budget", "Financial Analytics",
                     "Yearly Finance Home 🏡", "Tag"]
    clean_current_month_expenses = notion_response_simplifier(current_month_expenses, exclude=exclude_props)
    for entry in clean_current_month_expenses:
        if entry.get('Actual') and entry['Actual']:
            entry["Amount"] = entry["Amount"] * entry["Actual"]
    current_month_income = get_notion_pages(notion_client, database_id=os.environ["INCOME_DATABASE_ID"], filter={
        "and": [
            {
                "property": "Date",
                "date": {
                    "on_or_after": datetime.now().replace(day=1).strftime("%Y-%m-%d")
                }
            }
        ]
    })
    clean_current_month_income = notion_response_simplifier(current_month_income, exclude=exclude_props)
    total_income = sum(entry['Amount'] for entry in clean_current_month_income)
    bills_and_rent_past_data = get_notion_pages(notion_client, database_id=os.environ["EXPENSES_DATABASE_ID"], filter={
        "and": [
            {
                "property": "Date",
                "date": {
                    "before": datetime.now().replace(day=1).strftime("%Y-%m-%d")
                }
            },
            {
                "or": [
                    {
                        "property": "Sub Category",
                        "multi_select": {
                            "contains": "Bills 🧾"
                        }
                    },
                    {
                        "property": "Sub Category",
                        "multi_select": {
                            "contains": "Rent 💰"
                        }
                    }
                ]
            }
        ]
    })
    clean_bills_and_rent = notion_response_simplifier(bills_and_rent_past_data, exclude=exclude_props)
    expenses_goal = f"""
    I'm located in Israel so my currency is in ILS.
    My goal is to keep my "Need" type expenses under {total_income * 0.5} and my "Want" type expenses under {total_income * 0.3} each month.
    Bills & Rent this month should be predictable with the data from previous months.
    This is the 
    Based on the expenses so far this month: {clean_current_month_expenses}, provide me with a brief summary of how I'm doing towards my goals.
    This is the bills and rent data from previous months to help you understand my typical fixed costs: {clean_bills_and_rent}
    Your response should be concise and to the point. Make it with emojis and symbols so it will be engaging and easy to understand. No more than 3 sentences.
    This action is triggered when I log a new expense. So take into account that I've just logged an expense for {last_expense}.
    Locate if this new expense is "Want" or "Need" and reflect on that type of expense in your answer.
    Example responses (tailor them to the current situation): 
    Withing budget:
    "🟢 'Need' X / Y
    📈 At this pace: ~Z this month on "Needs"
    ✅ Looking good - spending is controlled, just keep an eye on Food & Drink ☕"

    Close to limit:
    "⚠️ Wants are getting tight — you’re at 85% of budget and we're only half way through the month. and relatively high spending in Category X.
    Food & Drink is the main driver. Consider slowing down this week 🍽️"

    Predictable breach:
    "🚨 At the current pace, Needs will hit 5,000 ILS this month
    — mainly due to consistent high spending in Category X.
    Consider adjusting your spending habits to stay within budget 📉"

    Over budget:
    "❌ This pushes Wants over budget.
    Current pace leads to 2,300 ILS this month — Food & Drink is the main leak 🚨"

    Stick to the examples structure but change it to be more engagins,  make it relevant to my current spending.
    """
    system_message = f"You are a personal finance assistant helping me track my expenses and stay within my budget."
    answer = ask_openai(expenses_goal, system_message=system_message)

    return answer.replace("**", "*")
def backfill_spending_habits():
    """
    One-time backfill: fetches January and February 2026 expenses and seeds
    budget_data/spending_habits.json with a 2-month baseline.
    Safe to run multiple times — skips a month if it was already tracked.
    """
    from tools.notion_tools import get_expenses_between_dates

    months = [
        ("2026-01-01", "2026-01-31", "2026-01"),
        ("2026-02-01", "2026-02-28", "2026-02"),
    ]

    habits_path = _BUDGET_DATA_DIR / "spending_habits.json"
    habits_path.parent.mkdir(parents=True, exist_ok=True)
    if habits_path.exists() and habits_path.read_text(encoding="utf-8").strip():
        habits = json.loads(habits_path.read_text(encoding="utf-8"))
    else:
        habits = {"last_updated": None, "months_tracked": 0, "by_subcategory": {}}

    processed = []
    for start, end, label in months:
        data = get_expenses_between_dates.invoke({"start_date": start, "end_date": end})
        months_tracked = habits.get("months_tracked", 0)
        existing = habits.get("by_subcategory", {})

        for sub, amount in data.get("by_subcategory", {}).items():
            if sub in existing:
                prev_avg = existing[sub]["avg"]
                prev_min = existing[sub]["min"]
                prev_max = existing[sub]["max"]
                new_avg = round(((prev_avg * months_tracked) + amount) / (months_tracked + 1), 2)
                existing[sub] = {
                    "avg": new_avg,
                    "min": round(min(prev_min, amount), 2),
                    "max": round(max(prev_max, amount), 2),
                    "last": round(amount, 2),
                }
            else:
                existing[sub] = {
                    "avg": round(amount, 2),
                    "min": round(amount, 2),
                    "max": round(amount, 2),
                    "last": round(amount, 2),
                }

        habits["by_subcategory"] = existing
        habits["months_tracked"] = months_tracked + 1
        habits["last_updated"] = label
        processed.append(label)

    habits_path.write_text(json.dumps(habits, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"✅ Backfill complete — seeded {len(processed)} months: {', '.join(processed)}. {len(habits['by_subcategory'])} subcategories tracked."


def update_spending_habits():
    """
    Fetches last month's expenses from Notion, computes per-subcategory stats,
    and updates budget_data/spending_habits.json with rolling averages.
    Run automatically on the 1st of each month.
    """
    from tools.notion_tools import get_expenses_between_dates

    today = datetime.now()
    first_of_last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    last_of_last_month = today.replace(day=1) - timedelta(days=1)
    month_label = first_of_last_month.strftime("%Y-%m")

    data = get_expenses_between_dates.invoke({
        "start_date": first_of_last_month.strftime("%Y-%m-%d"),
        "end_date": last_of_last_month.strftime("%Y-%m-%d"),
    })

    habits_path = _BUDGET_DATA_DIR / "spending_habits.json"
    habits_path.parent.mkdir(parents=True, exist_ok=True)
    if habits_path.exists() and habits_path.read_text(encoding="utf-8").strip():
        habits = json.loads(habits_path.read_text(encoding="utf-8"))
    else:
        habits = {"last_updated": None, "months_tracked": 0, "by_subcategory": {}}

    months_tracked = habits.get("months_tracked", 0)
    existing = habits.get("by_subcategory", {})

    for sub, amount in data.get("by_subcategory", {}).items():
        if sub in existing:
            prev_avg = existing[sub]["avg"]
            prev_min = existing[sub]["min"]
            prev_max = existing[sub]["max"]
            new_avg = round(((prev_avg * months_tracked) + amount) / (months_tracked + 1), 2)
            existing[sub] = {
                "avg": new_avg,
                "min": round(min(prev_min, amount), 2),
                "max": round(max(prev_max, amount), 2),
                "last": round(amount, 2),
            }
        else:
            existing[sub] = {
                "avg": round(amount, 2),
                "min": round(amount, 2),
                "max": round(amount, 2),
                "last": round(amount, 2),
            }

    habits["by_subcategory"] = existing
    habits["months_tracked"] = months_tracked + 1
    habits["last_updated"] = month_label
    habits_path.write_text(json.dumps(habits, ensure_ascii=False, indent=2), encoding="utf-8")

    return f"✅ Spending habits updated for {month_label} — {len(existing)} subcategories tracked."


properties = {
    "Description": {
        "type": "title",
        "content": "Coffee"
    },
    "Amount": {
        "type": "number",
        "content": 3.5
    },
    "Category": {
        "type": "multi_select",
        "content": ["Food & Drink"]
    },
    "Sub Category":{
        "type": "multi_select",
        "content": ["Bills 🧾"]
    },
    "Date": {
        "type": "date",
        "content": datetime.now().strftime("%Y-%m-%d")
    },
    "Tag": {
        "type": "multi_select",
        "content": ["Tal 👨🏻"]
    }
}
# print(log_expense(properties))