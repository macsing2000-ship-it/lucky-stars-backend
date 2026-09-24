import os
import random
import logging
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types
 import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp, LabeledPrice, PreCheckoutQuery

import database

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

database.init_db()

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
STARSFLOW_TOKEN = os.getenv("STARSFLOW_TOKEN", "")

WEBAPP_URL = os.getenv("WEBAPP_URL", "https://elaborate-pie-a4fa50.netlify.app")
BASE_URL = os.getenv("BASE_URL", "https://lucky-stars-backend.onrender.com")
STAR_PRICE_RUB = 1.95  # Стоимость 1 звезды в рублях

ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "")
ADMIN_IDS = [int(i.strip()) for i in ADMIN_IDS_RAW.split(",") if i.strip().isdigit()]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ----------------- STARSFLOW AUTO-DELIVERY -----------------

async def send_stars_via_starsflow(recipient: str, amount: int) -> bool:
    if not STARSFLOW_TOKEN:
        logger.error("STARSFLOW_TOKEN не задан!")
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
                logger.info(f"✅ [StarsFlow] Отправлено {amount} ⭐ для @{clean_username}")
                return True
            else:
                logger.error(f"❌ [StarsFlow Error] {response.status_code}: {response.text}")
                return False
    except Exception as e:
        logger.error(f"❌ Ошибка соединения с StarsFlow: {e}")
        return False

# ----------------- КОМАНДЫ ДЛЯ ОБЫЧНЫХ ПОЛЬЗОВАТЕЛЕЙ -----------------

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
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

# ----------------- АДМИН-ПАНЕЛЬ И КОМАНДЫ -----------------

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    text = (
        "👑 <b>Панель администратора Lucky Buy</b>\n\n"
        "Доступные команды:\n"
        "📊 <code>/stats</code> — статистика проекта\n"
        "🔍 <code>/user @username</code> или <code>/user id</code> — профиль игрока\n"
        "➕ <code>/give @username сумма</code> — начислить баланс\n"
        "➖ <code>/take @username сумма</code> — списать баланс\n"
        "✏️ <code>/setbalance @username сумма</code> — установить точный баланс\n"
        "🎟 <code>/tickets @username количество</code> — выдать или изменить билеты (+5 или -2)"
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
        f"🏆 Количество побед (Звёзды): <b>{stats['total_wins']}</b>\n"
        f"🎟 Всего выдано билетов: <b>{stats.get('total_tickets', 0)} шт.</b>"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("user"))
