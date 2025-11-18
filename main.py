import os
import sys
import threading
import logging
import time

from PIL import Image
# از PyMuPDF به عنوان "fitz" استفاده می‌شود.
import fitz

# کتابخانه‌های گوگل و تله‌بات
import telebot
from google import genai
from google.genai import types
from google.genai.errors import APIError

# اضافه شدن Flask و Firebase برای اجرای بات
from flask import Flask, request

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore

################################################################
# تنظیمات لاگ‌گیری
################################################################
# تنظیمات لاگ‌گیری استاندارد
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

################################################################
# متغیرهای محیطی و ثابت‌ها
################################################################
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID",
                               "6082991135")  # شناسه ادمین یا مقدار پیش‌فرض
MAX_RETRIES = 3
API_KEY_FILE = os.environ.get("API_KEY_FILE")

# دریافت کلیدهای API از متغیرهای محیطی
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not BOT_TOKEN:
    logger.error(
        "BOT_TOKEN is not defined. Please set the environment variable.")
if not GEMINI_API_KEY:
    logger.error(
        "GEMINI_API_KEY is not defined. Using an empty string, which will cause API calls to fail unless provided later."
    )

################################################################
# مقداردهی اولیه Firebase
################################################################
db = None
if API_KEY_FILE:
    try:
        # اگر از فایل JSON استفاده می‌شود (مانند محیط‌های تولیدی)
        cred = credentials.Certificate(API_KEY_FILE)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info(
            "Firebase Admin SDK initialized successfully with credentials file."
        )
    except Exception as e:
        logger.warning(
            f"Failed to initialize Firebase with credentials file: {e}")
    except Exception:  # اگر خطا داد، تلاش برای استفاده از تنظیمات پیش‌فرض یا متغیر محیطی
        pass  # ادامه کار بدون Firebase
else:
    try:
        # اگر در محیط‌هایی مانند Google Cloud یا Replit اجرا می‌شود
        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        db = firestore.client()
        logger.info("Firebase Admin SDK initialized successfully (default).")
    except Exception as e:
        logger.warning(
            f"Could not initialize Firebase Admin SDK. Query logging will be disabled. Error: {e}"
        )

################################################################
# مقداردهی اولیه Gemini و Telegram
################################################################
# تعریف کلاینت‌ها بعد از مطمئن شدن از وجود کلید
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("Gemini Client initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize Gemini Client: {e}")
        client = None
else:
    client = None

# Telebot initialization
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='MARKDOWN')
logger.info("TeleBot initialized successfully.")

# ذخیره سشن‌ها در حافظه
chat_sessions = {}

# مدل مورد استفاده
MODEL_NAME = "gemini-2.5-flash-preview-09-2025"

# دیکشنری برای ذخیره آخرین زمان‌های تعامل (برای مدیریت پیام‌های همزمان)
last_interaction_time = {}

# مسیری برای ذخیره فایل‌های موقت
TEMP_DIR = 'temp'
if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)
    logger.info(f"Created temporary directory: {TEMP_DIR}")

################################################################
# توابع Firebase
################################################################


def get_session_history(user_id):
    """بارگیری تاریخچه چت از Firestore."""
    if client is None:
        logger.warning("Gemini Client not initialized.")
        return None

    if db is None:
        logger.warning(
            "Firestore is not initialized. Using temporary in-memory chat session."
        )
        return chat_sessions.get(user_id,
                                 client.chats.create(model=MODEL_NAME))

    try:
        doc_ref = db.collection('user_chats').document(str(user_id))
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            # بازسازی سشن از تاریخچه پیام‌های ذخیره‌شده
            history_data = data.get('history', [])

            # تبدیل تاریخچه ذخیره‌شده (که شامل role و text است) به یک سشن چت جدید
            # تاریخچه باید به فرمت Content که شامل Parts است باشد.
            contents = []
            for item in history_data:
                # پیام‌های ذخیره شده فقط متنی هستند، بنابراین فقط یک Part متنی دارند.
                contents.append(
                    types.Content(role=item['role'],
                                  parts=[types.Part.from_text(item['text'])]))

            # ایجاد سشن با تاریخچه بازسازی شده
            return client.chats.create(model=MODEL_NAME, history=contents)
        else:
            # ایجاد سشن جدید اگر وجود ندارد
            return client.chats.create(model=MODEL_NAME)
    except Exception as e:
        logger.error(f"Error loading chat history for user {user_id}: {e}")
        return client.chats.create(model=MODEL_NAME)  # Fallback to new session


