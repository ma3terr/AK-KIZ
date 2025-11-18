import os
import logging
import time
from io import BytesIO
from PIL import Image
import fitz # PyMuPDF
import json # New import for handling JSON from Telegram

import telebot
from google import genai
from google.genai import types
from google.genai.errors import APIError

from flask import Flask, request, abort

import firebase_admin
from firebase_admin import credentials, firestore

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------- Environment / Config ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
API_KEY_FILE = os.environ.get("API_KEY_FILE") # optional firebase credentials file path
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE") # e.g. "https://my-app.up.railway.app"
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "6082991135")
TEMP_DIR = "temp"
MODEL_NAME = os.environ.get("MODEL_NAME", "gemini-2.5-flash-preview-09-2025")

if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set. Exiting.")
    raise SystemExit("BOT_TOKEN env var required")

if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY is not set. Gemini calls will fail.")

# ---------- Create temp dir ----------
os.makedirs(TEMP_DIR, exist_ok=True)

# ---------- Firebase init (optional) ----------
db = None
try:
    if API_KEY_FILE:
        # Assuming API_KEY_FILE points to a service account JSON path
        cred = credentials.Certificate(API_KEY_FILE)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("Initialized Firebase Admin with credentials file.")
    else:
        # Fallback to default credentials (e.g., if running on Google Cloud)
        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        db = firestore.client()
        logger.info("Initialized Firebase Admin (default).")
except Exception as e:
    logger.warning(f"Firebase init failed or not provided: {e}")
    db = None

# ---------- Gemini client ----------
client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("Gemini client initialized.")
    except Exception as e:
        logger.error(f"Failed to init Gemini client: {e}")
        client = None

# ---------- Telebot ----------
# The path must be unique for security, we use the token itself.
WEBHOOK_URL_PATH = f"/{BOT_TOKEN}" 
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='MARKDOWN')
logger.info("TeleBot instance created.")

# in-memory session fallback (Used if Firestore fails)
chat_sessions = {}
last_interaction_time = {}

# ---------- Helpers: Firebase session storage (Your original functions) ----------
def get_session_history(user_id):
    if client is None:
        logger.warning("Gemini client not initialized.")
        return None

    if db:
        try:
            doc = db.collection('user_chats').document(str(user_id)).get()
            if doc.exists:
                data = doc.to_dict()
                history_data = data.get('history', [])
                contents = []
                for item in history_data:
                    contents.append(types.Content(role=item['role'],
                                                  parts=[types.Part.from_text(item['text'])]))
                return client.chats.create(model=MODEL_NAME, history=contents)
        except Exception as e:
            logger.warning(f"Error loading session from Firestore: {e}")

    # Fallback to in-memory/new session creation
    if user_id in chat_sessions:
        return chat_sessions[user_id]
    try:
        return client.chats.create(model=MODEL_NAME) if client else None
    except Exception as e:
        logger.error(f"Failed creating new Gemini chat session: {e}")
        return None

def save_session_history(user_id, chat):
    if not db:
        chat_sessions[user_id] = chat # Save to in-memory if no Firestore
        return
    try:
        history = []
        for message in getattr(chat, "history", []):
            # Only save text parts for simplicity
            if len(message.parts) == 1 and getattr(message.parts[0], "text", None):
                history.append({'role': message.role, 'text': message.parts[0].text})
        db.collection('user_chats').document(str(user_id)).set({
            'history': history,
            'last_update': firestore.SERVER_TIMESTAMP
        }, merge=True)
    except Exception as e:
        logger.warning(f"Failed saving session to Firestore: {e}")

# ---------- File processing (Your original function) ----------
def process_file_part(file_path, mime_type):
    if 'image' in mime_type:
        try:
            img = Image.open(file_path)
            # You might need to resize if the image is too large for the API
            return types.Part.from_image(img)
        except Exception as e:
            logger.error(f"process image error: {e}")
            return None
    if 'pdf' in mime_type:
        try:
            # Extract the first page as an image (as designed by you)
            doc = fitz.open(file_path)
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            temp_img = os.path.join(TEMP_DIR, f"pdf_preview_{os.path.basename(file_path)}.png")
            pix.save(temp_img)
            doc.close()
            img = Image.open(temp_img)
            return types.Part.from_image(img)
        except Exception as e:
            logger.error(f"process pdf error: {e}")
            return None
    return None

