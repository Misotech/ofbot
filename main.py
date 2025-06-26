import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import uuid4

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.methods import CreateChatInviteLink


import hmac
import hashlib


from supabase import create_client, Client

import json  # ✅ понадобится для логирования payload

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from html import escape

import aiohttp

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY")
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID")

TRIBUTE_API_SECRET = os.getenv("TRIBUTE_API_SECRET")


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
    buttons = []
    if category == "of":
        label_sub = "Моя подписка" if lang == "ru" else "My subscription"
        label_plans = "📋 Тарифы" if lang == "ru" else "📋 Plans"
        buttons = [[KeyboardButton(text=label_sub)], [KeyboardButton(text=label_plans)]]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)




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
        
        # <-- Правильное извлечение данных в зависимости от content-type
        content_type = request.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                payload = await request.json()
            except:
                payload = {}
        elif "application/x-www-form-urlencoded" in content_type:
            try:
                payload = dict(await request.post())
            except:
                payload = {}
        else:
            payload = {}

        response = await handler(request)

        # логгируем
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
    await message.answer("💋 Hi!" if lang == "ru" else "💋 Hi!", reply_markup=keyboard)

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


@dp.message(F.text.in_(["My subscription", "Моя подписка"]))
async def my_subscription_text_handler(message: Message):
    user_id = message.from_user.id

    # Получаем активные подписки (как ты уже делаешь)
    subs_resp = supabase.table("subscriptions") \
        .select("tariff_id, ends_at") \
        .eq("user_id", user_id) \
        .eq("status", "active") \
        .execute()

    if not subs_resp.data:
        await message.answer("У вас нет активных подписок" if message.from_user.language_code == "ru" else "You have no active subscriptions")
        return

    msg_lines = []
    for sub in subs_resp.data:
        tariff_resp = supabase.table("tariffs") \
            .select("title", "channel_id") \
            .eq("id", sub["tariff_id"]) \
            .single() \
            .execute()
        if tariff_resp.data:
            title = tariff_resp.data["title"]
            channel_id = tariff_resp.data.get("channel_id", "N/A")
            ends_at = sub["ends_at"]
            msg_lines.append(f"📦 <b>{title}</b>\n🗓 Ends at: {ends_at}\n🔗 Channel: {channel_id}")

    await message.answer("\n\n".join(msg_lines), parse_mode="HTML")

@dp.message(F.text.in_(["📋 Тарифы", "📋 Plans"]))
async def plans_text_handler(message: Message):
    user_id = message.from_user.id

    user_resp = supabase.table("users").select("*").eq("id", user_id).single().execute()
    if not user_resp.data:
        await message.answer("❌ Ошибка. Напишите мне: @jp_agency")
        return

    user = user_resp.data
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
        await message.answer("❌ Нет доступных тарифов." if lang == "ru" else "❌ No active plans available.")
        return

    plan_text = "Выберите тариф 👇" if lang == "ru" else "Choose plan 👇"
    buttons = []

    for tariff in tariffs:
        duration_days = int(tariff["lifetime"]) // 1440
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


@dp.callback_query(F.data.startswith("pay_card_"))
async def card_payment_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    tariff_id = callback.data.split("_", 2)[2]

    # Получаем пользователя
    user_resp = supabase.table("users").select("lang").eq("id", user_id).single().execute()
    lang = user_resp.data["lang"] if user_resp.data else "en"

    # Проверка на активную подписку
    existing_sub = supabase.table("subscriptions") \
        .select("*") \
        .eq("user_id", user_id) \
        .eq("tariff_id", tariff_id) \
        .eq("status", "active") \
        .execute()

    if existing_sub.data:
        msg = "❌ У вас уже есть активная подписка на этот тариф." if lang == "ru" else "❌ You already have an active subscription to this plan."
        await callback.message.answer(msg)
        await callback.answer()
        return

    # Получаем тариф
    tariff_resp = supabase.table("tariffs").select("tribute_link", "title").eq("id", tariff_id).single().execute()
    if not tariff_resp.data:
        await callback.message.answer("❌ Tariff not found")
        await callback.answer()
        return

    tribute_link = tariff_resp.data.get("tribute_link")
    if not tribute_link:
        await callback.message.answer("❌ No payment link available")
        await callback.answer()
        return

    # Кнопка-ссылка
    button_text = "ОПЛАТИТЬ" if lang == "ru" else "PAY"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=button_text, url=tribute_link)]
    ])

    msg = "💳 Оплата картой:" if lang == "ru" else "💳 Pay by card:"
    await callback.message.answer(msg, reply_markup=kb)
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



