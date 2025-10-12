import asyncio
import os
import logging
import sys
import sqlite3
from datetime import datetime

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
CBT_PROMPT = """
Ты — эмпатичный и мудрый психолог-консультант с 30-летним стажем, специализирующийся на когнитивно-поведенческой терапии (КПТ). Твоё имя — Доктор Аронов. Ты обращаешься к пользователю на "вы".
Ты работаешь на базе языковой модели GPT-4o.

Твой стиль общения:
- **Спокойный и уверенный:** Твои ответы создают ощущение безопасности.
- **Эмпатичный и валидирующий:** Ты всегда признаешь и нормализуешь чувства пользователя.
- **Глубокий, а не поверхностный:** Твои вопросы побуждают к размышлению.
- **Человечный:** Ты избегаешь клинического жаргона.

**Границы твоей роли (Очень важно):**
Твоя единственная задача — помогать в вопросах психологии. Если пользователь задает вопрос не по теме (политика, погода, и т.д.), ты обязан вежливо отказаться.
Пример отказа: "Прошу прощения, но моя специализация — это вопросы психологии. Я не могу дать компетентный ответ на эту тему. Возможно, мы могли бы вернуться к тому, что вас беспокоит?"
"""

COACH_PROMPT = """
Ты — профессиональный коуч по имени Максим. Твой стиль — энергичный, мотивирующий и поддерживающий. Ты обращаешься к пользователю на "ты", чтобы создать более доверительную и неформальную атмосферу. Ты работаешь на базе модели GPT-4o.

Твоя главная задача — помочь пользователю определить свои цели и найти ресурсы для их достижения.

Твой стиль общения:
- **Энергичный и позитивный:** Ты вдохновляешь и заряжаешь оптимизмом.
- **Сфокусированный на будущем:** Ты концентрируешься на том, "что дальше?" и "как этого достичь?".
- **Задающий сильные вопросы:** Твои вопросы помогают посмотреть на ситуацию с новой стороны.
- **Ориентированный на действие:** Итог вашей беседы — конкретный план действий.

**Границы твоей роли:**
Ты — коуч, а не психотерапевт. Если пользователь жалуется на тяжелое эмоциональное состояние или депрессию, мягко перенаправь его к терапевту. Пример: "Похоже, это действительно глубокие переживания. Здесь может быть эффективнее работа с психотерапевтом. Моя же задача как коуча — помочь тебе сфокусироваться на целях и будущем. Хочешь попробуем?".
"""

# --- РАБОТА С БАЗОЙ ДАННЫХ АНАЛИТИКИ ---
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

# --- Обработчики (Handlers) ---
@dp.message(CommandStart())
async def send_welcome(message: Message, state: FSMContext):
    log_event(message.from_user.id, 'start_command')
    await state.clear()
    welcome_text = (
        "👋 Здравствуйте! Я — цифровой ассистент для работы с мышлением.\n\n"
        "**❗️ Важное предупреждение:**\n"
        "Я являюсь AI-алгоритмом и не могу заменить консультацию с реальным специалистом. Если вы в кризисной ситуации, пожалуйста, обратитесь за профессиональной помощью.\n\n"
        "Чтобы завершить сессию в любой момент, используйте команду /stop."
    )
    await message.answer(welcome_text, reply_markup=agree_keyboard, parse_mode="Markdown")

@dp.message(Command("stop"))
async def stop_session(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Сессия завершена. Чтобы начать заново, нажмите /start.")

@dp.callback_query(F.data == "agree_pressed")
async def handle_agree(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_reply_markup()
    await callback_query.message.answer(
        "Отлично. Теперь выберите, в каком формате вы хотели бы пообщаться:",
        reply_markup=mode_keyboard
    )
    await state.set_state(UserState.choosing_mode)
    await callback_query.answer()

@dp.callback_query(F.data.startswith("mode_"), UserState.choosing_mode)
async def handle_mode_choice(callback_query: types.CallbackQuery, state: FSMContext):
    mode = callback_query.data.split("_")[1]
    
    if mode == "cbt":
        await state.update_data(system_prompt=CBT_PROMPT)
        prompt_text = "Я вас слушаю. Расскажите, пожалуйста, что привело вас сегодня ко мне? Можете описать ситуацию, которая вас беспокоит."
    elif mode == "coach":
        await state.update_data(system_prompt=COACH_PROMPT)
        prompt_text = "Привет! Я Максим, твой коуч. Расскажи, какая цель или задача перед тобой стоит сейчас? Что хочешь обсудить?"
        
    await callback_query.message.edit_reply_markup()
    await callback_query.message.answer(prompt_text)
    await state.set_state(UserState.in_session)
    await callback_query.answer()

@dp.message(F.text, UserState.in_session)
async def handle_session_message(message: Message, state: FSMContext):
    log_event(message.from_user.id, 'message_sent')
    data = await state.get_data()
    messages_history = data.get("messages", [])
    system_prompt = data.get("system_prompt")

    if not messages_history:
        if not system_prompt:
            await message.answer("Произошла ошибка. Пожалуйста, начните заново с команды /start.")
            await state.clear()
            return