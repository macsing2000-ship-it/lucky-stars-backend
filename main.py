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

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = "https://monumental-stroopwafel-a20c3f.netlify.app"
BASE_URL = "https://lucky-stars-backend.onrender.com"
STAR_PRICE_RUB = 1.95  # Стоимость 1 звезды

# ID администратора(ов) через запятую (например: "12345678,98765432")
ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "")
ADMIN_IDS = [int(i.strip()) for i in ADMIN_IDS_RAW.split(",") if i.strip().isdigit()]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь администратором."""
    return user_id in ADMIN_IDS

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
        "📊 <code>/stats</code> — статистика проекта (игроки, оборот, выигрыши)\n"
        "🔍 <code>/user @username</code> или <code>/user id</code> — инфо о пользователе\n"
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
        await message.answer("⚠️ Формат: <code>/give @username сумма</code> (например: <code>/give @durov 500</code>)", parse_mode="HTML")
        return

    target = database.find_user(parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден в базе данных.")
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
        await message.answer("⚠️ Формат: <code>/tickets @username количество</code> (например: <code>/tickets @durov 5</code>)", parse_mode="HTML")
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


# ----------------- ЖИЗНЕННЫЙ ЦИКЛ ПРИЛОЖЕНИЯ -----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if BOT_TOKEN and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE":
        try:
            await bot.set_webhook(url=f"{BASE_URL}/webhook", drop_pending_updates=True)
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Играть ⭐️",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            )
            print("Telegram Webhook и Menu Button зарегистрированы!")
        except Exception as e:
            print(f"Ошибка при инициализации бота: {e}")
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
    
    # Списание стоимости броска
    database.update_user_balance(req.user_id, -cost)
    
    # Розыгрыш шанса
    roll = random.uniform(0, 100)
    is_win = roll <= req.chance
    
    # ЛОГИКА НАЧИСЛЕНИЯ ПРИЗОВ:
    # 1. Если победил шанс — выигрыш Звёзд (stars)
    # 2. Если не выиграл Звёзды — стрелка останавливается на билете, игроку начисляется +1 билет!
    tickets_won = 0
    if is_win:
        stars_won = req.stars
    else:
        stars_won = 0
        tickets_won = 1
        database.add_user_tickets(req.user_id, 1)

    database.log_game(req.user_id, req.username, req.stars, req.chance, cost, is_win, req.recipient, tickets_won)
    
    # Уведомление администратора в случае выигрыша звёзд
    if is_win:
        for admin_id in ADMIN_IDS:
            try:
                alert_text = (
                    "🔔 <b>ВНИМАНИЕ: НОВЫЙ ВЫИГРЫШ!</b>\n\n"
                    f"👤 Игрок: <b>{req.username}</b> (ID: <code>{req.user_id}</code>)\n"
                    f"🎁 Выигрыш: <b>{req.stars} ⭐ Звёзд</b>\n"
                    f"🎯 Шанс: <b>{req.chance}%</b> (оплачено: {cost:.2f} ₽)\n"
                    f"📤 <b>Кому выдать звёзды:</b> <code>{req.recipient}</code>\n\n"
                    f"<i>Отправьте звёзды подарком на указанный юзернейм!</i>"
                )
                await bot.send_message(admin_id, alert_text, parse_mode="HTML")
            except Exception as e:
                print(f"Не удалось отправить уведомление админу {admin_id}: {e}")

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
