import os
from datetime import datetime
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from supabase import create_client, Client

import json  # ✅ понадобится для логирования payload

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from html import escape


# --- ENVIRONMENT VARIABLES ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# --- INIT ---
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- MENU ---
def get_main_keyboard(lang: str, category: Optional[str]) -> ReplyKeyboardMarkup:
    if category == "of":
        label = "Моя подписка" if lang == "ru" else "My subscription"
        return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=label)]], resize_keyboard=True)
    return ReplyKeyboardMarkup(keyboard=[], resize_keyboard=True)


# --- HELPER: PARSE START PARAM ---
def parse_start_param(param: str):
    if not param.startswith("of_"):
        return None, None
    parts = param.split("_", 1)
    return "of", parts[1] if len(parts) > 1 else None


# --- LOGGING MIDDLEWARE ---
@web.middleware
async def logging_middleware(request, handler):
    try:
        method = request.method
        path = request.path
        ip = request.remote
        user_agent = request.headers.get("User-Agent", "")
        try:
            payload = await request.json()
        except:
            payload = {}

        response = await handler(request)

        # логгирование в supabase
        supabase.table("http_logs").insert({
            "method": method,
            "path": path,
            "status_code": response.status,
            "ip": ip,
            "user_agent": user_agent,
            "payload": payload,
            "source": detect_source(path, user_agent)
        }).execute()

        return response

    except Exception as e:
        supabase.table("http_logs").insert({
            "method": request.method,
            "path": request.path,
            "status_code": 500,
            "ip": request.remote,
            "user_agent": request.headers.get("User-Agent", ""),
            "payload": {},
            "source": "error"
        }).execute()
        raise


def detect_source(path: str, user_agent: str) -> str:
    if path.startswith("/webhook"):
        return "telegram"
    if "Mozilla" in user_agent:
        return "browser"
    return "unknown"


# --- COMMAND: /start ---

@dp.message(F.text.startswith("/start"))
async def start_handler(message: Message):
    user_id = message.from_user.id
    lang = message.from_user.language_code or "en"

    # Check start param
    args = message.text.split(" ")
    category, channel = None, None
    if len(args) > 1:
        category, channel = parse_start_param(args[1])

    # Check if user is already registered
    result = supabase.table("users").select("*").eq("id", user_id).execute()
    if not result.data:
        if not category:
            error_text = "❌ Error. Message me: @jp_agency" if lang == "en" else "❌ Ошибка. Напишите мне в личку: @jp_agency"
            await message.answer(error_text)
            return

        # Register new user
        supabase.table("users").insert({
            "id": user_id,
            "lang": lang,
            "created_at": datetime.utcnow().isoformat(),
            "category": category,
            "channel": channel
        }).execute()
    else:
        lang = result.data[0]["lang"]
        category = result.data[0]["category"]
        channel = result.data[0]["channel"]

    # Основная клавиатура
    keyboard = get_main_keyboard(lang, category)
    await message.answer("✅ Добро пожаловать!" if lang == "ru" else "✅ Welcome!", reply_markup=keyboard)

    # --- Получаем тарифы из supabase ---
    tariffs = supabase.table("tariffs") \
        .select("*") \
        .eq("is_active", True) \
        .eq("category", category) \
        .eq("channel_name", channel) \
        .execute().data

    if tariffs:
        plan_text = "Выберите тариф 👇" if lang == "ru" else "Choose plan 👇"
        buttons = []

        for tariff in tariffs:
            duration_days = int(tariff["lifetime"]) // 1440  # 60*24 = 1440 минут в дне
            text = f"{tariff['title']} | {tariff['price']}$ | {duration_days} days"
            callback_data = f"plan_{tariff['id']}"
            buttons.append([InlineKeyboardButton(text=text, callback_data=callback_data)])

        inline_kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(plan_text, reply_markup=inline_kb)

@dp.callback_query(F.data.startswith("plan_"))
async def plan_detail_handler(callback: CallbackQuery):
    tariff_id = callback.data.split("_", 1)[1]
    user_id = callback.from_user.id

    # Получаем пользователя
    result = supabase.table("users").select("*").eq("id", user_id).execute()
    if not result.data:
        await callback.answer("❌ Error. User not found.")
        return
    user = result.data[0]
    lang = user["lang"]

    # Получаем тариф
    tariff_resp = supabase.table("tariffs").select("*").eq("id", tariff_id).single().execute()
    if not tariff_resp.data:
        await callback.answer("❌ Tariff not found")
        return
    tariff = tariff_resp.data

    duration_days = int(tariff["lifetime"]) // 1440
    text = (
        f"<b>Plan:</b> {escape(tariff['title'])}\n"
        f"<b>Price:</b> {tariff['price']} {tariff['currency']}\n"
        f"<b>Time:</b> {duration_days} days\n\n"
        f"{escape(tariff.get('short_description') or '')}\n\n"
        f"{escape(tariff.get('description') or '')}"
    )

    # Инлайн-кнопки: оплата
    buttons = [
        [
            InlineKeyboardButton(text="💳 Pay by card", callback_data=f"pay_card_{tariff_id}"),
            InlineKeyboardButton(text="🪙 Pay by crypto", callback_data=f"pay_crypto_{tariff_id}")
        ],
        [
            InlineKeyboardButton(text="🔙 Back", callback_data="back_to_plans")
        ]
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    # Удаляем/редактируем старое сообщение
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "back_to_plans")
async def back_to_plan_list(callback: CallbackQuery):
    user_id = callback.from_user.id

    result = supabase.table("users").select("*").eq("id", user_id).execute()
    if not result.data:
        await callback.answer("❌ User not found")
        return
    user = result.data[0]
    lang = user["lang"]
    category = user["category"]
    channel = user["channel"]

    # Получаем тарифы
    tariffs = supabase.table("tariffs") \
        .select("*") \
        .eq("is_active", True) \
        .eq("category", category) \
        .eq("channel_name", channel) \
        .execute().data

    if not tariffs:
        await callback.message.edit_text("❌ No active plans available.")
        return

    plan_text = "Выберите тариф 👇" if lang == "ru" else "Choose plan 👇"
    buttons = []

    for tariff in tariffs:
        duration_days = int(tariff["lifetime"]) // 1440
        text = f"{tariff['title']} | {tariff['price']}$ | {duration_days} days"
        callback_data = f"plan_{tariff['id']}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=callback_data)])

    inline_kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(plan_text, reply_markup=inline_kb)
    await callback.answer()



# --- ECHO OTHER MESSAGES ---
@dp.message()
async def fallback_handler(message: Message):
    user_id = message.from_user.id
    result = supabase.table("users").select("*").eq("id", user_id).execute()
    if not result.data:
        await message.answer("❌ Ошибка. Напишите мне в личку: @jp_agency")
        return

    user = result.data[0]
    lang = user["lang"]
    category = user["category"]
    keyboard = get_main_keyboard(lang, category)

    await message.answer("🚧 В разработке." if lang == "ru" else "🚧 Under development.", reply_markup=keyboard)


# --- WEBHOOK SETUP ---
async def on_startup(app: web.Application):
    await bot.set_webhook(WEBHOOK_URL)


# --- APP SETUP ---
app = web.Application(middlewares=[logging_middleware])  # ✅ подключаем middleware
app["supabase"] = supabase

dp["base_url"] = WEBHOOK_URL
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
app.on_startup.append(on_startup)

if __name__ == "__main__":
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