def save_session_history(user_id, chat):
    """ذخیره تاریخچه چت در Firestore."""
    if db is None:
        logger.warning(
            "Firestore is not initialized. Skipping chat history save.")
        return

    try:
        # استخراج تاریخچه پیام‌ها برای ذخیره در دیتابیس
        # فقط پیام‌های متنی ذخیره می‌شوند
        history = []
        for message in chat.history:
            # بررسی می‌کنیم که پیام فقط شامل یک بخش متنی باشد
            if len(message.parts) == 1 and message.parts[0].text is not None:
                history.append({
                    'role': message.role,
                    'text': message.parts[0].text
                })
            # توجه: اگر پیام شامل تصویر یا PDF باشد، فقط بخش متنی آن (در صورت وجود) ذخیره می‌شود.
            # برای ذخیره تاریخچه کامل چندرسانه‌ای، نیاز به ذخیره فایل در Cloud Storage است که پیچیده‌تر است.

        doc_ref = db.collection('user_chats').document(str(user_id))
        doc_ref.set(
            {
                'history': history,
                'last_update': firestore.SERVER_TIMESTAMP
            },
            merge=True)
        logger.debug(f"Chat history saved for user {user_id}.")
    except Exception as e:
        logger.error(f"Error saving chat history for user {user_id}: {e}")


################################################################
# توابع پردازش فایل
################################################################


def process_file_part(file_path, mime_type):
    """ایجاد یک Part برای فایل (تصویر یا PDF)"""
    if 'image' in mime_type:
        try:
            # استفاده از Image.open از PIL
            img = Image.open(file_path)
            # Part.from_image از شیء PIL Image استفاده می‌کند
            return types.Part.from_image(img)
        except Exception as e:
            logger.error(f"Error processing image file {file_path}: {e}")
            return None
    elif 'pdf' in mime_type:
        try:
            # استفاده از fitz (PyMuPDF) برای تبدیل صفحه اول PDF به تصویر
            doc = fitz.open(file_path)
            page = doc.load_page(0)  # پردازش فقط صفحه اول
            # رندر کردن صفحه با رزولوشن مناسب (مثلاً 300dpi)
            pix = page.get_pixmap(matrix=fitz.Matrix(3, 3)) 

            # ذخیره پیکسل‌ها به عنوان یک فایل PNG موقت
            temp_img_path = os.path.join(
                TEMP_DIR, f"temp_pdf_{os.path.basename(file_path)}.png")
            pix.save(temp_img_path)
            doc.close() # بستن داکیومنت بعد از استفاده

            img = Image.open(temp_img_path)
            # Part.from_image از شیء PIL Image استفاده می‌کند
            return types.Part.from_image(img)

        except Exception as e:
            logger.error(f"Error processing PDF file {file_path}: {e}")
            return None
    else:
        # برای انواع فایل‌های دیگر
        return None


################################################################
# توابع پردازش پیام‌های ربات
################################################################


def get_gemini_response(user_id, user_prompt, file_part=None):
    """ارسال درخواست به مدل Gemini."""
    if client is None:
        return "متأسفانه اتصال به سرویس Gemini برقرار نیست. لطفاً GEMINI_API_KEY را بررسی کنید."

    # دریافت یا ایجاد سشن چت
    chat = get_session_history(user_id)
    if chat is None:
        return "خطای داخلی: سشن چت قابل ایجاد نیست."

    chat_sessions[user_id] = chat  # به‌روزرسانی در حافظه

    # ساخت لیست محتوا
    contents = []
    if file_part:
        contents.append(file_part)

    # اضافه کردن پیام متنی کاربر
    contents.append(user_prompt)

    # بررسی کنیم که contents خالی نباشد (اگر file_part و user_prompt هر دو خالی بودند)
    if not contents:
        return "پیام خالی دریافت شد. لطفاً متن یا فایل ارسال کنید."

    try:
        # ارسال محتوا به مدل
        response = chat.send_message(contents)

        # ذخیره تاریخچه جدید
        save_session_history(user_id, chat)

        return response.text
    except APIError as e:
        logger.error(f"Gemini API Error for user {user_id}: {e}")
        return "متأسفانه در اتصال به API گوگل مشکلی رخ داد. لطفاً چند دقیقه دیگر مجدداً امتحان کنید."
    except Exception as e:
        logger.error(
            f"An unexpected error occurred during Gemini call for user {user_id}: {e}"
        )
        return "یک خطای ناشناخته در پردازش درخواست شما رخ داد."


