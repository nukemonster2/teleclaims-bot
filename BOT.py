import os
import re
import sqlite3
import tempfile
import logging
import threading

from fastapi import FastAPI
import uvicorn

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from paddleocr import PaddleOCR

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

TOKEN = os.getenv("TOKEN")
PORT = int(os.getenv("PORT", 10000))

if not TOKEN:
    raise SystemExit("Missing TOKEN")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# FASTAPI (Render needs this)
# -------------------------------------------------------------------

app_web = FastAPI()

@app_web.get("/")
def health():
    return {"status": "ok"}

# -------------------------------------------------------------------
# LAZY OCR (IMPORTANT FOR RENDER)
# -------------------------------------------------------------------

ocr = None

def get_ocr():
    global ocr
    if ocr is None:
        logger.info("Loading PaddleOCR model...")
        ocr = PaddleOCR(use_angle_cls=True, lang="en")
    return ocr

# -------------------------------------------------------------------
# DATABASE
# -------------------------------------------------------------------

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

# -------------------------------------------------------------------
# OCR PARSER HELPERS
# -------------------------------------------------------------------

PRICE_RE = re.compile(r"\$?\s*(\d+\.\d{2})")
IGNORE_WORDS = {"cash", "change", "subtotal", "tax", "total", "thank"}

def clean_lines(text: str):
    return [l.strip() for l in text.split("\n") if l.strip()]

def extract_items_and_total(text: str):
    lines = clean_lines(text)

    items = []
    total = None

    for line in lines:
        low = line.lower()

        if "total" in low:
            m = PRICE_RE.search(line)
            if m:
                total = float(m.group(1))
            continue

        if any(w in low for w in IGNORE_WORDS):
            continue

        m = PRICE_RE.search(line)
        if not m:
            continue

        price = float(m.group(1))
        name = PRICE_RE.sub("", line).replace("$", "").strip()
        name = re.sub(r"^\d+\s*x\s*", "", name, flags=re.IGNORECASE)

        if len(name) < 2:
            continue

        items.append({"name": name, "price": price})

    computed_total = sum(i["price"] for i in items)
    if total is None:
        total = computed_total

    return items, total

def format_receipt(items, total):
    col = max((len(i["name"]) for i in items), default=10)

    header = f"{'Item':<{col}}  {'Price':>10}"
    sep = "-" * len(header)

    rows = [
        f"{i['name']:<{col}}  ${i['price']:>9.2f}"
        for i in items
    ]

    footer = f"{'TOTAL':<{col}}  ${total:>9.2f}"

    return "```\n" + "\n".join([header, sep] + rows + [sep, footer]) + "\n```"

# -------------------------------------------------------------------
# TELEGRAM HANDLERS
# -------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Receipt bot is running.")

async def upload_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return await update.message.reply_text("Send a receipt image.")

    file = await update.message.photo[-1].get_file()

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        await file.download_to_drive(custom_path=f.name)
        path = f.name

    try:
        ocr_engine = get_ocr()
        result = ocr_engine.ocr(path, cls=True)

        lines = []
        for block in result:
            for line in block:
                lines.append(line[1][0])

        text = "\n".join(lines)

    except Exception as e:
        logger.exception(e)
        return await update.message.reply_text("OCR failed.")

    finally:
        try:
            os.remove(path)
        except:
            pass

    items, total = extract_items_and_total(text)

    if not items:
        return await update.message.reply_text(
            f"OCR TEXT:\n{text}\n\nNo items detected."
        )

    await update.message.reply_text(
        format_receipt(items, total),
        parse_mode="Markdown"
    )

# -------------------------------------------------------------------
# TELEGRAM APP
# -------------------------------------------------------------------

def build_bot():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, upload_receipt))

    return app

def run_bot():
    bot = build_bot()
    logger.info("Bot running (polling mode)")
    bot.run_polling()

# -------------------------------------------------------------------
# ENTRYPOINT (RENDER SAFE)
# -------------------------------------------------------------------

if __name__ == "__main__":
    threading.Thread(target=run_bot).start()

    logger.info("Starting FastAPI server...")
    uvicorn.run(app_web, host="0.0.0.0", port=PORT)