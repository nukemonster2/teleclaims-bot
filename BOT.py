import os
import re
import logging
import tempfile
import sqlite3

from fastapi import FastAPI, Request
import uvicorn

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from paddleocr import PaddleOCR

# ---------------- CONFIG ----------------

TOKEN = os.getenv("TOKEN")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL")  # your Render URL like https://xxx.onrender.com
PORT = int(os.getenv("PORT", 10000))

if not TOKEN or not BASE_URL:
    raise SystemExit("Missing TOKEN or BASE_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app_web = FastAPI()

# ---------------- OCR (lazy load) ----------------

ocr = None

def get_ocr():
    global ocr
    if ocr is None:
        logger.info("Loading OCR...")
        ocr = PaddleOCR(use_angle_cls=True, lang="en")
    return ocr

# ---------------- TELEGRAM APP ----------------

tg_app = ApplicationBuilder().token(TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is live.")

async def upload_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.photo[-1].get_file()

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        await file.download_to_drive(custom_path=f.name)
        path = f.name

    try:
        result = get_ocr().ocr(path, cls=True)

        text = "\n".join(
            line[1][0]
            for block in result
            for line in block
        )

    except Exception as e:
        logger.exception(e)
        return await update.message.reply_text("OCR failed")

    finally:
        try:
            os.remove(path)
        except:
            pass

    await update.message.reply_text(f"OCR:\n{text}")

tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(MessageHandler(filters.PHOTO, upload_receipt))

# ---------------- FASTAPI ----------------

@app_web.get("/")
def home():
    return {"status": "ok"}

@app_web.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, tg_app.bot)

    await tg_app.process_update(update)
    return {"ok": True}

# ---------------- SET WEBHOOK ----------------

@app_web.on_event("startup")
async def on_start():
    await tg_app.initialize()

    webhook_url = f"{BASE_URL}/webhook"
    await tg_app.bot.set_webhook(webhook_url)

    logger.info(f"Webhook set to {webhook_url}")

# ---------------- RUN ----------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app_web,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )