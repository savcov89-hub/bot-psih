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

# КЛЮЧИ ЮKASSA ВРЕМЕННО НЕ НУЖНЫ
# YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
# YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8000))

if not all([TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, ADMIN_ID]):
    raise ValueError("Необходимо задать TELEGRAM_BOT_TOKEN, OPENAI_API_KEY и ADMIN_ID")

# Инициализация
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# ... (Ваши системные промпты CBT_PROMPT, COACH_PROMPT, PLAN_GENERATION_PROMPT) ...

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
DB_FILE = "bot_data.db"

def init_db():
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            subscription_status TEXT DEFAULT 'free',
            subscription_expires_at DATETIME
        )
    ''')
    conn.commit()
    conn.close()

# ... (код log_event, ensure_user_exists, get_stats_for_period, format_change и др. вспомогательные функции) ...

# --- Состояния (FSM) ---
class UserJourney(StatesGroup):
    survey_q1 = State()
    survey_q2 = State()
    plan_confirmation = State()
    in_session = State()

# --- Клавиатуры ---
agree_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Я понимаю и согласен", callback_data="agree_pressed")]])
plan_confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Готов(а) начать", callback_data="plan_accept")]
])

# ... (остальные клавиатуры) ...

# --- Обработчики (Handlers) ---
@dp.message(CommandStart())
async def send_welcome(message: Message, state: FSMContext):
    ensure_user_exists(message.from_user.id)
    log_event(message.from_user.id, 'start_command')
    await state.clear()
    welcome_text = (
        # ... ваш текст приветствия ...
    )
    await message.answer(welcome_text, reply_markup=agree_keyboard, parse_mode="Markdown")

# ... (код для /stop, /stats, handle_agree, start_survey, process_survey_q1, process_survey_q2_and_generate_plan) ...

# ОБНОВЛЕННЫЙ ХЕНДЛЕР ДЛЯ ПРЕДЛОЖЕНИЯ ОПЛАТЫ (ЗАГЛУШКА)
@dp.callback_query(F.data == "plan_accept", UserJourney.plan_confirmation)
async def offer_payment_dummy(callback_query: types.CallbackQuery, state: FSMContext):
    PRICE = 250.00
    
    # ВРЕМЕННАЯ ССЫЛКА-ЗАГЛУШКА
    # Ведет на тестовый магазин ЮKassa, чтобы ссылка была релевантной
    payment_url = "https://yookassa.ru/demo" 
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Оплатить 250 ₽", url=payment_url)]])
    
    await callback_query.message.edit_text(
        "Отлично! Доступ к сессиям предоставляется по подписке.\n\n"
        "**Тариф:**\n"
        "▫️ **250 рублей** за 7 дней доступа ко всем функциям бота.\n\n"
        "Нажмите кнопку ниже, чтобы перейти к оплате.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback_query.answer()
    await state.clear()

# ВЕБХУК ДЛЯ ЮKASSA ВРЕМЕННО НЕ НУЖЕН
# async def yookassa_webhook_handler(request):
#     ...

# --- Функции для запуска и остановки ---
async def on_startup(bot: Bot) -> None:
    # ... (код on_startup)

def main() -> None:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path="/webhook")
    
    # ВРЕМЕННО ОТКЛЮЧАЕМ ВЕБХУК ЮKASSA
    # app.router.add_post("/yookassa_webhook", yookassa_webhook_handler)

    setup_application(app, dp, bot=bot)
    
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)

if __name__ == "__main__":
    init_db()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main()

# ВАЖНО: я убрал из этого кода все функции, которые вам не нужны на этапе верификации, 
# чтобы не усложнять. Полную версию кода с аналитикой и подписками мы вернем после того, 
# как вы получите ключи.