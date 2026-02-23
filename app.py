import json
import inspect
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters
import asyncio
from agent.llm import get_llm
from agent.memory import MemoryStore
from agent.builder import build_agent
from router.intent_router import classify_intent
from tools.notion_tools import (
    notion_create_database_page,
    notion_create_file_upload,
    attach_file_to_notion_file_upload,
    notion_properties_from_receipt
)
from tools.receipt_tools import (
    receipt_extract_summary_from_pdf,
)
from tools.telegram_tools import markdown_v2_safe
import automation_functions as automation_module

load_dotenv()
llm = get_llm()
memory = MemoryStore()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram-assistant")


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN environment variable.")

CHAT_IDS: Dict[str, str] = {
    "receipts": os.getenv("TELEGRAM_CHAT_ID_RECEIPTS", ""),
    "personal_assistant": os.getenv("TELEGRAM_CHAT_ID_PERSONAL_ASSISTANT", ""),
    "logs": os.getenv("TELEGRAM_CHAT_ID_LOGS", ""),
    "automations": os.getenv("TELEGRAM_CHAT_ID_AUTOMATIONS", ""),
}
RECEIPT_CATEGORY_OPTIONS = [
    item.strip()
    for item in os.getenv(
        "RECEIPT_CATEGORY_OPTIONS",
        "Groceries,Decor,Restaurant,Bills,EV,Online Services,Therapy",
    ).split(",")
    if item.strip()
]


def _load_automation_functions() -> Dict[str, Any]:
    functions: Dict[str, Any] = {}
    for name in dir(automation_module):
        if name.startswith("_"):
            continue
        obj = getattr(automation_module, name)
        if not inspect.isfunction(obj) or obj.__module__ != automation_module.__name__:
            continue
        signature = inspect.signature(obj)
        has_required_params = any(
            parameter.default is inspect._empty
            and parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            for parameter in signature.parameters.values()
        )
        if not has_required_params:
            functions[name] = obj
    return functions


AUTOMATION_FUNCTIONS = _load_automation_functions()


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

    intent = await classify_intent(llm, message.text)
    agent = build_agent(llm, memory, intent)
    session_id = str(message.chat_id)
    out = await agent.ainvoke({"input": message.text}, config={"configurable": {"session_id": session_id}})
    response = out.get("output", "")
    if response:
        await message.reply_text(markdown_v2_safe(response, preserve_formatting=True), parse_mode="MarkdownV2")
    else:
        await message.reply_text("Sorry, I couldn't generate a response for that.")


async def _handle_automation_text(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (message.text or message.caption or "").strip()
    if not text:
        return
    func = AUTOMATION_FUNCTIONS.get(text)
    if not func:
        logger.info("Ignoring unknown automation command: %s", text)
        await message.reply_text(f"Unknown automation command: `{text}`")
        return

    await _safe_log(context, f"[automation] Running: {text}")
    try:
        result = await asyncio.to_thread(func)
        if result is None:
            result = "✅ Automation completed."
        await message.reply_text(str(result))
    except Exception as exc:
        logger.exception("Automation command failed: %s", text)
        await _safe_log(context, f"[automation:error] {text}: {exc}")
        await message.reply_text(f"Automation `{text}` failed.")


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

        vendor = markdown_v2_safe(receipt_data.get("vendor"))
        total = markdown_v2_safe(receipt_data.get("total_amount"))
        category = markdown_v2_safe(receipt_data.get("category"))
        pdf_type = markdown_v2_safe(receipt_data.get("source_pdf_type"))

        summary = (
            "✅ Receipt processed\\.\n"
            f"*Vendor*: {vendor}\n"
            f"*Total*: {total}\n"
            f"*Category*: {category}\n"
            f"*PDF Type*: {pdf_type}"
        )
        await message.reply_text(summary, parse_mode="MarkdownV2")

        if os.getenv("EXPENSES_DATABASE_ID", ""):
            notion_properties = notion_properties_from_receipt(receipt_data)
            file_upload = notion_create_file_upload()
            if not isinstance(file_upload, dict) or "file_upload_id" not in file_upload or not file_upload["ok"]:
                await _safe_log(context, f"[receipt:notion] Failed to upload file to Notion: {file_upload}")
                return
            attach_file_to_notion_file_upload(
                    file_upload["file_upload_id"],
                    file_path=str(tmp_path),
                    file_name=f"{vendor or 'Receipt'} - {notion_properties.get('Date', {}).get('content', {}).get('start', 'Unknown Date')}.pdf",
            )
            if notion_properties:
                create_res = notion_create_database_page.invoke(
                    {
                        "database_id": os.getenv("EXPENSES_DATABASE_ID", ""),
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
    message = update.message or update.channel_post
    if not message:
        return

    if str(message.chat_id) == CHAT_IDS.get("automations"):
        await _handle_automation_text(message, context)
        return

    if message.chat_id == int(CHAT_IDS.get("receipts", 0)):
        await message.reply_text("Please send receipt PDFs as documents, not as text.")
        return
    
    if message.chat_id == int(CHAT_IDS.get("logs", 0)):
        await message.reply_text("This chat is for logs only. Please use the personal assistant chat for interactions.")
        return

    if message.chat_id == int(CHAT_IDS.get("personal_assistant", 0)):
        await _handle_personal_assistant_text(update, context)
        return


async def route_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if "channel_post" in update._get_attrs():
        message = update.channel_post
    if not message or not message.document:
        return
    if str(message.chat_id) == CHAT_IDS.get("receipts"):
        await _handle_receipt_pdf(update, context)
        return

    await _safe_log(context, f"Received document from unregistered chat id: {message.chat_id}")
    await message.reply_text("Document uploads are only handled in the receipts chat.")


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, route_text))
    application.add_handler(MessageHandler(filters.Document.ALL, route_document))

    logger.info("Starting Telegram bot. Configured chat IDs: %s", {k: bool(v) for k, v in CHAT_IDS.items()})
    application.run_polling()


if __name__ == "__main__":
    main()
