# -*- coding: utf-8 -*-
# ربات تلگرام مجهز به هوش مصنوعی با قابلیت چت و تحلیل تصویر
# این نسخه بخش پردازش PDF را برای اطمینان از استقرار موفق در سرورهای ابری حذف کرده است.

import os
import logging
import time
from io import BytesIO
from PIL import Image

from flask import Flask, request, abort
import telebot

from google import genai
from google.genai import types
from google.genai.errors import APIError

# ---------------- Logging (تنظیمات لاگ‌گیری) ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- ENV (متغیرهای محیطی) ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE")
# استفاده از نام مدل پایدار
MODEL_NAME = "gemini-2.5-flash" 

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN محیطی تنظیم نشده است.")
if not GEMINI_API_KEY:
    logger.warning("⚠️ GEMINI_API_KEY تنظیم نشده است - پاسخ‌های Gemini شکست خواهند خورد.")
if not WEBHOOK_BASE:
    raise SystemExit("❌ WEBHOOK_BASE محیطی تنظیم نشده است.")

TEMP_DIR = "/tmp/bot_temp"
os.makedirs(TEMP_DIR, exist_ok=True)

# ---------------- Gemini (راه‌اندازی هوش مصنوعی) ----------------
client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("⭐ کلاینت Gemini با موفقیت راه‌اندازی شد.")
    except Exception as e:
        logger.error(f"❌ Gemini failed to initialize: {e}")

# ---------------- Telebot (راه‌اندازی ربات) ----------------
# مسیر وب‌هوک باید شامل توکن باشد تا ایمن باشد
WEBHOOK_URL_PATH = f"/{BOT_TOKEN}"
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# Session memory (حافظه موقت چت)
chat_sessions = {}

# ---------------- Gemini request wrapper (مدیریت چت) ----------------
def run_gemini(user_id, prompt, image_part=None):
    """
    ارسال پیام به مدل Gemini و مدیریت تاریخچه چت.
    """
    if client is None:
        return "❌ اتصال به Gemini برقرار نیست. لطفاً کلید API را بررسی کنید."

    # بارگذاری یا ایجاد نشست چت جدید (تاریخچه مکالمه حفظ می‌شود)
    if user_id not in chat_sessions:
        chat_sessions[user_id] = client.chats.create(model=MODEL_NAME)
    chat = chat_sessions[user_id]

    contents = []
    if image_part:
        contents.append(image_part)
    if prompt:
        contents.append(prompt)

    if not contents:
        return "لطفاً محتوای مورد نظر برای پردازش را بفرستید."

    try:
        res = chat.send_message(contents)
        return res.text
    except APIError as e:
        logger.error(f"❌ Gemini API Error: {e}")
        return f"❌ خطای API گوگل: لطفاً کلید خود را بررسی کنید."
    except Exception as e:
        logger.error(f"❌ Internal processing error: {e}", exc_info=True)
        return "❌ خطای داخلی در پردازش درخواست."

# ---------------- Telegram handlers (هندلرهای تلگرام) ----------------
@bot.message_handler(commands=["start", "help"])
def welcome(msg):
    """پاسخ به دستورات شروع و راهنما."""
    gemini_status = "✅ فعال" if client else "❌ غیرفعال"
    bot.reply_to(msg,
        f"سلام! من ربات هوش مصنوعی هستم. (وضعیت Gemini: {gemini_status})\n\n"
        "می‌توانی:\n"
        "• سوال بپرسی\n"
        "• عکس بفرستی تا تحلیل کنم"
    )

@bot.message_handler(content_types=["text"])
def text_handler(msg):
    """پاسخ به پیام‌های متنی."""
    uid = msg.chat.id
    bot.send_chat_action(uid, 'typing')
    out = run_gemini(uid, msg.text)
    bot.send_message(uid, out)

@bot.message_handler(content_types=["photo"])
def file_handler(msg):
    """تحلیل عکس‌ها."""
    uid = msg.chat.id
    caption = msg.caption or "این تصویر را تحلیل کن و یک توضیح مختصر بده."
    
    try:
        # دریافت فایل با بالاترین کیفیت
        file_id = msg.photo[-1].file_id
        info = bot.get_file(file_id)
        data = bot.download_file(info.file_path)

        # باز کردن تصویر در حافظه
        img = Image.open(BytesIO(data))
        # تبدیل تصویر به فرمت مورد نیاز Gemini
        part = types.Part.from_image(img)
        
        bot.send_chat_action(uid, 'typing')
        out = run_gemini(uid, caption, image_part=part)
        bot.send_message(uid, out)

    except Exception as e:
        logger.error(f"❌ Image processing error: {e}", exc_info=True)
        bot.reply_to(msg, "❌ خطای پردازش تصویر: فایل قابل دانلود یا تبدیل نیست.")

# ---------------- Flask Webhook (دریافت به‌روزرسانی‌ها) ----------------
app = Flask(__name__)

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    """هندلر اصلی وب‌هوک Flask."""
    if request.headers.get('content-type') == 'application/json':
        try:
            # دیکد کردن داده‌ها و فرستادن به Telebot
            upd = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
            bot.process_new_updates([upd])
        except Exception as e:
             logger.error(f"❌ Webhook processing failed: {e}", exc_info=True)
        return "OK", 200
    abort(403)

@app.route("/")
def home():
    """صفحه اصلی برای بررسی وضعیت سرور."""
    return "Bot running", 200

# ---------------- Setup Webhook & Run ----------------
def setup_webhook():
    """تنظیم وب‌هوک در تلگرام."""
    base = WEBHOOK_BASE.rstrip('/') 
    full = f"{base}{WEBHOOK_URL_PATH}"
    
    # حذف وب‌هوک قبلی برای اطمینان و تنظیم وب‌هوک جدید
    bot.remove_webhook()
    time.sleep(0.5) # کمی صبر برای اعمال تغییر
    ok = bot.set_webhook(full)
    if ok:
        logger.info(f"⭐ Webhook set: {full}")
    else:
        logger.error("❌ Webhook FAILED. Check WEBHOOK_BASE URL and connectivity.")

if __name__ == "__main__":
    setup_webhook()
    # استفاده از متغیر محیطی PORT که توسط Railway فراهم می‌شود
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🚀 Starting Flask app on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
