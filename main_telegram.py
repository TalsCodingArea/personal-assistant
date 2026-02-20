import calendar
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from tools.notion_tools import (
    notion_create_database_page,
    notion_create_file_upload,
    notion_get_database_pages,
    attach_file_to_notion_file_upload,
    get_expenses_between_dates,
    get_movies_data_from_notion_database,
    update_movie_property
)
from tools.receipt_tools import (
    receipt_detect_pdf_content_type,
    receipt_extract_summary_from_pdf,
    receipt_extract_summary_from_pdf_url,
)

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram-assistant")


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN environment variable.")

# Fill these IDs in your .env (or directly here if preferred).
# Telegram IDs are often numeric and can be negative for groups/supergroups.
CHAT_IDS: Dict[str, str] = {
    "receipts": os.getenv("TELEGRAM_CHAT_ID_RECEIPTS", ""),
    "personal_assistant": os.getenv("TELEGRAM_CHAT_ID_PERSONAL_ASSISTANT", ""),
    "logs": os.getenv("TELEGRAM_CHAT_ID_LOGS", ""),
    "automations": os.getenv("TELEGRAM_CHAT_ID_AUTOMATIONS", ""),
}

MEMORY_FILE = Path(os.getenv("PERSONAL_ASSISTANT_MEMORY_FILE", "data/personal_assistant_memory.json"))
MEMORY_MAX_MESSAGES = int(os.getenv("PERSONAL_ASSISTANT_MEMORY_MAX_MESSAGES", "40"))

RECEIPT_CATEGORY_OPTIONS = [
    item.strip()
    for item in os.getenv(
        "RECEIPT_CATEGORY_OPTIONS",
        "Groceries,Decor,Restaurant,Bills,EV,Online Services,Therapy",
    ).split(",")
    if item.strip()
]

AUTO_LOG_RECEIPTS_TO_NOTION = os.getenv("AUTO_LOG_RECEIPTS_TO_NOTION", "true").lower() == "true"
NOTION_RECEIPTS_DATABASE_ID = os.getenv("EXPENSES_DATABASE_ID", "")
NOTION_VENDOR_PROPERTY = os.getenv("NOTION_RECEIPT_VENDOR_PROPERTY", "Description")
NOTION_AMOUNT_PROPERTY = os.getenv("NOTION_RECEIPT_AMOUNT_PROPERTY", "Amount")
NOTION_CATEGORY_PROPERTY = os.getenv("NOTION_RECEIPT_CATEGORY_PROPERTY", "Category")

TOOLS = [
    get_expenses_between_dates,
    get_movies_data_from_notion_database,
    update_movie_property,
]

SYSTEM_PROMPT = (
    "You are Tal's personal assistant. Use tools when needed. "
    "Be concise, practical, and deterministic. "
    "All expenses are documented in NIS (₪)."
    "If you don't know something, say you don't know instead of making it up."
    "The way Tal manages his expenses is that he has categories for every expense, such as Home 🏡, Car 🚗, Food 🍔, etc. Each category can have subcategories, such as Home 🏡 > Groceries 🛒 or Car 🚗 > EV 🔋. "
    "The Bills 🧾 subcategory is for regular monthly bills like rent, utilities, internet, etc. so it is predictable from previous months (total amount and date of charge). Consider that when giving a monthly summary."
    "All expenses have a Typ of Want, Need or Waste. When Need are expenses that are necessary and unavoidable, such as rent, bills, groceries, etc. When Want are expenses that are not strictly necessary but add value to life, such as going out to eat, entertainment, etc. When Waste are expenses that couldn't been avoided like uncanceled subscriptions."
    "A monthly evaluation should be short with colorful details indicating good, avrage or bad, reflect if the Need expenses are predicted to be higher than 50 percent of the income and if so, which sub categories are the main contributors. It should also reflect on the Waste expenses and if there are any predicted for the month based on previous months, and if so, which ones. Finally, it should reflect on the Want expenses and if there are any predicted for the month based on previous months, and if so, which ones."
)

MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-4o")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("Missing OPENAI_API_KEY environment variable.")

PERSONAL_ASSISTANT_MODEL = init_chat_model(
    model=MODEL_NAME,
    model_provider="openai",
    temperature=0.5,
    api_key=OPENAI_API_KEY,
)
PERSONAL_ASSISTANT_AGENT = create_react_agent(
    model=PERSONAL_ASSISTANT_MODEL,
    tools=TOOLS,
    prompt=SYSTEM_PROMPT,
)


def _chat_role(chat_id: int) -> Optional[str]:
    cid = str(chat_id)
    for role, configured_id in CHAT_IDS.items():
        if configured_id and cid == configured_id:
            return role
    return None


