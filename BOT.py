import os
import re
import sqlite3
import requests
import logging

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- CONFIG ----------------

TOKEN = os.getenv("TOKEN")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
OCR_API_KEY = os.getenv("OCR_API_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))

if not TOKEN:
    raise RuntimeError("Missing TOKEN env variable")

if not RENDER_EXTERNAL_URL:
    raise RuntimeError("Missing RENDER_EXTERNAL_URL env variable")

ADMIN_IDS = []
if ADMIN_IDS_RAW:
    ADMIN_IDS = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]

# ---------------- LOGGING ----------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- DATABASE ----------------

conn = sqlite3.connect("claims.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    item TEXT,
    link TEXT,
    price REAL,
    status TEXT
)
""")
conn.commit()

# ---------------- BOT ----------------

app = ApplicationBuilder().token(TOKEN).build()

# ---------------- COMMANDS ----------------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    text = (
        "📦 *Purchase Claim Bot*\n\n"
        "User Commands:\n"
        "/request <item> <link> <price>\n"
        "/list\n"
        "/help\n\n"
        "Or send a receipt image for OCR."
    )

    if user_id in ADMIN_IDS:
        text += "\n\n🔐 Admin:\n/approve <id>\n/reject <id>"

    await update.message.reply_text(text, parse_mode="Markdown")


async def request_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        item = context.args[0]
        link = context.args[1]
        price = float(context.args[2])
    except:
        await update.message.reply_text("Usage: /request <item> <link> <price>")
        return

    user = update.effective_user

    cursor.execute("""
        INSERT INTO requests (user_id, username, item, link, price, status)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user.id, user.username, item, link, price, "PENDING"))

    conn.commit()

    await update.message.reply_text(
        f"✅ Request created\nItem: {item}\nPrice: ${price}\nStatus: PENDING"
    )


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("Not authorized")

    try:
        rid = int(context.args[0])
    except:
        return await update.message.reply_text("Usage: /approve <id>")

    cursor.execute("UPDATE requests SET status='APPROVED' WHERE id=?", (rid,))
    conn.commit()

    await update.message.reply_text(f"Request #{rid} approved")


async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("Not authorized")

    try:
        rid = int(context.args[0])
    except:
        return await update.message.reply_text("Usage: /reject <id>")

    cursor.execute("UPDATE requests SET status='REJECTED' WHERE id=?", (rid,))
    conn.commit()

    await update.message.reply_text(f"Request #{rid} rejected")


async def list_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT * FROM requests")
    rows = cursor.fetchall()

    if not rows:
        return await update.message.reply_text("No requests")

    msg = ""
    for r in rows:
        msg += (
            f"ID: {r[0]}\n"
            f"Item: {r[3]}\n"
            f"Price: ${r[5]}\n"
            f"Status: {r[6]}\n\n"
        )

    await update.message.reply_text(msg)


# ---------------- OCR HANDLER ----------------

async def upload_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return await update.message.reply_text("Send a photo of receipt")

    file = await update.message.photo[-1].get_file()
    file_path = await file.download_to_drive()

    try:
        with open(file_path, "rb") as f:
            r = requests.post(
                "https://api.ocr.space/parse/image",
                files={"file": f},
                data={
                    "apikey": OCR_API_KEY,
                    "language": "eng",
                },
                timeout=30,
            )

        data = r.json()
        text = data["ParsedResults"][0]["ParsedText"]

    except Exception as e:
        logger.exception(e)
        return await update.message.reply_text("OCR failed")

    prices = re.findall(r"\d+\.\d{2}", text)
    price = prices[0] if prices else "N/A"

    await update.message.reply_text(
        f"📄 Extracted:\n{text}\n\n💰 Price: {price}"
    )


# ---------------- ROUTES ----------------

app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("request", request_item))
app.add_handler(CommandHandler("approve", approve))
app.add_handler(CommandHandler("reject", reject))
app.add_handler(CommandHandler("list", list_requests))
app.add_handler(MessageHandler(filters.PHOTO, upload_receipt))


# ---------------- WEBHOOK START ----------------

async def post_init(app):
    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
    await app.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set: {webhook_url}")


app.post_init = post_init

# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{RENDER_EXTERNAL_URL}/webhook",
    )