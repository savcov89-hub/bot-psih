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

@dp.message(Command("stop"), StateFilter("*"))
async def stop_session(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Сессия завершена. Чтобы начать заново, нажмите /start.")

@dp.message(Command("stats"), StateFilter("*"))
async def stats_command(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        await message.answer("У вас нет доступа к этой команде.")
        return
    
    await message.answer("📊 Выберите период для отображения статистики:", reply_markup=stats_keyboard)

@dp.callback_query(F.data.startswith("stats_"))
async def handle_stats_period(callback_query: types.CallbackQuery):
    if str(callback_query.from_user.id) != ADMIN_ID:
        await callback_query.answer("У вас нет доступа к этой команде.", show_alert=True)
        return

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

    cursor.execute(f"SELECT COUNT(DISTINCT user_id) FROM analytics WHERE event_type = 'start_command' {date_filter.replace('WHERE', 'AND') if date_filter else ''}")
    start_users = cursor.fetchone()[0]

    cursor.execute(f"SELECT COUNT(DISTINCT user_id) FROM analytics {date_filter}")
    total_users = cursor.fetchone()[0]
    
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
    
    await callback_query.message.edit_text(stats_text, parse_mode="Markdown")
    await callback_query.answer()

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
        messages_history.append({"role": "system", "content": system_prompt})

    messages_history.append({"role": "user", "content": message.text})

    thinking_message = await message.answer("Думаю... 🤔")

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages_history,
            temperature=0.75,
            max_tokens=500,
        )
        gpt_answer = response.choices[0].message.content

        messages_history.append({"role": "assistant", "content": gpt_answer})
        await state.update_data(messages=messages_history)
        await state.set_state(UserState.in_session)
        await thinking_message.edit_text(gpt_answer)

    except Exception as e:
        print(f"Ошибка при вызове OpenAI API: {e}")
        logging.error(f"Ошибка при вызове OpenAI API: {e}")
        await thinking_message.edit_text("Произошла ошибка. Пожалуйста, попробуйте еще раз позже или завершите сессию командой /stop.")

@dp.message()
async def handle_other_messages(message: Message):
    await message.answer("Чтобы начать, пожалуйста, используйте команду /start.")

# --- Функции для запуска и остановки вебхука ---
async def on_startup(bot: Bot) -> None:
    webhook_url_from_env = os.getenv("WEBHOOK_URL")
    if webhook_url_from_env:
        await bot.set_webhook(f"{webhook_url_from_env}/webhook")
    else:
        logging.warning("WEBHOOK_URL не установлен, бот не будет работать на сервере.")

async def on_shutdown(bot: Bot) -> None:
    await bot.delete_webhook()

def main() -> None:
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    
    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)

if __name__ == "__main__":
    init_db()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main()