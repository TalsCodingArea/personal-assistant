import os, re, json
from typing import Dict, Any, Optional, Tuple
import httpx
from slack_bolt import App as SlackApp
from slack_bolt.adapter.socket_mode import SocketModeHandler
from base_scripts import *
from automation_functions import *
import importlib.util
import inspect
from dotenv import load_dotenv
from slack_sdk.errors import SlackApiError

load_dotenv()

def run_ollama(prompt: str, *, json_mode: bool = False, model: str = "qwen2.5:1.5b-instruct", system: Optional[str] = None) -> str:
    base = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0}
    }
    if json_mode:
        payload["format"] = "json"
    if system:
        payload["system"] = system
    with httpx.Client(timeout=60.0) as client:
        # Probe the server so failures are obvious
        client.get(f"{base}/api/version").raise_for_status()
        r = client.post(f"{base}/api/generate", json=payload)
        r.raise_for_status()
        return r.json().get("response", "").strip()

def load_module_from_file(filepath):
    """Dynamically load a Python module from a file path."""
    module_name = os.path.splitext(os.path.basename(filepath))[0]
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def extract_tools_and_functions(filepath):
    module = load_module_from_file(filepath)

    TOOLS = {}
    FUNCTIONS = {}

    for name, obj in vars(module).items():
        if inspect.isfunction(obj) and obj.__module__ == module.__name__:
            # Extract arguments
            sig = inspect.signature(obj)
            args = [str(p) for p in sig.parameters.values()]

            # Fill TOOLS dict
            TOOLS[name] = {
                "description": inspect.getdoc(obj),
                "args": args,
            }

            # Fill FUNCTIONS dict
            FUNCTIONS[name] = obj

    return TOOLS, FUNCTIONS

TOOLS, FUNCTIONS = extract_tools_and_functions("functions.py")
AUTOMATION_FUNCTIONS = extract_tools_and_functions("automation_functions.py")[1]
TASKS = {"Add a to-do item": "add_to_things", "Add a movie to watchlist database": "add_movie", "Log a watched movie and rating": "log_movie_watch_and_rating", "Give a movie suggestion": "suggest_movie", "Give a financial evaluation": "get_monthly_financial_evaluation", "Process a receipt using its URL": "receipt_url_to_notion_with_evaluation"}
TASKS_CONTEXT = """
The user can perform the following tasks:
0. Add a to-do item: Adds a new item to the Things app to help the user manage their tasks.
1. Add a movie to watchlist database: Adds a movie title to the user's watchlist database in Notion.
2. Log a watched movie and rating: Logs a movie that the user has watched along with their rating into the Notion database.
3. Give a movie suggestion: Suggests a movie for the user to watch based on their preferences.
4. Give a financial evaluation: Provides a monthly financial evaluation based on the user's expenses and income.
5. Process a receipt using its URL: Processes a receipt image from a given URL and adds the relevant information to the user's Notion database.
If the user is sending a URL, it is most likely for the "Process a receipt using its URL" task.
If he sends a movie name and rating, it is most likely for the "Log a watched movie and rating" task.
If he's just sending a movie name, it is most likely for the "Add a movie to watchlist database" task.
Asking for a movie suggestion is most likely when the user is requesting a recommendation without providing a specific title.
Your goal is to help the user by selecting the most appropriate task from the list above.
Given the user message, extract the main task to be performed.
Return ONLY a JSON object with 'task' and 'index' fields, where 'task' is the task name and 'index' is its position in the list.
"""

# --- ENV ---
BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
OLLAMA = os.environ.get("OLLAMA_HOST", "http://ollama:11434").rstrip("/")
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "llama3.1:8b")
CHAT_CHANNEL_ID = "C09DXFG7P70"
LOGS_CHANNEL_ID = "C09QX3M5H8U"
EXPENSE_CHANNEL = "C0A491EC83B"

# --- SLACK APP ---
app = SlackApp(token=BOT_TOKEN)

# ------------ Router prompt -------------
ROUTER_SYSTEM = (
    "You are a tool router. Choose ONE tool and matching arguments according to the user's message.\n"
    "Return your answer as a JSON object with 'tool' and 'args' fields.\n"
)

ASSISTANCE_SYSTEM = (
    "You are a personal assistant. The user has triggered a request for a certain process to start.\n"
    "Write a playful one-line response that explains the process that is starting according to the docstring.\n"
)

def resolve_sender_name(client, event):
    """
    Return a human-readable sender name for any message:
    - Human users -> real_name or display_name
    - Bots/integrations -> bot_profile name
    - Fallback to user ID if needed
    """
    # If message from a normal user
    user_id = event.get("user")
    if user_id:
        try:
            ui = client.users_info(user=user_id)
            profile = ui.get("user", {}).get("profile", {})
            return profile.get("display_name") or profile.get("real_name") or user_id
        except SlackApiError as e:
            print(f"[users_info error] {e}")
            return user_id

    # If message from a bot/integration
    bot_profile = event.get("bot_profile") or {}
    if bot_profile:
        return bot_profile.get("name") or bot_profile.get("real_name") or "Bot"

    # Edge cases (system messages, etc.)
    return "Unknown Sender"

