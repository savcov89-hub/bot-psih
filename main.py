import asyncio
import os
import logging
import sys
import sqlite3
from datetime import datetime, timedelta
import uuid

from dotenv import load_dotenv
from yookassa import Configuration, Payment

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
    raise ValueError("Необходимо задать все переменные окружения")

Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

# Инициализация
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# ... (Ваши системные промпты CBT_PROMPT и COACH_PROMPT) ...
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

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
DB_FILE = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS analytics (...)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            subscription_status TEXT DEFAULT 'free',
            subscription_expires_at DATETIME
        )
    ''')
    conn.commit()
    conn.close()

# ... (код log_event, ensure_user_exists, is_user_subscribed, get_stats_for_period, format_change) ...

# --- Состояния (FSM) ---
class UserJourney(StatesGroup):
    survey_q1 = State()
    survey_q2 = State()
    plan_confirmation = State()
    in_session = State() # Старое состояние

# --- Клавиатуры ---
agree_keyboard = ...
plan_confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Готов(а) начать", callback_data="plan_accept")]
])

# --- Обработчики (Handlers) ---
@dp.message(CommandStart())
async def send_welcome(message: Message, state: FSMContext):
    ensure_user_exists(message.from_user.id)
    log_event(message.from_user.id, 'start_command')
    await state.clear()
    # ... (код приветствия) ...
    await message.answer(welcome_text, reply_markup=agree_keyboard, parse_mode="Markdown")

# ОБНОВЛЕННЫЙ ХЕНДЛЕР: НАЧИНАЕМ ОПРОС
@dp.callback_query(F.data == "agree_pressed")
async def start_survey(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_reply_markup()
    await callback_query.message.answer(
        "Отлично! Чтобы я мог составить для вас персональный план, ответьте, пожалуйста, на пару вопросов.\n\n"
        "**1. Опишите кратко, какая основная трудность или проблема вас сейчас беспокоит?**"
    , parse_mode="Markdown")
    await state.set_state(UserJourney.survey_q1)
    await callback_query.answer()

# ХЕНДЛЕР ДЛЯ ПЕРВОГО ВОПРОСА ОПРОСА
@dp.message(UserJourney.survey_q1)
async def process_survey_q1(message: Message, state: FSMContext):
    await state.update_data(q1=message.text)
    await message.answer(
        "Спасибо! И второй вопрос:\n\n"
        "**2. Какого результата вы хотели бы достичь в идеале? Что должно измениться?**"
    , parse_mode="Markdown")
    await state.set_state(UserJourney.survey_q2)

# ХЕНДЛЕР ДЛЯ ВТОРОГО ВОПРОСА И ГЕНЕРАЦИИ ПЛАНА
@dp.message(UserJourney.survey_q2)
async def process_survey_q2_and_generate_plan(message: Message, state: FSMContext):
    await state.update_data(q2=message.text)
    user_data = await state.get_data()
    
    thinking_message = await message.answer("Анализирую ваши ответы и составляю план... 🧠")

    try:
        prompt = PLAN_GENERATION_PROMPT.format(q1=user_data['q1'], q2=user_data['q2'])
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        plan_text = response.choices[0].message.content

        await thinking_message.edit_text(
            f"{plan_text}\n\nЕсли вы готовы начать работу по этому плану, нажмите кнопку ниже.",
            reply_markup=plan_confirm_keyboard,
            parse_mode="Markdown"
        )
        await state.set_state(UserJourney.plan_confirmation)
    except Exception as e:
        logging.error(f"Ошибка при генерации плана: {e}")
        await thinking_message.edit_text("Произошла ошибка при составлении плана. Попробуйте начать заново: /start")
        await state.clear()

# ХЕНДЛЕР ДЛЯ ПОДТВЕРЖДЕНИЯ ПЛАНА И ПРЕДЛОЖЕНИЯ ОПЛАТЫ
@dp.callback_query(F.data == "plan_accept", UserJourney.plan_confirmation)
async def offer_payment(callback_query: types.CallbackQuery, state: FSMContext):
    PRICE = 250.00
    
    payment = Payment.create({
        "amount": {"value": f"{PRICE:.2f}", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": f"https://t.me/{await bot.get_me().username}"},
        "capture": True,
        "description": f"Подписка на 1 неделю для user_id: {callback_query.from_user.id}",
        "metadata": {"user_id": callback_query.from_user.id, "duration_days": 7}
    }, uuid.uuid4())

    payment_url = payment.confirmation.confirmation_url
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
    await state.clear() # Очищаем состояние после предложения оплаты

# ... (остальные ваши хендлеры, включая /stats, /stop и вебхук для ЮKassa) ...
# ВАЖНО: в вебхуке ЮKassa нужно будет читать duration_days из metadata

# --- Функции для запуска и остановки ---
# ... (код main, on_startup, on_shutdown) ...

if __name__ == "__main__":
    init_db()
    # ... (остальной код)