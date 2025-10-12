import asyncio
import os
import logging
import sys
import sqlite3 # Добавляем импорт для работы с базой
from datetime import datetime # Добавляем импорт для работы с датой и временем

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from openai import AsyncOpenAI

# Загружаем переменные окружения
load_dotenv()

# --- Конфигурация ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL") 
# ВАЖНО: Укажите ваш Telegram ID, чтобы только вы могли видеть статистику
ADMIN_ID = os.getenv("ADMIN_ID") 

WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8000))

# Проверяем наличие токенов
if not TELEGRAM_BOT_TOKEN or not OPENAI_API_KEY or not ADMIN_ID:
    raise ValueError("Необходимо задать TELEGRAM_BOT_TOKEN, OPENAI_API_KEY и ADMIN_ID")

# Инициализация
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# --- Системные промпты ---
CBT_PROMPT = "..." # Ваш промпт для КПТ
COACH_PROMPT = "..." # Ваш промпт для Коуча

# --- НОВЫЙ БЛОК: РАБОТА С БАЗОЙ ДАННЫХ АНАЛИТИКИ ---

DB_FILE = "analytics.db"

def init_db():
    """Инициализирует базу данных и создает таблицу, если ее нет."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            timestamp DATETIME NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def log_event(user_id: int, event_type: str):
    """Записывает событие в базу данных."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    timestamp = datetime.utcnow()
    cursor.execute(
        "INSERT INTO analytics (user_id, event_type, timestamp) VALUES (?, ?, ?)",
        (user_id, event_type, timestamp)
    )
    conn.commit()
    conn.close()

# --- Состояния и Клавиатуры (остаются без изменений) ---
class UserState(StatesGroup):
    # ... (код состояний)

# ... (код клавиатур)

# --- Обработчики (Handlers) ---
@dp.message(CommandStart())
async def send_welcome(message: Message, state: FSMContext):
    # ЛОГИРУЕМ СОБЫТИЕ: Пользователь нажал /start
    log_event(message.from_user.id, 'start_command')
    
    await state.clear()
    # ... (ваш код приветствия)

# ... (остальные ваши хендлеры: stop_session, handle_agree, handle_mode_choice) ...

# Универсальный обработчик сообщений в сессии
@dp.message(F.text, UserState.in_session)
async def handle_session_message(message: Message, state: FSMContext):
    # ЛОГИРУЕМ СОБЫТИЕ: Пользователь отправил сообщение
    log_event(message.from_user.id, 'message_sent')
    
    # ... (ваш остальной код обработки сообщения)

# --- НОВЫЙ ХЕНДЛЕР: КОМАНДА ДЛЯ ПОЛУЧЕНИЯ СТАТИСТИКИ ---
@dp.message(Command("stats"))
async def get_stats(message: Message):
    # Проверяем, что команду вызвал админ
    if str(message.from_user.id) != ADMIN_ID:
        await message.answer("У вас нет доступа к этой команде.")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # 1. Считаем уникальных пользователей, нажавших /start
    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM analytics WHERE event_type = 'start_command'")
    start_users = cursor.fetchone()[0]

    # 2. Считаем общее количество уникальных пользователей
    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM analytics")
    total_users = cursor.fetchone()[0]

    # 3. Считаем пользователей, отправивших более 5 сообщений
    cursor.execute("""
        SELECT COUNT(*) FROM (
            SELECT user_id FROM analytics 
            WHERE event_type = 'message_sent' 
            GROUP BY user_id 
            HAVING COUNT(*) > 5
        )
    """)
    active_users = cursor.fetchone()[0]

    conn.close()

    stats_text = (
        "📊 **Статистика бота**\n\n"
        f"▫️ **Нажали /start:** {start_users} чел.\n"
        f"▫️ **Всего уникальных пользователей:** {total_users} чел.\n"
        f"▫️ **Активные (более 5 сообщений):** {active_users} чел."
    )
    await message.answer(stats_text, parse_mode="Markdown")

# ... (остальные ваши хендлеры и функции запуска) ...

if __name__ == "__main__":
    init_db() # Инициализируем базу данных при старте
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main()