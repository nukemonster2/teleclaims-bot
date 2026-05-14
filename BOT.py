import os
import re
import sqlite3
import tempfile
import logging
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.getenv("TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
OCR_API_KEY = os.getenv("OCR_API_KEY")
PORT = int(os.environ.get("PORT", 10000))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if not TOKEN:
    raise SystemExit("Missing TOKEN environment variable.")

if not OCR_API_KEY:
    logger.warning("OCR_API_KEY is not set. Receipt OCR will fail until provided.")

conn = sqlite3.connect("claims.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        item TEXT,
        link TEXT,
        price REAL,
        status TEXT
    )
    """
)
conn.commit()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PRICE_RE = re.compile(r"\$?\s*(\d+\.\d{2})")
QTY_RE   = re.compile(r"^(\d+)\s*x\s+", re.IGNORECASE)
IGNORE_KEYWORDS = {"cash", "change", "subtotal", "tax", "total", "amount", "receipt", "thank you"}


def format_request(row):
    return (
        f"ID: {row[0]}\n"
        f"User: {row[2] or 'unknown'} ({row[1]})\n"
        f"Item: {row[3]}\n"
        f"Link: {row[4]}\n"
        f"Price: ${row[5]:.2f}\n"
        f"Status: {row[6]}"
    )


def chunk_text(text, limit=4000):
    chunks = []
    while text:
        part = text[:limit]
        split_at = part.rfind("\n\n")
        if split_at > 0 and len(text) > limit:
            part = text[:split_at]
        chunks.append(part)
        text = text[len(part):].lstrip()
    return chunks


def parse_request_args(args):
    if len(args) < 3:
        raise ValueError("Usage: /request <item> <link> <price>")
    try:
        price = float(args[-1])
    except ValueError:
        raise ValueError("Price must be a number.")
    link = args[-2]
    item = " ".join(args[:-2]).strip()
    if not item:
        raise ValueError("Item description cannot be empty.")
    return item, link, price


# ---------------------------------------------------------------------------
# Receipt OCR parsing
# ---------------------------------------------------------------------------

def extract_text_from_ocr_response(result):
    if not isinstance(result, dict):
        return None
    parsed = result.get("ParsedResults")
    if not parsed:
        return None
    return parsed[0].get("ParsedText")


def is_ignored_line(line):
    """Return True for footer/summary lines (cash, change, total, etc.)."""
    lower = line.lower()
    return any(word in lower for word in IGNORE_KEYWORDS)


def is_currency_noise(line):
    """
    Detect lines where the only letter is an OCR misread of '$' as 's'/'S'.
    e.g. 's 117.00' -> after stripping price -> 's' -> length 1 -> noise.
    """
    without_price = PRICE_RE.sub("", line).replace("$", "").strip()
    return len(without_price) <= 1


def is_watermark_or_domain(token):
    """
    Detect single-token domain/watermark strings like 'modif.ai' or 'store.com'.
    These have no spaces and match a valid domain pattern.
    """
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}", token.strip()))


def _parse_name_qty(raw_name):
    """
    Split '2x Lorem ipsum' into ('Lorem ipsum', 2).
    Returns (raw_name, 1) if no quantity prefix is found.
    """
    raw_name = raw_name.replace("lx", "1x").replace("Ix", "1x").strip()
    m = QTY_RE.match(raw_name)
    if m:
        return raw_name[m.end():].strip(), int(m.group(1))
    return raw_name, 1


def _classify_lines(lines):
    """
    Classify receipt lines into three buckets:
      - inline_items:      name + price on same line  (Layout A)
      - name_only_lines:   name only                  (Layout B)
      - standalone_prices: price only                 (Layout B)

    ignore_next_price tracks when a standalone price line immediately follows
    an ignored keyword line (e.g. CASH on one line, $200 on the next) so that
    orphaned prices are not mistakenly paired with item names.
    """
    ignore_next_price = False
    inline_items      = []
    name_only_lines   = []
    standalone_prices = []

    for line in lines:
        if is_ignored_line(line):
            ignore_next_price = True
            continue

        has_letters   = bool(re.search(r"[a-zA-Z]", line))
        price_match   = PRICE_RE.search(line)
        is_price_only = bool(re.fullmatch(r"[\$\s\d\.]+", line))

        if price_match and has_letters and not is_price_only:
            # Inline item candidate — reject noise and watermarks
            if is_currency_noise(line) or is_watermark_or_domain(line.strip()):
                ignore_next_price = False
                continue
            ignore_next_price = False
            inline_items.append(line)

        elif price_match and is_price_only:
            if ignore_next_price:
                # This price belongs to an ignored line (e.g. CASH amount) — skip it
                ignore_next_price = False
            else:
                standalone_prices.append(float(price_match.group(1)))

        elif has_letters:
            if is_watermark_or_domain(line.strip()):
                continue
            ignore_next_price = False
            name_only_lines.append(line)

        else:
            ignore_next_price = False

    return inline_items, name_only_lines, standalone_prices


def extract_items_and_total(text):
    """
    Parse receipt OCR text into (items, total).

    Each item is a dict: {"name": str, "qty": int, "price": float}

    Handles:
      - Layout A: price inline      "1x Lorem ipsum $ 35.00"
      - Layout B: price on next line "1x Lorem ipsum\\n35.00"
      - OCR misreading '$' as 's'/'S'
      - Orphaned CASH/CHANGE prices on separate lines
      - Watermark/domain strings (e.g. modif.ai)
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Step 1: find the receipt total
    total = None
    for i, line in enumerate(lines):
        if "total" in line.lower():
            m = PRICE_RE.search(line)
            if m:
                total = float(m.group(1))
                break
            for j in range(i + 1, min(i + 3, len(lines))):
                m = PRICE_RE.search(lines[j])
                if m:
                    total = float(m.group(1))
                    break
            if total is not None:
                break

    # Step 2: classify lines
    inline_items, name_only_lines, standalone_prices = _classify_lines(lines)

    # Step 3: build structured item list
    items = []

    if inline_items:
        # Layout A: strip price (and any trailing lone 's' from $ misread) from line
        for line in inline_items:
            m = PRICE_RE.search(line)
            price    = float(m.group(1))
            raw_name = PRICE_RE.sub("", line).replace("$", "").strip().rstrip("-").strip()
            raw_name = re.sub(r"\s+[sS]$", "", raw_name).strip()
            name, qty = _parse_name_qty(raw_name)
            items.append({"name": name, "qty": qty, "price": price})
    else:
        # Layout B: pair name lines with standalone price lines in order
        for i in range(min(len(name_only_lines), len(standalone_prices))):
            name, qty = _parse_name_qty(name_only_lines[i])
            items.append({"name": name, "qty": qty, "price": standalone_prices[i]})

    if total is None:
        total = sum(it["price"] for it in items)

    return items, total


def format_receipt_table(items, total):
    """
    Build a Telegram monospace table:

      Item          Qty   Price
      --------------------------
      Lorem ipsum     1  $35.00
      Lorem ipsum     2  $15.00
      --------------------------
      TOTAL              $117.00
    """
    col_name  = max((len(it["name"]) for it in items), default=4)
    col_name  = max(col_name, 4)
    col_qty   = 3
    col_price = max((len(f"${it['price']:.2f}") for it in items), default=5)
    col_price = max(col_price, 5)

    header = f"{'Item':<{col_name}}  {'Qty':>{col_qty}}  {'Price':>{col_price}}"
    sep    = "-" * len(header)
    rows   = [
        f"{it['name']:<{col_name}}  {it['qty']:>{col_qty}}  ${it['price']:>{col_price - 1}.2f}"
        for it in items
    ]
    footer = f"{'TOTAL':<{col_name + col_qty + 2}}  ${total:>{col_price - 1}.2f}"
    return "\n".join(["```", header, sep] + rows + [sep, footer, "```"])


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    help_text = (
        "📦 *Purchase Claim Bot*\n\n"
        "User Commands:\n"
        "  /list - Show your requests\n"
        "  /status <request_id> - Get request status\n"
        "  /help - Show this message\n"
        "Send a receipt photo and I will try to extract the text and a price."
    )
    if user_id in ADMIN_IDS:
        help_text += (
            "\n\n🔐 Admin Commands:\n"
            "  /approve <request_id> - Approve a request\n"
            "  /reject <request_id> - Reject a request\n"
            "  /pending - Show pending requests\n"
            "  /list all - Show all requests\n"
        )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def request_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        item, link, price = parse_request_args(context.args)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    user = update.effective_user
    cursor.execute(
        "INSERT INTO requests (user_id, username, item, link, price, status) VALUES (?, ?, ?, ?, ?, ?)",
        (user.id, user.username, item, link, price, "PENDING"),
    )
    conn.commit()
    await update.message.reply_text(
        f"Request created:\nItem: {item}\nLink: {link}\nPrice: ${price:.2f}\nStatus: PENDING"
    )


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /approve <request_id>")
        return
    try:
        request_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Request ID must be an integer.")
        return
    cursor.execute("SELECT status FROM requests WHERE id=?", (request_id,))
    row = cursor.fetchone()
    if not row:
        await update.message.reply_text(f"Request #{request_id} not found.")
        return
    if row[0] == "APPROVED":
        await update.message.reply_text(f"Request #{request_id} is already approved.")
        return
    cursor.execute("UPDATE requests SET status='APPROVED' WHERE id=?", (request_id,))
    conn.commit()
    await update.message.reply_text(f"Request #{request_id} APPROVED.")


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /reject <request_id>")
        return
    try:
        request_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Request ID must be an integer.")
        return
    cursor.execute("SELECT status FROM requests WHERE id=?", (request_id,))
    row = cursor.fetchone()
    if not row:
        await update.message.reply_text(f"Request #{request_id} not found.")
        return
    if row[0] == "REJECTED":
        await update.message.reply_text(f"Request #{request_id} is already rejected.")
        return
    cursor.execute("UPDATE requests SET status='REJECTED' WHERE id=?", (request_id,))
    conn.commit()
    await update.message.reply_text(f"Request #{request_id} REJECTED.")


async def list_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS and context.args and context.args[0].lower() == "all":
        cursor.execute("SELECT * FROM requests ORDER BY id")
    else:
        cursor.execute(
            "SELECT * FROM requests WHERE user_id=? ORDER BY id",
            (update.effective_user.id,),
        )
    rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("No requests found.")
        return
    message = "\n\n".join(format_request(row) for row in rows)
    for chunk in chunk_text(message):
        await update.message.reply_text(chunk)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /status <request_id>")
        return
    try:
        request_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Request ID must be an integer.")
        return
    cursor.execute("SELECT * FROM requests WHERE id=?", (request_id,))
    row = cursor.fetchone()
    if not row:
        await update.message.reply_text(f"Request #{request_id} not found.")
        return
    if row[1] != update.effective_user.id and update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("You can only view your own requests.")
        return
    await update.message.reply_text(format_request(row))


async def pending_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Not authorized.")
        return
    cursor.execute("SELECT * FROM requests WHERE status='PENDING' ORDER BY id")
    rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("No pending requests.")
        return
    message = "\n\n".join(format_request(row) for row in rows)
    for chunk in chunk_text(message):
        await update.message.reply_text(chunk)


async def upload_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Please send a photo of the receipt.")
        return
    if not OCR_API_KEY:
        await update.message.reply_text("OCR is disabled because OCR_API_KEY is not set.")
        return

    file = await update.message.photo[-1].get_file()
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_file:
        await file.download_to_drive(custom_path=temp_file.name)
        file_path = temp_file.name

    try:
        with open(file_path, "rb") as f:
            response = requests.post(
                "https://api.ocr.space/parse/image",
                files={"file": f},
                data={"apikey": OCR_API_KEY, "language": "eng"},
                timeout=30,
            )
        response.raise_for_status()
        result = response.json()
    except Exception:
        logger.exception("OCR request failed")
        await update.message.reply_text("OCR failed. Please try again with a clearer receipt image.")
        return
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass

    text = extract_text_from_ocr_response(result)
    if not text:
        await update.message.reply_text("OCR did not detect any text. Please try a clearer image.")
        return

    items, total = extract_items_and_total(text)
    if not items:
        await update.message.reply_text(f"Extracted text:\n{text}\n\nNo item prices detected.")
        return

    await update.message.reply_text(format_receipt_table(items, total), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------

def build_application():
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("request", request_command))
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("reject", reject_command))
    application.add_handler(CommandHandler("list", list_requests))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("pending", pending_requests))
    application.add_handler(MessageHandler(filters.PHOTO, upload_receipt))
    return application


def run_bot():
    app = build_application()
    if RENDER_EXTERNAL_URL:
        logger.info("Starting webhook mode.")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{RENDER_EXTERNAL_URL}/{TOKEN}",
        )
    else:
        logger.info("Starting polling mode.")
        app.run_polling()


if __name__ == "__main__":
    run_bot()