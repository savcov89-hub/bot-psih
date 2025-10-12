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
# ... (Код для init_db, ensure_user_exists, is_user_subscribed без изменений) ...

# --- Состояния (FSM) ---
class UserJourney(StatesGroup):
    survey_q1 = State()
    survey_q2 = State()
    plan_confirmation = State()
    waiting_for_promo = State()

# --- Клавиатуры ---
# ... (Код для клавиатур без изменений) ...

# --- Обработчики (Handlers) ---
# ... (Код для /start, /promo, /subscription и других обработчиков без изменений) ...

async def yookassa_webhook_handler(request):
    # ... (Код вебхука ЮKassa без изменений) ...
    return web.Response(status=200)

async def charge_recurring_payments():
    # ... (Код для автосписаний без изменений) ...
    pass # Вставьте ваш код сюда

# --- ИСПРАВЛЕННЫЙ БЛОК ЗАПУСКА ---

# Функция для запуска планировщика
async def on_startup_scheduler(app):
    scheduler = AsyncIOScheduler(timezone="UTC")
    # Запускаем проверку раз в день в 10:00 UTC
    scheduler.add_job(charge_recurring_payments, 'cron', hour=10, minute=0)
    scheduler.start()

# Основные функции запуска и остановки
async def on_startup(bot: Bot) -> None:
    webhook_url_from_env = os.getenv("WEBHOOK_URL")
    if webhook_url_from_env:
        await bot.set_webhook(f"{webhook_url_from_env}/webhook")
    else:
        logging.warning("WEBHOOK_URL не установлен.")

async def on_shutdown(bot: Bot) -> None:
    await bot.delete_webhook()

def main() -> None:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    
    # Регистрируем запуск планировщика вместе со стартом веб-приложения
    app.on_startup.append(on_startup_scheduler)

    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path="/webhook")
    app.router.add_post("/yookassa_webhook", yookassa_webhook_handler)
    
    setup_application(app, dp, bot=bot)
    
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)

if __name__ == "__main__":
    init_db()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main()