import os, re, json, sys, time, shutil, subprocess
from typing import Dict, Any, Optional, Tuple
import httpx
from slack_bolt import App as SlackApp
from slack_bolt.adapter.socket_mode import SocketModeHandler
from base_scripts import *
import importlib.util
import inspect
from dotenv import load_dotenv
from slack_sdk.errors import SlackApiError

load_dotenv()

def run_ollama(prompt: str, *, json_mode: bool = False, model: str = "qwen2.5:1.5b-instruct") -> str:
    base = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0}
    }
    if json_mode:
        payload["format"] = "json"

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

# --- ENV ---
BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
OLLAMA = os.environ.get("OLLAMA_HOST", "http://ollama:11434").rstrip("/")
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "llama3.1:8b")
TARGET_CHANNEL_ID = "C09DXFG7P70"

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
        if 'receipt' in text.lower():
            url = re.search(r'(https?://\S+)', text).group(1)[:-1]
            decision = f"{{\"tool\":\"receipt_url_to_notion_with_evaluation\",\"args\":{{\"pdf_url\":\"{url}\"}}}}"
        else:
            decision = run_ollama(
                f"{ROUTER_SYSTEM}\n"
                f"Available tools: {json.dumps(TOOLS)}\n"
                f"User message: {text}\n"
                'Return ONLY JSON: "tool":"","args":{}}', json_mode=True
            )
        decision = json.loads(decision)
        greeting_message = run_ollama(
            f"{ASSISTANCE_SYSTEM}\n"
            f"The process's docstring is: {TOOLS[decision['tool']]['description']}\n"
            'Return ONLY JSON: {"response":"one-liner response"}', json_mode=True
        )
        greeting_message = json.loads(greeting_message)
    except Exception as e:
        return f"⚠️ Router call failed: {e}", "", {}
    return greeting_message["response"], decision["tool"], decision["args"]

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
    # Only process plain messages in the target channel
    if event.get("channel") != TARGET_CHANNEL_ID:
        return

    # Ignore non-message content (edits, joins, etc.)
    subtype = event.get("subtype")
    if subtype and subtype not in (None, "thread_broadcast"):
        return

    sender = resolve_sender_name(client, event)
    text = event.get("text", "")

    if sender == "Tal Shaubi":
        greeting_message, tool, args = (handle_message_via_router(text))
        say(greeting_message)
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
            channel=TARGET_CHANNEL_ID,
            text="🤖 Personal Assistant Bot is now online and ready to assist you!"
        )
    except SlackApiError as e:
        print(f"[startup message error] {e}")

if __name__ == "__main__":
    print("✅ Starting Slack bot with Ollama router...", flush=True)
    print(f"ENV OLLAMA={OLLAMA} MODEL={ROUTER_MODEL}", flush=True)
    send_startup_message(app.client)
    SocketModeHandler(app, APP_TOKEN).start()