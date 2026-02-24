import os
import sqlite3
import re
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.getenv("TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
OCR_API_KEY = os.getenv("OCR_API_KEY")

# --- Database ---
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

# --- Commands ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    help_text = (
        "📦 *Purchase Claim Bot*\n\n"
        "User Commands:\n"
        "/request <item> <link> <price>\n"
        "/list  → View all requests\n"
        "/help  → Show this message\n"
        "Or just upload a receipt image and the bot will extract info!"
    )
    if user_id in ADMIN_IDS:
        help_text += "\n\n🔐 Admin Commands:\n/approve <request_id>\n/reject <request_id>"
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        item, link, price = context.args[0], context.args[1], float(context.args[2])
    except:
        await update.message.reply_text("Usage:\n/request <item> <link> <price>")
        return
    user = update.effective_user
    cursor.execute("""
        INSERT INTO requests (user_id, username, item, link, price, status)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user.id, user.username, item, link, price, "PENDING"))
    conn.commit()
    await update.message.reply_text(f"Request created. Item: {item}, Price: ${price}, Status: PENDING")

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Not authorized.")
        return
    try:
        request_id = int(context.args[0])
    except:
        await update.message.reply_text("Usage: /approve <request_id>")
        return
    cursor.execute("UPDATE requests SET status='APPROVED' WHERE id=?", (request_id,))
    conn.commit()
    await update.message.reply_text(f"Request #{request_id} APPROVED.")

async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Not authorized.")
        return
    try:
        request_id = int(context.args[0])
    except:
        await update.message.reply_text("Usage: /reject <request_id>")
        return
    cursor.execute("UPDATE requests SET status='REJECTED' WHERE id=?", (request_id,))
    conn.commit()
    await update.message.reply_text(f"Request #{request_id} REJECTED.")

async def list_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute("SELECT * FROM requests")
    rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("No requests.")
        return
    message = ""
    for r in rows:
        message += f"ID: {r[0]}\nItem: {r[3]}\nPrice: ${r[5]}\nStatus: {r[6]}\n\n"
    await update.message.reply_text(message)

# --- Receipt OCR Handler ---
async def upload_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Please send a photo of the receipt.")
        return
    file = await update.message.photo[-1].get_file()
    file_path = await file.download_to_drive()

    with open(file_path, "rb") as f:
        r = requests.post(
            "https://api.ocr.space/parse/image",
            files={"file": f},
            data={"apikey": OCR_API_KEY, "language": "eng"},
        )
    result = r.json()
    try:
        text = result["ParsedResults"][0]["ParsedText"]
    except Exception:
        await update.message.reply_text("OCR failed. Try a clearer image.")
        return

    # Extract first number as price (example)
    prices = re.findall(r"\d+\.\d{2}", text)
    price = prices[0] if prices else "N/A"

    await update.message.reply_text(f"Extracted text:\n{text}\n\nDetected price: {price}")

# --- Bot Setup ---
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("request", request))
app.add_handler(CommandHandler("approve", approve))
app.add_handler(CommandHandler("reject", reject))
app.add_handler(CommandHandler("list", list_requests))
app.add_handler(MessageHandler(filters.PHOTO, upload_receipt))

PORT = int(os.environ.get("PORT", 10000))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")  # e.g., https://teleclaims-bot.onrender.com

app.run_webhook(
    listen="0.0.0.0",
    port=PORT,
    url_path=TOKEN,            # <-- IMPORTANT: set url_path to your bot token
    webhook_url=f"{RENDER_EXTERNAL_URL}/{TOKEN}"  # <-- Telegram POSTs here
)