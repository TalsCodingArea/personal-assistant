from base_scripts import *
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
load_dotenv()

GMAIL_SMTP_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

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
    last_90_days_data = get_notion_pages(notion_client, database_id="REDACTED_NOTION_DB_ID", filter=filter_dict)
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
    last_90_days_workouts = get_notion_pages(notion_client, database_id="REDACTED_NOTION_DB_ID", filter=filter_dict)
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