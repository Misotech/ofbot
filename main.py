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
            lang_code = message.from_user.language_code or "en"
            error_text = "❌ Error. Message me: @jp_agency" if lang_code == "en" else "❌ Ошибка. Напишите мне в личку: @jp_agency"
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

    keyboard = get_main_keyboard(lang, category)
    await message.answer("✅ Добро пожаловать!" if lang == "ru" else "✅ Welcome!", reply_markup=keyboard)


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


app = web.Application()
dp["base_url"] = WEBHOOK_URL
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
app.on_startup.append(on_startup)

if __name__ == "__main__":
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
