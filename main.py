import os
import random
import logging
import httpx
import hashlib
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp

import database

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализируем базу данных
try:
    database.init_db()
except Exception as e:
    logger.error(f"Ошибка БД: {e}")

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
STARSFLOW_TOKEN = os.getenv("STARSFLOW_TOKEN", "").strip()
WEBAPP_URL = "https://elaborate-pie-a4fa50.netlify.app"
BASE_URL = "https://lucky-stars-backend.onrender.com"
STAR_PRICE_RUB = 1.95

# Ключи CrystalPay
CRYSTALPAY_LOGIN = os.getenv("PAYMENT_SHOP_ID", "").strip()       # Shop Login в CrystalPay
CRYSTALPAY_SECRET = os.getenv("PAYMENT_SECRET_KEY", "").strip()   # Secret Key 1 в CrystalPay

# ID администратора(ов)
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

# ----------------- АВТО-ОТПРАВКА ЗВЁЗД ЧЕРЕЗ STARSFLOW API -----------------

async def send_stars_via_starsflow(recipient: str, amount: int) -> bool:
    if not STARSFLOW_TOKEN:
        logger.warning("STARSFLOW_TOKEN не задан. Пропускаем авто-выдачу.")
        return False

    clean_username = recipient.replace("@", "").strip()
    if not clean_username:
        return False

    url = "https://tgstars.tg/api/v1/orders/create"
    headers = {
        "Authorization": f"Bearer {STARSFLOW_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "username": clean_username,
        "amount": amount,
        "service": "stars"
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=12.0)
            if response.status_code == 200:
                logger.info(f"✅ [StarsFlow] Успешно отправлено {amount} ⭐ пользователю @{clean_username}")
                return True
            else:
                logger.error(f"❌ [StarsFlow API Error]: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"❌ Ошибка соединения с StarsFlow API: {e}")
        return False


# ----------------- КОМАНДЫ ДЛЯ ОБЫЧНЫХ ПОЛЬЗОВАТЕЛЕЙ -----------------

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    uname = message.from_user.username or message.from_user.first_name or f"id{user_id}"
    
    user = database.get_or_create_user(user_id, uname)

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


# ----------------- АДМИН-ПАНЕЛЬ И КОМАНДЫ -----------------

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    text = (
        "👑 <b>Панель администратора Lucky Buy</b>\n\n"
        "Доступные команды:\n"
        "📊 <code>/stats</code> — статистика проекта\n"
        "🔍 <code>/user @username</code> или <code>/user id</code> — инфо о пользователе\n"
        "➕ <code>/give @username сумма</code> — начислить баланс\n"
        "➖ <code>/take @username сумма</code> — списать баланс\n"
        "✏️ <code>/setbalance @username сумма</code> — установить точный баланс\n"
        "🎟 <code>/tickets @username количество</code> — выдать билеты"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("stats"))
async def stats_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    stats = database.get_stats()
    text = (
        "📈 <b>Статистика проекта:</b>\n\n"
        f"👥 Всего пользователей: <b>{stats['total_users']}</b>\n"
        f"🎲 Всего игр сыграно: <b>{stats['total_games']}</b>\n"
        f"💸 Общий оборот ставок: <b>{stats['total_turnover']} ₽</b>\n"
        f"🏆 Побед (Звёзды): <b>{stats['total_wins']}</b>\n"
        f"🎟 Билетов выдано: <b>{stats.get('total_tickets', 0)} шт.</b>"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("user"))
async def user_info_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("⚠️ Формат: <code>/user @username</code>", parse_mode="HTML")
        return

    target = database.find_user(parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден в базе данных.", parse_mode="HTML")
        return

    text = (
        f"👤 <b>Профиль пользователя:</b>\n\n"
        f"🆔 ID: <code>{target['user_id']}</code>\n"
        f"Имя / Юзернейм: <b>{target['username']}</b>\n"
        f"💰 Баланс: <b>{target['balance']:.2f} ₽</b>\n"
        f"🎟 Билеты: <b>{target['tickets']} шт.</b>\n"
        f"📅 Зарегистрирован: {target['created_at']}"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("give"))
async def give_balance_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("⚠️ Формат: <code>/give @username сумма</code>", parse_mode="HTML")
        return

    target = database.find_user(parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден в базе.", parse_mode="HTML")
        return

    try:
        amount = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("❌ Неверная сумма.", parse_mode="HTML")
        return

    database.update_user_balance(target["user_id"], amount)
    new_data = database.find_user(str(target["user_id"]))

    await message.answer(
        f"✅ Начислено <b>+{amount:.2f} ₽</b> пользователю {target['username']}!\n"
        f"💰 Новый баланс: <b>{new_data['balance']:.2f} ₽</b>",
        parse_mode="HTML"
    )

    if bot:
        try:
            await bot.send_message(
                target["user_id"],
                f"💳 <b>Ваш баланс пополнен на +{amount:.2f} ₽!</b>\n"
                f"Текущий баланс: <b>{new_data['balance']:.2f} ₽</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass

@dp.message(Command("take"))
async def take_balance_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("⚠️ Формат: <code>/take @username сумма</code>", parse_mode="HTML")
        return

    target = database.find_user(parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден.", parse_mode="HTML")
        return

    try:
        amount = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("❌ Неверная сумма.", parse_mode="HTML")
        return

    database.update_user_balance(target["user_id"], -amount)
    new_data = database.find_user(str(target["user_id"]))

    await message.answer(
        f"✅ Списано <b>-{amount:.2f} ₽</b> у {target['username']}.\n"
        f"💰 Баланс: <b>{new_data['balance']:.2f} ₽</b>",
        parse_mode="HTML"
    )

# ----------------- ЖИЗНЕННЫЙ ЦИКЛ -----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if bot:
        try:
            await bot.set_webhook(
                url=f"{BASE_URL}/webhook", 
                drop_pending_updates=False,
                allowed_updates=["message", "callback_query"]
            )
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Играть ⭐️",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            )
            logger.info("Telegram Webhook активен!")
        except Exception as e:
            logger.error(f"Ошибка вебхука: {e}")
    yield
    if bot:
        await bot.session.close()

app = FastAPI(title="Lucky Stars API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/webhook")
def webhook_check():
    return {"status": "active"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    if not bot:
        return {"ok": False, "error": "Bot token not configured"}
    try:
        update = types.Update.model_validate(await request.json(), context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Ошибка апдейта: {e}")
    return {"ok": True}


# ----------------- ПЛАТЕЖИ: CRYSTALPAY INTEGRATION -----------------

class DepositRequest(BaseModel):
    user_id: int
    username: str
    amount: float

@app.post("/api/deposit/create")
async def create_deposit(req: DepositRequest):
    """Создание счета в CrystalPay"""
    if not CRYSTALPAY_LOGIN or not CRYSTALPAY_SECRET:
        raise HTTPException(status_code=500, detail="Платежная система CrystalPay не настроена на сервере.")
    
    if req.amount < 10.0:
        raise HTTPException(status_code=400, detail="Минимальная сумма пополнения — 10 ₽")

    url = "https://api.crystalpay.io/v2/invoice/create/"
    payload = {
        "auth_login": CRYSTALPAY_LOGIN,
        "auth_secret": CRYSTALPAY_SECRET,
        "amount": req.amount,
        "type": "topup",
        "lifetime": 15,
        "extra": str(req.user_id), # сохраняем id пользователя, чтобы узнать его при оплате
        "callback_url": f"{BASE_URL}/api/payment/callback"
    }

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, timeout=10.0)
            data = res.json()
            
            if data.get("error") == False:
                return {"url": data.get("url"), "id": data.get("id")}
            else:
                logger.error(f"Ошибка CrystalPay API: {data}")
                raise HTTPException(status_code=500, detail=f"Ошибка платежки: {data.get('errors')}")
    except Exception as e:
        logger.error(f"Ошибка создания счета: {e}")
        raise HTTPException(status_code=500, detail="Не удалось связаться с CrystalPay")


@app.post("/api/payment/callback")
async def crystalpay_callback(request: Request):
    """Вебхук от CrystalPay об успешной оплате"""
    try:
        data = await request.json()
        
        # Получаем параметры
        invoice_id = data.get("id")
        state = data.get("state")
        amount = float(data.get("amount", 0))
        user_id_str = data.get("extra")
        received_signature = data.get("signature")
        
        if not invoice_id or state != "payed" or not user_id_str:
            return {"ok": True}

        # Верификация подписи CrystalPay
        # Сигнатура строится как md5(id + extra + state + amount + secret_key)
        # ВНИМАНИЕ: amount должен быть в том формате, как прислал CrystalPay (обычно целое или float)
        # Для безопасности проверяем наличие подписи
        if received_signature:
            sign_str = f"{invoice_id}:{user_id_str}:{state}:{data.get('amount')}:{CRYSTALPAY_SECRET}"
            expected_signature = hashlib.md5(sign_str.encode('utf-8')).hexdigest()
            
            # Если подписи не сходятся — это фейковый запрос
            if received_signature != expected_signature:
                logger.error("🛑 [CrystalPay] Неверная подпись вебхука!")
                raise HTTPException(status_code=400, detail="Invalid signature")

        user_id = int(user_id_str)
        
        # Начисляем рубли в базу данных
        database.update_user_balance(user_id, amount)
        logger.info(f"💰 [CrystalPay] Баланс пользователя {user_id} успешно пополнен на +{amount} ₽")
        
        # Уведомляем пользователя через бота
        if bot:
            try:
                user_info = database.get_or_create_user(user_id)
                text = (
                    f"💳 <b>Баланс успешно пополнен!</b>\n\n"
                    f"Сумма зачисления: <b>{amount:.2f} ₽</b>\n"
                    f"Текущий баланс в рулетке: <b>{user_info['balance']:.2f} ₽</b>"
                )
                await bot.send_message(user_id, text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение об оплате пользователю {user_id}: {e}")

        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка обработки коллбэка платежа: {e}")
        return {"ok": True}


# ----------------- API ДЛЯ ВЕБ-ПРИЛОЖЕНИЯ -----------------

class SpinRequest(BaseModel):
    user_id: int
    username: str
    stars: int
    chance: int
    recipient: str

@app.get("/")
def home():
    return {"status": "running", "message": "Lucky Stars Live!"}

@app.get("/api/user/{user_id}")
def get_user(user_id: int, username: str = ""):
    try:
        user_data = database.get_or_create_user(user_id, username)
        return user_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/spin")
async def make_spin(req: SpinRequest):
    user = database.get_or_create_user(req.user_id, req.username)
    
    full_cost = req.stars * STAR_PRICE_RUB
    cost = (full_cost * (req.chance / 100.0)) * 1.05
    
    if user["balance"] < cost:
        raise HTTPException(status_code=400, detail="Недостаточно средств на балансе")
    
    database.update_user_balance(req.user_id, -cost)
    
    roll = random.uniform(0, 100)
    is_win = roll <= req.chance
    
    tickets_won = 0
    auto_sent = False
    
    if is_win:
        stars_won = req.stars
        auto_sent = await send_stars_via_starsflow(req.recipient, stars_won)
    else:
        stars_won = 0
        tickets_won = 1
        database.add_user_tickets(req.user_id, 1)

    database.log_game(req.user_id, req.username, req.stars, req.chance, cost, is_win, req.recipient, tickets_won)
    
    if is_win and bot:
        status_str = "✅ Авто-доставка успешна!" if auto_sent else "⚠️ Не удалось отправить автоматически (проверьте баланс StarsFlow)"
        for admin_id in ADMIN_IDS:
            try:
                alert_text = (
                    "🔔 <b>ВНИМАНИЕ: НОВЫЙ ВЫИГРЫШ!</b>\n\n"
                    f"👤 Игрок: <b>{req.username}</b> (ID: <code>{req.user_id}</code>)\n"
                    f"🎁 Выигрыш: <b>{req.stars} ⭐ Звёзд</b>\n"
                    f"🎯 Шанс: <b>{req.chance}%</b> (оплачено: {cost:.2f} ₽)\n"
                    f"📤 <b>Кому выдать:</b> <code>{req.recipient}</code>\n"
                    f"⚡ <b>Авто-выдача:</b> {status_str}"
                )
                await bot.send_message(admin_id, alert_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Ошибка уведомления админа: {e}")

    updated_user = database.get_or_create_user(req.user_id)
    
    return {
        "success": True,
        "is_win": is_win,
        "reward_type": "stars" if is_win else "ticket",
        "stars_won": stars_won,
        "tickets_won": tickets_won,
        "new_balance": round(updated_user["balance"], 2),
        "new_tickets": updated_user["tickets"],
        "cost_paid": round(cost, 2)
    }
