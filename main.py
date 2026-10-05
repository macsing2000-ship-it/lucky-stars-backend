import os
import random
import logging
import asyncio
import httpx
import hmac
import hashlib
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp,
    LabeledPrice, PreCheckoutQuery, CallbackQuery
)

import database

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

try:
    database.init_db()
    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payout_requests (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username TEXT,
                    recipient TEXT,
                    stars INTEGER,
                    cost NUMERIC(10, 2),
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sbp_deposits (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username TEXT,
                    amount NUMERIC(10, 2),
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
except Exception as e:
    logger.error(f"Ошибка БД: {e}")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
STARSFLOW_TOKEN = os.getenv("STARSFLOW_TOKEN", "").strip()
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "").strip()

BASE_URL = "https://lucky-stars-backend.onrender.com"
WEBAPP_URL = "https://lucky-stars-backend.onrender.com"

STAR_PRICE_RUB = 1.95
STARS_DEPOSIT_RATE = 1.50
MAX_STARS_WIN = 1000

ADMIN_IDS = []
admin_raw = os.getenv("ADMIN_ID", "")
if admin_raw:
    for item in admin_raw.split(","):
        cleaned = item.strip()
        if cleaned.isdigit():
            ADMIN_IDS.append(int(cleaned))

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def send_message_safely(chat_id: int, text: str, reply_markup=None):
    if not bot: return
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception as e:
        logger.warning(f"Ошибка отправки {chat_id}: {e}")

async def send_stars_via_starsflow(recipient: str, amount: int) -> tuple:
    if not STARSFLOW_TOKEN: return False, "Токен StarsFlow не настроен"
    clean_username = recipient.replace("@", "").strip()
    url = "https://tgstars.tg/api/v1/client/orders/stars"
    headers = {"Authorization": f"Bearer {STARSFLOW_TOKEN}", "Content-Type": "application/json"}
    payload = {"username": clean_username, "quantity": amount}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=15.0)
            if response.status_code in (200, 201):
                return True, "Заказ принят StarsFlow"
            return False, f"Ошибка API: {response.status_code}"
    except Exception as e:
        return False, str(e)

@dp.message(Command("start"))
async def start_handler(message: types.Message, command: CommandObject = None):
    user_id = message.from_user.id
    uname = message.from_user.username or message.from_user.first_name or f"id{user_id}"
    referrer_id = None
    if command and command.args and command.args.startswith("ref_"):
        ref_str = command.args.replace("ref_", "").strip()
        if ref_str.isdigit() and int(ref_str) != user_id:
            referrer_id = int(ref_str)

    user = database.get_or_create_user(user_id, uname, referrer_id)
    
    text = (
        f"👋 <b>Добро пожаловать в сервис Lucky Stars!</b>\n\n"
        f"Здесь вы можете приобрести <b>Telegram Stars ⭐</b> и цифровые подарки через систему Mystery Box.\n\n"
        f"💰 Ваш баланс баллов: <b>{user['balance']:.2f} ₽</b>\n"
        f"🎟 Билеты: <b>{user['tickets']} шт.</b>\n\n"
        f"👥 <b>Партнерская программа:</b>\n"
        f"Приглашайте друзей и получайте <b>10% от их покупок</b> + <b>1 бонусный билет 🎟</b>!\n\n"
        f"👇 Нажмите кнопку ниже, чтобы открыть магазин:"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎁 Открыть магазин Stars", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

@dp.message(Command("rules"))
async def rules_handler(message: types.Message):
    text = (
        "⚖️ <b>Пользовательское соглашение и правила сервиса</b>\n\n"
        "1. Сервис является цифровой витриной Mystery Box. Мы предоставляем услуги по автоматизированной доставке цифрового контента (Telegram Stars и подарки).\n"
        "2. Баланс пользователя в приложении — это виртуальные бонусные баллы лояльности, используемые исключительно для оплаты услуг внутри сервиса. Они не являются электронными денежными средствами и не подлежат обмену на фиатную валюту.\n"
        "3. Любая транзакция является покупкой доступа к цифровому алгоритму распределения бонусов (Mystery Box).\n"
        "4. Услуга считается оказанной в полном объеме в момент списания баллов и формирования запроса на доставку контента.\n"
        "5. Пользуясь сервисом, вы подтверждаете свое совершеннолетие."
    )
    await message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data.startswith("sbp_"))