def handle_text(message):
    """پردازش پیام‌های متنی."""
    user_id = message.chat.id
    user_prompt = message.text

    # بررسی آخرین زمان تعامل
    if user_id in last_interaction_time and (
            time.time() - last_interaction_time[user_id]) < 1:
        logger.info(f"Ignoring rapid message from user {user_id}.")
        return

    last_interaction_time[user_id] = time.time()
    logger.info(f"Received text message from user {user_id}: {user_prompt}")

    try:
        bot.send_chat_action(user_id, 'typing')
        response_text = get_gemini_response(user_id, user_prompt)
        bot.send_message(user_id, response_text)
    except Exception as e:
        logger.error(f"Error handling text message: {e}")
        bot.send_message(
            user_id,
            "متأسفانه یک خطای داخلی رخ داد. جزئیات خطا در کنسول ثبت شد. لطفاً دوباره امتحان کنید."
        )


def handle_photo(message):
    """پردازش پیام‌های تصویری."""
    user_id = message.chat.id

    if not message.photo:
        return

    logger.info(f"Received photo message from user {user_id}.")
    temp_file_path = None # تعریف مسیر موقت در بیرون بلوک try

    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        file_extension = os.path.splitext(file_info.file_path)[-1]
        temp_file_path = os.path.join(
            TEMP_DIR, f"{user_id}_{time.time()}{file_extension}")

        with open(temp_file_path, 'wb') as new_file:
            new_file.write(downloaded_file)

        image_part = process_file_part(temp_file_path, 'image/jpeg')
        user_prompt = message.caption if message.caption else "لطفاً این تصویر را تحلیل کن."

        if image_part is None:
            bot.send_message(user_id, "متأسفانه فایل تصویری قابل پردازش نبود.")
            return

        bot.send_chat_action(user_id, 'typing')
        response_text = get_gemini_response(user_id,
                                            user_prompt,
                                            file_part=image_part)
        bot.send_message(user_id, response_text)

    except Exception as e:
        logger.error(f"Error handling photo message: {e}")
        bot.send_message(user_id,
                         "متأسفانه در پردازش تصویر شما مشکلی پیش آمد.")
    finally:
        # حذف فایل موقت در هر صورت
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)


def handle_document(message):
    """پردازش پیام‌های داکیومنت (مانند PDF)."""
    user_id = message.chat.id

    if not message.document:
        return

    mime_type = message.document.mime_type
    logger.info(
        f"Received document message from user {user_id} with MIME type: {mime_type}"
    )

    if 'pdf' not in mime_type:
        bot.send_message(user_id, "من فقط می‌توانم فایل‌های PDF را تحلیل کنم.")
        return

    temp_file_path = None
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        file_extension = os.path.splitext(file_info.file_path)[-1]
        temp_file_path = os.path.join(
            TEMP_DIR, f"{user_id}_{time.time()}{file_extension}")

        with open(temp_file_path, 'wb') as new_file:
            new_file.write(downloaded_file)

        document_part = process_file_part(temp_file_path, mime_type)
        user_prompt = message.caption if message.caption else "لطفاً صفحه اول این داکیومنت را خلاصه کن."

        if document_part is None:
            bot.send_message(user_id, "متأسفانه فایل PDF قابل پردازش نبود.")
            return

        bot.send_chat_action(user_id, 'typing')
        response_text = get_gemini_response(user_id,
                                            user_prompt,
                                            file_part=document_part)
        bot.send_message(user_id, response_text)

    except Exception as e:
        logger.error(f"Error handling document message: {e}")
        bot.send_message(
            user_id, "متأسفانه در پردازش داکیومنت (PDF) شما مشکلی پیش آمد.")
    finally:
        # حذف فایل موقت در هر صورت
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)


