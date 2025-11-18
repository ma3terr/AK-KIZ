import os
import sys
import telebot
from flask import Flask, request
import logging
from google import genai
from io import BytesIO
from PIL import Image

# --- تنظیمات و لاگینگ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- متغیرهای محیطی ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
API_KEY_GEMINI = os.environ.get('API_KEY_GEMINI')

if not BOT_TOKEN:
    logging.error("❌ BOT_TOKEN محیطی تنظیم نشده است. برنامه متوقف می‌شود.")
    sys.exit(1)

if not API_KEY_GEMINI:
    logging.error("❌ API_KEY_GEMINI محیطی تنظیم نشده است. ربات تنها به پیام‌های متنی ساده پاسخ خواهد داد.")
    gemini_enabled = False
else:
    gemini_enabled = True
    try:
        # پیکربندی کلاینت جمینای
        gemini_client = genai.Client(api_key=API_KEY_GEMINI)
        MODEL_NAME = 'gemini-2.5-flash'
        logging.info("⭐ کلاینت Gemini با موفقیت راه‌اندازی شد.")
    except Exception as e:
        logging.error(f"❌ خطای راه‌اندازی Gemini Client: {e}")
        gemini_enabled = False

# --- راه‌اندازی ربات و وب‌سرور ---
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# --- مسیر وب‌هوک Flask ---
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        try:
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
        except Exception as e:
            # این خطاها شامل کرش‌های ناگهانی در حین process_new_updates یا خطای JSON هستند
            logging.error(f"⚠️ خطای پردازش به‌روزرسانی (احتمالاً کرش): {e}", exc_info=True)
        return "OK", 200
    else:
        logging.warning("درخواست غیر JSON دریافت شد.")
        return "Invalid Content Type", 403

# --- هندلرهای پیام ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    status_msg = "فعال" if gemini_enabled else "غیرفعال (API_KEY_GEMINI موجود نیست)"
    response_text = f"""
سلام! من یک ربات تحلیلگر تصویر هستم.
وضعیت Gemini: **{status_msg}**

شما می‌توانید:
1. **یک تصویر** بفرستید. من آن را با Gemini تحلیل می‌کنم و توضیحات کاملی می‌دهم.
2. **یک تصویر** به همراه **متن** (caption) بفرستید. من تصویر را بر اساس دستورالعمل متنی شما تحلیل می‌کنم.
3. فقط **پیام متنی** بفرستید.

**توجه:** اگر ربات به پیام تصویری پاسخ نداد، لطفاً لاگ‌های Railway را بررسی کنید.
"""
    try:
        bot.reply_to(message, response_text, parse_mode="Markdown")
        logging.info(f"✅ پاسخ به /start برای کاربر {message.from_user.id}")
    except Exception as e:
        logging.error(f"❌ خطای پاسخ به /start: {e}", exc_info=True)

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    if not gemini_enabled:
        bot.reply_to(message, "❗️ متأسفم، کلید API جمینای تنظیم نشده است. نمی‌توانم تصاویر را پردازش کنم.")
        return

    # 1. گرفتن بهترین کیفیت عکس
    file_id = message.photo[-1].file_id
    prompt = message.caption if message.caption else "تصویر را با جزئیات کامل و به زبان فارسی تحلیل کن و توضیح بده."

    bot.send_chat_action(message.chat.id, 'typing')
    
    try:
        # 2. گرفتن اطلاعات فایل و دانلود آن
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # 3. تبدیل به فرمت PIL Image
        image_stream = BytesIO(downloaded_file)
        pil_image = Image.open(image_stream)
        
        # 4. آماده‌سازی محتوا برای Gemini
        contents = [prompt, pil_image]
        
        logging.info(f"💫 ارسال تصویر به Gemini با پرامپت: '{prompt[:50]}...'")
        
        # 5. فراخوانی API Gemini
        response = gemini_client.models.generate_content(
            model=MODEL_NAME,
            contents=contents
        )
        
        # 6. ارسال پاسخ
        bot.reply_to(message, response.text)
        logging.info(f"✅ پاسخ Gemini با موفقیت برای کاربر {message.from_user.id} ارسال شد.")
        
    except telebot.apihelper.ApiTelegramException as e:
        error_msg = f"❗️ خطای تلگرام در دانلود یا ارسال پاسخ: {e}"
        logging.error(error_msg, exc_info=True)
        bot.reply_to(message, f"❌ خطای اتصال تلگرام (Telegram API Error):\n`{str(e)}`")
        
    except genai.errors.APIError as e:
        error_msg = f"❗️ خطای API Gemini: {e}"
        logging.error(error_msg, exc_info=True)
        bot.reply_to(message, f"❌ خطای جمینای (Gemini API Error):\n`{str(e)}`")

    except Exception as e:
        # پوشش هرگونه خطای ناشناخته (مثل خطای PIL، کمبود حافظه، ...)
        error_msg = f"❗️ خطای ناشناخته در پردازش تصویر: {e}"
        logging.error(error_msg, exc_info=True)
        bot.reply_to(message, f"❌ متأسفم، خطایی در پردازش رخ داد. لطفاً لاگ‌های Railway را بررسی کنید. (خطا: {type(e).__name__})")

@bot.message_handler(func=lambda message: True, content_types=['text'])
def handle_text(message):
    try:
        if gemini_enabled:
            # اگر فقط متن بود، می‌توانیم از Gemini برای چت معمولی استفاده کنیم
            bot.send_chat_action(message.chat.id, 'typing')
            
            logging.info(f"💫 ارسال پیام متنی به Gemini برای کاربر {message.from_user.id}")
            response = gemini_client.models.generate_content(
                model=MODEL_NAME,
                contents=[message.text]
            )
            bot.reply_to(message, response.text)
            logging.info("✅ پاسخ متنی Gemini ارسال شد.")
        else:
            # در صورت عدم وجود کلید API
            response_text = "پیام متنی شما دریافت شد. کلید API جمینای تنظیم نشده است، بنابراین فقط پاسخ ساده می‌دهم."
            bot.reply_to(message, response_text)
            
    except genai.errors.APIError as e:
        error_msg = f"❗️ خطای API Gemini در حالت متنی: {e}"
        logging.error(error_msg, exc_info=True)
        bot.reply_to(message, f"❌ خطای جمینای در پاسخ متنی:\n`{str(e)}`")

    except Exception as e:
        error_msg = f"❌ خطای عمومی در پاسخ متنی: {e}"
        logging.error(error_msg, exc_info=True)
        bot.reply_to(message, "❌ متأسفم، خطایی در پاسخ متنی رخ داد.")


# --- اجرای وب‌سرور ---
if __name__ == "__main__":
    WEBHOOK_URL_BASE = os.environ.get('WEBHOOK_BASE')
    WEBHOOK_URL_PATH = f'/{BOT_TOKEN}'

    if WEBHOOK_URL_BASE:
        full_webhook_url = f"{WEBHOOK_URL_BASE.rstrip('/')}{WEBHOOK_URL_PATH}"
        try:
            bot.set_webhook(url=full_webhook_url)
            logging.info(f"⭐ وب‌هوک با موفقیت تنظیم شد: {full_webhook_url}")
        except Exception as e:
            logging.error(f"❌ خطای تنظیم وب‌هوک: {e}", exc_info=True)
    else:
        logging.warning("⚠️ متغیر WEBHOOK_BASE تنظیم نشده است. ربات ممکن است به‌روزرسانی‌ها را دریافت نکند.")
        
    port = int(os.environ.get('PORT', 8080))
    logging.info(f"🚀 شروع برنامه Flask روی پورت {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
