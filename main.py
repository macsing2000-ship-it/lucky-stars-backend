import os
import re
import asyncio
import json
import time
import random
import logging
import httpx
import hmac
import hashlib
from html import escape as h
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp,
    LabeledPrice, PreCheckoutQuery, CallbackQuery, ChatMemberUpdated
)

import database

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Юзернейм или ID канала для проверки подписки по заданию
REQUIRED_CHANNEL = "@RollVS"

# Инициализируем базу данных и создаем необходимые таблицы, если их нет
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
                CREATE TABLE IF NOT EXISTS completed_tasks (
                    user_id BIGINT,
                    task_id TEXT,
                    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, task_id)
                );
                CREATE TABLE IF NOT EXISTS crypto_invoices_done (
                    invoice_id BIGINT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS stars_payments_done (
                    charge_id TEXT PRIMARY KEY,
                    user_id BIGINT,
                    stars INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS shop_orders (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username TEXT,
                    gift_name TEXT,
                    ticket_cost INTEGER,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
except Exception as e:
    logger.error(f"Ошибка БД при создании таблиц: {e}")

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
STARSFLOW_TOKEN = os.getenv("STARSFLOW_TOKEN", "").strip()
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "").strip()

# Канал для вывода выигрышей (задай в Render или оставь ID)
PAYOUT_CHANNEL_ID = os.getenv("PAYOUT_CHANNEL_ID", "").strip()

WEBAPP_URL = "https://lucky-stars-backend.onrender.com?v=2"
BASE_URL = "https://lucky-stars-backend.onrender.com"

# КУРСЫ ВАЛЮТ И ЛИМИТЫ
STAR_PRICE_RUB = 1.95       # Стоимость 1 звезды в игре (при ставках)
STARS_DEPOSIT_RATE = 1.50   # Зачисление рублей за 1 ⭐ пополнения
MAX_STARS_WIN = 1000        # Жесткий лимит выигрыша: максимум 1000 звезд
MIN_CHANCE, MAX_CHANCE = 1, 75      # Допустимый шанс в рулетке (как на ползунке в приложении)
MIN_STARS_BUY, MAX_STARS_BUY = 50, 1000   # Покупка Stars за баланс по фиксированной цене
HOUSE_MARGIN = 1.05         # Наценка на ставку (5%)

# Реферальный процент со ставок. ДОЛЖЕН быть меньше наценки (5%), иначе игроку выгодно
# завести второй аккаунт по своей же ссылке и играть «в плюс». Было 10%.
REF_PERCENT = float(os.getenv("REF_PERCENT", "0.03"))

# Билет за проигрыш даём только если ставка не меньше этой суммы.
# Без порога спин 50⭐/1% стоил ~1 ₽ и давал билет: 10 ₽ -> 10 билетов -> подарок на 15⭐ (~29 ₽).
MIN_COST_FOR_TICKET = float(os.getenv("MIN_COST_FOR_TICKET", "10"))

# Курс USDT для пополнений через CryptoBot в крипте (fiat-счета идут в рублях и курс не нужен)
USDT_RUB_RATE = float(os.getenv("USDT_RUB_RATE", "95"))

# Ежедневный бонус по дням серии: (билеты, рубли на баланс). После 7-го дня круг начинается заново.
DAILY_REWARDS = [(1, 0.0), (0, 1.0), (1, 0.0), (0, 2.0), (2, 0.0), (0, 3.0), (3, 5.0)]

# Цены подарков в билетах — ТОЛЬКО на сервере (клиенту цену не доверяем)
SHOP_PRICES = {
    "Сердечко 💖": 10, "Мишка 🧸": 10,
    "Подарок 🎁": 18, "Роза 🌹": 18,
    "Торт 🎂": 35, "Букет тюльпанов 💐": 35, "Ракета 🚀": 35,
    "Кубок 🏆": 70, "Кольцо 💍": 70,
}

# Ежедневные напоминания: окно отправки по МСК [REMINDER_HOUR, REMINDER_HOUR + 4) и секрет для внешнего cron
REMINDER_HOUR_MSK = int(os.getenv("REMINDER_HOUR_MSK", "18"))
CRON_SECRET = os.getenv("CRON_SECRET", "").strip()

# Проверка подписи Telegram initData. Для локальных тестов можно временно выставить ALLOW_UNSIGNED_API=1
ALLOW_UNSIGNED = os.getenv("ALLOW_UNSIGNED_API", "").strip() == "1"

# Секрет вебхука: Telegram присылает его в заголовке X-Telegram-Bot-Api-Secret-Token.
# Без него кто угодно мог слать на /webhook поддельные апдейты (фейковые оплаты, команды от имени админа).
# Если WEBHOOK_SECRET не задан, он выводится из токена бота — настраивать ничего не обязательно.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or (
    hashlib.sha256(f"webhook:{BOT_TOKEN}".encode()).hexdigest()[:48] if BOT_TOKEN else ""
)

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

def verify_init_data(init_data: str, max_age: int = 172800):
    """Проверяет подпись Telegram WebApp initData. Возвращает dict пользователя из Telegram или None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        if not received_hash:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(pairs.get("user", "{}"))
        user["id"] = int(user["id"])
        return user
    except Exception:
        return None

def require_auth(user_id: int, init_data: str, fallback_name: str = "") -> str:
    """Запрос должен прийти из Mini App именно от этого пользователя.
    Возвращает имя игрока из ПОДПИСАННЫХ данных Telegram (имени из тела запроса не доверяем)."""
    if ALLOW_UNSIGNED:
        return fallback_name
    tg_user = verify_init_data(init_data)
    if tg_user is None:
        raise HTTPException(status_code=401, detail="Сессия недействительна. Закройте и заново откройте приложение из Telegram.")
    if tg_user["id"] != user_id:
        raise HTTPException(status_code=403, detail="Нет доступа")
    if tg_user.get("username"):
        return "@" + tg_user["username"]
    return tg_user.get("first_name") or f"id{user_id}"

_USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")

def clean_recipient(value: str) -> str:
    value = (value or "").strip()
    if not _USERNAME_RE.match(value):
        raise HTTPException(status_code=400, detail="Укажите корректный @username получателя (у получателя должен быть username в Telegram)")
    return value.lstrip("@")

def player_link(name: str, user_id: int) -> str:
    """Ссылка на игрока: по @username, если он есть, иначе по id."""
    if _USERNAME_RE.match(name or ""):
        return f"https://t.me/{name.lstrip('@')}"
    return f"tg://user?id={user_id}"

async def post_win_to_channel(is_purchase: bool, text: str):
    """В канал выплат публикуем только выигрыши, покупки игроков туда не попадают."""
    if not is_purchase:
        await post_to_payout_channel(text)

def resolve_target_user(sender_id: int, sender_username: str, query: str):
    clean_query = query.strip().lstrip("@")
    if clean_query.lower() in ("me", "я") or clean_query == str(sender_id) or (sender_username and clean_query.lower() == sender_username.lower()):
        return database.get_or_create_user(sender_id, sender_username)
    target = database.find_user(clean_query)
    if target:
        return target
    if clean_query.isdigit():
        return database.get_or_create_user(int(clean_query))
    return None

async def post_to_payout_channel(text: str):
    """Отправка сообщения в публичный/приватный канал выплат (если задан PAYOUT_CHANNEL_ID)"""
    if bot and PAYOUT_CHANNEL_ID:
        try:
            await bot.send_message(PAYOUT_CHANNEL_ID, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка отправки в канал выплат: {e}")


# ----------------- АВТО-ОТПРАВКА ЗВЁЗД ЧЕРЕЗ STARSFLOW API -----------------

async def send_stars_via_starsflow(recipient: str, amount: int) -> tuple:
    """Возвращает (успех, описание, неизвестно_ли_состояние).
    «Неизвестно» — когда запрос мог дойти до StarsFlow, но ответа мы не получили (таймаут, 5xx):
    в этом случае повторять отправку автоматически нельзя, иначе возможна двойная выплата."""
    if not STARSFLOW_TOKEN:
        return False, "STARSFLOW_TOKEN не задан в настройках Render!", False

    clean_username = recipient.replace("@", "").strip()
    if not clean_username:
        return False, "Неверный формат юзернейма получателя", False

    url = "https://tgstars.tg/api/v1/client/orders/stars"
    headers = {
        "Authorization": f"Bearer {STARSFLOW_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {
        "username": clean_username,
        "quantity": amount
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=15.0)
    except httpx.ConnectError as e:
        # Соединение даже не установилось — запрос точно не дошёл
        return False, f"Не удалось подключиться: {e}", False
    except Exception as e:
        return False, f"Нет ответа от StarsFlow: {e!r}", True

    status = response.status_code
    if status in (200, 201):
        try:
            order_id = (response.json().get("order") or {}).get("id", "OK")
        except Exception:
            order_id = "OK"
        return True, f"Заказ #{order_id} принят StarsFlow!", False
    if status >= 500:
        return False, f"Код {status}: {response.text[:300]}", True
    return False, f"Код {status}: {response.text[:300]}", False


# ----------------- ОБРАБОТКА ПОКУПОК ИЗ МАГАЗИНА (ПОДАРКИ) -----------------

@dp.callback_query(F.data.startswith("shop_"))
async def handle_shop_action(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[0]
    order_id = int(parts[1])

    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, username, gift_name, ticket_cost, status FROM shop_orders WHERE id = %s", (order_id,))
            req = cur.fetchone()

    if not req:
        await callback.answer("Заказ не найден в базе!", show_alert=True)
        return

    o_id = req[0] if isinstance(req, tuple) else req["id"]
    u_id = req[1] if isinstance(req, tuple) else req["user_id"]
    uname = req[2] if isinstance(req, tuple) else req["username"]
    gift = req[3] if isinstance(req, tuple) else req["gift_name"]
    cost = req[4] if isinstance(req, tuple) else req["ticket_cost"]
    status = req[5] if isinstance(req, tuple) else req["status"]

    if status != 'pending':
        await callback.answer(f"Этот заказ уже обработан (статус: {status})!", show_alert=True)
        return
    if not database.claim_request("shop_orders", o_id):
        await callback.answer("Заказ уже обрабатывается, подождите.", show_alert=True)
        return

    admin_mention = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.first_name
    clean_uname = uname.strip().lstrip("@")

    if action == "shop_confirm":
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE shop_orders SET status = 'completed' WHERE id = %s", (o_id,))
                conn.commit()

        new_text = (
            f"{callback.message.html_text}\n\n"
            f"✅ <b>ВЫДАНО ВРУЧНУЮ</b>\n"
            f"👤 Отметил админ: {admin_mention}"
        )
        await callback.message.edit_text(new_text, parse_mode="HTML", reply_markup=None)

        # Отправляем уведомление пользователю
        if bot:
            try:
                await bot.send_message(
                    u_id,
                    f"🎉 <b>Ваш подарок успешно отправлен!</b>\n\n"
                    f"🎁 Товар: <b>{gift}</b>\n"
                    f"Администратор отправил подарок на ваш аккаунт!",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        # Публикуем в канал выплат
        await post_to_payout_channel(
            f"🎁 <b>ВЫДАЧА ПОДАРКА ИЗ МАГАЗИНА</b>\n\n"
            f"👤 Игрок: @{h(clean_uname)}\n"
            f"🛍 Товар: <b>{gift}</b>\n"
            f"🎟 Обменено билетов: <b>{cost} шт.</b>\n"
            f"Статус: ✅ Выдано"
        )
        await callback.answer("Подарок отмечен как выданный!")

    elif action == "shop_cancel":
        # Возвращаем билеты
        database.add_user_tickets(u_id, cost)
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE shop_orders SET status = 'cancelled' WHERE id = %s", (o_id,))
                conn.commit()

        new_text = (
            f"{callback.message.html_text}\n\n"
            f"❌ <b>ОТКЛОНЕНО С ВОЗВРАТОМ</b>\n"
            f"👤 Отклонил админ: {admin_mention}\n"
            f"🎟 Возвращено билетов: <b>{cost} шт.</b>"
        )
        await callback.message.edit_text(new_text, parse_mode="HTML", reply_markup=None)

        if bot:
            try:
                await bot.send_message(
                    u_id,
                    f"⚠️ <b>Заказ подарка \"{gift}\" отменён.</b>\n\n"
                    f"🎟 Вам возвращено: <b>+{cost} билетов</b>.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        await callback.answer("Заказ отменён, билеты возвращены игроку!")


# ----------------- ОБРАБОТКА ВЫПЛАТЫ ВЫИГРЫШЕЙ -----------------

@dp.callback_query(F.data.startswith("payout_"))
async def handle_payout_action(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[0]
    req_id = int(parts[1])

    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, username, recipient, stars, cost, status, kind FROM payout_requests WHERE id = %s", (req_id,))
            req = cur.fetchone()

    if not req:
        await callback.answer("Заявка не найдена в базе!", show_alert=True)
        return

    p_id = req[0] if isinstance(req, tuple) else req["id"]
    u_id = req[1] if isinstance(req, tuple) else req["user_id"]
    uname = req[2] if isinstance(req, tuple) else req["username"]
    recip = req[3] if isinstance(req, tuple) else req["recipient"]
    stars = req[4] if isinstance(req, tuple) else req["stars"]
    cost = float(req[5] if isinstance(req, tuple) else req["cost"])
    status = req[6] if isinstance(req, tuple) else req["status"]
    kind = (req.get("kind") if isinstance(req, dict) else None) or "win"
    is_purchase = (kind == "purchase")

    if status != 'pending':
        await callback.answer(f"Эта заявка уже обработана (статус: {status})!", show_alert=True)
        return
    if not database.claim_request("payout_requests", p_id):
        await callback.answer("Заявка уже обрабатывается, подождите.", show_alert=True)
        return

    admin_mention = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.first_name
    clean_recip = recip.strip().lstrip("@")

    if action == "payout_sf":
        await callback.answer("Отправляем запрос в StarsFlow...", show_alert=False)
        success, details, uncertain = await send_stars_via_starsflow(recip, stars)

        if success:
            with database.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE payout_requests SET status = 'completed_sf' WHERE id = %s", (p_id,))
                    conn.commit()

            new_text = (
                f"{callback.message.html_text}\n\n"
                f"✅ <b>ВЫПЛАЧЕНО ЧЕРЕЗ STARSFLOW</b>\n"
                f"👤 Одобрил админ: {admin_mention}\n"
                f"ℹ️ {details}"
            )
            await callback.message.edit_text(new_text, parse_mode="HTML", reply_markup=None)
            # В Live-ленту выигрыш уже попал в момент спина — второй раз не пишем

            if bot:
                try:
                    await bot.send_message(
                        u_id,
                        f"{'🛒 <b>Ваша покупка доставлена!</b>' if is_purchase else '🎉 <b>Ваш выигрыш отправлен!</b>'}\n\n"
                        f"🎁 <b>+{stars} ⭐ Telegram Stars</b> успешно доставлены на аккаунт <b>{recip}</b>!",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            # Публикуем в канал выплат
            await post_win_to_channel(is_purchase,
                f"🏆 <b>ВЫПЛАТА ВЫИГРЫША STARS</b>\n\n"
                f"👤 Получатель: @{clean_recip}\n"
                f"⭐ Выигрыш: <b>+{stars} ⭐ Telegram Stars</b>\n"
                f"Статус: ✅ Доставлено"
            )
        elif uncertain:
            # StarsFlow не ответил (таймаут/обрыв): заказ мог пройти. Не возвращаем заявку в очередь,
            # чтобы повторное нажатие не отправило звёзды второй раз.
            await callback.message.reply(
                f"⚠️ <b>StarsFlow не ответил по заявке #{p_id}</b> — заказ мог пройти.\n"
                f"<code>{h(details)}</code>\n\n"
                f"Проверьте заказы в StarsFlow. Если звёзды НЕ ушли — верните заявку в очередь командой "
                f"<code>/unstuck {p_id}</code> и нажмите кнопку ещё раз.",
                parse_mode="HTML"
            )
        else:
            database.release_request("payout_requests", p_id)
            await callback.message.reply(f"❌ Ошибка StarsFlow при отправке #{p_id}:\n<code>{h(details)}</code>", parse_mode="HTML")

    elif action == "payout_manual":
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE payout_requests SET status = 'completed_manual' WHERE id = %s", (p_id,))
                conn.commit()

        new_text = (
            f"{callback.message.html_text}\n\n"
            f"✅ <b>ВЫДАНО ВРУЧНУЮ</b>\n"
            f"👤 Отметил админ: {admin_mention}"
        )
        await callback.message.edit_text(new_text, parse_mode="HTML", reply_markup=None)

        if bot:
            try:
                await bot.send_message(
                    u_id,
                    f"{'🛒 <b>Ваша покупка выдана!</b>' if is_purchase else '🎉 <b>Ваш выигрыш подтверждён!</b>'}\n\n"
                    f"🎁 <b>{stars} ⭐ Telegram Stars</b> успешно выданы на аккаунт <b>{recip}</b>!",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        # Публикуем в канал выплат
        await post_win_to_channel(is_purchase,
            f"🏆 <b>ВЫПЛАТА ВЫИГРЫША STARS</b>\n\n"
            f"👤 Получатель: @{clean_recip}\n"
            f"⭐ Выигрыш: <b>+{stars} ⭐ Telegram Stars</b>\n"
            f"Статус: ✅ Выдано вручную"
        )
        await callback.answer("Заявка отмечена выполненной!")

    elif action == "payout_cancel":
        database.update_user_balance(u_id, float(cost))
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE payout_requests SET status = 'cancelled_refunded' WHERE id = %s", (p_id,))
                conn.commit()

        new_text = (
            f"{callback.message.html_text}\n\n"
            f"❌ <b>ОТКЛОНЕНО С ВОЗВРАТОМ</b>\n"
            f"👤 Отклонил админ: {admin_mention}\n"
            f"💰 Игроку возвращено: <b>{cost:.2f} ₽</b>"
        )
        await callback.message.edit_text(new_text, parse_mode="HTML", reply_markup=None)

        if bot:
            try:
                await bot.send_message(
                    u_id,
                    f"⚠️ <b>Заявка на {'покупку' if is_purchase else 'выигрыш'} {stars} ⭐ отклонена администратором.</b>\n\n"
                    f"💰 Сумма (<b>+{cost:.2f} ₽</b>) полностью возвращена на ваш игровой баланс.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        await callback.answer("Заявка отменена, ставка возвращена!")


# ----------------- КОМАНДЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ -----------------

@dp.message(Command("start"))
async def start_handler(message: types.Message, command: CommandObject = None):
    user_id = message.from_user.id
    uname = message.from_user.username or message.from_user.first_name or f"id{user_id}"
    
    is_new = False
    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
            if not cur.fetchone():
                is_new = True

    referrer_id = None
    args = command.args if command else None
    if not args:
        parts = message.text.split()
        if len(parts) > 1:
            args = parts[1]

    if args and args.startswith("ref_"):
        try:
            ref_str = args.replace("ref_", "").strip()
            if ref_str.isdigit() and int(ref_str) != user_id:
                referrer_id = int(ref_str)
        except Exception:
            pass

    user = database.get_or_create_user(user_id, uname, referrer_id)

    # Билет пригласившему уже начислен внутри get_or_create_user (раньше тут начислялся второй раз).
    # Уведомляем, только если реферал реально засчитан (пригласивший есть в базе).
    if is_new and user.get("referrer_id") and bot:
        try:
            await bot.send_message(
                user["referrer_id"],
                f"🎉 <b>У вас новый реферал!</b>\n"
                f"👤 Игрок: {h(uname if not message.from_user.username else '@' + uname)}\n"
                f"🎟 Вам начислен <b>+1 билет 🎟</b>!",
                parse_mode="HTML"
            )
        except Exception:
            pass

    text = (
        f"👋 <b>Добро пожаловать в Lucky Stars!</b>\n\n"
        f"Здесь ты можешь выиграть и забрать <b>Telegram Stars ⭐</b> с повышенным шансом!\n\n"
        f"💰 Твой баланс: <b>{user['balance']:.2f} ₽</b>\n"
        f"🎟 Билеты: <b>{user['tickets']} шт.</b>\n\n"
        f"👥 <b>Приглашай друзей:</b>\n"
        f"Получай <b>{REF_PERCENT * 100:g}% от каждой их ставки</b> + <b>1 билет 🎟 за друга (сразу)!</b>\n\n"
        f"👇 Нажми на кнопку ниже, чтобы открыть приложение:"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚀 Открыть Lucky Stars", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@dp.message(Command("rules"))
async def rules_handler(message: types.Message):
    text = (
        "⚖️ <b>Пользовательское соглашение и правила Lucky Stars</b>\n\n"
        "1. Сервис предоставляет доступ к интерактивной программе приобретения цифровых товаров (Telegram Stars и подарки).\n"
        "2. Баланс в приложении выражен в учетных баллах (₽) и предназначен исключительно для использования внутри сервиса.\n"
        "3. Все выигрыши начисляются в виде официальных цифровых активов (Telegram Stars / подарки) на указанный Telegram-аккаунт.\n"
        "4. Все операции по пополнению являются окончательными.\n"
        "5. Участвуя в программе, вы подтверждаете, что вам исполнилось 18 лет."
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("help"))
async def help_handler(message: types.Message):
    text = (
        "ℹ️ <b>Как играть в Lucky Stars</b>\n\n"
        "1. Открой приложение кнопкой «Играть ⭐️» внизу чата.\n"
        "2. Выбери, сколько звёзд хочешь выиграть, и шанс — чем выше шанс, тем дороже спин.\n"
        "3. Выигрыш отправляется на указанный @username в течение 24 часов.\n"
        f"4. За проигрыш со ставкой от {MIN_COST_FOR_TICKET:g} ₽ дают билет 🎟 — билеты меняются на подарки в магазине.\n\n"
        "Команды:\n"
        "/start — главное меню и баланс\n"
        "/rules — правила сервиса\n"
        "/mute — выключить напоминания о бонусе (/unmute — включить)\n\n"
        "<i>Есть риск проигрыша. Только для 18+.</i>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=open_app_keyboard())


BOT_COMMANDS = [
    ("start", "Главное меню и баланс"),
    ("help", "Как играть"),
    ("rules", "Правила сервиса"),
    ("mute", "Выключить напоминания"),
    ("unmute", "Включить напоминания"),
]
BOT_DESCRIPTION = (
    "🎰 Lucky Stars — рулетка Telegram Stars.\n\n"
    "Выбирай размер выигрыша и шанс, крути и забирай звёзды на свой аккаунт. "
    "Бесплатные билеты за ежедневный бонус и друзей — меняй их на подарки.\n\n"
    "Есть риск проигрыша. 18+."
)
BOT_SHORT_DESCRIPTION = "Рулетка Telegram Stars: выбирай шанс и выигрывай звёзды. 18+"

async def setup_bot_profile():
    """Команды в меню «/» и описание на пустом экране чата."""
    from aiogram.types import BotCommand
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
    except Exception as e:
        logger.error(f"Не удалось обновить профиль бота: {e}")


# ----------------- АДМИН-ПАНЕЛЬ И КОМАНДЫ -----------------

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    text = (
        "👑 <b>Панель администратора Lucky Stars</b>\n\n"
        "Доступные команды:\n"
        "📊 <code>/stats</code> — статистика проекта\n"
        "🔍 <code>/user @username</code> или <code>/user id</code> — инфо о пользователе\n"
        "➕ <code>/give @username сумма</code> — начислить баланс\n"
        "➖ <code>/take @username сумма</code> — списать баланс\n"
        "✏️ <code>/setbalance @username сумма</code> — установить точный баланс\n"
        "🎟 <code>/tickets @username количество</code> — выдать билеты\n"
        "🧪 <code>/testwin количество</code> — прямой тест StarsFlow\n"
        "↩️ <code>/unstuck номер</code> — вернуть зависшую заявку в очередь\n\n"
        "📢 <code>/broadcast</code> — рассылка всем игрокам (текст или фото)\n"
        "⏰ <code>/remindtest</code> — посмотреть ежедневное напоминание"
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("testwin"))
async def test_win_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    amount = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 50
    username = message.from_user.username or message.from_user.first_name

    await message.answer(f"⏳ Отправляем тестовый запрос на {amount} ⭐ для @{username} в StarsFlow...")
    success, details, uncertain = await send_stars_via_starsflow(username, amount)
    if success:
        await message.answer(f"✅ <b>УСПЕХ! Звёзды отправлены!</b>\n\n{h(details)}", parse_mode="HTML")
    else:
        note = "\n\n⚠️ Ответа не было — заказ мог пройти, проверьте в StarsFlow." if uncertain else ""
        await message.answer(f"❌ <b>Тест не удался!</b>\n\n<code>{h(details)}</code>{note}", parse_mode="HTML")

@dp.message(Command("unstuck"))
async def unstuck_handler(message: types.Message):
    """Возвращает заявку из 'processing' в очередь (после проверки, что звёзды не ушли)."""
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("⚠️ Формат: <code>/unstuck номер_заявки</code>", parse_mode="HTML")
        return
    database.release_request("payout_requests", int(parts[1]))
    await message.answer(f"↩️ Заявка #{parts[1]} возвращена в очередь (если была в обработке). Кнопки в исходном сообщении снова работают.")

@dp.message(Command("stats"))
async def stats_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    try:
        stats = database.get_stats()
        text = (
            "📈 <b>Статистика проекта:</b>\n\n"
            f"👥 Всего пользователей: <b>{stats['total_users']}</b>\n"
            f"🎲 Всего игр сыграно: <b>{stats['total_games']}</b>\n"
            f"💸 Общий оборот ставок: <b>{stats['total_turnover']} ₽</b>\n"
            f"🏆 Побед (Звёзды): <b>{stats['total_wins']}</b>\n"
            f"🎟 Билетов выдано: <b>{stats.get('total_tickets', 0)} шт.</b>"
        )
        try:
            f = database.get_funnel()
            pct = lambda a, b: f"{a / b * 100:.0f}%" if b else "—"
            text += (
                "\n\n🔻 <b>Воронка:</b>\n"
                f"1️⃣ Запустили бота: <b>{f['total']}</b>\n"
                f"2️⃣ Открыли Mini App: <b>{f['opened']}</b> ({pct(f['opened'], f['total'])})\n"
                f"3️⃣ Сыграли хотя бы раз: <b>{f['played']}</b> ({pct(f['played'], f['total'])})\n"
                f"4️⃣ Сыграли 2+ раза: <b>{f['played_2plus']}</b> ({pct(f['played_2plus'], f['played'])} от сыгравших)\n"
                f"5️⃣ Вернулись в другой день: <b>{f['returned']}</b> ({pct(f['returned'], f['played'])} от сыгравших)\n\n"
                f"💳 Пополняли баланс: <b>{f['deposited']}</b> ({pct(f['deposited'], f['total'])})\n"
                f"🎁 Брали ежедневный бонус: <b>{f['daily_users']}</b>\n"
                f"👥 Пришли по рефералке: <b>{f['referred']}</b>\n"
                f"🆕 Новых за 24ч / 7 дней: <b>{f['new_24h']}</b> / <b>{f['new_7d']}</b>\n"
                "<i>Пункт 2 считается с момента обновления бота.</i>"
            )
        except Exception as fe:
            logger.error(f"Ошибка воронки: {fe}")
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка статистики: {e}")
        await message.answer(f"❌ Ошибка статистики: {e}")

@dp.message(Command("user"))
async def user_info_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("⚠️ Формат: <code>/user @username</code> или ID", parse_mode="HTML")
        return

    target = resolve_target_user(message.from_user.id, message.from_user.username, parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден в базе данных.", parse_mode="HTML")
        return

    text = (
        f"👤 <b>Профиль пользователя:</b>\n\n"
        f"🆔 ID: <code>{target['user_id']}</code>\n"
        f"Имя / Юзернейм: <b>{target['username']}</b>\n"
        f"💰 Баланс: <b>{target['balance']:.2f} ₽</b>\n"
        f"🎟 Билеты: <b>{target['tickets']} шт.</b>\n"
        f"👥 Рефералов: <b>{target.get('ref_count', 0)} чел.</b>\n"
        f"💸 Доход с реф.: <b>{target.get('ref_earned', 0.0):.2f} ₽</b>"
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

    target = resolve_target_user(message.from_user.id, message.from_user.username, parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден.", parse_mode="HTML")
        return

    try:
        amount = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("❌ Неверная сумма.", parse_mode="HTML")
        return

    database.update_user_balance(target["user_id"], amount)
    new_data = resolve_target_user(message.from_user.id, message.from_user.username, str(target["user_id"]))

    await message.answer(
        f"✅ Начислено <b>+{amount:.2f} ₽</b> пользователю {target['username']}!\n"
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

    target = resolve_target_user(message.from_user.id, message.from_user.username, parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден.", parse_mode="HTML")
        return

    try:
        amount = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("❌ Неверная сумма.", parse_mode="HTML")
        return

    database.update_user_balance(target["user_id"], -amount)
    new_data = resolve_target_user(message.from_user.id, message.from_user.username, str(target["user_id"]))

    await message.answer(
        f"✅ Списано <b>-{amount:.2f} ₽</b> у {target['username']}.\n"
        f"💰 Баланс: <b>{new_data['balance']:.2f} ₽</b>",
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

    target = resolve_target_user(message.from_user.id, message.from_user.username, parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден.", parse_mode="HTML")
        return

    try:
        amount = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("❌ Неверная сумма.", parse_mode="HTML")
        return

    database.set_user_balance(target["user_id"], amount)
    await message.answer(
        f"✅ Баланс {target['username']} установлен на <b>{amount:.2f} ₽</b>.",
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

    target = resolve_target_user(message.from_user.id, message.from_user.username, parts[1])
    if not target:
        await message.answer("❌ Пользователь не найден.", parse_mode="HTML")
        return

    try:
        count = int(parts[2])
    except ValueError:
        await message.answer("❌ Количество должно быть числом.", parse_mode="HTML")
        return

    database.add_user_tickets(target["user_id"], count)
    new_data = resolve_target_user(message.from_user.id, message.from_user.username, str(target["user_id"]))
    await message.answer(
        f"✅ Билеты для {target['username']}: <b>{new_data['tickets']} шт.</b>",
        parse_mode="HTML"
    )


# ----------------- ОПЛАТА TELEGRAM STARS (XTR) -----------------

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    ok = pre_checkout_query.currency == "XTR" and (pre_checkout_query.invoice_payload or "").startswith("stars_")
    if ok:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    else:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Неизвестный счёт")

@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payment = message.successful_payment
    user_id = message.from_user.id

    stars_amount = payment.total_amount  # фактически оплаченная сумма из Telegram

    # Идемпотентность: один и тот же платёж не зачисляем дважды
    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO stars_payments_done (charge_id, user_id, stars) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING RETURNING charge_id",
                (payment.telegram_payment_charge_id, user_id, stars_amount)
            )
            is_new_payment = cur.fetchone() is not None
            conn.commit()
    if not is_new_payment:
        logger.warning(f"Повторный апдейт оплаты {payment.telegram_payment_charge_id} — пропущен")
        return

    rub_added = stars_amount * STARS_DEPOSIT_RATE
    database.update_user_balance(user_id, rub_added)
    
    bonus = database.process_first_deposit_bonus(user_id)
    bonus_msg = f"\n🎁 <b>+10.00 ₽</b> бонуса за ваш первый депозит!" if bonus > 0 else ""

    await message.answer(
        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
        f"Зачислено за {stars_amount} ⭐: <b>+{rub_added:.2f} ₽</b>{bonus_msg}",
        parse_mode="HTML"
    )



# ----------------- РАССЫЛКИ И НАПОМИНАНИЯ -----------------

BROADCAST_WAIT = set()      # админы, от которых бот ждёт сообщение для рассылки
BROADCAST_PENDING = {}      # admin_id -> (chat_id, message_id) — ждёт подтверждения
BROADCAST_RUNNING = False
BACKGROUND_TASKS = set()

def spawn(coro):
    """Запуск фоновой задачи с удержанием ссылки (иначе её может собрать сборщик мусора)."""
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task

def open_app_keyboard(text: str = "🎮 Открыть Lucky Stars"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=WEBAPP_URL))]])

async def notify_admins(text: str):
    if not bot:
        return
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass

async def deliver(user_id: int, send):
    """Отправка одному пользователю. Возвращает 'ok' | 'blocked' | 'fail'.
    Блокировки бота запоминаем, чтобы не слать им в следующий раз."""
    for _ in range(2):
        try:
            await send()
            return "ok"
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            await asyncio.to_thread(database.set_blocked, user_id, True)
            return "blocked"
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "chat not found" in msg or "deactivated" in msg:
                await asyncio.to_thread(database.set_blocked, user_id, True)
                return "blocked"
            return "fail"
        except Exception:
            return "fail"
    return "fail"

async def run_broadcast(admin_id: int, from_chat_id: int, message_id: int):
    global BROADCAST_RUNNING
    if BROADCAST_RUNNING:
        await notify_admins("⚠️ Другая рассылка ещё идёт, дождитесь её завершения.")
        return
    BROADCAST_RUNNING = True
    try:
        user_ids = await asyncio.to_thread(database.get_broadcast_recipients)
        keyboard = open_app_keyboard()
        sent = blocked = failed = 0
        for uid in user_ids:
            res = await deliver(uid, lambda uid=uid: bot.copy_message(
                chat_id=uid, from_chat_id=from_chat_id, message_id=message_id, reply_markup=keyboard))
            if res == "ok":
                sent += 1
            elif res == "blocked":
                blocked += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)  # ~20 сообщений/сек, в пределах лимитов Telegram
        try:
            await bot.send_message(
                admin_id,
                "📢 <b>Рассылка завершена</b>\n\n"
                f"✅ Доставлено: <b>{sent}</b>\n"
                f"🚫 Заблокировали бота: <b>{blocked}</b>\n"
                f"⚠️ Ошибки: <b>{failed}</b>\n"
                f"Всего в списке: <b>{len(user_ids)}</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Ошибка рассылки: {e}")
        await notify_admins(f"❌ Рассылка остановлена ошибкой: <code>{e}</code>")
    finally:
        BROADCAST_RUNNING = False

def _waiting_broadcast(message: types.Message) -> bool:
    return (
        message.from_user is not None
        and message.from_user.id in BROADCAST_WAIT
        and not (message.text or "").startswith("/")
    )

@dp.message(Command("broadcast"))
async def broadcast_start(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    BROADCAST_PENDING.pop(message.from_user.id, None)
    BROADCAST_WAIT.add(message.from_user.id)
    await message.answer(
        "📢 <b>Рассылка всем игрокам</b>\n\n"
        "Отправьте <b>одним сообщением</b> то, что нужно разослать: текст или фото с подписью "
        "(жирный шрифт, ссылки и эмодзи сохранятся). Под сообщением автоматически появится кнопка «Открыть Lucky Stars».\n\n"
        "Отмена — /cancel",
        parse_mode="HTML"
    )

@dp.message(Command("cancel"))
async def broadcast_cancel(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    BROADCAST_WAIT.discard(message.from_user.id)
    BROADCAST_PENDING.pop(message.from_user.id, None)
    await message.answer("❎ Рассылка отменена.")

@dp.message(_waiting_broadcast)
async def broadcast_capture(message: types.Message):
    admin_id = message.from_user.id
    if not is_admin(admin_id):
        BROADCAST_WAIT.discard(admin_id)
        return
    BROADCAST_WAIT.discard(admin_id)
    BROADCAST_PENDING[admin_id] = (message.chat.id, message.message_id)
    total = len(await asyncio.to_thread(database.get_broadcast_recipients))
    try:
        await bot.copy_message(chat_id=message.chat.id, from_chat_id=message.chat.id,
                               message_id=message.message_id, reply_markup=open_app_keyboard())
    except Exception as e:
        BROADCAST_PENDING.pop(admin_id, None)
        await message.answer(f"❌ Не удалось сделать предпросмотр: <code>{e}</code>", parse_mode="HTML")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Отправить ({total})", callback_data="bc_send"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel"),
    ]])
    await message.answer(f"👆 Так сообщение увидят игроки.\nОтправить <b>{total}</b> пользователям?",
                         parse_mode="HTML", reply_markup=keyboard)

@dp.callback_query(F.data.in_({"bc_send", "bc_cancel"}))
async def broadcast_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return
    pending = BROADCAST_PENDING.pop(callback.from_user.id, None)
    if callback.data == "bc_cancel" or not pending:
        text = "❎ Рассылка отменена." if callback.data == "bc_cancel" else "⌛ Черновик устарел, начните заново: /broadcast"
        await callback.message.edit_text(text)
        await callback.answer()
        return
    await callback.message.edit_text("⏳ Рассылка запущена. Отчёт придёт, когда закончится.")
    await callback.answer()
    spawn(run_broadcast(callback.from_user.id, pending[0], pending[1]))

# ----- ЕЖЕДНЕВНОЕ НАПОМИНАНИЕ О БОНУСЕ -----

def reminder_text(streak: int, at_risk: bool) -> str:
    footer = "\n\n<i>Отключить напоминания: /mute</i>"
    if at_risk and streak >= 2:
        return (f"🔥 <b>Твоя серия: {streak} дн. подряд!</b>\n"
                "Забери ежедневный бонус сегодня, чтобы не потерять её. Награда растёт с каждым днём 🎁" + footer)
    return ("🎁 <b>Ежедневный бонус ждёт тебя!</b>\n"
            "Бесплатные билеты и баллы — заберёшь за пару секунд." + footer)

async def run_daily_reminder(force: bool = False):
    """Раз в сутки напоминает о бонусе тем, кто брал его за последнюю неделю и ещё не брал сегодня."""
    if not bot:
        return {"skipped": "bot disabled"}
    if not force:
        hour = datetime.now(database.MSK).hour
        if not (REMINDER_HOUR_MSK <= hour < REMINDER_HOUR_MSK + 4):
            return {"skipped": "вне окна отправки"}
        today = database.msk_today()
        if not await asyncio.to_thread(database.claim_reminder_day, today):
            return {"skipped": "сегодня уже отправлено"}

    targets = await asyncio.to_thread(database.get_reminder_targets)
    keyboard = open_app_keyboard("🎁 Забрать бонус")
    sent = blocked = failed = 0
    for t in targets:
        text = reminder_text(t["streak"], t["at_risk"])
        res = await deliver(t["user_id"], lambda uid=t["user_id"], text=text: bot.send_message(
            uid, text, parse_mode="HTML", reply_markup=keyboard))
        if res == "ok":
            sent += 1
        elif res == "blocked":
            blocked += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)

    if not force:
        await asyncio.to_thread(database.finish_reminder_day, database.msk_today(), sent)
    await notify_admins(
        "⏰ <b>Ежедневные напоминания</b>\n"
        f"✅ Доставлено: <b>{sent}</b> из <b>{len(targets)}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked}</b> · ⚠️ Ошибки: <b>{failed}</b>"
    )
    return {"sent": sent, "blocked": blocked, "failed": failed, "total": len(targets)}

async def reminder_loop():
    """Проверка раз в 10 минут. Работает, пока сервис не «спит»; на бесплатном Render
    его дополнительно будит внешний cron через /api/cron/daily."""
    while True:
        try:
            await run_daily_reminder()
        except Exception as e:
            logger.error(f"Ошибка ежедневного напоминания: {e}")
        await asyncio.sleep(600)

@dp.message(Command("remindtest"))
async def remind_test(message: types.Message):
    """Присылает админу оба варианта напоминания (только ему) — для проверки внешнего вида."""
    if not is_admin(message.from_user.id):
        return
    kb = open_app_keyboard("🎁 Забрать бонус")
    await message.answer(reminder_text(5, True), parse_mode="HTML", reply_markup=kb)
    await message.answer(reminder_text(0, False), parse_mode="HTML", reply_markup=kb)

@dp.message(Command("mute"))
async def mute_handler(message: types.Message):
    await asyncio.to_thread(database.set_notify_daily, message.from_user.id, False)
    await message.answer("🔕 Напоминания о ежедневном бонусе отключены. Включить обратно: /unmute")

@dp.message(Command("unmute"))
async def unmute_handler(message: types.Message):
    await asyncio.to_thread(database.set_notify_daily, message.from_user.id, True)
    await message.answer("🔔 Напоминания о ежедневном бонусе включены.")

@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated):
    """Запоминаем, кто заблокировал бота или вернулся, чтобы рассылки не шли в пустоту."""
    if event.chat.type != "private":
        return
    status = event.new_chat_member.status
    await asyncio.to_thread(database.set_blocked, event.from_user.id, status in ("kicked", "left"))


# ----------------- ЖИЗНЕННЫЙ ЦИКЛ FASTAPI -----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if bot:
        try:
            await bot.set_webhook(
                url=f"{BASE_URL}/webhook",
                drop_pending_updates=False,
                allowed_updates=["message", "callback_query", "pre_checkout_query", "my_chat_member"],
                secret_token=WEBHOOK_SECRET
            )
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Играть ⭐️",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            )
            await setup_bot_profile()
            logger.info("Telegram Webhook активен!")
        except Exception as e:
            logger.error(f"Ошибка вебхука: {e}")
    reminder_task = asyncio.create_task(reminder_loop()) if bot else None
    yield
    if reminder_task:
        reminder_task.cancel()
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

@app.get("/", response_class=HTMLResponse)
def home():
    if os.path.exists("index.html"):
        return FileResponse("index.html", headers={"Cache-Control": "no-cache"})
    return HTMLResponse("<h2>Lucky Stars App is active!</h2>")

@app.get("/bg.jpg")
def serve_bg():
    if os.path.exists("bg.jpg"):
        return FileResponse("bg.jpg")
    raise HTTPException(status_code=404, detail="bg.jpg не найден")

@app.get("/webhook")
def webhook_check():
    return {"status": "active"}

@app.post("/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    if not bot:
        return {"ok": False, "error": "Bot token not configured"}
    # Принимаем апдейты только от Telegram (он присылает секрет, заданный в set_webhook)
    if not WEBHOOK_SECRET or not hmac.compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        logger.warning("Отклонён запрос на /webhook без правильного секрета")
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        update = types.Update.model_validate(await request.json(), context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"Ошибка апдейта Telegram: {e}")
    return {"ok": True}


# ----------------- API: ПОЛУЧЕНИЕ ЮЗЕРА И СТАТУСА ЗАДАНИЙ -----------------

@app.get("/api/user/{user_id}")
def get_user(user_id: int, username: str = "", x_telegram_init_data: str = Header(default="")):
    username = require_auth(user_id, x_telegram_init_data, username)
    try:
        user_data = database.get_or_create_user(user_id, username)
        try:
            database.mark_app_opened(user_id)
            user_data["daily"] = database.get_daily_status(user_id, DAILY_REWARDS)
        except Exception as de:
            logger.error(f"Ошибка daily/funnel: {de}")
        
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT task_id FROM completed_tasks WHERE user_id = %s", (user_id,))
                completed = [row["task_id"] for row in cur.fetchall()]
                user_data["completed_tasks"] = completed

        return user_data
    except Exception as e:
        logger.error(f"Ошибка /api/user: {e}")
        raise HTTPException(status_code=500, detail="Ошибка сервера, попробуйте позже")


# ----------------- API: ПРОВЕРКА ЗАДАНИЯ ПОДПИСКИ -----------------

class TaskVerifyRequest(BaseModel):
    user_id: int

@app.post("/api/tasks/verify-sub")
async def verify_sub_task(req: TaskVerifyRequest, x_telegram_init_data: str = Header(default="")):
    require_auth(req.user_id, x_telegram_init_data)
    if not bot:
        raise HTTPException(status_code=500, detail="Бот не инициализирован")

    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM completed_tasks WHERE user_id = %s AND task_id = 'sub_channel'", (req.user_id,))
            if cur.fetchone():
                return {"success": False, "error": "Награда за это задание уже получена!"}

    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=req.user_id)
        if member.status in ["member", "administrator", "creator"]:
            with database.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO completed_tasks (user_id, task_id) VALUES (%s, 'sub_channel') ON CONFLICT DO NOTHING RETURNING user_id",
                        (req.user_id,)
                    )
                    first_time = cur.fetchone() is not None
                    conn.commit()
            if not first_time:
                return {"success": False, "error": "Награда за это задание уже получена!"}

            database.add_user_tickets(req.user_id, 1)
            return {"success": True, "message": "Билет успешно начислен!"}
        else:
            return {"success": False, "error": "Вы еще не подписаны на канал!"}
    except Exception as e:
        logger.error(f"Ошибка проверки подписки: {e}")
        return {"success": False, "error": "Не удалось проверить подписку. Убедитесь, что подписаны на канал."}


# ----------------- LIVE-ЛЕНТА -----------------

@app.get("/api/live-feed")
def get_live_feed_endpoint():
    try:
        return database.get_live_feed()
    except Exception:
        return []


# ----------------- ВЕБХУК ДЛЯ CRYPTOBOT -----------------

@app.post("/api/cryptobot/webhook")
async def cryptobot_webhook(request: Request):
    try:
        body_bytes = await request.body()
        signature = request.headers.get("crypto-pay-api-signature", "")
        
        # Подпись обязательна: без неё любой мог бы «пополнить» баланс поддельным запросом
        if not CRYPTOBOT_TOKEN:
            logger.error("CryptoBot webhook отклонён: CRYPTOBOT_TOKEN не задан")
            return {"ok": True}
        secret = hashlib.sha256(CRYPTOBOT_TOKEN.encode()).digest()
        calculated_sig = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(calculated_sig, signature):
            logger.warning("CryptoBot webhook отклонён: неверная или отсутствующая подпись")
            return {"ok": True}

        data = await request.json()
        update_type = data.get("update_type")
        
        if update_type == "invoice_paid":
            payload_data = data.get("payload", {})
            user_id_str = payload_data.get("payload")
            
            currency_type = payload_data.get("currency_type")
            if currency_type == "fiat":
                rub_to_add = float(payload_data.get("amount", "0"))
            else:
                usdt_paid = float(payload_data.get("amount", "0"))
                rub_to_add = round(usdt_paid * USDT_RUB_RATE, 2)
            
            if user_id_str and user_id_str.isdigit():
                user_id = int(user_id_str)

                # Идемпотентность: один и тот же счёт не зачисляем дважды
                invoice_id = payload_data.get("invoice_id")
                if invoice_id is not None:
                    with database.get_connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "INSERT INTO crypto_invoices_done (invoice_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING invoice_id",
                                (int(invoice_id),)
                            )
                            is_new_invoice = cur.fetchone() is not None
                            conn.commit()
                    if not is_new_invoice:
                        return {"ok": True}

                database.update_user_balance(user_id, rub_to_add)
                
                bonus = database.process_first_deposit_bonus(user_id)
                bonus_str = "\n🎁 <b>+10.00 ₽</b> бонус за первый депозит зачислен!" if bonus > 0 else ""

                if bot:
                    try:
                        await bot.send_message(
                            user_id,
                            f"💳 <b>Оплата через CryptoBot получена!</b>\n\n"
                            f"Сумма: <b>+{rub_to_add:.2f} ₽</b>{bonus_str}",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка CryptoBot: {e}")
        return {"ok": True}


# ----------------- API: СОЗДАНИЕ СЧЕТОВ -----------------

class InvoiceRequest(BaseModel):
    user_id: int
    stars: int

@app.post("/api/deposit/stars-invoice")
async def create_stars_invoice(req: InvoiceRequest, x_telegram_init_data: str = Header(default="")):
    require_auth(req.user_id, x_telegram_init_data)
    if not bot:
        raise HTTPException(status_code=500, detail="Бот не инициализирован")
    if req.stars < 10:
        raise HTTPException(status_code=400, detail="Минимум 10 звезд")

    try:
        title = f"Пополнение на {req.stars} ⭐"
        description = f"Зачисление средств на игровой баланс Lucky Stars"
        payload = f"stars_{req.stars}"
        currency = "XTR"
        prices = [LabeledPrice(label="Telegram Stars", amount=req.stars)]

        invoice_link = await bot.create_invoice_link(
            title=title, description=description, payload=payload, currency=currency, prices=prices
        )
        return {"invoice_link": invoice_link}
    except Exception as e:
        logger.error(f"Ошибка создания инвойса Stars: {e}")
        raise HTTPException(status_code=500, detail="Ошибка инвойса")


class CryptoBotRequest(BaseModel):
    user_id: int
    amount_rub: float

@app.post("/api/deposit/cryptobot")
async def create_cryptobot_invoice(req: CryptoBotRequest, x_telegram_init_data: str = Header(default="")):
    require_auth(req.user_id, x_telegram_init_data)
    if req.amount_rub > 100000:
        raise HTTPException(status_code=400, detail="Слишком большая сумма")
    if not CRYPTOBOT_TOKEN:
        raise HTTPException(status_code=500, detail="CryptoBot не настроен")
    if req.amount_rub < 10:
        raise HTTPException(status_code=400, detail="Минимум 10 ₽")

    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN, "Content-Type": "application/json"}
    payload = {
        "currency_type": "fiat",
        "fiat": "RUB",
        "amount": f"{req.amount_rub:.2f}",
        "description": f"Пополнение Lucky Stars на {req.amount_rub:.0f} ₽",
        "payload": str(req.user_id)
    }

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, json=payload, headers=headers, timeout=10.0)
            data = res.json()
            if data.get("ok"):
                return {"pay_url": data["result"]["pay_url"]}
            raise HTTPException(status_code=400, detail="Ошибка CryptoBot API")
    except Exception:
        raise HTTPException(status_code=500, detail="Ошибка связи")


# ----------------- API: МАГАЗИН ПОДАРКОВ -----------------

class ShopBuyRequest(BaseModel):
    user_id: int
    username: str
    gift_name: str
    ticket_cost: int = 0  # игнорируется: цена берётся из SHOP_PRICES на сервере

@app.post("/api/shop/buy")
async def buy_shop_gift(req: ShopBuyRequest, x_telegram_init_data: str = Header(default="")):
    req.username = require_auth(req.user_id, x_telegram_init_data, req.username)
    price = SHOP_PRICES.get(req.gift_name)
    if price is None:
        raise HTTPException(status_code=400, detail="Такого товара нет в магазине")

    database.get_or_create_user(req.user_id, req.username)
    if database.try_spend_tickets(req.user_id, price) is None:
        raise HTTPException(status_code=400, detail=f"Не хватает билетов! Нужно {price} 🎟")
    updated_user = database.get_or_create_user(req.user_id)

    # Записываем заказ в таблицу shop_orders
    order_id = None
    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO shop_orders (user_id, username, gift_name, ticket_cost, status)
                VALUES (%s, %s, %s, %s, 'pending') RETURNING id;
            """, (req.user_id, req.username, req.gift_name, price))
            row = cur.fetchone()
            order_id = row["id"] if isinstance(row, dict) else row[0]
            conn.commit()

    gift_icon = "🎁"
    if "Сердечко" in req.gift_name: gift_icon = "💖"
    elif "Мишка" in req.gift_name: gift_icon = "🧸"
    elif "Роза" in req.gift_name: gift_icon = "🌹"
    elif "Торт" in req.gift_name: gift_icon = "🎂"
    elif "Кольцо" in req.gift_name: gift_icon = "💍"
    
    try:
        database.log_live_drop(req.user_id, req.username, "buy_gift", req.gift_name, gift_icon)
    except Exception:
        pass

    clean_user = req.username.strip().lstrip("@")
    if bot and order_id:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Выдано вручную", callback_data=f"shop_confirm:{order_id}")],
                [InlineKeyboardButton(text="❌ Отклонить (вернуть билеты)", callback_data=f"shop_cancel:{order_id}")]
            ]
        )

        for admin_id in ADMIN_IDS:
            try:
                alert_text = (
                    "🛍 <b>НОВЫЙ ЗАКАЗ В МАГАЗИНЕ ПОДАРКОВ!</b>\n\n"
                    f"🆔 Заказ: <code>#{order_id}</code>\n"
                    f"👤 Игрок: <b>@{h(clean_user)}</b> (ID: <code>{req.user_id}</code>)\n"
                    f"🎁 Товар: <b>{req.gift_name}</b>\n"
                    f"🎟 Списано билетов: <b>{price} шт.</b>\n"
                    f"📊 Остаток: <b>{updated_user['tickets']} шт.</b>\n\n"
                    f"👉 <a href='{player_link(clean_user, req.user_id)}'>Диалог с игроком</a>"
                )
                await bot.send_message(admin_id, alert_text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
            except Exception:
                pass

    return {
        "success": True,
        "gift_name": req.gift_name,
        "new_tickets": updated_user["tickets"]
    }


# ----------------- API РУЛЕТКИ -----------------

class SpinRequest(BaseModel):
    user_id: int
    username: str
    stars: int
    chance: int
    recipient: str

@app.post("/api/spin")
async def make_spin(req: SpinRequest, x_telegram_init_data: str = Header(default="")):
    req.username = require_auth(req.user_id, x_telegram_init_data, req.username)
    if not (MIN_CHANCE <= req.chance <= MAX_CHANCE):
        raise HTTPException(status_code=400, detail=f"Шанс должен быть от {MIN_CHANCE}% до {MAX_CHANCE}%")
    req.recipient = clean_recipient(req.recipient)
    if req.stars > MAX_STARS_WIN:
        raise HTTPException(status_code=400, detail=f"Максимальный размер выигрыша — {MAX_STARS_WIN} ⭐")
    if req.stars < 50:
        raise HTTPException(status_code=400, detail="Минимальный размер выигрыша — 50 ⭐")

    database.get_or_create_user(req.user_id, req.username)

    full_cost = req.stars * STAR_PRICE_RUB
    cost = round((full_cost * (req.chance / 100.0)) * HOUSE_MARGIN, 2)

    # Атомарное списание: два одновременных запроса не уведут баланс в минус
    if database.try_spend_balance(req.user_id, cost) is None:
        raise HTTPException(status_code=400, detail="Недостаточно средств на балансе")
    
    ref_id, ref_bonus = database.reward_referrer_on_bet(req.user_id, cost, REF_PERCENT)
    if ref_id and ref_bonus > 0 and bot:
        try:
            await bot.send_message(
                ref_id,
                f"💰 <b>Партнерский бонус!</b>\n"
                f"Ваш реферал сделал ставку в рулетке на сумму <b>{cost:.2f} ₽</b>.\n"
                f"Вам начислено {REF_PERCENT * 100:g}%: <b>+{ref_bonus:.2f} ₽</b>!",
                parse_mode="HTML"
            )
        except Exception:
            pass
    
    roll = random.uniform(0, 100)
    is_win = roll <= req.chance
    
    tickets_won = 0
    payout_id = None
    
    if is_win:
        stars_won = req.stars
        try:
            database.log_live_drop(req.user_id, req.username, "win_stars", f"+{req.stars} ⭐", "⭐")
        except Exception:
            pass

        with database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO payout_requests (user_id, username, recipient, stars, cost, status)
                    VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING id;
                """, (req.user_id, req.username, req.recipient, stars_won, cost))
                row = cur.fetchone()
                payout_id = row["id"] if isinstance(row, dict) else row[0]
                conn.commit()
    else:
        stars_won = 0
        # Утешительный билет — только за ставку от MIN_COST_FOR_TICKET, иначе билеты фармятся за копейки
        if cost >= MIN_COST_FOR_TICKET:
            tickets_won = 1
            database.add_user_tickets(req.user_id, 1)

    database.log_game(req.user_id, req.username, req.stars, req.chance, cost, is_win, req.recipient, tickets_won)
    
    if is_win and bot and payout_id:
        clean_user = req.username.strip().lstrip("@")
        clean_recip = req.recipient.strip().lstrip("@")
        
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Отправить через StarsFlow", callback_data=f"payout_sf:{payout_id}")],
                [InlineKeyboardButton(text="✅ Выдано вручную", callback_data=f"payout_manual:{payout_id}")],
                [InlineKeyboardButton(text="❌ Отклонить (вернуть ставку)", callback_data=f"payout_cancel:{payout_id}")]
            ]
        )

        for admin_id in ADMIN_IDS:
            try:
                alert_text = (
                    "🔔 <b>НОВЫЙ ВЫИГРЫШ (Ожидает подтверждения)!</b>\n\n"
                    f"🆔 Заявка: <code>#{payout_id}</code>\n"
                    f"👤 Игрок: <b>@{h(clean_user)}</b> (ID: <code>{req.user_id}</code>)\n"
                    f"🎁 Выигрыш: <b>{req.stars} ⭐ Звёзд</b>\n"
                    f"🎯 Шанс: <b>{req.chance}%</b> (оплачено: {cost:.2f} ₽)\n"
                    f"📤 <b>Получатель:</b> @{clean_recip}"
                )
                await bot.send_message(admin_id, alert_text, parse_mode="HTML", reply_markup=keyboard)
            except Exception:
                pass

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


# ----------------- API: ЕЖЕДНЕВНЫЙ БОНУС -----------------

class DailyClaimRequest(BaseModel):
    user_id: int

@app.post("/api/daily/claim")
async def claim_daily(req: DailyClaimRequest, x_telegram_init_data: str = Header(default="")):
    require_auth(req.user_id, x_telegram_init_data)
    database.get_or_create_user(req.user_id)
    res = database.claim_daily_bonus(req.user_id, DAILY_REWARDS)
    if res.get("success"):
        res["daily"] = database.get_daily_status(req.user_id, DAILY_REWARDS)
    return res


# ----------------- API: ПОКУПКА STARS ЗА БАЛАНС (ФИКСИРОВАННАЯ ЦЕНА) -----------------

class StarsBuyRequest(BaseModel):
    user_id: int
    username: str
    stars: int
    recipient: str

@app.post("/api/shop/stars")
async def buy_stars(req: StarsBuyRequest, x_telegram_init_data: str = Header(default="")):
    req.username = require_auth(req.user_id, x_telegram_init_data, req.username)
    if not (MIN_STARS_BUY <= req.stars <= MAX_STARS_BUY):
        raise HTTPException(status_code=400, detail=f"Количество звёзд: от {MIN_STARS_BUY} до {MAX_STARS_BUY}")
    recipient = clean_recipient(req.recipient)

    database.get_or_create_user(req.user_id, req.username)
    price = round(req.stars * STAR_PRICE_RUB, 2)

    new_balance = database.try_spend_balance(req.user_id, price)
    if new_balance is None:
        raise HTTPException(status_code=400, detail="Недостаточно средств на балансе")

    payout_id = None
    try:
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO payout_requests (user_id, username, recipient, stars, cost, status, kind)
                    VALUES (%s, %s, %s, %s, %s, 'pending', 'purchase') RETURNING id;
                """, (req.user_id, req.username, recipient, req.stars, price))
                row = cur.fetchone()
                payout_id = row["id"] if isinstance(row, dict) else row[0]
                conn.commit()
    except Exception as e:
        logger.error(f"Ошибка создания заявки на покупку: {e}")
        database.update_user_balance(req.user_id, price)  # возвращаем деньги
        raise HTTPException(status_code=500, detail="Не удалось оформить заказ, деньги возвращены")

    if bot and payout_id:
        clean_user = req.username.strip().lstrip("@")
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Отправить через StarsFlow", callback_data=f"payout_sf:{payout_id}")],
                [InlineKeyboardButton(text="✅ Выдано вручную", callback_data=f"payout_manual:{payout_id}")],
                [InlineKeyboardButton(text="❌ Отклонить (вернуть деньги)", callback_data=f"payout_cancel:{payout_id}")]
            ]
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    "🛒 <b>ПОКУПКА STARS (оплачено с баланса)</b>\n\n"
                    f"🆔 Заявка: <code>#{payout_id}</code>\n"
                    f"👤 Покупатель: <b>@{h(clean_user)}</b> (ID: <code>{req.user_id}</code>)\n"
                    f"⭐ Количество: <b>{req.stars} ⭐</b>\n"
                    f"💰 Оплачено: <b>{price:.2f} ₽</b>\n"
                    f"📤 <b>Получатель:</b> @{recipient}",
                    parse_mode="HTML", reply_markup=keyboard
                )
            except Exception:
                pass

    return {"success": True, "price": price, "new_balance": round(new_balance, 2)}


# ----------------- API: ВНЕШНИЙ CRON ДЛЯ НАПОМИНАНИЙ -----------------

@app.get("/api/cron/daily")
async def cron_daily(key: str = ""):
    """Дёргается внешним cron раз в день (будит бесплатный Render). Защищён секретом CRON_SECRET."""
    if not CRON_SECRET or not hmac.compare_digest(key, CRON_SECRET):
        raise HTTPException(status_code=403, detail="forbidden")
    spawn(run_daily_reminder())  # отвечаем сразу, рассылка идёт в фоне
    return {"ok": True}
