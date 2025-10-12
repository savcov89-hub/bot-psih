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
    # ... (код log_event без изменений)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    timestamp = datetime.utcnow()
    cursor.execute(
        "INSERT INTO analytics (user_id, event_type, timestamp) VALUES (?, ?, ?)",
        (user_id, event_type, timestamp)
    )
    conn.commit()
    conn.close()

# --- НОВЫЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ АНАЛИТИКИ ---
def get_stats_for_period(date_filter: str):
    """Получает статистику за указанный период."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Уникальные пользователи, нажавшие /start
    cursor.execute(f"SELECT COUNT(DISTINCT user_id) FROM analytics WHERE event_type = 'start_command' {date_filter.replace('WHERE', 'AND') if date_filter else ''}")
    start_users = cursor.fetchone()[0]

    # Всего уникальных пользователей
    cursor.execute(f"SELECT COUNT(DISTINCT user_id) FROM analytics {date_filter}")
    total_users = cursor.fetchone()[0]
    
    # Активные пользователи (> 5 сообщений)
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
    return {"start": start_users, "total": total_users, "active": active_users}

def format_change(current, previous):
    """Форматирует процентное изменение между двумя числами."""
    if previous == 0:
        return ""  # Не показывать изменение, если предыдущее значение было ноль
    
    change = ((current - previous) / previous) * 100
    
    if change > 0:
        return f" `( +{change:.0f}% 📈 )`"
    elif change < 0:
        return f" `( {change:.0f}% 📉 )`"
    else:
        return " `( 0% )`"

# --- Состояния (FSM) ---
class UserState(StatesGroup):
    choosing_mode = State()
    in_session = State()

# --- Клавиатуры ---
agree_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Я понимаю и согласен", callback_data="agree_pressed")]])
mode_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Психология (КПТ)", callback_data="mode_cbt")], [InlineKeyboardButton(text="Коучинг", callback_data="mode_coach")]])

# ОБНОВЛЕННАЯ КЛАВИАТУРА ДЛЯ СТАТИСТИКИ
stats_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Сегодня", callback_data="stats_today"), InlineKeyboardButton(text="Вчера", callback_data="stats_yesterday")],
    [InlineKeyboardButton(text="7 дней", callback_data="stats_7d"), InlineKeyboardButton(text="30 дней", callback_data="stats_30d")],
    [InlineKeyboardButton(text="Сравнить 7 дней", callback_data="stats_compare7d")],
    [InlineKeyboardButton(text="Сравнить 30 дней", callback_data="stats_compare30d")],
    [InlineKeyboardButton(text="За всё время", callback_data="stats_all")]
])

# КЛАВИАТУРА С КНОПКОЙ "НАЗАД"
back_to_stats_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⬅️ Назад к выбору периода", callback_data="stats_back")]
])

# --- Обработчики (Handlers) ---
# ... (код для /start, /stop, handle_agree, handle_mode_choice и handle_session_message без изменений) ...

@dp.message(Command("stats"), StateFilter("*"))
async def stats_command(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        await message.answer("У вас нет доступа к этой команде.")
        return
    await message.answer("📊 Выберите период для отображения статистики:", reply_markup=stats_keyboard)

# НОВЫЙ ХЕНДЛЕР ДЛЯ КНОПКИ "НАЗАД"
@dp.callback_query(F.data == "stats_back")
async def handle_stats_back(callback_query: types.CallbackQuery):
    if str(callback_query.from_user.id) != ADMIN_ID:
        await callback_query.answer("У вас нет доступа к этой команде.", show_alert=True)
        return
    await callback_query.message.edit_text(
        "📊 Выберите период для отображения статистики:",
        reply_markup=stats_keyboard
    )
    await callback_query.answer()

# ОБНОВЛЕННЫЙ ХЕНДЛЕР ДЛЯ ОБРАБОТКИ ПЕРИОДОВ
@dp.callback_query(F.data.startswith("stats_"))
async def handle_stats_period(callback_query: types.CallbackQuery):
    if str(callback_query.from_user.id) != ADMIN_ID:
        await callback_query.answer("У вас нет доступа к этой команде.", show_alert=True)
        return

    period = callback_query.data.split("_")[1]
    stats_text = ""

    # Обработка обычных периодов
    if period in ["today", "yesterday", "7d", "30d", "all"]:
        date_filter_map = {
            "today": "WHERE DATE(timestamp) = DATE('now', 'utc')",
            "yesterday": "WHERE DATE(timestamp) = DATE('now', '-1 day', 'utc')",
            "7d": "WHERE DATE(timestamp) >= DATE('now', '-7 days', 'utc')",
            "30d": "WHERE DATE(timestamp) >= DATE('now', '-30 days', 'utc')",
            "all": ""
        }
        period_text_map = {
            "today": "за сегодня", "yesterday": "за вчера", "7d": "за последние 7 дней",
            "30d": "за последние 30 дней", "all": "за всё время"
        }
        
        stats = get_stats_for_period(date_filter_map[period])
        stats_text = (
            f"📊 **Статистика бота {period_text_map[period]}**\n\n"
            f"▫️ **Нажали /start:** {stats['start']} чел.\n"
            f"▫️ **Всего уникальных:** {stats['total']} чел.\n"
            f"▫️ **Активные (> 5):** {stats['active']} чел."
        )
    
    # Обработка сравнения периодов
    elif period in ["compare7d", "compare30d"]:
        days = 7 if period == "compare7d" else 30
        
        current_filter = f"WHERE DATE(timestamp) >= DATE('now', '-{days} days', 'utc')"
        current_stats = get_stats_for_period(current_filter)

        previous_filter = f"WHERE DATE(timestamp) >= DATE('now', '-{days*2} days', 'utc') AND DATE(timestamp) < DATE('now', '-{days} days', 'utc')"
        previous_stats = get_stats_for_period(previous_filter)
        
        stats_text = (
            f"📊 **Сравнение статистики за {days} дней**\n"
            f"_(Последние {days} vs. Предыдущие {days})_\n\n"
            f"▫️ **Нажали /start:** {current_stats['start']}{format_change(current_stats['start'], previous_stats['start'])}\n"
            f"▫️ **Всего уникальных:** {current_stats['total']}{format_change(current_stats['total'], previous_stats['total'])}\n"
            f"▫️ **Активные (> 5):** {current_stats['active']}{format_change(current_stats['active'], previous_stats['active'])}"
        )
    
    if stats_text:
        await callback_query.message.edit_text(stats_text, parse_mode="Markdown", reply_markup=back_to_stats_keyboard)

    await callback_query.answer()

# ... (остальные ваши хендлеры и функции запуска без изменений) ...

if __name__ == "__main__":
    init_db()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main()