# ---------- Gemini interaction (Your original function) ----------
def get_gemini_response(user_id, user_prompt, file_part=None):
    if client is None:
        return "اتصال به Gemini برقرار نیست. GEMINI_API_KEY را بررسی کنید."

    chat = get_session_history(user_id)
    if chat is None:
        return "خطا در ایجاد سشن چت."

    # This is handled inside get_session_history now
    # chat_sessions[user_id] = chat 

    contents = []
    if file_part:
        contents.append(file_part)
    if user_prompt:
        contents.append(user_prompt)

    if not contents:
        return "هیچ ورودی‌ای ارسال نشده."

    try:
        response = chat.send_message(contents)
        save_session_history(user_id, chat)
        return getattr(response, "text", str(response))
    except APIError as e:
        logger.error(f"Gemini APIError: {e}")
        return "خطای API گوگل: لطفاً بعداً تلاش کنید."
    except Exception as e:
        logger.error(f"Gemini unexpected error: {e}")
        return "خطای داخلی در پردازش درخواست."

# ---------- Telebot Handlers (برنامه‌های پاسخ‌دهنده به پیام‌ها) ----------

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, 
                 "سلام! من یک ربات هوش مصنوعی هستم که با Gemini کار می‌کنم. \n\n"
                 "شما می‌توانید هر سوالی بپرسید یا عکس و فایل PDF بفرستید تا آن‌ها را تحلیل کنم.\n\n"
                 "برای شروع کافیست یک پیام بفرستید!")

@bot.message_handler(content_types=['text'])
def handle_text(message):
    logger.info(f"Received text from {message.chat.id}")
    user_id = message.chat.id
    try:
        bot.send_chat_action(user_id, 'typing')
        response_text = get_gemini_response(user_id, message.text)
        bot.send_message(user_id, response_text)
    except Exception as e:
        logger.error(f"Error handling text message: {e}")
        bot.send_message(user_id, "متأسفانه در پردازش پیام خطایی رخ داد.")

@bot.message_handler(content_types=['photo', 'document'])
def handle_multimedia(message):
    user_id = message.chat.id
    
    # 1. Determine file type and get file ID
    if message.content_type == 'photo':
        file_id = message.photo[-1].file_id # Get the highest resolution photo
        mime_type = 'image/jpeg' 
        caption = message.caption
    elif message.content_type == 'document':
        file_id = message.document.file_id
        mime_type = message.document.mime_type or 'application/octet-stream'
        caption = message.caption
    else:
        return

    logger.info(f"Received file (ID: {file_id}, Type: {mime_type}) from {user_id}")

    # Check for unsupported file types
    if 'image' not in mime_type and 'pdf' not in mime_type:
        bot.reply_to(message, "فقط فایل‌های تصویری (JPEG, PNG) و PDF پشتیبانی می‌شوند.")
        return

    # 2. Download the file
    file_info = bot.get_file(file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    temp_file_path = os.path.join(TEMP_DIR, f"{file_id}.{mime_type.split('/')[-1]}")
    
    try:
        with open(temp_file_path, 'wb') as new_file:
            new_file.write(downloaded_file)

        # 3. Process the file (convert PDF to image or load image)
        file_part = process_file_part(temp_file_path, mime_type)
        if file_part is None:
             bot.reply_to(message, "خطا در پردازش فایل: فایل قابل خواندن نیست یا فرمت پشتیبانی نمی‌شود.")
             return

        # 4. Get response from Gemini
        bot.send_chat_action(user_id, 'typing')
        response_text = get_gemini_response(user_id, caption or "این فایل/عکس را تحلیل کن.", file_part)
        bot.send_message(user_id, response_text)

    except Exception as e:
        logger.error(f"Error handling multimedia message: {e}")
        bot.send_message(user_id, "متأسفانه در پردازش فایل خطایی رخ داد.")
    finally:
        # 5. Cleanup temp file
        if os.path.exists(temp_file_path):
             os.remove(temp_file_path)


# ---------- Flask App & Webhook Setup (بخش جدید برای راه‌اندازی سرور) ----------
app = Flask(__name__)

@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '!', 200
    else:
        abort(403)

@app.route('/')
def index():
    # This route is mainly for checking if the app is alive
    return "Telegram Bot is running and awaiting webhook calls.", 200

# Function to set up the webhook
def setup_webhook():
    if not WEBHOOK_BASE:
        logger.error("WEBHOOK_BASE is not set. Cannot set webhook.")
        return

    webhook_url = WEBHOOK_BASE + WEBHOOK_URL_PATH
    logger.info(f"Attempting to set webhook to: {webhook_url}")
    
    try:
        bot.remove_webhook() # Remove any previous webhook
        time.sleep(1) # Wait a second
        if bot.set_webhook(url=webhook_url):
            logger.info(f"Webhook set successfully to {webhook_url}")
        else:
            logger.error("Failed to set webhook (bot.set_webhook returned False)")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        
if __name__ == '__main__':
    # 1. Setup Webhook (this needs to be done once)
    setup_webhook()
    
    # 2. Start the Flask server
    # Railway environment sets the PORT environment variable
    port = int(os.environ.get('PORT', 5000)) 
    logger.info(f"Starting Flask server on port {port}...")
    # host='0.0.0.0' is required for Railway to expose the port correctly
    app.run(host='0.0.0.0', port=port)
