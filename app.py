import json
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
    message = update.message
    if not message:
        if update.channel_post:
            if str(update.channel_post.chat_id) == CHAT_IDS.get("logs"):
                await update.channel_post.reply_text("Logs chat is active.")
                return

            if str(update.channel_post.chat_id) == CHAT_IDS.get("receipts"):
                await update.channel_post.reply_text("Send a PDF file here to process receipts.")
                return

            if str(update.channel_post.chat_id) == CHAT_IDS.get("automations"):
                await update.channel_post.reply_text("Automations chat is active.")
                return

            logger.info("Ignoring text from unregistered chat id: %s", update.channel_post.chat_id)
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
