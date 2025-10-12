import asyncio
import os
import logging
import sys
import sqlite3
from datetime importdatetime, timedelta
import uuid # Для генерации уникального ключа платежа

from dotenv import load_dotenv

# Библиотека для работы с ЮKassa
from yookassa import Configuration, Payment

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

# НОВЫЕ КЛЮЧИ ДЛЯ ЮKASSA
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8000))

# Проверяем наличие всех токенов
if not all([TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, ADMIN_ID, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY]):
    raise ValueError("Необходимо задать все переменные окружения")

# Настраиваем SDK ЮKassa
Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

# Инициализация
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# ... (Ваши системные промпты CBT_PROMPT и COACH_PROMPT) ...

# --- РАБОТА С БАЗОЙ ДАННЫХ (АНАЛИТИКА + ПОДПИСКИ) ---
DB_FILE = "bot_data.db" # Переименуем для ясности

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Таблица аналитики
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS analytics (...)
    ''')
    # НОВАЯ ТАБЛИЦА для пользователей и их подписок
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            subscription_status TEXT DEFAULT 'free',
            subscription_expires_at DATETIME
        )
    ''')
    conn.commit()
    conn.close()

def log_event(user_id: int, event_type: str):
    # ... (код log_event без изменений)

# НОВАЯ ФУНКЦИЯ: проверка или добавление пользователя в БД
def ensure_user_exists(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if cursor.fetchone() is None:
        cursor.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
    conn.close()

# НОВАЯ ФУНКЦИЯ: проверка статуса подписки
async def is_user_subscribed(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT subscription_status, subscription_expires_at FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        status, expires_at_str = result
        if status == 'paid' and expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at > datetime.utcnow():
                return True
    return False

# НОВЫЙ "ОХРАННИК" (MIDDLEWARE)
class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        # Проверяем только текстовые сообщения от пользователей
        if isinstance(event, types.Message) and event.text and not event.text.startswith('/'):
            is_subscribed = await is_user_subscribed(event.from_user.id)
            # Здесь можно добавить логику, например, ограничивать количество бесплатных сообщений
            # Для простоты, пока просто продолжаем
            
        return await handler(event, data)

# --- Состояния и Клавиатуры ---
# ... (код состояний и клавиатур) ...

# --- Обработчики (Handlers) ---
@dp.message(CommandStart())
async def send_welcome(message: Message, state: FSMContext):
    ensure_user_exists(message.from_user.id) # Добавляем пользователя в БД при старте
    log_event(message.from_user.id, 'start_command')
    # ... (ваш код приветствия)

# НОВЫЙ ХЕНДЛЕР: команда /subscribe
@dp.message(Command("subscribe"), StateFilter("*"))
async def subscribe_command(message: Message):
    # Цена подписки в рублях
    PRICE = 199.00 
    
    payment = Payment.create({
        "amount": {
            "value": f"{PRICE:.2f}",
            "currency": "RUB"
        },
        "confirmation": {
            "type": "redirect",
            "return_url": f"https://t.me/{await bot.get_me().username}" # Куда вернется пользователь
        },
        "capture": True,
        "description": f"Подписка на 1 месяц для user_id: {message.from_user.id}",
        "metadata": {
            "user_id": message.from_user.id
        }
    }, uuid.uuid4())

    payment_url = payment.confirmation.confirmation_url
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить подписку", url=payment_url)]
    ])
    await message.answer(f"Стоимость подписки на 1 месяц: {PRICE} руб.", reply_markup=keyboard)

# ... (остальные ваши хендлеры) ...

# НОВЫЙ ХЕНДЛЕР: ВЕБХУК ДЛЯ ЮKASSA
async def yookassa_webhook_handler(request):
    try:
        event_json = await request.json()
        payment = event_json.get('object')
        
        # Проверяем, что оплата прошла успешно (succeeded)
        if payment and payment.get('status') == 'succeeded' and payment.get('paid'):
            user_id = int(payment['metadata']['user_id'])
            
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            # Продлеваем подписку на 30 дней от текущего момента
            expires_at = datetime.utcnow() + timedelta(days=30)
            
            cursor.execute(
                "UPDATE users SET subscription_status = ?, subscription_expires_at = ? WHERE user_id = ?",
                ('paid', expires_at.isoformat(), user_id)
            )
            conn.commit()
            conn.close()

            # Сообщаем пользователю об успешной оплате
            await bot.send_message(user_id, "✅ Оплата прошла успешно! Ваша подписка активирована на 30 дней.")
            
        return web.Response(status=200)
    except Exception as e:
        logging.error(f"Ошибка в обработчике ЮKassa: {e}")
        return web.Response(status=500)

# --- Функции для запуска и остановки ---
async def on_startup(bot: Bot) -> None:
    # ... (код on_startup)

def main() -> None:
    # ... (код main)
    # Регистрируем "охранника"
    dp.update.middleware(SubscriptionMiddleware())
    # Регистрируем вебхук для ЮKassa
    app.router.add_post("/yookassa_webhook", yookassa_webhook_handler)
    # ... (остальной код запуска)

if __name__ == "__main__":
    init_db()
    # ... (остальной код)