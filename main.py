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
Ты — опытный психолог-методолог. На основе ответов пользователя на 5 вопросов, составь краткий, понятный и мотивирующий план из 3-4 сессий, который решает его проблему.
Вопрос 1 (Проблема): {q1}
Вопрос 2 (Идеальный результат): {q2}
Вопрос 3 (Что мешает): {q3}
Вопрос 4 (Что уже пробовал): {q4}
Вопрос 5 (Как проявляется в поведении): {q5}

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
            subscription_expires_at DATETIME,
            yookassa_payment_method_id TEXT,
            session_plan TEXT
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

def log_event(user_id: int, event_type: str):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    timestamp = datetime.utcnow()
    cursor.execute(
        "INSERT INTO analytics (user_id, event_type, timestamp) VALUES (?, ?, ?)",
        (user_id, event_type, timestamp)
    )
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

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ АНАЛИТИКИ ---
def get_stats_for_period(date_filter: str):
    """Получает статистику за указанный период."""
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
    return {"start": start_users, "total": total_users, "active": active_users}

def format_change(current, previous):
    """Форматирует абсолютное и процентное изменение между двумя числами."""
    if previous == 0:
        if current > 0:
            return f"\n└─ `(+{current} vs 0)`"
        return "\n└─ `(без изменений)`"

    absolute_diff = current - previous
    
    if absolute_diff == 0:
        return "\n└─ `(без изменений)`"
        
    percent_change = (absolute_diff / previous) * 100
    
    sign = "+" if absolute_diff > 0 else ""
    emoji = "📈" if absolute_diff > 0 else "📉"
    
    return f"\n└─ `{sign}{absolute_diff} ({sign}{percent_change:.0f}%) {emoji}`"

# --- Состояния (FSM) ---
class UserJourney(StatesGroup):
    survey_q1 = State()
    survey_q2 = State()
    survey_q3 = State()
    survey_q4 = State()
    survey_q5 = State()
    plan_confirmation = State()
    waiting_for_promo = State()
    in_session = State()

# --- Клавиатуры ---
agree_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Я понимаю и согласен", callback_data="agree_pressed")]])
plan_confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Готов(а) начать", callback_data="plan_accept")]])
my_subscription_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить автопродление", callback_data="cancel_subscription")]])
payment_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Оплатить 250 ₽", callback_data="pay_subscription")],
    [InlineKeyboardButton(text="🎁 У меня есть промокод", callback_data="enter_promo")]
])
stats_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Сегодня", callback_data="stats_today"), InlineKeyboardButton(text="Вчера", callback_data="stats_yesterday")],
    [InlineKeyboardButton(text="7 дней", callback_data="stats_7d"), InlineKeyboardButton(text="30 дней", callback_data="stats_30d")],
    [InlineKeyboardButton(text="Сравнить 7 дней", callback_data="stats_compare7d")],
    [InlineKeyboardButton(text="Сравнить 30 дней", callback_data="stats_compare30d")],
    [InlineKeyboardButton(text="За всё время", callback_data="stats_all")]
])
back_to_stats_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="⬅️ Назад к выбору периода", callback_data="stats_back")]
])

# --- Обработчики (Handlers) ---
@dp.message(CommandStart())
async def send_welcome(message: Message, state: FSMContext):
    ensure_user_exists(message.from_user.id)
    log_event(message.from_user.id, 'start_command')
    await state.clear()
    welcome_text = (
        "👋 Здравствуйте! Я — цифровой ассистент для работы с мышлением.\n\n"
        "**❗️ Важное предупреждение:**\n"
        "Я являюсь AI-алгоритмом и не могу заменить консультацию с реальным специалистом. Если вы в кризисной ситуации, пожалуйста, обратитесь за профессиональной помощью."
    )
    is_subscribed = await is_user_subscribed(message.from_user.id)
    if is_subscribed:
        await message.answer(f"{welcome_text}\n\nУ вас активна подписка. Чтобы начать сессию, просто напишите мне. Для управления подпиской используйте команду /subscription.", parse_mode="Markdown")
    else:
        await message.answer(f"{welcome_text}\n\nЧтобы начать, нажмите кнопку ниже. Также вы можете ввести промокод командой /promo.", reply_markup=agree_keyboard, parse_mode="Markdown")

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

