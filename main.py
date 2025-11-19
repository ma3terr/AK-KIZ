import os
import json
import logging
import io
from telebot import TeleBot
from flask import Flask, request, jsonify
from google import genai
from google.genai.errors import APIError
from PIL import Image

# --- تنظیمات اولیه و متغیرهای محیطی ---
# تلاش برای دریافت متغیرهای حیاتی از محیط Railway
BOT_TOKEN = os.environ.get('BOT_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
# WEBHOOK_BASE آدرس اصلی اپلیکیشن در Railway است (مثال: https://ak-kiz-production.up.railway.app)
WEBHOOK_BASE = os.environ.get('WEBHOOK_BASE') 

# پیکربندی Webhook
WEBHOOK_URL_PATH = f'/{BOT_TOKEN}' # مسیر محلی در سرور
WEBHOOK_URL = f'{WEBHOOK_BASE}{WEBHOOK_URL_PATH}' # آدرس کامل برای تنظیم در تلگرام

# تنظیم لاگ‌نویسی برای کمک به دیباگ
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- راه‌اندازی ربات و مدل Gemini ---
if not BOT_TOKEN or not GEMINI_API_KEY or not WEBHOOK_BASE:
    logger.error("!!! متغیرهای محیطی حیاتی (BOT_TOKEN, GEMINI_API_KEY, WEBHOOK_BASE) تنظیم نشده‌اند. !!!")
    # در محیط Gunicorn، این خروج باعث توقف پروسه می‌شود
    # اما در Railway معمولاً این متغیرها تنظیم شده‌اند.
    # برای جلوگیری از خطای ناگهانی در زمان Import، ادامه می‌دهیم اما با لاگ خطا.
    pass

# راه‌اندازی ربات تلگرام
# threaded=False برای محیط Webhook ضروری است
bot = TeleBot(BOT_TOKEN, threaded=False)

# راه‌اندازی سرویس Gemini
gemini_client = None
if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("Gemini client initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize Gemini client: {e}")

# --- توابع مدیریت پیام (Handler Functions) ---

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    """پاسخ به دستورات /start و /help"""
    welcome_text = (
        "سلام! من ربات هوش مصنوعی شما هستم. 👋\n"
        "هر سوالی دارید بپرسید یا یک عکس به همراه توضیح برای من بفرستید.\n"
        "من از مدل پیشرفته Gemini برای پاسخگویی استفاده می‌کنم."
    )
    bot.reply_to(message, welcome_text)

def generate_response(contents, chat_id, message_id):
    """تابع مرکزی برای تولید پاسخ با Gemini"""
    if not gemini_client:
        bot.reply_to(message_id, "متأسفانه سرویس هوش مصنوعی هنوز فعال نشده است. لطفاً متغیرهای API Key را بررسی کنید.")
        return

    try:
        # ارسال پیام اولیه برای نشان دادن اینکه ربات در حال کار است
        bot.send_chat_action(chat_id, 'typing')
        
        # تولید محتوا توسط Gemini
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents
        )
        
        # ارسال پاسخ به کاربر
        bot.reply_to(message_id, response.text)
        logger.info(f"Response sent to {chat_id}.")
        
    except APIError as e:
        logger.error(f"Gemini API Error for {chat_id}: {e}")
        bot.reply_to(message_id, "متأسفانه به دلیل خطای API هوش مصنوعی قادر به پاسخگویی نیستم. لطفاً دوباره تلاش کنید.")
    except Exception as e:
        logger.error(f"General Error for {chat_id}: {e}")
        bot.reply_to(message_id, "یک خطای ناشناخته رخ داد. تیم فنی در حال بررسی مشکل است.")

@bot.message_handler(content_types=['text'])
def handle_text_message(message):
    """پاسخ به تمام پیام‌های متنی"""
    user_prompt = message.text
    chat_id = message.chat.id
    
    logger.info(f"Received text message from {chat_id}: {user_prompt[:50]}...")
    generate_response(user_prompt, chat_id, message.message_id)

@bot.message_handler(content_types=['photo'])
def handle_photo_message(message):
    """پاسخ به پیام‌های شامل عکس"""
    chat_id = message.chat.id
    # اگر توضیحی همراه عکس نباشد، یک درخواست پیش‌فرض ارسال می‌کند
    caption = message.caption or "این عکس چیست؟ لطفا آن را توصیف کن."
    
    logger.info(f"Received photo message from {chat_id} with caption: {caption}")
    
    try:
        # دریافت بزرگترین سایز عکس
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        # تبدیل فایل باینری به شیء Image از PIL
        image_stream = io.BytesIO(downloaded_file)
        img = Image.open(image_stream)
        
        # ساخت محتوای ترکیبی برای Gemini (عکس + متن)
        contents = [img, caption]
        
        generate_response(contents, chat_id, message.message_id)

    except Exception as e:
        logger.error(f"Error handling photo from {chat_id}: {e}")
        bot.reply_to(message.message_id, "متأسفانه در پردازش عکس شما مشکلی پیش آمد.")

# --- راه‌اندازی وب‌هوک Flask ---

# Flask App باید در سطح ماژول تعریف شود تا Gunicorn آن را پیدا کند.
app = Flask(__name__)

@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    """نقطه پایانی که تلگرام پیام‌ها را به آن ارسال می‌کند."""
    # بررسی کنید که درخواست از نوع JSON باشد
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data(as_text=True)
        update = json.loads(json_string)
        # پردازش آپدیت توسط telebot
        bot.process_new_updates([update])
        # پاسخ 200 (OK) ضروری است تا تلگرام بداند پیام دریافت شده است
        return jsonify(status="ok"), 200
    else:
        # اگر فرمت داده‌ها درست نباشد، کد 403 را برمی‌گرداند
        return jsonify(status="bad request"), 403

# مسیر اصلی / که برای تست سلامت سرور استفاده می‌شود
@app.route('/')
def index():
    return "ربات تلگرام در حال اجرا است و منتظر دریافت پیام از طریق وب‌هوک است.", 200

# --- تنظیم وب‌هوک در زمان استقرار ---

def set_webhook_on_startup():
    """تنظیم وب‌هوک در تلگرام پس از شروع موفقیت‌آمیز برنامه."""
    if not WEBHOOK_BASE:
        logger.error("Cannot set webhook: WEBHOOK_BASE is not defined.")
        return
        
    try:
        # حذف وب‌هوک‌های قدیمی (در صورت وجود)
        bot.remove_webhook()
        # تنظیم وب‌هوک جدید
        if bot.set_webhook(url=WEBHOOK_URL):
            logger.info(f"Webhook set successfully to: {WEBHOOK_URL}")
        else:
            logger.error("!!! Webhook setting failed. Check your BOT_TOKEN and WEBHOOK_BASE. !!!")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}. Check network connectivity or environment variables.")

# تابع set_webhook_on_startup در زمان import شدن ماژول توسط Gunicorn اجرا می‌شود
set_webhook_on_startup()