async def user_info_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("⚠️ Формат: <code>/user @username</code> или <code>/user 123456789</code>", parse_mode="HTML")
        return

    target = database.find_user(parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден в базе данных.")
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
        await message.answer("❌ Пользователь не найден.")
        return

    try:
        amount = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("❌ Неверная сумма.")
        return

    database.update_user_balance(target["user_id"], amount)
    new_data = database.find_user(str(target["user_id"]))

    await message.answer(
        f"✅ Успешно начислено <b>+{amount:.2f} ₽</b> пользователю {target['username']}!\n"
        f"💰 Новый баланс: <b>{new_data['balance']:.2f} ₽</b>",
        parse_mode="HTML"
    )

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
        await message.answer("❌ Пользователь не найден.")
        return

    try:
        amount = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("❌ Неверная сумма.")
        return

    database.update_user_balance(target["user_id"], -amount)
    new_data = database.find_user(str(target["user_id"]))

    await message.answer(
        f"✅ Списано <b>-{amount:.2f} ₽</b> у пользователя {target['username']}.\n"
        f"💰 Текущий баланс: <b>{new_data['balance']:.2f} ₽</b>",
        parse_mode="HTML"
    )

@dp.message(Command("setbalance"))
async def set_balance_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("⚠️ Формат: <code>/setbalance @username сумма</code>", parse_mode="HTML")
        return

    target = database.find_user(parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return

    try:
        amount = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("❌ Неверная сумма.")
        return

    database.set_user_balance(target["user_id"], amount)
    await message.answer(
        f"✅ Баланс пользователя {target['username']} установлен на <b>{amount:.2f} ₽</b>.",
        parse_mode="HTML"
    )

@dp.message(Command("tickets"))
async def tickets_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("⚠️ Формат: <code>/tickets @username количество</code>", parse_mode="HTML")
        return

    target = database.find_user(parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return

    try:
        count = int(parts[2])
    except ValueError:
        await message.answer("❌ Количество должно быть целым числом.")
        return

    database.add_user_tickets(target["user_id"], count)
    new_data = database.find_user(str(target["user_id"]))
    await message.answer(
        f"✅ Обновлено количество билетов для {target['username']} на <b>{count:+d} 🎟</b>.\n"
        f"Всего билетов: <b>{new_data['tickets']} шт.</b>",
        parse_mode="HTML"
    )

# ----------------- ИСПРАВЛЕННАЯ ОБРАБОТКА STARS ПЛАТЕЖЕЙ -----------------

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    # Мгновенно подтверждаем транзакцию для Telegram
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    
    try:
        parts = payload.split("_")
        user_id = int(parts[1])
        stars_paid = int(parts[2])
        
        rub_amount = stars_paid * STAR_PRICE_RUB
        database.update_user_balance(user_id, rub_amount)
        new_data = database.find_user(str(user_id))

        await message.answer(
            f"🎉 <b>Оплата прошла успешно!</b>\n\n"
            f"💳 Ваш баланс пополнен на <b>+{rub_amount:.2f} ₽</b> (за {stars_paid} ⭐)\n"
            f"💰 Текущий баланс: <b>{new_data['balance']:.2f} ₽</b>\n\n"
            f"Удачи в игре! 🚀",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка зачисления Stars: {e}")

# ----------------- ЖИЗНЕННЫЙ ЦИКЛ ПРИЛОЖЕНИЯ -----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE":
        try:
            await bot.set_webhook(
                url=f"{BASE_URL}/webhook",
                allowed_updates=["message", "pre_checkout_query", "callback_query"],
                drop_pending_updates=True
            )
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Играть ⭐️", web_app=WebAppInfo(url=WEBAPP_URL))
            )
            logger.info("Telegram Webhook и Menu Button успешно зарегистрированы!")
        except Exception as e:
            logger.error(f"Ошибка при инициализации бота: {e}")
    yield
    if BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE":
        await bot.delete_webhook()
        await bot.session.close()

app = FastAPI(title="Lucky Stars API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

# ----------------- API ДЛЯ ВЕБ-ПРИЛОЖЕНИЯ -----------------

class InvoiceRequest(BaseModel):
    user_id: int
    stars: int

class SpinRequest(BaseModel):
    user_id: int
    username: str
    stars: int
    chance: int
    recipient: str

@app.get("/")
def home():
    return {"status": "running", "message": "Lucky Stars Backend Live!"}

@app.get("/api/user/{user_id}")
def get_user(user_id: int, username: str = ""):
    try:
        return database.get_or_create_user(user_id, username)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/invoice/create")
async def create_invoice(req: InvoiceRequest):
    try:
        title = f"Пополнение на {req.stars} ⭐"
        description = f"Пополнение баланса Lucky Buy на {req.stars} звёзд"
        payload = f"dep_{req.user_id}_{req.stars}"
        prices = [LabeledPrice(label="Telegram Stars", amount=req.stars)]

        link = await bot.create_invoice_link(
            title=title,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=prices
        )
        return {"invoice_link": link}
    except Exception as e:
        logger.error(f"Ошибка создания инвойса: {e}")
        raise HTTPException(status_code=500, detail="Не удалось создать счет")

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
    
    if is_win:
        status_str = "✅ Авто-доставка успешна!" if auto_sent else "⚠️ Требуется ручная отправка (проверьте баланс StarsFlow)"
        for admin_id in ADMIN_IDS:
            try:
                alert_text = (
                    "🔔 <b>ВНИМАНИЕ: НОВЫЙ ВЫИГРЫШ!</b>\n\n"
                    f"👤 Игрок: <b>{req.username}</b> (ID: <code>{req.user_id}</code>)\n"
                    f"🎁 Выигрыш: <b>{req.stars} ⭐ Звёзд</b>\n"
                    f"🎯 Шанс: <b>{req.chance}%</b> (оплачено: {cost:.2f} ₽)\n"
                    f"📤 <b>Кому выдать:</b> <code>{req.recipient}</code>\n"
                    f"⚡ <b>Статус авто-выдачи:</b> {status_str}"
                )
                await bot.send_message(admin_id, alert_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Не удалось отправить админу {admin_id}: {e}")

    updated_user = database.get_or_create_user(req.user_id)
    
    return {
        "success": True,
        "is_win": is_win,
        "reward_type": "stars" if is_win else "ticket",
        "stars_won": stars_won,
        "tickets_won": tickets_won,
        "auto_sent": auto_sent,
        "new_balance": round(updated_user["balance"], 2),
        "new_tickets": updated_user["tickets"],
        "cost_paid": round(cost, 2)
    }