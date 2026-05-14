import os
import re
import sqlite3
import tempfile
import logging

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

from paddleocr import PaddleOCR

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

TOKEN = os.getenv("TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

PORT = int(os.environ.get("PORT", 10000))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not TOKEN:
    raise SystemExit("Missing TOKEN")

# ---------------------------------------------------------------------------
# OCR INIT (PaddleOCR)
# ---------------------------------------------------------------------------

ocr = PaddleOCR(use_angle_cls=True, lang="en")

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# REGEX
# ---------------------------------------------------------------------------

PRICE_RE = re.compile(r"\$?\s*(\d+\.\d{2})")

IGNORE_WORDS = {"cash", "change", "subtotal", "tax", "total", "thank"}

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def clean_lines(text: str):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return lines


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

# ---------------------------------------------------------------------------
# RECEIPT PARSER (ROBUST)
# ---------------------------------------------------------------------------

def extract_items_and_total(text: str):
    lines = clean_lines(text)

    items = []
    total = None

    for line in lines:
        low = line.lower()

        # capture total
        if "total" in low:
            m = PRICE_RE.search(line)
            if m:
                total = float(m.group(1))
            continue

        # ignore footer noise
        if any(w in low for w in IGNORE_WORDS):
            continue

        m = PRICE_RE.search(line)
        if not m:
            continue

        price = float(m.group(1))

        name = PRICE_RE.sub("", line).replace("$", "").strip()

        # remove fake OCR quantity like "2x"
        name = re.sub(r"^\d+\s*x\s*", "", name, flags=re.IGNORECASE)

        if len(name) < 2:
            continue

        items.append({
            "name": name,
            "qty": 1,
            "price": price
        })

    computed_total = sum(i["price"] for i in items)

    if total is None:
        total = computed_total

    return items, total

# ---------------------------------------------------------------------------
# TELEGRAM HANDLER
# ---------------------------------------------------------------------------

async def upload_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message.photo:
        return await update.message.reply_text("Send a receipt image.")

    file = await update.message.photo[-1].get_file()

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        await file.download_to_drive(custom_path=f.name)
        path = f.name

    try:
        result = ocr.ocr(path, cls=True)

        # flatten OCR output
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

# ---------------------------------------------------------------------------
# BASIC COMMANDS (kept minimal)
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Receipt bot is running.")

# ---------------------------------------------------------------------------
# BUILD APP
# ---------------------------------------------------------------------------

def build_app():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, upload_receipt))

    return app

def run():
    app = build_app()
    logger.info("Bot running in polling mode")
    app.run_polling()

if __name__ == "__main__":
    run()