# main.py
import os
import logging
import time
from io import BytesIO
from PIL import Image
import fitz  # PyMuPDF

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
API_KEY_FILE = os.environ.get("API_KEY_FILE")  # optional firebase credentials file path
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE")  # e.g. "https://my-app.up.railway.app"
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
        cred = credentials.Certificate(API_KEY_FILE)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("Initialized Firebase Admin with credentials file.")
    else:
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
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='MARKDOWN')
logger.info("TeleBot instance created.")

# in-memory session fallback
chat_sessions = {}
last_interaction_time = {}

# ---------- Helpers: Firebase session storage ----------
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

    if user_id in chat_sessions:
        return chat_sessions[user_id]
    try:
        return client.chats.create(model=MODEL_NAME) if client else None
    except Exception as e:
        logger.error(f"Failed creating new Gemini chat session: {e}")
        return None

def save_session_history(user_id, chat):
    if not db:
        return
    try:
        history = []
        for message in getattr(chat, "history", []):
            if len(message.parts) == 1 and getattr(message.parts[0], "text", None):
                history.append({'role': message.role, 'text': message.parts[0].text})
        db.collection('user_chats').document(str(user_id)).set({
            'history': history,
            'last_update': firestore.SERVER_TIMESTAMP
        }, merge=True)
    except Exception as e:
        logger.warning(f"Failed saving session to Firestore: {e}")

# ---------- File processing ----------
def process_file_part(file_path, mime_type):
    if 'image' in mime_type:
        try:
            img = Image.open(file_path)
            return types.Part.from_image(img)
        except Exception as e:
            logger.error(f"process image error: {e}")
            return None
    if 'pdf' in mime_type:
        try:
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

# ---------- Gemini interaction ----------
def get_gemini_response(user_id, user_prompt, file_part=None):
    if client is None:
        return "اتصال به Gemini برقرار نیست. GEMINI_API_KEY را بررسی کنید."

    chat = get_session_history(user_id)
    if chat is None:
        return "خطا در ایجاد سشن چت."

    chat_sessions[user_id] = chat

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

#