async def handle_sbp_action(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    parts = callback.data.split(":")
    action, dep_id = parts[0], int(parts[1])
    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, amount, status FROM sbp_deposits WHERE id = %s", (dep_id,))
            dep = cur.fetchone()
    if not dep or (dep[3] if isinstance(dep, tuple) else dep["status"]) != 'pending': return

    u_id = dep[1] if isinstance(dep, tuple) else dep["user_id"]
    amount = float(dep[2] if isinstance(dep, tuple) else dep["amount"])

    if action == "sbp_approve":
        database.update_user_balance(u_id, amount)
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE sbp_deposits SET status = 'approved' WHERE id = %s", (dep_id,))
                conn.commit()
        await callback.message.edit_text(f"✅ Пополнение на {amount} ₽ одобрено.")
        await send_message_safely(u_id, f"💳 <b>Пополнение успешно!</b>\nНа ваш баланс зачислено: <b>{amount:.2f} ₽</b>")
    else:
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE sbp_deposits SET status = 'declined' WHERE id = %s", (dep_id,))
                conn.commit()
        await callback.message.edit_text(f"❌ Пополнение на {amount} ₽ отклонено.")

@dp.callback_query(F.data.startswith("payout_"))
async def handle_payout_action(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    parts = callback.data.split(":")
    action, req_id = parts[0], int(parts[1])
    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, username, recipient, stars, cost, status FROM payout_requests WHERE id = %s", (req_id,))
            req = cur.fetchone()
    if not req or (req[6] if isinstance(req, tuple) else req["status"]) != 'pending': return

    u_id = req[1] if isinstance(req, tuple) else req["user_id"]
    recip = req[3] if isinstance(req, tuple) else req["recipient"]
    stars = req[4] if isinstance(req, tuple) else req["stars"]

    if action == "payout_sf":
        success, details = await send_stars_via_starsflow(recip, stars)
        if success:
            with database.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE payout_requests SET status = 'completed_sf' WHERE id = %s", (req_id,))
                    conn.commit()
            await callback.message.edit_text(f"✅ Выплачено {stars} ⭐ через StarsFlow")
            await send_message_safely(u_id, f"🎉 <b>Цифровой бонус доставлен!</b>\n{stars} ⭐ зачислены на @{recip.replace('@','')}")
        else:
            await callback.answer(f"Ошибка SF: {details}", show_alert=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if bot:
        await bot.set_webhook(url=f"{BASE_URL}/webhook", allowed_updates=["message", "callback_query", "pre_checkout_query"])
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Магазин ⭐️", web_app=WebAppInfo(url=WEBAPP_URL)))
    yield
    if bot: await bot.session.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/", response_class=HTMLResponse)
def serve_index():
    return FileResponse("index.html")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.post("/api/spin")
async def make_spin(req: dict):
    user = database.get_or_create_user(req['user_id'], req['username'])
    full_cost = req['stars'] * STAR_PRICE_RUB
    cost = (full_cost * (req['chance'] / 100.0)) * 1.05
    if user["balance"] < cost: raise HTTPException(status_code=400, detail="Недостаточно баллов")
    
    database.update_user_balance(req['user_id'], -cost)
    roll = random.uniform(0, 100)
    is_win = roll <= req['chance']
    
    ref_id, ref_bonus = database.reward_referrer_on_bet(req['user_id'], cost)
    if ref_id and ref_bonus > 0:
        asyncio.create_task(send_message_safely(ref_id, f"💰 <b>Партнерский бонус!</b>\nЗачислено: <b>+{ref_bonus:.2f} ₽</b>"))

    new_tickets = user["tickets"]
    if is_win:
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO payout_requests (user_id, username, recipient, stars, cost) VALUES (%s, %s, %s, %s, %s)",
                           (req['user_id'], req['username'], req['recipient'], req['stars'], cost))
                conn.commit()
    else:
        database.add_user_tickets(req['user_id'], 1)
        new_tickets += 1

    return {"is_win": is_win, "new_balance": round(user["balance"] - cost, 2), "new_tickets": new_tickets}

@app.post("/api/deposit/sbp")
async def sbp_dep(req: dict):
    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sbp_deposits (user_id, username, amount) VALUES (%s, %s, %s) RETURNING id",
                       (req['user_id'], req['username'], req['amount']))
            row = cur.fetchone()
            conn.commit()
    for aid in ADMIN_IDS:
        await send_message_safely(aid, f"💳 <b>Заявка СБП!</b>\nСумма: {req['amount']} ₽\nОт: @{req['username']}", 
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Одобрить", callback_data=f"sbp_approve:{row[0]}")]]))
    return {"success": True}

@app.post("/api/deposit/stars-invoice")
async def stars_dep(req: dict):
    link = await bot.create_invoice_link(title=f"Баллы: {req['stars']} ⭐", description="Пополнение баланса", 
                                        payload=f"stars_{req['stars']}", currency="XTR", prices=[LabeledPrice(label="Stars", amount=req['stars'])])
    return {"invoice_link": link}

@app.post("/api/deposit/cryptobot")
async def crypto_dep(req: dict):
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    payload = {"currency_type": "fiat", "fiat": "RUB", "amount": req['amount_rub'], "payload": str(req['user_id'])}
    async with httpx.AsyncClient() as client:
        res = await client.post(url, json=payload, headers=headers)
        return {"pay_url": res.json()["result"]["pay_url"]}
