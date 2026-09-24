import os
import random
import logging
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp, LabeledPrice, PreCheckoutQuery

import database

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация базы данных
database.init_db()

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
STARSFLOW_TOKEN = os.getenv("STARSFLOW_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://elaborate-pie-a4fa50.netlify.app")
BASE_URL = os.getenv("BASE_URL", "https://lucky-stars-backend.onrender.com")
STAR_PRICE_RUB = 1.95  # Курс: 1 звезда = 1.95 рубля на баланс

ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "")
ADMIN_IDS = [int(i.strip()) for i in ADMIN_IDS_RAW.split(",") if i.strip().isdigit()]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def send_stars_via_starsflow(recipient: str, amount: int) -> bool:
    if not STARSFLOW_TOKEN: return False
    clean_username = recipient.replace("@", "").strip()
    url = "https://tgstars.tg/api/v1/orders/create"
    headers = {"Authorization": f"Bearer {STARSFLOW_TOKEN}", "Content-Type": "application/json"}
    payload = {"username": clean_username, "amount": amount, "service": "stars"}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=12.0)
            return response.status_code == 200
    except: return False

# --- ОБРАБОТКА КОМАНД И ПЛАТЕЖЕЙ В ТЕЛЕГРАМ ---

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    database.get_or_create_user(user_id, username)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Играть в Lucky Buy", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await message.answer("🎁 <b>Добро пожаловать!</b>\nПополняйте баланс и выигрывайте Звёзды!", parse_mode="HTML", reply_markup=keyboard)

# Подтверждение платежа (обязательно для Telegram)
@dp.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)

# Зачисление после успешной оплаты
@dp.message(lambda m: m.successful_payment is not None)
async def success_pay(message: types.Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("dep_"):
        _, uid, stars = payload.split("_")
        amount_rub = int(stars) * STAR_PRICE_RUB
        database.update_user_balance(int(uid), amount_rub)
        await message.answer(f"✅ Баланс пополнен на {amount_rub:.2f} ₽ ({stars} ⭐)")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot.set_webhook(url=f"{BASE_URL}/webhook", drop_pending_updates=True)
    yield
    await bot.delete_webhook()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.post("/webhook")
async def webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

# --- API ДЛЯ МИНИ-ПРИЛОЖЕНИЯ ---

class InvReq(BaseModel): user_id: int; stars: int
class SpinReq(BaseModel): user_id: int; username: str; stars: int; chance: int; recipient: str

@app.get("/api/user/{user_id}")
def get_user(user_id: int, username: str = ""):
    return database.get_or_create_user(user_id, username)

@app.post("/api/invoice/create")
async def create_invoice(req: InvReq):
    link = await bot.create_invoice_link(
        title=f"Пополнение {req.stars} ⭐",
        description="Зачисление на игровой баланс",
        payload=f"dep_{req.user_id}_{req.stars}",
        provider_token="", currency="XTR",
        prices=[LabeledPrice(label="Stars", amount=req.stars)]
    )
    return {"invoice_link": link}

@app.post("/api/spin")
async def spin(req: SpinReq):
    user = database.get_or_create_user(req.user_id, req.username)
    cost = (req.stars * STAR_PRICE_RUB * (req.chance / 100.0)) * 1.05
    if user["balance"] < cost: raise HTTPException(status_code=400, detail="Нет денег")
    
    database.update_user_balance(req.user_id, -cost)
    is_win = random.uniform(0, 100) <= req.chance
    auto_sent = False
    
    if is_win:
        auto_sent = await send_stars_via_starsflow(req.recipient, req.stars)
    else:
        database.add_user_tickets(req.user_id, 1)

    database.log_game(req.user_id, req.username, req.stars, req.chance, cost, is_win, req.recipient, 0 if is_win else 1)
    updated = database.get_or_create_user(req.user_id)
    return {
        "success": True, "is_win": is_win, "stars_won": req.stars if is_win else 0,
        "new_balance": round(updated["balance"], 2), "new_tickets": updated["tickets"]
    }