@dp.callback_query(F.data.startswith("stats_"))
async def handle_stats_period(callback_query: types.CallbackQuery):
    if str(callback_query.from_user.id) != ADMIN_ID:
        await callback_query.answer("У вас нет доступа к этой команде.", show_alert=True)
        return

    period = callback_query.data.split("_")[1]
    stats_text = ""

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
    
    elif period in ["compare7d", "compare30d"]:
        days = 7 if period == "compare7d" else 30
        
        current_filter = f"WHERE DATE(timestamp) >= DATE('now', '-{days} days', 'utc')"
        current_stats = get_stats_for_period(current_filter)

        previous_filter = f"WHERE DATE(timestamp) >= DATE('now', '-{days*2} days', 'utc') AND DATE(timestamp) < DATE('now', '-{days} days', 'utc')"
        previous_stats = get_stats_for_period(previous_filter)
        
        stats_text = (
            f"📊 **Сравнение статистики за {days} дней**\n"
            f"_(Последние {days} vs. Предыдущие {days})_\n\n"
            f"▫️ **Нажали /start:** {current_stats['start']} (vs {previous_stats['start']}){format_change(current_stats['start'], previous_stats['start'])}\n"
            f"▫️ **Всего уникальных:** {current_stats['total']} (vs {previous_stats['total']}){format_change(current_stats['total'], previous_stats['total'])}\n"
            f"▫️ **Активные (> 5):** {current_stats['active']} (vs {previous_stats['active']}){format_change(current_stats['active'], previous_stats['active'])}"
        )
    
    if stats_text:
        await callback_query.message.edit_text(stats_text, parse_mode="Markdown", reply_markup=back_to_stats_keyboard)

    await callback_query.answer()

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
        await message.answer(f"✅ Промокод успешно активирован! Ваша подписка действительна на {duration_days} дней.\n\nЧтобы начать сессию, просто напишите мне любое сообщение.")
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

@dp.callback_query(F.data == "agree_pressed")
async def start_survey(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_reply_markup()
    await callback_query.message.answer(
        "Отлично! Чтобы составить для вас персональный план, ответьте, пожалуйста, на 5 вопросов.\n\n"
        "**1. Опишите кратко, какая основная трудность или проблема вас сейчас беспокоит?**",
        parse_mode="Markdown"
    )
    await state.set_state(UserJourney.survey_q1)
    await callback_query.answer()

@dp.message(UserJourney.survey_q1)
async def process_survey_q1(message: Message, state: FSMContext):
    await state.update_data(q1=message.text)
    await message.answer("**2. Какого результата вы хотели бы достичь в идеале? Что должно измениться?**", parse_mode="Markdown")
    await state.set_state(UserJourney.survey_q2)

@dp.message(UserJourney.survey_q2)
async def process_survey_q2(message: Message, state: FSMContext):
    await state.update_data(q2=message.text)
    await message.answer("**3. Как вы думаете, что вам больше всего мешает достичь этого результата?**", parse_mode="Markdown")
    await state.set_state(UserJourney.survey_q3)

@dp.message(UserJourney.survey_q3)
async def process_survey_q3(message: Message, state: FSMContext):
    await state.update_data(q3=message.text)
    await message.answer("**4. Что вы уже пробовали делать для решения этой проблемы?**", parse_mode="Markdown")
    await state.set_state(UserJourney.survey_q4)

@dp.message(UserJourney.survey_q4)
async def process_survey_q4(message: Message, state: FSMContext):
    await state.update_data(q4=message.text)
    await message.answer("**5. Как эта проблема проявляется в вашем поведении? (например, 'избегаю общения', 'откладываю дела')**", parse_mode="Markdown")
    await state.set_state(UserJourney.survey_q5)

@dp.message(UserJourney.survey_q5)
async def process_survey_q5_and_generate_plan(message: Message, state: FSMContext):
    await state.update_data(q5=message.text)
    user_data = await state.get_data()
    
    thinking_message = await message.answer("Анализирую ваши ответы и составляю план... 🧠")
    try:
        prompt = PLAN_GENERATION_PROMPT.format(
            q1=user_data.get('q1'), q2=user_data.get('q2'), q3=user_data.get('q3'),
            q4=user_data.get('q4'), q5=user_data.get('q5')
        )
        response = await openai_client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": prompt}], temperature=0.7
        )
        plan_text = response.choices[0].message.content

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
        logging.error(f"Ошибка при генерации плана: {e}")
        await thinking_message.edit_text("Произошла ошибка при составлении плана. Попробуйте начать заново: /start")
        await state.clear()