def _load_memory() -> List[Dict[str, str]]:
    if not MEMORY_FILE.exists():
        return []
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        messages = data.get("messages", [])
        if not isinstance(messages, list):
            return []
        clean: List[Dict[str, str]] = []
        for item in messages:
            if (
                isinstance(item, dict)
                and item.get("role") in ("user", "assistant")
                and isinstance(item.get("content"), str)
            ):
                clean.append({"role": item["role"], "content": item["content"]})
        return clean[-MEMORY_MAX_MESSAGES:]
    except Exception as exc:
        logger.warning("Failed to load memory file: %s", exc)
        return []


def _save_memory(messages: List[Dict[str, str]]) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"messages": messages[-MEMORY_MAX_MESSAGES:]}
    MEMORY_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _assistant_reply_with_tools(user_text: str, memory_items: List[Dict[str, str]]) -> str:
    now = datetime.now().astimezone()
    month_start = now.replace(day=1).date().isoformat()
    month_end = now.replace(day=calendar.monthrange(now.year, now.month)[1]).date().isoformat()
    date_context = (
        f"Current date context: today is {now.date().isoformat()} "
        f"({now.strftime('%A')}), current month is {now.strftime('%B %Y')} "
        f"from {month_start} to {month_end}, timezone is {now.tzname() or 'local time'}."
    )
    messages: List[Any] = [SystemMessage(content=SYSTEM_PROMPT), SystemMessage(content=date_context)]
    for item in memory_items[-MEMORY_MAX_MESSAGES:]:
        if item["role"] == "user":
            messages.append(HumanMessage(content=item["content"]))
        elif item["role"] == "assistant":
            messages.append(AIMessage(content=item["content"]))
    messages.append(HumanMessage(content=user_text))

    response = PERSONAL_ASSISTANT_AGENT.invoke({"messages": messages})
    response_messages = response.get("messages", [])
    for msg in reversed(response_messages):
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, str):
                text = content.strip()
                if text:
                    return text
            if isinstance(content, list):
                text_parts: List[str] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
                text = "\n".join(text_parts).strip()
                if text:
                    return text
    return "Done."


async def _safe_log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    logs_chat = CHAT_IDS.get("logs")
    if not logs_chat:
        return
    try:
        await context.bot.send_message(chat_id=int(logs_chat), text=text)
    except Exception as exc:
        logger.warning("Failed sending logs message: %s", exc)


async def _handle_personal_assistant_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    user_text = (message.text or message.caption or "").strip()
    if not user_text:
        return

    memory = _load_memory()

    try:
        reply_text = _assistant_reply_with_tools(user_text, memory)
    except Exception as exc:
        logger.exception("Assistant agent execution failed")
        await message.reply_text("I hit an error while processing your request.")
        await _safe_log(context, f"[personal_assistant:error] {exc}")
        return

    await message.reply_text(reply_text)
    memory.extend(
        [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply_text},
        ]
    )
    _save_memory(memory)


def _notion_properties_from_receipt(receipt_json: Dict[str, object]) -> Dict[str, Dict[str, object]]:
    vendor = receipt_json.get("vendor")
    total_amount = receipt_json.get("total_amount")
    category = receipt_json.get("category")
    date = receipt_json.get("date")
    properties: Dict[str, Dict[str, object]] = {}

    if isinstance(vendor, str) and vendor.strip():
        properties[NOTION_VENDOR_PROPERTY] = {"type": "title", "content": vendor.strip()}
    if isinstance(total_amount, (int, float)):
        properties[NOTION_AMOUNT_PROPERTY] = {"type": "number", "content": float(total_amount)}
    if isinstance(category, str) and category.strip():
        if category.strip() == "Uncategorized":
            properties["Category"] = {"type": "multi_select", "content": ["Uncategorized"]}
        elif category.strip() == "Groceries":
            properties["Category"] = {"type": "multi_select", "content": ["Home 🏡"]}
            properties["Sub Category"] = {"type": "multi_select", "content": ["Groceries 🛒"]}
        elif category.strip() == "EV":
            properties["Category"] = {"type": "multi_select", "content": ["Car 🚗"]}
            properties["Sub Category"] = {"type": "multi_select", "content": ["Electric 🔋"]}
        elif category.strip() == "Bills":
            properties["Category"] = {"type": "multi_select", "content": ["Home 🏡"]}
            properties["Sub Category"] = {"type": "multi_select", "content": ["Bills 🧾"]}
    if isinstance(date, str):
        properties["Date"] = {"type": "date", "content": {"start": date}}
    properties["Tag"] = {"type": "multi_select", "content": ["Tal 👨🏻"]}
    properties["Type"] = {"type": "select", "content": "Need"}
    properties["Payment Method"] = {"type": "select", "content": "Credit"}
    return properties


