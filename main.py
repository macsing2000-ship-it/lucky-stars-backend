import os
import random
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp

import database

# Инициализируем базу данных
database.init_db()

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = "https://monumental-stroopwafel-a20c3f.netlify.app"
BASE_URL = "https://lucky-stars-backend.onrender.com"
STAR_PRICE_RUB = 1.95  # Стоимость 1 звезды

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Обработчик команды /start в чате бота
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
    # Регистрируем пользователя в БД (если еще нет)
    user = database.get_or_create_user(user_id, username)

    text = (
        f"👋 <b>Добро пожаловать в Lucky Buy!</b>\n\n"
        f"Здесь ты можешь выиграть и забрать <b>Telegram Stars ⭐</b> с повышенным шансом!\n\n"
        f"💰 Твой баланс: <b>{user['balance']:.2f} ₽</b>\n"
        f"🎟 Билеты: <b>{user['tickets']} шт.</b>\n\n"
        f"👇 Нажми на кнопку ниже, чтобы открыть приложение:"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Открыть Lucky Buy",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            ]
        ]
    )

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


# Настройка жизненного цикла FastAPI (при старте сервера регистрируем вебхук и кнопку меню)
@asynccontextmanager
async def lifespan(app: FastAPI):
    if BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE":
        try:
            # Устанавливаем Webhook для получения сообщений от Telegram
            await bot.set_webhook(url=f"{BASE_URL}/webhook", drop_pending_updates=True)
            
            # Устанавливаем кнопку меню в левом нижнем углу чата (Menu Button)
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Играть ⭐️",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            )
            print("Telegram Webhook и Menu Button успешно зарегистрированы!")
        except Exception as e:
            print(f"Ошибка при инициализации бота: {e}")
    yield
    if BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE":
        await bot.delete_webhook()
        await bot.session.close()


app = FastAPI(title="Lucky Stars API", lifespan=lifespan)

# CORS для фронтенда на Netlify
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Эндпоинт для приема сообщений от Telegram
@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


# API ДЛЯ ФРОНТЕНДА
class SpinRequest(BaseModel):
    user_id: int
    username: str
    stars: int
    chance: int
    recipient: str

@app.get("/")
def home():
    return {"status": "running", "message": "Lucky Stars Backend & Bot are Live!"}

@app.get("/api/user/{user_id}")
def get_user(user_id: int, username: str = ""):
    try:
        user_data = database.get_or_create_user(user_id, username)
        return user_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/spin")
def make_spin(req: SpinRequest):
    user = database.get_or_create_user(req.user_id, req.username)
    
    full_cost = req.stars * STAR_PRICE_RUB
    cost = (full_cost * (req.chance / 100.0)) * 1.05
    
    if user["balance"] < cost:
        raise HTTPException(status_code=400, detail="Недостаточно средств на балансе")
    
    database.update_user_balance(req.user_id, -cost)
    
    roll = random.uniform(0, 100)
    is_win = roll <= req.chance
    
    database.log_game(req.user_id, req.stars, req.chance, cost, is_win)
    
    updated_user = database.get_or_create_user(req.user_id)
    
    return {
        "success": True,
        "is_win": is_win,
        "new_balance": round(updated_user["balance"], 2),
        "stars_won": req.stars if is_win else 0,
        "cost_paid": round(cost, 2)
    }