@dp.callback_query(F.data == "plan_accept", UserJourney.plan_confirmation)
async def show_payment_options(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_text(
        "Отлично! Доступ к сессиям предоставляется по подписке.\n\n"
        "**Тариф:**\n"
        "▫️ **250 рублей** за 7 дней доступа.\n\n"
        "Выберите удобный для вас вариант:",
        reply_markup=payment_keyboard,
        parse_mode="Markdown"
    )
    await callback_query.answer()

@dp.callback_query(F.data == "pay_subscription", UserJourney.plan_confirmation)
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
    
    await callback_query.message.answer(
        "Нажмите на кнопку ниже, чтобы перейти к оплате.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Перейти к оплате", url=payment.confirmation.confirmation_url)]])
    )
    await callback_query.answer()
    await state.clear()

@dp.callback_query(F.data == "enter_promo", UserJourney.plan_confirmation)
async def ask_for_promo(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_text("Пожалуйста, введите ваш промокод:")
    await state.set_state(UserJourney.waiting_for_promo)
    await callback_query.answer()

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
            await bot.send_message(user_id,
                f"✅ Оплата прошла успешно! Ваша подписка активирована на {duration_days} дней.\n\n"
                "Чтобы начать нашу первую сессию по вашему персональному плану, просто отправьте мне любое сообщение."
            )
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

@dp.message(F.text, UserJourney.in_session)
async def handle_paid_session(message: Message, state: FSMContext):
    data = await state.get_data()
    messages_history = data.get("messages", [])

    messages_history.append({"role": "user", "content": message.text})

    thinking_message = await message.answer("Думаю...")
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages_history,
            temperature=0.75,
        )
        gpt_answer = response.choices[0].message.content
        messages_history.append({"role": "assistant", "content": gpt_answer})
        await state.update_data(messages=messages_history)
        await thinking_message.edit_text(gpt_answer)
    except Exception as e:
        logging.error(f"Ошибка в handle_paid_session: {e}")
        await thinking_message.edit_text("Произошла ошибка. Попробуйте еще раз.")

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

        personalized_prompt = SESSION_PROMPT.format(plan=session_plan)
        
        await state.set_state(UserJourney.in_session)
        
        first_message_response = await openai_client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "system", "content": personalized_prompt}], temperature=0.7
        )
        first_message = first_message_response.choices[0].message.content
        
        await state.update_data(messages=[
            {"role": "system", "content": personalized_prompt},
            {"role": "assistant", "content": first_message}
        ])
        
        await message.answer(first_message)
    else:
        await message.answer("Чтобы начать, пожалуйста, используйте команду /start.")

# --- Функции для запуска ---
async def on_startup_scheduler(app):
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(charge_recurring_payments, 'cron', day_of_week='*', hour=10, minute=0)
    scheduler.start()

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