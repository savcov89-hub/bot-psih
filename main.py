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

# ... (Ваши системные промпты, например, PLAN_GENERATION_PROMPT) ...

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
DB_FILE = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS analytics (id INTEGER PRIMARY KEY, user_id INTEGER, event_type TEXT, timestamp DATETIME)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            subscription_status TEXT DEFAULT 'free',
            subscription_expires_at DATETIME,
            yookassa_payment_method_id TEXT 
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            duration_days INTEGER NOT NULL,
            is_active INTEGER DEFAULT 1
        )
    ''')
    conn.commit()
    conn.close()

def ensure_user_exists(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if cursor.fetchone() is None:
        cursor.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
    conn.close()

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

# --- Состояния (FSM) ---
class UserJourney(StatesGroup):
    survey_q1 = State()
    survey_q2 = State()
    plan_confirmation = State()
    waiting_for_promo = State()

# --- Клавиатуры ---
agree_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Я понимаю и согласен", callback_data="agree_pressed")]])
plan_confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Готов(а) начать", callback_data="plan_accept")]])
my_subscription_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить автопродление", callback_data="cancel_subscription")]])

# --- Обработчики (Handlers) ---
@dp.message(CommandStart())
async def send_welcome(message: Message, state: FSMContext):
    ensure_user_exists(message.from_user.id)
    await state.clear()
    welcome_text = "👋 Здравствуйте! Я — цифровой ассистент..." # Ваше приветствие
    
    is_subscribed = await is_user_subscribed(message.from_user.id)
    if is_subscribed:
        await message.answer(f"{welcome_text}\n\nУ вас активна подписка. Чтобы управлять ей, используйте команду /subscription.", parse_mode="Markdown")
    else:
        await message.answer(f"{welcome_text}\n\nЧтобы начать, нажмите кнопку ниже. Также вы можете ввести промокод командой /promo.", reply_markup=agree_keyboard, parse_mode="Markdown")

@dp.message(Command("promo"), StateFilter("*"))
async def promo_command(message: Message, state: FSMContext):
    await message.answer("Введите ваш промокод:")
    await state.set_state(UserJourney.waiting_for_promo)

@dp.message(UserJourney.waiting_for_promo)
async def process_promo_code(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT duration_days FROM promo_codes WHERE code = ? AND is_active = 1", (code,))
    result = cursor.fetchone()
    
    if result:
        duration_days = result[0]
        expires_at = datetime.utcnow() + timedelta(days=duration_days)
        cursor.execute(
            "UPDATE users SET subscription_status = ?, subscription_expires_at = ? WHERE user_id = ?",
            ('paid', expires_at.isoformat(), message.from_user.id)
        )
        cursor.execute("UPDATE promo_codes SET is_active = 0 WHERE code = ?", (code,))
        conn.commit()
        await message.answer(f"✅ Промокод успешно активирован! Ваша подписка действительна на {duration_days} дней.")
    else:
        await message.answer("❌ Промокод не найден или уже был использован.")
    
    conn.close()
    await state.clear()

@dp.message(Command("subscription"), StateFilter("*"))
async def subscription_command(message: Message):
    is_subscribed = await is_user_subscribed(message.from_user.id)
    if is_subscribed:
        await message.answer("Вы можете отменить следующее списание.", reply_markup=my_subscription_keyboard)
    else:
        await message.answer("У вас нет активной подписки. Чтобы начать, используйте команду /start.")

@dp.callback_query(F.data == "cancel_subscription")
async def cancel_subscription_handler(callback_query: types.CallbackQuery):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET yookassa_payment_method_id = NULL WHERE user_id = ?", (callback_query.from_user.id,))
    conn.commit()
    conn.close()
    await callback_query.message.edit_text("✅ Автопродление подписки отменено. Текущая подписка будет действовать до конца оплаченного периода.")

# ... (Код опроса и генерации плана: start_survey, process_survey_q1, process_survey_q2_and_generate_plan) ...

@dp.callback_query(F.data == "plan_accept", UserJourney.plan_confirmation)
async def offer_payment(callback_query: types.CallbackQuery, state: FSMContext):
    PRICE = 250.00
    payment = Payment.create({
        "amount": {"value": f"{PRICE:.2f}", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": f"https://t.me/{(await bot.get_me()).username}"},
        "capture": True,
        "description": "Подписка на 7 дней (с автопродлением)",
        "save_payment_method": True,
        "metadata": {"user_id": callback_query.from_user.id, "duration_days": 7}
    }, uuid.uuid4())
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Оплатить 250 ₽", url=payment.confirmation.confirmation_url)]])
    await callback_query.message.edit_text(
        "**Тариф:**\n▫️ **250 рублей** за 7 дней доступа.\n\nПодписка будет продлеваться автоматически каждую неделю. Вы можете отменить её в любой момент.",
        reply_markup=keyboard, parse_mode="Markdown"
    )

async def yookassa_webhook_handler(request):
    try:
        event_json = await request.json()
        payment = event_json.get('object')
        
        if payment and payment.get('status') == 'succeeded' and payment.get('paid'):
            user_id = int(payment['metadata']['user_id'])
            duration_days = int(payment['metadata'].get('duration_days', 7))
            expires_at = datetime.utcnow() + timedelta(days=duration_days)
            
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            payment_method_id = payment.get('payment_method', {}).get('id')
            cursor.execute(
                "UPDATE users SET subscription_status = ?, subscription_expires_at = ?, yookassa_payment_method_id = ? WHERE user_id = ?",
                ('paid', expires_at.isoformat(), payment_method_id, user_id)
            )
            conn.commit()
            conn.close()
            await bot.send_message(user_id, f"✅ Оплата прошла успешно! Ваша подписка активирована на {duration_days} дней.")
    except Exception as e:
        logging.error(f"Ошибка в обработчике ЮKassa: {e}")
    return web.Response(status=200)

async def charge_recurring_payments():
    logging.info("Starting recurring payment check...")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, yookassa_payment_method_id FROM users WHERE subscription_status = 'paid' AND subscription_expires_at < ? AND yookassa_payment_method_id IS NOT NULL", (datetime.utcnow(),))
    
    users_to_charge = cursor.fetchall()
    conn.close()

    for user_id, payment_method_id in users_to_charge:
        try:
            Payment.create({
                "amount": {"value": "250.00", "currency": "RUB"},
                "capture": True,
                "payment_method_id": payment_method_id,
                "description": "Автопродление подписки на 7 дней",
                "metadata": {"user_id": user_id, "duration_days": 7}
            })
            logging.info(f"Successfully charged user {user_id}")
        except Exception as e:
            logging.error(f"Failed to charge user {user_id}: {e}")
            await bot.send_message(user_id, "⚠️ Не удалось продлить подписку. Пожалуйста, проверьте вашу карту и оплатите вручную через команду /start.")

# --- Функции для запуска ---
async def on_startup(bot: Bot) -> None:
    webhook_url_from_env = os.getenv("WEBHOOK_URL")
    await bot.set_webhook(f"{webhook_url_from_env}/webhook")

async def on_shutdown(bot: Bot) -> None:
    await bot.delete_webhook()

def main() -> None:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path="/webhook")
    app.router.add_post("/yookassa_webhook", yookassa_webhook_handler)
    setup_application(app, dp, bot=bot)
    
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(charge_recurring_payments, 'cron', day_of_week='*', hour=10, minute=0) # Раз в день
    scheduler.start()
    
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)

if __name__ == "__main__":
    init_db()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main()