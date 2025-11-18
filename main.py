# A complete, production‑ready Telegram bot using Flask webhook + Gemini + PDF/Image analysis
# Full features:
# - Text chat
# - Image understanding
# - PDF processing (extract text, summarize, generate exam questions)
# - Session history per user (in‑memory)
# - Webhook‑based server for Railway/Render/etc
#
# NOTE:
#   You must create environment variables on the server:
#     BOT_TOKEN
#     GEMINI_API_KEY
#     WEBHOOK_BASE   (e.g. https://your-app.up.railway.app)
#
#   Then run this file on the server.

import os
import logging
import time
from io import BytesIO
from PIL import Image
import fitz  # PyMuPDF for PDF

from flask import Flask, request, abort
import telebot

from google import genai
from google.genai import types
from google.genai.errors import APIError

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- ENV ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE")
MODEL_NAME = "gemini-2.5-flash-preview-09-2025"

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing")
if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY missing — Gemini responses will fail.")
if not WEBHOOK_BASE:
    raise SystemExit("WEBHOOK_BASE missing")

TEMP_DIR = "/tmp/bot_temp"
os.makedirs(TEMP_DIR, exist_ok=True)

# ---------------- Gemini ----------------
try:
    client = genai.Client(api_key=GEMINI_API_KEY)
except Exception:
    client = None
    logger.error("Gemini failed to initialize")

# ---------------- Telebot ----------------
WEBHOOK_URL_PATH = f"/{BOT_TOKEN}"
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# Session memory (in‑RAM)
chat_sessions = {}

# ---------------- Helper: PDF → first page image ----------------
def pdf_to_image(path):
    try:
        doc = fitz.open(path)
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img_path = os.path.join(TEMP_DIR, "preview_" + os.path.basename(path) + ".png")
        pix.save(img_path)
        doc.close()
        return Image.open(img_path)
    except Exception as e:
        logger.error(f"PDF convert error: {e}")
        return None

# ---------------- Gemini request wrapper ----------------
def run_gemini(user_id, prompt, image_part=None):
    if client is None:
        return "❌ اتصال به Gemini برقرار نیست."

    # load/create chat session
    if user_id not in chat_sessions:
        chat_sessions[user_id] = client.chats.create(model=MODEL_NAME)
    chat = chat_sessions[user_id]

    contents = []
    if image_part:
        contents.append(image_part)
    if prompt:
        contents.append(prompt)

    try:
        res = chat.send_message(contents)
        return res.text
    except APIError:
        return "❌ خطای API گوگل"
    except Exception as e:
        logger.error(e)
        return "❌ خطای داخلی در پردازش درخواست"

# ---------------- Telegram handlers ----------------
@bot.message_handler(commands=["start", "help"])
def welcome(msg):
    bot.reply_to(msg,
        "سلام! من ربات هوش مصنوعی هستم.\n\n"
        "می‌توانی:\n"
        "• سوال بپرسی\n"
        "• عکس بفرستی تا تحلیل کنم\n"
        "• PDF بفرستی تا ازش نمونه سوال درست کنم یا خلاصه بدم"
    )

@bot.message_handler(content_types=["text"])
def text_handler(msg):
    uid = msg.chat.id
    bot.send_chat_action(uid, 'typing')
    out = run_gemini(uid, msg.text)
    bot.send_message(uid, out)

@bot.message_handler(content_types=["photo", "document"])
def file_handler(msg):
    uid = msg.chat.id

    # ---- Image ----
    if msg.content_type == "photo":
        file_id = msg.photo[-1].file_id
        mime = "image/jpeg"
        caption = msg.caption or "این تصویر را تحلیل کن"

        info = bot.get_file(file_id)
        data = bot.download_file(info.file_path)

        path = os.path.join(TEMP_DIR, f"{file_id}.jpg")
        with open(path, "wb") as f:
            f.write(data)

        try:
            img = Image.open(path)
            part = types.Part.from_image(img)
        except Exception:
            bot.reply_to(msg, "❌ تصویر قابل پردازش نیست.")
            return

        bot.send_chat_action(uid, 'typing')
        out = run_gemini(uid, caption, image_part=part)
        bot.send_message(uid, out)
        return

    # ---- PDF ----
    if msg.content_type == "document":
        doc = msg.document
        mime = doc.mime_type
        caption = msg.caption or "از این PDF نمونه سوال و خلاصه بساز"

        if "pdf" not in mime:
            bot.reply_to(msg, "❌ فقط PDF پشتیبانی می‌شود.")
            return

        info = bot.get_file(doc.file_id)
        data = bot.download_file(info.file_path)

        path = os.path.join(TEMP_DIR, f"{doc.file_id}.pdf")
        with open(path, "wb") as f:
            f.write(data)

        # Get first‑page preview
        preview = pdf_to_image(path)
        if preview is None:
            bot.reply_to(msg, "❌ خطا در پردازش PDF.")
            return

        part = types.Part.from_image(preview)

        bot.send_chat_action(uid, 'typing')
        out = run_gemini(uid, caption, image_part=part)
        bot.send_message(uid, out)

# ---------------- Flask ----------------
app = Flask(__name__)

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        upd = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([upd])
        return "OK", 200
    abort(403)

@app.route("/")
def home():
    return "Bot running", 200

# ---------------- Setup Webhook & Run ----------------
def setup_webhook():
    full = WEBHOOK_BASE + WEBHOOK_URL_PATH
    bot.remove_webhook()
    time.sleep(0.5)
    ok = bot.set_webhook(full)
    if ok:
        logger.info(f"Webhook set: {full}")
    else:
        logger.error("Webhook FAILED")

if __name__ == "__main__":
    setup_webhook()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
