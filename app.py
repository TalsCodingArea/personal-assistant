import os, re, json, sys, time, shutil, subprocess
from typing import Dict, Any, Optional
import httpx
from slack_bolt import App as SlackApp
from slack_bolt.adapter.socket_mode import SocketModeHandler
from base_scripts import *
import importlib.util
import inspect
from dotenv import load_dotenv


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

load_dotenv()


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

# --- SLACK APP ---
app = SlackApp(token=BOT_TOKEN)

# ------------ Router prompt -------------
ROUTER_SYSTEM = (
    "You are a tool router. Choose ONE tool and matching arguments according to the user's message.\n"
    "Return your answer as a JSON object with 'tool' and 'args' fields.\n"
)

ASSISTANCE_SYSTEM = (
    "You are a personal assistant. Help the user with their requests.\n"
    "Write a playful one-line response that explains the starting process.\n"
)



def run_tool_and_format(tool: str, args: Dict[str, Any]) -> str:
    try:
        data = FUNCTIONS[tool](args)
        return data
    except Exception as e:
        return f"⚠️ Tool `{tool}` failed: {e}"

def handle_message_via_router(text: str) -> str:
    decision = run_ollama(
        f"{ROUTER_SYSTEM}\n"
        f"Available tools: {json.dumps(TOOLS)}\n"
        f"User message: {text}\n"
        'Return ONLY JSON: "tool":"","args":{}}', json_mode=True
    )
    decision = json.loads(decision)
    greeting_message = run_ollama(
        f"{ASSISTANCE_SYSTEM}\n"
        f"User message: {text}\n"
        f"The process that is starting: {TOOLS[decision['tool']]['description']}\n"
        'Return ONLY JSON: {"response":"one-liner response"}', json_mode=True
    )
    greeting_message = json.loads(greeting_message)
    return greeting_message["response"], decision["tool"], decision["args"]


print("Processing your request...")
greeting_message, tool, args = (handle_message_via_router("I'm with my girlfriend and we want to watch a romantic comedy movie. Can you suggest one?"))
print(greeting_message)
print(run_tool_and_format(tool, args))

# ------------ Slack handlers -------------
@app.event("message")
def on_dm(body, say, logger):
    ev = body.get("event", {})
    if ev.get("channel_type") == "im" and not ev.get("bot_id"):
        text = ev.get("text","")
        logger.info(f"[DM] {text}")
        say(handle_message_via_router(text))

@app.event("message")
def on_channel_message(body, say, logger):
    ev = body.get("event", {})
    if ev.get("channel_type") in ("channel", "general") and not ev.get("bot_id"):
        text = ev.get("text","")
        logger.info(f"[channel] {text}")
        if re.search(r"<@[^>]+>", text):  # bot mentioned
            text = re.sub(r"<@[^>]+>\s*", "", text)
            say("Processing your request...")
            greeting_message, tool, args = (handle_message_via_router(text))
            say(greeting_message)
            say(run_tool_and_format(tool, args))

@app.event("app_mention")
def on_mention(body, say, logger):
    text = body.get("event", {}).get("text", "")
    logger.info(f"[@mention] {text}")
    text = re.sub(r"<@[^>]+>\s*", "", text)
    greeting_message, tool, args = (handle_message_via_router(text))
    say(greeting_message)
    say(run_tool_and_format(tool, args))

@app.command("/help")
def help_command(ack, say):
    ack()
    help_text = (
        f"""Hello! I'm your personal assistant bot. These are functions that I can perform and their descriptions:
        {json.dumps(TOOLS, indent=2)}
        """
    )
    say(help_text)

if __name__ == "__main__":
    print("✅ Starting Slack bot with Ollama router...", flush=True)
    print(f"ENV OLLAMA={OLLAMA} MODEL={ROUTER_MODEL}", flush=True)
    SocketModeHandler(app, APP_TOKEN).start()
    print("✅ Slack bot started.", flush=True)