@dp.callback_query(F.data.startswith("pay_crypto_"))
async def crypto_payment_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    tariff_id = callback.data.split("_", 2)[2]

    # Получаем пользователя
    user_result = supabase.table("users").select("*").eq("id", user_id).execute()
    if not user_result.data:
        await callback.answer("❌ User not found")
        return
    user = user_result.data[0]
    lang = user["lang"]
    locale = lang

    # Получаем тариф
    tariff_result = supabase.table("tariffs").select("*").eq("id", tariff_id).single().execute()
    if not tariff_result.data:
        await callback.answer("❌ Tariff not found")
        return
    tariff = tariff_result.data

# ⛔️ Проверка на существующую активную подписку
    existing_sub = supabase.table("subscriptions") \
        .select("*") \
        .eq("user_id", user_id) \
        .eq("tariff_id", tariff_id) \
        .eq("status", "active") \
        .execute()

    
    if existing_sub.data:
        msg = "❌ У вас уже есть активная подписка на этот тариф." if lang == "ru" else "❌ You already have an active subscription to this plan."
        await callback.message.answer(msg)
        await callback.answer()
        return

    amount = float(tariff["price"])

    # Создаём order_id отдельно
    order_id = f"{user_id}-{tariff_id}-{int(datetime.utcnow().timestamp())}"

    # --- Запрос в CryptoCloud ---
    url = "https://api.cryptocloud.plus/v2/invoice/create"
    headers = {
    "Authorization": f"Token {CRYPTOCLOUD_API_KEY}",
    "Content-Type": "application/json"
}
    payload = {
    "shop_id": CRYPTOCLOUD_SHOP_ID,
    "amount": amount,
    "currency": "USD",
    "locale": locale,
    "add_fields": {
        "user_id": str(user_id),
        "tariff_id": tariff_id
    },
    "order_id": order_id  # ← здесь мы вставляем значение переменной
}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status != 200:
                await callback.message.answer("❌ Ошибка при создании счета")
                return
            resp_data = await response.json()
            pay_link = resp_data.get("result", {}).get("link")
            if pay_link:
                separator = "&" if "?" in pay_link else "?"
                pay_link += f"{separator}lang={locale}"
            
            if not pay_link:
                await callback.message.answer("❌ Не удалось получить ссылку на оплату")
                return

            # Сохраняем в Supabase
            supabase.table("invoices").insert({
                "id": str(uuid4()),
                "user_id": user_id,
                "tariff_id": tariff_id,
                "order_id": order_id,  # ← правильно
                "invoice_link": pay_link,
                "amount": amount,
                "currency": "USD",
                "status": "created",
                "raw_response": resp_data
            }).execute()


            await callback.message.answer(
                "🪙 <b>Оплатите по ссылке:</b>\n" + pay_link if lang == "ru"
                else "🪙 <b>Pay with crypto:</b>\n" + pay_link,
                parse_mode="HTML"
            )
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


@dp.callback_query(F.data == "my_subscription")
async def my_subscription_handler(callback: CallbackQuery):
    user_id = callback.from_user.id

    # Получаем все активные подписки пользователя
    subs_resp = supabase.table("subscriptions") \
        .select("tariff_id, ends_at") \
        .eq("user_id", user_id) \
        .eq("status", "active") \
        .execute()

    if not subs_resp.data or len(subs_resp.data) == 0:
        await callback.answer("У вас нет активных подписок" if callback.from_user.language_code == "ru" else "You have no active subscriptions", show_alert=True)
        return

    msg_lines = []
    for sub in subs_resp.data:
        tariff_resp = supabase.table("tariffs") \
            .select("title", "channel_id") \
            .eq("id", sub["tariff_id"]) \
            .single() \
            .execute()
        if tariff_resp.data:
            title = tariff_resp.data["title"]
            channel_id = tariff_resp.data.get("channel_id", "N/A")
            ends_at = sub["ends_at"]
            msg_lines.append(f"📦 <b>{escape(title)}</b>\n🗓 Ends at: {ends_at}\n🔗 Channel: {channel_id}")

    text = "\n\n".join(msg_lines)

    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


# --- WEBHOOK SETUP ---
async def on_startup(app: web.Application):
    await bot.set_webhook(WEBHOOK_URL)


