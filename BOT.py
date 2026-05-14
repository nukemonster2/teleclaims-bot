import os
import re
import sqlite3
import tempfile
import logging
import requests

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

TOKEN = os.getenv("TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
OCR_API_KEY = os.getenv("OCR_API_KEY")

PORT = int(os.environ.get("PORT", 10000))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not TOKEN:
    raise SystemExit("Missing TOKEN")

# ---------------------------------------------------------------------------
# DB
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

IGNORE = {"cash", "change", "subtotal", "tax", "total", "thank"}

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def chunk_text(text, limit=4000):
    out = []
    while text:
        out.append(text[:limit])
        text = text[limit:]
    return out


def format_request(row):
    return (
        f"ID: {row[0]}\n"
        f"User: {row[2] or 'unknown'} ({row[1]})\n"
        f"Item: {row[3]}\n"
        f"Link: {row[4]}\n"
        f"Price: ${row[5]:.2f}\n"
        f"Status: {row[6]}"
    )

# ---------------------------------------------------------------------------
# OCR PARSER (FIXED CORE LOGIC)
# ---------------------------------------------------------------------------

def clean_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"\s+", " ", line)
    return line


def is_noise(line: str) -> bool:
    l = line.lower()
    return any(k in l for k in IGNORE)


def extract_items_and_total(text: str):
    lines = [clean_line(l) for l in text.splitlines() if l.strip()]

    items = []
    total = None

    for i, line in enumerate(lines):

        if is_noise(line):
            m = PRICE_RE.search(line)
            if "total" in line.lower() and m:
                total = float(m.group(1))
            continue

        price_match = PRICE_RE.search(line)
        if not price_match:
            continue

        price = float(price_match.group(1))

        # detect name part
        name = PRICE_RE.sub("", line).replace("$", "").strip()

        # REMOVE unreliable qty logic (critical fix)
        name = re.sub(r"^\d+\s*x\s+", "", name, flags=re.IGNORECASE)

        # skip garbage
        if len(name) < 2:
            continue

        items.append({
            "name": name,
            "qty": 1,   # IMPORTANT: treat as line item price
            "price": price
        })

    computed_total = sum(i["price"] for i in items)

    if total is None:
        total = computed_total
    else:
        # sanity check
        if abs(total - computed_total) > 0.01:
            logger.warning(f"Mismatch OCR={total} computed={computed_total}")

    return items, total

# ---------------------------------------------------------------------------
# FORMAT OUTPUT
# ---------------------------------------------------------------------------

def format_receipt(items, total):
    col = max(len(i["name"]) for i in items) if items else 10
    header = f"{'Item':<{col}}  {'Price':>10}"
    sep = "-" * len(header)

    rows = [
        f"{i['name']:<{col}}  ${i['price']:>9.2f}"
        for i in items
    ]

    footer = f"{'TOTAL':<{col}}  ${total:>9.2f}"

    return "```\n" + "\n".join([header, sep] + rows + [sep, footer]) + "\n```"

# ---------------------------------------------------------------------------
# TELEGRAM HANDLERS
# ---------------------------------------------------------------------------

async def upload_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message.photo:
        return await update.message.reply_text("Send a receipt photo.")

    if not OCR_API_KEY:
        return await update.message.reply_text("OCR not configured.")

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
                timeout=30
            )
        data = r.json()
    finally:
        os.remove(path)

    text = data["ParsedResults"][0]["ParsedText"]

    items, total = extract_items_and_total(text)

    if not items:
        return await update.message.reply_text("No items detected.")

    await update.message.reply_text(
        format_receipt(items, total),
        parse_mode="Markdown"
    )

# ---------------------------------------------------------------------------
# BASIC BOT SETUP
# ---------------------------------------------------------------------------

def build_app():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.PHOTO, upload_receipt))
    return app


def run():
    app = build_app()

    if RENDER_EXTERNAL_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{RENDER_EXTERNAL_URL}/{TOKEN}",
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    run()