################################################################
# هندلرهای تله‌بات
################################################################
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    """پاسخ به دستورات /start و /help."""
    welcome_message = (
        "سلام! من ربات دستیار شما هستم که از مدل **Google Gemini** استفاده می‌کنم.\n\n"
        "شما می‌توانید:\n"
        "1. **سوالات متنی** خود را بپرسید.\n"
        "2. **تصویر** (Photo) بفرستید تا تحلیلش کنم.\n"
        "3. **فایل PDF** بفرستید تا صفحه اول آن را خلاصه کنم.\n\n"
        "هر موقع خواستید یک مکالمه جدید را شروع کنید، از دستور /reset استفاده کنید."
    )
    bot.reply_to(message, welcome_message)


@bot.message_handler(commands=['reset'])
def reset_session(message):
    """ریست کردن سشن چت."""
    user_id = message.chat.id
    if user_id in chat_sessions:
        del chat_sessions[user_id]

    if db:
        try:
            db.collection('user_chats').document(str(user_id)).delete()
            logger.info(
                f"Chat history for user {user_id} deleted from Firestore.")
        except Exception as e:
            logger.warning(
                f"Could not delete chat history from Firestore: {e}")

    bot.reply_to(message,
                 "تاریخچه مکالمه شما پاک شد. یک مکالمه جدید را شروع کنید.")


# هندلر برای پیام‌های متنی
@bot.message_handler(content_types=['text'])
def handle_text_messages(message):
    """هندلر اصلی برای متن."""
    handle_text(message)


# هندلر برای پیام‌های تصویری
@bot.message_handler(content_types=['photo'])
def handle_photo_messages(message):
    """هندلر اصلی برای عکس."""
    handle_photo(message)


# هندلر برای داکیومنت‌ها (مانند PDF)
@bot.message_handler(content_types=['document'])
def handle_document_messages(message):
    """هندلر اصلی برای داکیومنت."""
    handle_document(message)


# هندلر برای انواع محتوای دیگر
@bot.message_handler(func=lambda message: True,
                     content_types=[
                         'audio', 'video', 'voice', 'sticker', 'location',
                         'contact', 'venue', 'dice', 'poll'
                     ])
def default_handler(message):
    """پاسخ به محتواهای غیرپشتیبانی‌شده."""
    bot.reply_to(
        message,
        "متأسفانه من فقط می‌توانم پیام‌های متنی، تصاویر و فایل‌های PDF را پردازش کنم."
    )


################################################################
# توابع اصلی اجرای برنامه
################################################################


def run_telegram_bot():
    """شروع پولینگ (Polling) تله‌بات."""
    try:
        logger.info("Bot started successfully. Polling for updates...")
        # از infinity_polling به جای polling معمولی استفاده کنید تا بهتر کار کند.
        bot.infinity_polling(timeout=30, long_polling_timeout=5)
    except Exception as e:
        logger.error(f"Critical error during bot polling: {e}")
        # اگر خطا جدی بود، برنامه باید از نو شروع شود.
        # در Replit، این کار باعث Restart شدن می‌شود.


# برای اینکه ربات در Replit همیشه فعال بماند، به یک سرور Flask نیاز داریم.
app = Flask(__name__)


@app.route('/')
def home():
    """مسیر اصلی برای چک کردن وضعیت."""
    return "Study Bot is running and kept awake by uptime monitor."


def run_server():
    """شروع سرور Flask روی پورت 8080 برای فعال نگه داشتن ربات در Replit."""
    # ** اصلاح پورت: در محیط Replit، سرور حتماً باید روی 8080 اجرا شود، نه 5000. **
    app.run(host='0.0.0.0', port=8080)
    logger.info("Flask server is running on port 8080.")


if __name__ == '__main__':
    # این کد برای اجرای همزمان بات تله‌گرام و سرور Flask ضروری است

    # 1. شروع سرور Flask در یک نخ (Background Thread)
    # سرور HTTP را در یک نخ مجزا اجرا می‌کنیم
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True  # اجازه می‌دهد در صورت اتمام نخ اصلی، این نخ هم بسته شود
    server_thread.start()

    # 2. شروع بات تله‌گرام در نخ اصلی (Main Thread)
    # از آنجایی که Polling یک حلقه بی‌پایان است، آن را در نخ اصلی اجرا می‌کنیم.
    run_telegram_bot()
