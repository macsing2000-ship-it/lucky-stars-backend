import os
import random
import logging
import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import parse_qsl

import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    MenuButtonWebApp,
)

import database

# ----------------- НАСТРОЙКИ -----------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

database.init_db()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
STARSFLOW_TOKEN = os.getenv("STARSFLOW_TOKEN", "")
WEBAPP_URL = os.getenv(
    "WEBAPP_URL",
    "https://elaborate-pie-a4fa50.netlify.app",
)
BASE_URL = os.getenv(
    "BASE_URL",
    "https://lucky-stars-backend.onrender.com",
)

# Разрешённые origin'ы через запятую.
# Например:
# ALLOWED_ORIGINS=https://elaborate-pie-a4fa50.netlify.app
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", WEBAPP_URL).split(",")
    if origin.strip()
]

# Цена одной звезды.
STAR_PRICE_RUB = Decimal("1.95")

# Ограничения игры. При необходимости поменяй под свою механику.
MIN_STARS = 1
MAX_STARS = 10000
MIN_CHANCE = 1
MAX_CHANCE = 100

# Максимальная длина username/recipient.
MAX_USERNAME_LENGTH = 64

ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "")
ADMIN_IDS = [
    int(i.strip())
    for i in ADMIN_IDS_RAW.split(",")
    if i.strip().isdigit()
]

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN не задан.")
if not STARSFLOW_TOKEN:
    logger.warning("STARSFLOW_TOKEN не задан.")

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

# Простейшая защита от параллельных spin-запросов одного пользователя.
_spin_locks: dict[int, asyncio.Lock] = {}


def get_spin_lock(user_id: int) -> asyncio.Lock:
    lock = _spin_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _spin_locks[user_id] = lock
    return lock


