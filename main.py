import asyncio
import os
import logging
import sys
import sqlite3
from datetime import datetime, timedelta
import uuid

from dotenv import load_dotenv
from yookassa import Configuration, Payment
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
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
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8000))

if not all([TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, ADMIN_ID, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY]):
    raise ValueError("Необходимо задать все переменные окружения, включая ключи ЮKassa")

Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

# Инициализация
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# --- Системные промпты ---
PLAN_GENERATION_PROMPT = """
Ты — опытный психолог-методолог. На основе ответов пользователя на два вопроса, составь краткий, понятный и мотивирующий план из 3-4 сессий.
Вопрос 1 (Проблема): {q1}
Вопрос 2 (Цель): {q2}

Твой ответ должен быть структурирован строго следующим образом:
Заголовок: **Ваш персональный план работы**
Далее по пунктам, например:
**Сессия 1:** [Название сессии]. [Краткое описание, что будет происходить].
**Сессия 2:** [Название сессии]. [Краткое описание].
**Сессия 3:** [Название сессии]. [Краткое описание].
"""

SESSION_PROMPT = """
Ты — AI-психолог, работающий по методу КПТ. Пользователь оплатил подписку и начинает сессию. Твоя задача — быть поддерживающим, эмпатичным и вести его по персональному плану.

**Вот план пользователя:**
{plan}

Начни первую сессию. Поздоровайся, упомяни первую тему из плана и задай открытый вопрос, чтобы начать её обсуждение. Например: "Здравствуйте! Рад начать нашу работу. Согласно нашему плану, первая сессия посвящена [тема первой сессии]. Расскажите, что у вас на уме по этому поводу?"

Веди диалог, помогая пользователю анализировать свои мысли и чувства. Будь кратким и задавай по одному вопросу за раз.
"""

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
DB_FILE = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            subscription_status TEXT DEFAULT 'free',
            subscription_expires_at DATETIME,
            yookassa_payment_method_id TEXT,
            session_plan TEXT
        )
    ''')
    # ... (остальные таблицы)
    conn.commit()
    conn.close()

# ... (остальные функции для работы с БД: ensure_user_exists, is_user_subscribed) ...

# --- Состояния (FSM) ---
class UserJourney(StatesGroup):
    survey_q1 = State()
    survey_q2 = State()
    plan_confirmation = State()
    waiting_for_promo = State()
    in_session = State()

# ... (Клавиатуры) ...

# --- Обработчики (Handlers) ---
# ... (Код для /start, /promo, /subscription и других хендлеров) ...

# ОБНОВЛЕННЫЙ ХЕНДЛЕР ГЕНЕРАЦИИ ПЛАНА
@dp.message(UserJourney.survey_q2)
async def process_survey_q2_and_generate_plan(message: Message, state: FSMContext):
    await state.update_data(q2=message.text)
    user_data = await state.get_data()
    
    thinking_message = await message.answer("Анализирую ваши ответы и составляю план... 🧠")

    try:
        prompt = PLAN_GENERATION_PROMPT.format(q1=user_data['q1'], q2=user_data['q2'])
        response = await openai_client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": prompt}], temperature=0.7
        )
        plan_text = response.choices[0].message.content

        # СОХРАНЯЕМ ПЛАН В БАЗУ ДАННЫХ
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET session_plan = ? WHERE user_id = ?", (plan_text, message.from_user.id))
        conn.commit()
        conn.close()

        await thinking_message.edit_text(
            f"{plan_text}\n\nЕсли вы готовы начать работу по этому плану, нажмите кнопку ниже.",
            reply_markup=plan_confirm_keyboard, parse_mode="Markdown"
        )
        await state.set_state(UserJourney.plan_confirmation)
    except Exception as e:
        # ... (обработка ошибок)

# ОБНОВЛЕННЫЙ ВЕБХУК ЮKASSA
async def yookassa_webhook_handler(request):
    try:
        event_json = await request.json()
        payment = event_json.get('object')
        
        if payment and payment.get('status') == 'succeeded' and payment.get('paid'):
            user_id = int(payment['metadata']['user_id'])
            # ... (логика продления подписки и сохранения карты) ...
            
            # ИЗМЕНЕННОЕ СООБЩЕНИЕ ДЛЯ ПОЛЬЗОВАТЕЛЯ
            await bot.send_message(user_id, 
                f"✅ Оплата прошла успешно! Ваша подписка активирована.\n\n"
                "Чтобы начать нашу первую сессию по вашему персональному плану, просто отправьте мне любое сообщение."
            )
    except Exception as e:
        logging.error(f"Ошибка в обработчике ЮKassa: {e}")
    return web.Response(status=200)

# ОБНОВЛЕННЫЙ "ЛОВЕЦ ОСТАЛЬНЫХ СООБЩЕНИЙ"
@dp.message()
async def handle_other_messages(message: Message, state: FSMContext):
    is_subscribed = await is_user_subscribed(message.from_user.id)
    current_state = await state.get_state()

    if is_subscribed and current_state is None:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT session_plan FROM users WHERE user_id = ?", (message.from_user.id,))
        result = cursor.fetchone()
        conn.close()

        session_plan = result[0] if result and result[0] else "План не найден. Начните с общих вопросов."

        # Создаем персональный системный промпт
        personalized_prompt = SESSION_PROMPT.format(plan=session_plan)
        
        await state.set_state(UserJourney.in_session)
        await state.update_data(messages=[{"role": "system", "content": personalized_prompt}])

        # Генерируем первое сообщение сессии с помощью AI
        first_message_response = await openai_client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "system", "content": personalized_prompt}], temperature=0.7
        )
        first_message = first_message_response.choices[0].message.content
        
        # Обновляем историю, чтобы бот не повторял приветствие
        await state.update_data(messages=[
            {"role": "system", "content": personalized_prompt},
            {"role": "assistant", "content": first_message}
        ])
        
        await message.answer(first_message)
    else:
        await message.answer("Чтобы начать, пожалуйста, используйте команду /start.")

# ... (Остальной код без изменений: on_startup, on_shutdown, main, и т.д.) ...