def run_tool_and_format(tool: str, args: Dict[str, Any]) -> str:
    try:
        data = FUNCTIONS[tool](**args)
        return data
    except Exception as e:
        return f"⚠️ Tool `{tool}` failed: {e}"

def handle_message_via_router(text: str) -> Tuple[str, str, Dict[str, Any]]:
    try:
        text = text.replace("<", " ").replace(">", " ")
        task = run_ollama(
            f"You are a task extractor. Given the user message, extract the main task to be performed.\n"
            f"User message: {text}\n"
            f"Tasks: {json.dumps(list(TASKS.keys()))}\n"
            'Return ONLY a JSON object with the name of the task that best matches the user message where "task" is the task name.',
            json_mode=True,
            system=TASKS_CONTEXT
        )
        task = json.loads(task)["task"]
        decision = run_ollama(
            f"The user wants to perform the task: {task}\n"
            f"This is the description for the function that will perform this task: {TOOLS[TASKS[task]]['description']}\n"
            f"The function arguments are: {TOOLS[TASKS[task]]['args']}\n"
            f"Based on the user's original message: {text}\n"
            f"Return ONLY a JSON object with 'args' field, where 'args' is a dictionary of arguments to call the described function.", json_mode=True
        )
        decision = json.loads(decision)
        greeting_message = run_ollama(
            f"{ASSISTANCE_SYSTEM}\n"
            f"The process's docstring is: {TOOLS[TASKS[task]]['description']}\n"
            'Return ONLY JSON: {"response":"one-liner response"}', json_mode=True
        )
        greeting_message = json.loads(greeting_message)
    except Exception as e:
        return f"⚠️ Router call failed: {e}", "", {}
    return greeting_message["response"], TASKS[task], decision["args"]

def file_share_subtype_handler(event, logger):
    files = event.get("files", [])
    for file in files:
        if file.get('filetype') in ['jpg', 'png', 'pdf']:
            return (
                f"📄 Detected a file upload: {file.get('name')}. Processing receipt...",
                "file_receipt_to_notion_with_evaluation",
                {"file_dict": file}
            )
    return ""

# ------------ Slack handlers -------------
@app.event("app_mention")
def on_mention(body, say, logger):
    text = body.get("event", {}).get("text", "")
    logger.info(f"[@mention] {text}")
    text = re.sub(r"<@[^>]+>\s*", "", text)
    greeting_message, tool, args = (handle_message_via_router(text))
    say(greeting_message)
    say(run_tool_and_format(tool, args))

@app.event("message")
def on_message_events(body, event, client, logger, say):
    """
    Listens to all message events the bot receives.
    Filters to a single channel by ID.
    Skips non-standard message subtypes (joins, pins, edits).
    Prints the sender's name.
    """
    channel = event.get("channel")
    subtype = event.get("subtype")
    bot_id = event.get("bot_id")
    text = event.get("text", "")
    sender = resolve_sender_name(client, event)
    if channel == EXPENSE_CHANNEL:
        say(evaluate_expense(text))
    if channel == LOGS_CHANNEL_ID:
        if subtype not in (None, "bot_message", "thread_broadcast"):
            return
        try:
            func = AUTOMATION_FUNCTIONS.get(text)
            if func:
                say(func(), channel=CHAT_CHANNEL_ID)
            else:
                say(f"⚠️ No automation function found for the command: {text}")
        except Exception as e:
            say(logger.error(f"[automation error] {e}"))
        return

    elif channel == CHAT_CHANNEL_ID:
        if subtype and subtype not in (None, "thread_broadcast"):
            if subtype == "file_share" and sender == "Tal Shaubi":
                greeting_message, tool, args = file_share_subtype_handler(event, logger)
                if tool != "":
                    say(greeting_message, channel=LOGS_CHANNEL_ID)
                    say(run_tool_and_format(tool, args))
                return
            return
        elif sender == "Tal Shaubi":
            greeting_message, tool, args = (handle_message_via_router(text))
            say(greeting_message, channel=LOGS_CHANNEL_ID)
            if tool != "":
                say(run_tool_and_format(tool, args))
            else:
                say("⚠️ No suitable tool found for this request.")

@app.command("/help")
def help_command(ack, say):
    ack()
    help_text = (
        f"""Hello! I'm your personal assistant bot. These are functions that I can perform and their descriptions:
        {json.dumps(TOOLS, indent=2)}
        """
    )
    say(help_text)

def send_startup_message(client):
    try:
        client.chat_postMessage(
            channel=CHAT_CHANNEL_ID,
            text="🤖 Personal Assistant Bot is now online and ready to assist you!"
        )
    except SlackApiError as e:
        print(f"[startup message error] {e}")

if __name__ == "__main__":
    print("✅ Starting Slack bot with Ollama router...", flush=True)
    print(f"ENV OLLAMA={OLLAMA} MODEL={ROUTER_MODEL}", flush=True)
    send_startup_message(app.client)
    SocketModeHandler(app, APP_TOKEN).start()