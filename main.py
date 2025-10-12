import asyncio
import os
import logging
import sys
import sqlite3
from datetime import datetime

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, StateFilter
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

# --- РАБОТА С БАЗОЙ ДАННЫХ АНАЛИТИКИ ---
DB_FILE = "analytics.db"

def init_db():
    # ... (код init_db без изменений)

def log_event(user_id: int, event_type: str):
    # ... (код log_event без изменений)

# --- Состояния (FSM) ---
class UserState(StatesGroup):
    choosing_mode = State()
    in_session = State()

# --- Клавиатуры ---
agree_button = InlineKeyboardButton(text="Я понимаю и согласен", callback_data="agree_pressed")
agree_keyboard = InlineKeyboardMarkup(inline_keyboard=[[agree_button]])

mode_cbt_button = InlineKeyboardButton(text="Психология (КПТ)", callback_data="mode_cbt")
mode_coach_button = InlineKeyboardButton(text="Коучинг", callback_data="mode_coach")
mode_keyboard = InlineKeyboardMarkup(inline_keyboard=[[mode_cbt_button], [mode_coach_button]])

# НОВАЯ КЛАВИАТУРА ДЛЯ ВЫБОРА ПЕРИОДА СТАТИСТИКИ
stats_today_button = InlineKeyboardButton(text="Сегодня", callback_data="stats_today")
stats_yesterday_button = InlineKeyboardButton(text="Вчера", callback_data="stats_yesterday")
stats_7d_button = InlineKeyboardButton(text="7 дней", callback_data="stats_7d")
stats_30d_button = InlineKeyboardButton(text="30 дней", callback_data="stats_30d")
stats_all_button = InlineKeyboardButton(text="За всё время", callback_data="stats_all")
stats_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [stats_today_button, stats_yesterday_button],
    [stats_7d_button, stats_30d_button],
    [stats_all_button]
])

# --- Обработчики (Handlers) ---
# ... (код для /start, /stop, handle_agree, handle_mode_choice и handle_session_message без изменений) ...

# ОБНОВЛЕННЫЙ ХЕНДЛЕР /stats: теперь он отправляет меню
@dp.message(Command("stats"), StateFilter("*"))
async def stats_command(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        await message.answer("У вас нет доступа к этой команде.")
        return
    
    await message.answer("📊 Выберите период для отображения статистики:", reply_markup=stats_keyboard)

# НОВЫЙ ХЕНДЛЕР: ловит нажатия на кнопки статистики
@dp.callback_query(F.data.startswith("stats_"))
async def handle_stats_period(callback_query: types.CallbackQuery):
    period = callback_query.data.split("_")[1]
    
    date_filter = ""
    period_text = ""
    
    if period == "today":
        date_filter = "WHERE DATE(timestamp) = DATE('now', 'utc')"
        period_text = "за сегодня"
    elif period == "yesterday":
        date_filter = "WHERE DATE(timestamp) = DATE('now', '-1 day', 'utc')"
        period_text = "за вчера"
    elif period == "7d":
        date_filter = "WHERE DATE(timestamp) >= DATE('now', '-7 days', 'utc')"
        period_text = "за последние 7 дней"
    elif period == "30d":
        date_filter = "WHERE DATE(timestamp) >= DATE('now', '-30 days', 'utc')"
        period_text = "за последние 30 дней"
    elif period == "all":
        date_filter = ""
        period_text = "за всё время"

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # 1. Считаем уникальных пользователей, нажавших /start
    cursor.execute(f"SELECT COUNT(DISTINCT user_id) FROM analytics WHERE event_type = 'start_command' {date_filter.replace('WHERE', 'AND') if date_filter else ''}")
    start_users = cursor.fetchone()[0]

    # 2. Считаем общее количество уникальных пользователей
    cursor.execute(f"SELECT COUNT(DISTINCT user_id) FROM analytics {date_filter}")
    total_users = cursor.fetchone()[0]
    
    # 3. Считаем активных пользователей
    # Важно: фильтр по дате нужно применять до группировки
    cursor.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT user_id FROM analytics 
            {date_filter} {'AND' if date_filter else 'WHERE'} event_type = 'message_sent' 
            GROUP BY user_id 
            HAVING COUNT(*) > 5
        )
    """)
    active_users = cursor.fetchone()[0]
    
    conn.close()

    stats_text = (
        f"📊 **Статистика бота {period_text}**\n\n"
        f"▫️ **Нажали /start:** {start_users} чел.\n"
        f"▫️ **Всего уникальных пользователей:** {total_users} чел.\n"
        f"▫️ **Активные (более 5 сообщений):** {active_users} чел."
    )
    # Редактируем сообщение с кнопками, заменяя его на отчет
    await callback_query.message.edit_text(stats_text, parse_mode="Markdown")
    await callback_query.answer()

# ... (остальные ваши хендлеры и функции запуска без изменений) ...

if __name__ == "__main__":
    init_db()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main()