async def _handle_receipt_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if "channel_post" in update._get_attrs():
        message = update.channel_post
    if not message or not message.document:
        return

    document = message.document
    filename = document.file_name or "receipt.pdf"
    if not filename.lower().endswith(".pdf"):
        await message.reply_text("Please send a PDF receipt.")
        return

    await message.reply_text("Processing receipt...")
    tmp_path: Optional[Path] = None

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        await telegram_file.download_to_drive(custom_path=str(tmp_path))

        receipt_data = receipt_extract_summary_from_pdf.invoke(
            {
                "pdf_path": str(tmp_path),
                "category_options": RECEIPT_CATEGORY_OPTIONS,
            }
        )

        if not isinstance(receipt_data, dict):
            raise ValueError("Receipt extraction returned an unexpected result type.")

        vendor = receipt_data.get("vendor")
        total = receipt_data.get("total_amount")
        category = receipt_data.get("category")
        pdf_type = receipt_data.get("source_pdf_type")

        summary = (
            "Receipt processed.\n"
            f"Vendor: {vendor}\n"
            f"Total: {total}\n"
            f"Category: {category}\n"
            f"PDF Type: {pdf_type}"
        )
        await message.reply_text(summary)

        if AUTO_LOG_RECEIPTS_TO_NOTION and NOTION_RECEIPTS_DATABASE_ID:
            notion_properties = _notion_properties_from_receipt(receipt_data)
            file_upload = notion_create_file_upload.invoke(
                {
                    "file_path": str(tmp_path),
                    "file_name": f"{vendor or 'Receipt'} - {notion_properties.get('Date', {}).get('content', {}).get('start', 'Unknown Date')}.pdf",
                }
            )
            if not isinstance(file_upload, dict) or "file_upload_id" not in file_upload or not file_upload["ok"]:
                await _safe_log(context, f"[receipt:notion] Failed to upload file to Notion: {file_upload}")
                return
            attach_file_to_notion_file_upload.invoke(
                {
                    "file_upload_id": file_upload["file_upload_id"],
                    "file_path": str(tmp_path),
                    "file_name": f"{vendor or 'Receipt'} - {notion_properties.get('Date', {}).get('content', {}).get('start', 'Unknown Date')}.pdf",
                }
            )
            if notion_properties:
                create_res = notion_create_database_page.invoke(
                    {
                        "database_id": NOTION_RECEIPTS_DATABASE_ID,
                        "properties": notion_properties,
                        "file_property_name": "Invoice",
                        "file_upload_id": file_upload["file_upload_id"],
                        "file_name": f"{vendor or 'Receipt'} - {notion_properties['Date']['content']['start'] or 'Unknown Date'}.pdf",
                    }
                )
                await _safe_log(context, f"[receipt:notion] Created Notion page: {create_res}")
            else:
                await _safe_log(context, "[receipt:notion] Skipped Notion write due to empty mapped properties.")

        await _safe_log(context, f"[receipt] {json.dumps(receipt_data, ensure_ascii=False)}")
    except Exception as exc:
        logger.exception("Receipt processing failed")
        await message.reply_text("I couldn't process this receipt PDF.")
        await _safe_log(context, f"[receipt:error] {exc}")
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        if update.channel_post:
            role = _chat_role(update.channel_post.chat_id)
            if role == "logs":
                await update.channel_post.reply_text("Logs chat is active.")
                return

            if role == "receipts":
                await update.channel_post.reply_text("Send a PDF file here to process receipts.")
                return

            if role == "automations":
                await update.channel_post.reply_text("Automations chat is active.")
                return

            logger.info("Ignoring text from unregistered chat id: %s", update.channel_post.chat_id)
        return
    role = _chat_role(message.chat_id)
    if role == "personal_assistant":
        await _handle_personal_assistant_text(update, context)
        return



async def route_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if "channel_post" in update._get_attrs():
        message = update.channel_post
    if not message or not message.document:
        return
    role = _chat_role(message.chat_id)
    if role == "receipts":
        await _handle_receipt_pdf(update, context)
        return

    await message.reply_text("Document uploads are only handled in the receipts chat.")


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, route_text))
    application.add_handler(MessageHandler(filters.Document.ALL, route_document))

    logger.info("Starting Telegram bot. Configured chat IDs: %s", {k: bool(v) for k, v in CHAT_IDS.items()})
    application.run_polling()


if __name__ == "__main__":
    main()