async def tribute_webhook_handler(request: web.Request, bot: Bot):
    try:
        raw_body = await request.read()
        data = json.loads(raw_body.decode('utf-8'))
        
        print("📥 Tribute webhook received:", data)
        
        # Проверка подписи
        signature = request.headers.get("trbt-signature")
        if signature and TRIBUTE_API_SECRET:
            computed_signature = hmac.new(
                key=TRIBUTE_API_SECRET.encode(),
                msg=raw_body,
                digestmod=hashlib.sha256
            ).hexdigest()
            
            if not hmac.compare_digest(computed_signature, signature):
                print("❌ Invalid Tribute signature")
                return web.json_response({"ok": False, "error": "Invalid signature"}, status=403)
        
        event_name = data.get("name")
        payload = data.get("payload", {})
        telegram_user_id = payload.get("telegram_user_id")
        
        if not telegram_user_id:
            return web.json_response({"ok": False, "error": "Missing telegram_user_id"}, status=400)
        
        supabase = request.app["supabase"]
        
        # Получаем язык пользователя из базы
        user_resp = supabase.table("users") \
            .select("lang") \
            .eq("id", telegram_user_id) \
            .execute()
        
        lang = "en"  # значение по умолчанию
        if user_resp.data:
            lang = user_resp.data[0].get("lang", "en")
        
        if event_name == "new_subscription":
            subscription_id = payload.get("subscription_id")
            subscription_name = payload.get("subscription_name")
            expires_at = payload.get("expires_at")
            
            if not all([subscription_id, subscription_name, expires_at]):
                error_msg = "Недостаточно данных" if lang == "ru" else "Missing required fields"
                return web.json_response({"ok": False, "error": error_msg}, status=400)
            
            # Находим тариф по subscription_name (соответствует title в таблице тарифов)
            tariff_resp = supabase.table("tariffs") \
                .select("*") \
                .eq("title", subscription_name) \
                .limit(1) \
                .execute()
            
            if not tariff_resp.data:
                print(f"❌ Tariff not found for subscription_name: {subscription_name}")
                error_msg = "Тариф не найден" if lang == "ru" else "Tariff not found"
                return web.json_response({"ok": False, "error": error_msg}, status=404)
            
            tariff = tariff_resp.data[0]
            tariff_id = tariff["id"]
            channel_id = tariff.get("channel_id")
            started_at = datetime.now(timezone.utc).isoformat()
            
            # Обновляем или создаем подписку
            supabase.table("subscriptions").upsert({
                "id": subscription_id,
                "user_id": telegram_user_id,
                "tariff_id": tariff_id,
                "started_at": started_at,
                "ends_at": expires_at,
                "status": "active",
                "price": payload.get("price"),
                "currency": payload.get("currency"),
                "updated_at": started_at
            }).execute()
            
            # Формируем сообщения на нужном языке
            if lang == "ru":
                success_msg = (
                    f"🎉 Подписка активирована!\n\n"
                    f"📝 Тариф: {subscription_name}\n"
                    f"💰 Стоимость: {payload.get('amount')} {payload.get('currency')}\n"
                    f"⏳ Действует до: {expires_at}"
                )
                invite_msg = f"🔗 Ссылка для вступления: {{link}}"
            else:
                success_msg = (
                    f"🎉 Subscription activated!\n\n"
                    f"📝 Plan: {subscription_name}\n"
                    f"💰 Amount: {payload.get('amount')} {payload.get('currency')}\n"
                    f"⏳ Valid until: {expires_at}"
                )
                invite_msg = f"🔗 Invite link: {{link}}"
            
            # Отправляем сообщение пользователю
            try:
                await bot.send_message(chat_id=telegram_user_id, text=success_msg)
                
                # Если есть channel_id, создаем инвайт-ссылку
                if channel_id:
                    invite = await bot.create_chat_invite_link(
                        chat_id=channel_id,
                        member_limit=1
                    )
                    await bot.send_message(
                        chat_id=telegram_user_id,
                        text=invite_msg.format(link=invite.invite_link)
                    )
            except Exception as e:
                print(f"❌ Error sending message: {e}")
            
            return web.json_response({"ok": True})
        
        elif event_name == "cancelled_subscription":
            # Обработка отмены подписки с учетом языка
            subscription_name = payload.get("subscription_name", "")
            cancel_reason = payload.get("cancel_reason", "")
            
            if lang == "ru":
                msg = (
                    f"❌ Ваша подписка отменена\n\n"
                    f"📝 Тариф: {subscription_name}\n"
                    f"📌 Причина: {cancel_reason or 'не указана'}"
                )
            else:
                msg = (
                    f"❌ Your subscription has been cancelled\n\n"
                    f"📝 Plan: {subscription_name}\n"
                    f"📌 Reason: {cancel_reason or 'not specified'}"
                )
            
            try:
                await bot.send_message(chat_id=telegram_user_id, text=msg)
            except Exception as e:
                print(f"❌ Error sending cancellation message: {e}")
            
            return web.json_response({"ok": True})
        
        return web.json_response({"ok": True, "message": "Event not processed"})
    
    except Exception as e:
        print(f"❌ Tribute webhook error: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def crypto_webhook(request: web.Request):
    try:
        data = await request.post()  # form-data от CryptoCloud

        status = data.get("status")
        order_id = data.get("order_id")
        # invoice_id = data.get("invoice_id")
        # token = data.get("token")

        print("📥 CryptoCloud webhook received:", dict(data))

        if status != "success" or not order_id:
            return web.json_response({"ok": True, "msg": "Ignored non-success status"}, status=200)

        # Обновляем invoice
        update_result = supabase.table("invoices") \
            .update({
                "status": "paid",
                "paid_at": datetime.utcnow().isoformat(),
                # "invoice_id": invoice_id,
                # "token": token
            }) \
            .eq("order_id", order_id) \
            .execute()

        if not update_result.data:
            print(f"⚠️ Invoice {order_id} not found in database.")
            return web.json_response({"ok": False, "error": "Invoice not found"}, status=404)

        print(f"✅ Invoice {order_id} marked as paid.")

        # --- Получаем user_id, tariff_id из invoices ---
        invoice = update_result.data[0]
        user_id = invoice["user_id"]
        tariff_id = invoice["tariff_id"]

        # --- Получаем lang пользователя ---
        user_resp = supabase.table("users").select("lang").eq("id", user_id).single().execute()
        lang = user_resp.data["lang"] if user_resp.data else "en"

        # --- Отправляем сообщение пользователю ---
        try:
            msg_text = "✅ Оплата прошла успешно!" if lang == "ru" else "✅ Payment successful!"
            await bot.send_message(chat_id=user_id, text=msg_text)
        except Exception as e:
            print(f"❌ Error sending payment success message to user {user_id}: {e}")

        # --- Получаем тариф для времени подписки и канала ---
        tariff_resp = supabase.table("tariffs").select("lifetime", "channel_id", "title").eq("id", tariff_id).single().execute()
        if not tariff_resp.data:
            print(f"⚠️ Tariff {tariff_id} not found.")
            return web.json_response({"ok": True})

        tariff = tariff_resp.data
        lifetime_min = int(tariff["lifetime"])
        channel_id = tariff.get("channel_id")

        started_at = datetime.utcnow()
        ends_at = started_at + timedelta(minutes=lifetime_min)

        # --- Создаем запись подписки ---
        sub_id = str(uuid4())
        supabase.table("subscriptions").insert({
            "id": sub_id,
            "user_id": user_id,
            "tariff_id": tariff_id,
            "order_id": order_id,
            "started_at": started_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "status": "active",
            "created_at": started_at.isoformat()
            # "invoice_id": invoice_id
        }).execute()

        # --- Создаем invite ссылку для канала и отправляем ---
        if channel_id:
            try:
                # Получаем уже существующую или создаем новую ссылку
                invite = await bot.create_chat_invite_link(chat_id=channel_id, expire_date=None, member_limit=None)
                invite_link = invite.invite_link
                invite_msg = (
                    f"📢 Вы получили доступ к каналу по подписке: {tariff['title']}\n\n"
                    f"Ссылка для вступления: {invite_link}"
                    if lang == "ru" else
                    f"📢 You have been granted access to the channel for your subscription: {tariff['title']}\n\n"
                    f"Invite link: {invite_link}"
                )
                await bot.send_message(chat_id=user_id, text=invite_msg)
            except Exception as e:
                print(f"❌ Error creating or sending invite link for user {user_id}: {e}")

        return web.json_response({"ok": True})

    except Exception as e:
        print("❌ Webhook error:", e)
        return web.json_response({"ok": False, "error": str(e)}, status=500)



# --- APP SETUP ---
app = web.Application(middlewares=[logging_middleware])  # ✅ подключаем middleware
app["supabase"] = supabase

dp["base_url"] = WEBHOOK_URL
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")

# Регистрируем CryptoCloud Webhook вручную
app.router.add_post("/webhook/cryptocloud", crypto_webhook)
app.router.add_post("/webhook/tribute", lambda r: tribute_webhook_handler(r, bot))

app.on_startup.append(on_startup)

if __name__ == "__main__":
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
