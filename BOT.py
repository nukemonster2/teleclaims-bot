import sqlite3
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)
import os

TOKEN = os.getenv("TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))


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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    help_text = (
        "📦 *Purchase Claim Bot*\n\n"
        "User Commands:\n"
        "/request <item> <link> <price>\n"
        "Example:\n"
        "/request ArduinoNano https://shopee.sg/... 18.90\n\n"
        "/list  → View all requests\n"
        "/help  → Show this message\n"
    )

    if user_id in ADMIN_IDS:
        help_text += (
            "\n🔐 Admin Commands:\n"
            "/approve <request_id>\n"
            "/reject <request_id>\n"
        )

    await update.message.reply_text(help_text, parse_mode="Markdown")
async def request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        item = context.args[0]
        link = context.args[1]
        price = float(context.args[2])
    except:
        await update.message.reply_text(
            "Usage:\n/request <item> <link> <price>"
        )
        return

    user = update.effective_user

    cursor.execute("""
        INSERT INTO requests (user_id, username, item, link, price, status)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user.id, user.username, item, link, price, "PENDING"))
    conn.commit()

    request_id = cursor.lastrowid

    await update.message.reply_text(
        f"Request #{request_id} created.\n"
        f"Item: {item}\nPrice: ${price}\nStatus: PENDING"
    )


async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Not authorized.")
        return

    try:
        request_id = int(context.args[0])
    except:
        await update.message.reply_text("Usage:\n/approve <request_id>")
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
        await update.message.reply_text("Usage:\n/reject <request_id>")
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
        message += (
            f"ID: {r[0]}\n"
            f"Item: {r[3]}\n"
            f"Price: ${r[5]}\n"
            f"Status: {r[6]}\n\n"
        )

    await update.message.reply_text(message)



app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("request", request))
app.add_handler(CommandHandler("approve", approve))
app.add_handler(CommandHandler("reject", reject))
app.add_handler(CommandHandler("list", list_requests))
app.add_handler(CommandHandler("help", help_command))
app.run_polling()