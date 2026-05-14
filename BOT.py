import os
import re
import sqlite3
import tempfile
import logging
import requests

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- ENV ----------------

TOKEN = os.getenv("TOKEN")
OCR_API_KEY = os.getenv("OCR_API_KEY")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))

if not TOKEN:
    raise RuntimeError("Missing TOKEN")
if not RENDER_EXTERNAL_URL:
    raise RuntimeError("Missing RENDER_EXTERNAL_URL")

ADMIN_IDS = []
if os.getenv("ADMIN_IDS"):
    ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS").split(",")]

# ---------------- LOGGING ----------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- DB ----------------

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

# ---------------- OCR ----------------

PRICE_RE = re.compile(r"\$?\s*(\d+\.\d{2})")


# ---------------- HELPERS ----------------

def format_row(r):
    return (
        f"ID: {r[0]}\n"
        f"User: {r[2]} ({r[1]})\n"
        f"Item: {r[3]}\n"
        f"Link: {r[4]}\n"
        f"Price: ${r[5]:.2f}\n"
        f"Status: {r[6]}"
    )


# ---------------- HANDLERS ----------------

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/request <item> <link> <price>\n/list\n/status <id>"
    )


async def request_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        item = context.args[0]
        link = context.args[1]
        price = float(context.args[2])
    except:
        return await update.message.reply_text("Usage: /request <item> <link> <price>")

    user = update.effective_user

    cursor.execute(
        "INSERT INTO requests VALUES (NULL,?,?,?,?,?,?)",
        (user.id, user.username, item, link, price, "PENDING"),
    )
    conn.commit()

    await update.message.reply_text("Request created ✅")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT * FROM requests WHERE user_id=?", (update.effective_user.id,))
    rows = cursor.fetchall()

    if not rows:
        return await update.message.reply_text("No requests")

    msg = "\n\n".join(format_row(r) for r in rows)
    await update.message.reply_text(msg)


async def upload_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return await update.message.reply_text("Send a photo")

    if not OCR_API_KEY:
        return await update.message.reply_text("OCR not configured")

    file = await update.message.photo[-1].get_file()

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        await file.download_to_drive(custom_path=f.name)
        path = f.name

    try:
        with open(path, "rb") as img:
            r = requests.post(
                "https://api.ocr.space/parse/image",
                files={"file": img},
                data={"apikey": OCR_API_KEY, "language": "eng"},
                timeout=30,
            )

        data = r.json()
        text = data["ParsedResults"][0]["ParsedText"]

    except Exception as e:
        logger.exception(e)
        return await update.message.reply_text("OCR failed")

    finally:
        try:
            os.remove(path)
        except:
            pass

    prices = PRICE_RE.findall(text)
    price = prices[0] if prices else "N/A"

    await update.message.reply_text(f"{text}\n\nPrice: {price}")


# ---------------- BOT ----------------

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("help", help_cmd))
app.add_handler(CommandHandler("request", request_cmd))
app.add_handler(CommandHandler("list", list_cmd))
app.add_handler(MessageHandler(filters.PHOTO, upload_receipt))


# ---------------- WEBHOOK (FIXED) ----------------

async def post_init(app):
    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
    await app.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set → {webhook_url}")


app.post_init = post_init


# ---------------- RUN ----------------

if __name__ == "__main__":
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{RENDER_EXTERNAL_URL}/webhook",
    )