def money(value: Decimal) -> Decimal:
    """Округление денег до копеек."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ----------------- TELEGRAM WEB APP AUTH -----------------

def validate_telegram_init_data(init_data: str, bot_token: str) -> dict:
    """
    Проверяет Telegram WebApp initData по официальной схеме:
    secret_key = HMAC_SHA256("WebAppData", bot_token)
    data_check_string = отсортированные пары без hash
    hash = HMAC_SHA256(secret_key, data_check_string)
    """
    if not init_data:
        raise HTTPException(
            status_code=401,
            detail="Telegram initData не передан",
        )

    if not bot_token:
        raise HTTPException(
            status_code=500,
            detail="BOT_TOKEN не настроен на сервере",
        )

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Некорректный Telegram initData",
        )

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(
            status_code=401,
            detail="В initData отсутствует hash",
        )

    data_check_string = "\n".join(
        f"{key}={pairs[key]}"
        for key in sorted(pairs.keys())
    )

    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(
            status_code=401,
            detail="Недействительный Telegram initData",
        )

    # Защита от старого initData.
    auth_date_raw = pairs.get("auth_date")
    try:
        auth_date = int(auth_date_raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=401,
            detail="Некорректный auth_date",
        )

    max_age = int(os.getenv("TELEGRAM_INIT_DATA_MAX_AGE", "86400"))
    if abs(time.time() - auth_date) > max_age:
        raise HTTPException(
            status_code=401,
            detail="Telegram initData устарел",
        )

    user_raw = pairs.get("user")
    if not user_raw:
        raise HTTPException(
            status_code=401,
            detail="В initData отсутствует пользователь Telegram",
        )

    try:
        telegram_user = json.loads(user_raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=401,
            detail="Некорректные данные пользователя Telegram",
        )

    if not isinstance(telegram_user, dict) or not telegram_user.get("id"):
        raise HTTPException(
            status_code=401,
            detail="Некорректный пользователь Telegram",
        )

    return telegram_user


def username_for_database(telegram_user: dict) -> str:
    username = telegram_user.get("username")
    if username:
        return f"@{username}"

    return telegram_user.get("first_name") or str(telegram_user["id"])


# ----------------- STARSFLOW -----------------

async def send_stars_via_starsflow(recipient: str, amount: int) -> bool:
    if not STARSFLOW_TOKEN:
        logger.error("STARSFLOW_TOKEN не задан.")
        return False

    clean_username = recipient.replace("@", "").strip()

    if not clean_username:
        logger.error("Передан пустой username для отправки Stars.")
        return False

    if len(clean_username) > MAX_USERNAME_LENGTH:
        logger.error("Слишком длинный username получателя.")
        return False

    if amount < MIN_STARS or amount > MAX_STARS:
        logger.error("Некорректное количество Stars: %s", amount)
        return False

    url = "https://tgstars.tg/api/v1/orders/create"
    headers = {
        "Authorization": f"Bearer {STARSFLOW_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "username": clean_username,
        "amount": amount,
        "service": "stars",
    }

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
            )

        if response.status_code < 200 or response.status_code >= 300:
            logger.error(
                "StarsFlow HTTP error: %s - %s",
                response.status_code,
                response.text[:1000],
            )
            return False

        # Не считаем HTTP 2xx гарантией успеха, если API вернуло JSON
        # с явным success=false/error.
        try:
            data = response.json()
        except ValueError:
            data = None

        if isinstance(data, dict):
            if data.get("success") is False:
                logger.error("StarsFlow API вернуло success=false: %s", data)
                return False

            status = str(data.get("status", "")).lower()
            if status in {"failed", "error", "cancelled", "canceled"}:
                logger.error("StarsFlow API вернуло status=%s: %s", status, data)
                return False

        logger.info(
            "[StarsFlow] Успешно создана отправка %s ⭐ пользователю @%s",
            amount,
            clean_username,
        )
        return True

    except httpx.TimeoutException:
        logger.error("Таймаут при обращении к StarsFlow API.")
        return False
    except httpx.HTTPError as exc:
        logger.error("HTTP ошибка StarsFlow API: %s", exc)
        return False
    except Exception:
        logger.exception("Неожиданная ошибка StarsFlow API.")
        return False


# ----------------- КОМАНДЫ ПОЛЬЗОВАТЕЛЕЙ -----------------

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else message.from_user.first_name
    )

    user = database.get_or_create_user(user_id, username)

    text = (
        "👋 <b>Добро пожаловать в Lucky Buy!</b>\n\n"
        "Здесь ты можешь выиграть и забрать "
        "<b>Telegram Stars ⭐</b> с повышенным шансом!\n\n"
        f"💰 Твой баланс: <b>{user['balance']:.2f} ₽</b>\n"
        f"🎟 Билеты: <b>{user['tickets']} шт.</b>\n\n"
        "👇 Нажми на кнопку ниже, чтобы открыть приложение:"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Открыть Lucky Buy",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            ]
        ]
    )

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ----------------- АДМИН-ПАНЕЛЬ -----------------

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    text = (
        "👑 <b>Панель администратора Lucky Buy</b>\n\n"
        "Доступные команды:\n"
        "📊 <code>/stats</code> — статистика проекта\n"
        "🔍 <code>/user @username</code> или <code>/user id</code> — пользователь\n"
        "➕ <code>/give @username сумма</code> — начислить баланс\n"
        "➖ <code>/take @username сумма</code> — списать баланс\n"
        "✏️ <code>/setbalance @username сумма</code> — установить баланс\n"
        "🎟 <code>/tickets @username количество</code> — изменить билеты"
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
        await message.answer(
            "⚠️ Формат: <code>/user @username</code> или "
            "<code>/user 123456789</code>",
            parse_mode="HTML",
        )
        return

    target = database.find_user(parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден в базе данных.")
        return

    text = (
        "👤 <b>Профиль пользователя:</b>\n\n"
        f"🆔 ID: <code>{target['user_id']}</code>\n"
        f"Имя / Юзернейм: <b>{target['username']}</b>\n"
        f"💰 Баланс: <b>{target['balance']:.2f} ₽</b>\n"
        f"🎟 Билеты: <b>{target['tickets']} шт.</b>\n"
        f"📅 Зарегистрирован: {target['created_at']}"
    )

    await message.answer(text, parse_mode="HTML")


def parse_positive_money(raw: str) -> Decimal:
    try:
        value = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        raise ValueError("Неверная сумма")

    if not value.is_finite() or value <= 0:
        raise ValueError("Сумма должна быть положительной")

    return money(value)


@dp.message(Command("give"))
async def give_balance_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "⚠️ Формат: <code>/give @username сумма</code>",
            parse_mode="HTML",
        )
        return

    target = database.find_user(parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден в базе данных.")
        return

    try:
        amount = parse_positive_money(parts[2])
    except ValueError:
        await message.answer("❌ Неверная сумма. Она должна быть больше 0.")
        return

    database.update_user_balance(target["user_id"], float(amount))
    new_data = database.find_user(str(target["user_id"]))

    await message.answer(
        f"✅ Успешно начислено <b>+{amount:.2f} ₽</b> "
        f"пользователю {target['username']}!\n"
        f"💰 Новый баланс: <b>{new_data['balance']:.2f} ₽</b>",
        parse_mode="HTML",
    )

    if bot:
        try:
            await bot.send_message(
                target["user_id"],
                f"💳 <b>Ваш баланс пополнен на +{amount:.2f} ₽!</b>\n"
                f"Текущий баланс: <b>{new_data['balance']:.2f} ₽</b>",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Не удалось уведомить пользователя %s",
                target["user_id"],
            )


@dp.message(Command("take"))
async def take_balance_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "⚠️ Формат: <code>/take @username сумма</code>",
            parse_mode="HTML",
        )
        return

    target = database.find_user(parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return

    try:
        amount = parse_positive_money(parts[2])
    except ValueError:
        await message.answer("❌ Неверная сумма. Она должна быть больше 0.")
        return

    if Decimal(str(target["balance"])) < amount:
        await message.answer("❌ Недостаточно средств у пользователя.")
        return

    database.update_user_balance(target["user_id"], -float(amount))
    new_data = database.find_user(str(target["user_id"]))

    await message.answer(
        f"✅ Списано <b>-{amount:.2f} ₽</b> у пользователя "
        f"{target['username']}.\n"
        f"💰 Текущий баланс: <b>{new_data['balance']:.2f} ₽</b>",
        parse_mode="HTML",
    )


@dp.message(Command("setbalance"))
async def set_balance_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "⚠️ Формат: <code>/setbalance @username сумма</code>",
            parse_mode="HTML",
        )
        return

    target = database.find_user(parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден.")
        return

    try:
        amount = Decimal(parts[2].replace(",", "."))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        amount = money(amount)
    except (InvalidOperation, ValueError):
        await message.answer("❌ Неверная сумма.")
        return

    database.set_user_balance(target["user_id"], float(amount))

    await message.answer(
        f"✅ Баланс пользователя {target['username']} "
        f"установлен на <b>{amount:.2f} ₽</b>.",
        parse_mode="HTML",
    )


@dp.message(Command("tickets"))
async def tickets_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "⚠️ Формат: <code>/tickets @username количество</code>",
            parse_mode="HTML",
        )
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

    current_tickets = int(target["tickets"])
    if current_tickets + count < 0:
        await message.answer("❌ Количество билетов не может стать отрицательным.")
        return

    database.add_user_tickets(target["user_id"], count)
    new_data = database.find_user(str(target["user_id"]))

    await message.answer(
        f"✅ Обновлено количество билетов для "
        f"{target['username']} на <b>{count:+d} 🎟</b>.\n"
        f"Всего билетов: <b>{new_data['tickets']} шт.</b>",
        parse_mode="HTML",
    )


# ----------------- ЖИЗНЕННЫЙ ЦИКЛ -----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if bot:
        try:
            await bot.set_webhook(
                url=f"{BASE_URL}/webhook",
                drop_pending_updates=True,
            )
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Играть ⭐️",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            )
            logger.info("Telegram Webhook и Menu Button зарегистрированы.")
        except Exception:
            logger.exception("Ошибка при инициализации бота.")

    yield

    if bot:
        try:
            await bot.delete_webhook()
            await bot.session.close()
        except Exception:
            logger.exception("Ошибка при завершении Telegram bot session.")


app = FastAPI(title="Lucky Stars API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ----------------- TELEGRAM WEBHOOK -----------------

@app.post("/webhook")
async def telegram_webhook(request: Request):
    if not bot:
        raise HTTPException(status_code=503, detail="Bot is not configured")

    update = types.Update.model_validate(
        await request.json(),
        context={"bot": bot},
    )
    await dp.feed_update(bot, update)

    return {"ok": True}


# ----------------- API ВЕБ-ПРИЛОЖЕНИЯ -----------------

class SpinRequest(BaseModel):
    init_data: str = Field(..., min_length=1, max_length=10000)
    stars: int = Field(..., ge=MIN_STARS, le=MAX_STARS)
    chance: int = Field(..., ge=MIN_CHANCE, le=MAX_CHANCE)
    recipient: str = Field(..., min_length=1, max_length=MAX_USERNAME_LENGTH)

    @field_validator("recipient")
    @classmethod
    def validate_recipient(cls, value: str) -> str:
        value = value.strip()

        if value.startswith("@"):
            value = value[1:]

        # Telegram username: 5-32 chars, letters/numbers/underscore.
        # Оставляем несколько более широкий лимит на случай username-получателя
        # в используемом StarsFlow API.
        if not value:
            raise ValueError("Пустой username получателя")

        if not all(
            ch.isalnum() or ch == "_"
            for ch in value
        ):
            raise ValueError("Некорректный username получателя")

        return value


@app.get("/")
def home():
    return {
        "status": "running",
        "message": "Lucky Stars Backend Live!",
    }


@app.get("/api/user/{user_id}")
def get_user(
    user_id: int,
    init_data: str,
):
    telegram_user = validate_telegram_init_data(init_data, BOT_TOKEN)

    authenticated_id = int(telegram_user["id"])
    if authenticated_id != user_id:
        raise HTTPException(
            status_code=403,
            detail="Нельзя запросить данные другого пользователя",
        )

    username = username_for_database(telegram_user)

    try:
        return database.get_or_create_user(user_id, username)
    except Exception:
        logger.exception("Ошибка получения пользователя %s", user_id)
        raise HTTPException(
            status_code=500,
            detail="Ошибка базы данных",
        )


@app.post("/api/spin")
async def make_spin(req: SpinRequest):
    telegram_user = validate_telegram_init_data(
        req.init_data,
        BOT_TOKEN,
    )

    authenticated_user_id = int(telegram_user["id"])
    authenticated_username = username_for_database(telegram_user)

    # recipient НЕ берём из Telegram username автоматически:
    # пользователь может указать получателя, но его запрос всё равно
    # привязан к реальному Telegram user_id через initData.
    user_id = authenticated_user_id

    try:
        user = database.get_or_create_user(
            user_id,
            authenticated_username,
        )
    except Exception:
        logger.exception("Ошибка получения пользователя %s", user_id)
        raise HTTPException(
            status_code=500,
            detail="Ошибка базы данных",
        )

    full_cost = STAR_PRICE_RUB * Decimal(req.stars)
    cost = money(
        full_cost
        * (Decimal(req.chance) / Decimal("100"))
        * Decimal("1.05")
    )

    if cost <= 0:
        raise HTTPException(
            status_code=400,
            detail="Некорректная стоимость игры",
        )

    lock = get_spin_lock(user_id)

    async with lock:
        # Повторно читаем баланс уже под lock.
        user = database.get_or_create_user(
            user_id,
            authenticated_username,
        )

        balance = Decimal(str(user["balance"]))

        if balance < cost:
            raise HTTPException(
                status_code=400,
                detail="Недостаточно средств на балансе",
            )

        # Списание производится только после проверки.
        database.update_user_balance(
            user_id,
            -float(cost),
        )

        roll = random.uniform(0, 100)
        is_win = roll <= req.chance

        tickets_won = 0
        auto_sent = False
        stars_won = 0

        if is_win:
            stars_won = req.stars

            # StarsFlow получает очищенный username.
            auto_sent = await send_stars_via_starsflow(
                req.recipient,
                stars_won,
            )

            if not auto_sent:
                # Деньги за игру уже списаны, но выдача не состоялась.
                # Главное — не выдавать пользователю фиктивный успешный приз.
                logger.error(
                    "WIN DELIVERY FAILED: user_id=%s recipient=%s stars=%s",
                    user_id,
                    req.recipient,
                    stars_won,
                )

        else:
            tickets_won = 1
            database.add_user_tickets(user_id, 1)

        try:
            database.log_game(
                user_id,
                authenticated_username,
                req.stars,
                req.chance,
                float(cost),
                is_win,
                req.recipient,
                tickets_won,
            )
        except Exception:
            # Игра уже произошла. Логируем критическую ошибку,
            # но не пытаемся повторно списывать баланс.
            logger.exception(
                "Не удалось записать игру в БД: user_id=%s",
                user_id,
            )

        # Уведомление администраторов.
        if is_win and bot:
            status_str = (
                "✅ Авто-доставка успешна!"
                if auto_sent
                else "⚠️ Доставка не выполнена — требуется ручная проверка"
            )

            alert_text = (
                "🔔 <b>НОВЫЙ ВЫИГРЫШ</b>\n\n"
                f"👤 Игрок: <b>{authenticated_username}</b> "
                f"(ID: <code>{user_id}</code>)\n"
                f"🎁 Выигрыш: <b>{req.stars} ⭐</b>\n"
                f"🎯 Шанс: <b>{req.chance}%</b> "
                f"(стоимость: {cost:.2f} ₽)\n"
                f"📤 Получатель: <code>@{req.recipient}</code>\n"
                f"⚡ <b>Статус:</b> {status_str}"
            )

            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        alert_text,
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.exception(
                        "Не удалось отправить уведомление админу %s",
                        admin_id,
                    )

        updated_user = database.get_or_create_user(
            user_id,
            authenticated_username,
        )

        return {
            "success": True,
            "is_win": is_win,
            "reward_type": "stars" if is_win else "ticket",
            "stars_won": stars_won,
            "tickets_won": tickets_won,
            "auto_sent": auto_sent,
            "new_balance": round(float(updated_user["balance"]), 2),
            "new_tickets": updated_user["tickets"],
            "cost_paid": float(cost),
        }
