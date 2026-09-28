import os
import random
import logging
import httpx
import hmac
import hashlib
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp,
    LabeledPrice, PreCheckoutQuery, CallbackQuery
)

import database

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализируем базу данных
try:
    database.init_db()
except Exception as e:
    logger.error(f"Ошибка БД при старте: {e}")

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
STARSFLOW_TOKEN = os.getenv("STARSFLOW_TOKEN", "").strip()
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "").strip()

WEBAPP_URL = "https://elaborate-pie-a4fa50.netlify.app"
BASE_URL = "https://lucky-stars-backend.onrender.com"

# КУРСЫ ВАЛЮТ
STAR_PRICE_RUB = 1.95       # Стоимость 1 звезды в игре (при ставках)
STARS_DEPOSIT_RATE = 1.50   # Зачисление рублей за 1 ⭐ пополнения

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

def resolve_target_user(query: str, sender: types.User):
    target = database.find_user(query)
    if not target and sender:
        clean_q = query.strip().lstrip("@").lower()
        sender_uname = (sender.username or "").strip().lstrip("@").lower()
        sender_id_str = str(sender.id)
        
        if clean_q == sender_uname or clean_q == sender_id_str or clean_q == "me":
            uname = sender.username or sender.first_name
            target = database.get_or_create_user(sender.id, uname)
    return target


# ----------------- АВТО-ОТПРАВКА ЗВЁЗД ЧЕРЕЗ STARSFLOW API -----------------

async def send_stars_via_starsflow(recipient: str, amount: int) -> tuple:
    if not STARSFLOW_TOKEN:
        return False, "STARSFLOW_TOKEN не задан в настройках Render!"

    clean_username = recipient.replace("@", "").strip()
    if not clean_username:
        return False, "Неверный формат юзернейма получателя"

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
            status = response.status_code
            if status in (200, 201):
                res_data = response.json()
                order_id = res_data.get("order", {}).get("id", "OK")
                return True, f"Заказ #{order_id} принят StarsFlow!"
            else:
                return False, f"Код {status}: {response.text}"
    except Exception as e:
        return False, f"Ошибка сети: {str(e)}"


# ----------------- ОБРАБОТКА КНОПОК ПОДТВЕРЖДЕНИЯ ВЫПЛАТЫ -----------------

@dp.callback_query(F.data.startswith("payout_"))
async def handle_payout_action(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[0]   # payout_sf, payout_manual, payout_cancel
    req_id = int(parts[1])

    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, username, recipient, stars, cost, status FROM payout_requests WHERE id = %s", (req_id,))
            req = cur.fetchone()

    if not req:
        await callback.answer("Заявка не найдена в базе!", show_alert=True)
        return

    p_id, u_id, uname, recip, stars, cost, status = req["id"], req["user_id"], req["username"], req["recipient"], req["stars"], req["cost"], req["status"]

    if status != 'pending':
        await callback.answer(f"Эта заявка уже обработана (статус: {status})!", show_alert=True)
        return

    admin_mention = f"@{callback.from_user.username}" if callback.from_user.username else callback.from_user.first_name

    if action == "payout_sf":
        await callback.answer("Отправляем запрос в StarsFlow...", show_alert=False)
        success, details = await send_stars_via_starsflow(recip, stars)
        
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

            if bot:
                try:
                    await bot.send_message(
                        u_id,
                        f"🎉 <b>Ваш выигрыш отправлен!</b>\n\n"
                        f"🎁 <b>+{stars} ⭐ Telegram Stars</b> успешно доставлены на аккаунт <b>{recip}</b>!",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
        else:
            await callback.message.reply(f"❌ Ошибка StarsFlow при отправке #{p_id}:\n<code>{details}</code>", parse_mode="HTML")

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
                    f"🎉 <b>Ваш выигрыш подтверждён!</b>\n\n"
                    f"🎁 <b>{stars} ⭐ Telegram Stars</b> успешно выданы на аккаунт <b>{recip}</b>!",
                    parse_mode="HTML"
                )
            except Exception:
                pass
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
                    f"⚠️ <b>Заявка на выигрыш {stars} ⭐ отклонена администратором.</b>\n\n"
                    f"💰 Сумма ставки (<b>+{cost:.2f} ₽</b>) полностью возвращена на ваш игровой баланс.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        await callback.answer("Заявка отменена, ставка возвращена!")


# ----------------- КОМАНДЫ ДЛЯ ОБЫЧНЫХ ПОЛЬЗОВАТЕЛЕЙ -----------------

@dp.message(Command("start"))
async def start_handler(message: types.Message, command: CommandObject = None):
    user_id = message.from_user.id
    uname = message.from_user.username or message.from_user.first_name or f"id{user_id}"
    
    referrer_id = None
    if command and command.args and command.args.startswith("ref_"):
        ref_str = command.args.replace("ref_", "").strip()
        if ref_str.isdigit():
            referrer_id = int(ref_str)

    user = database.get_or_create_user(user_id, uname, referrer_id=referrer_id)

    text = (
        f"👋 <b>Добро пожаловать в Lucky Buy!</b>\n\n"
        f"Здесь ты можешь выиграть и забрать <b>Telegram Stars ⭐</b> с повышенным шансом!\n\n"
        f"💰 Твой баланс: <b>{user['balance']:.2f} ₽</b>\n"
        f"🎟 Билеты: <b>{user['tickets']} шт.</b>\n\n"
        f"👥 <b>Приглашай друзей:</b>\nПолучай <b>10% от каждой их ставки</b> + <b>1 билет 🎟</b> за друга!"
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
        "🎟 <code>/tickets @username количество</code> — выдать билеты\n"
        "🧪 <code>/testwin количество</code> — прямой тест StarsFlow"
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
    
    success, details = await send_stars_via_starsflow(username, amount)
    if success:
        await message.answer(f"✅ <b>УСПЕХ! Звёзды отправлены!</b>\n\n{details}", parse_mode="HTML")
    else:
        await message.answer(f"❌ <b>Тест не удался!</b>\n\n<code>{details}</code>", parse_mode="HTML")

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
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка получения статистики: {e}")
        await message.answer(f"❌ Ошибка статистики: {e}")

@dp.message(Command("user"))
async def user_info_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("⚠️ Формат: <code>/user @username</code> или ID", parse_mode="HTML")
        return

    target = resolve_target_user(parts[1], message.from_user)
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

    target = resolve_target_user(parts[1], message.from_user)
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

@dp.message(Command("take"))
async def take_balance_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("⚠️ Формат: <code>/take @username сумма</code>", parse_mode="HTML")
        return

    target = resolve_target_user(parts[1], message.from_user)
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

@dp.message(Command("setbalance"))
async def set_balance_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("⚠️ Формат: <code>/setbalance @username сумма</code>", parse_mode="HTML")
        return

    target = resolve_target_user(parts[1], message.from_user)
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

    target = resolve_target_user(parts[1], message.from_user)
    if not target:
        await message.answer("❌ Пользователь не найден.", parse_mode="HTML")
        return

    try:
        count = int(parts[2])
    except ValueError:
        await message.answer("❌ Количество должно быть числом.", parse_mode="HTML")
        return

    database.add_user_tickets(target["user_id"], count)
    new_data = database.find_user(str(target["user_id"]))
    await message.answer(
        f"✅ Билеты для {target['username']}: <b>{new_data['tickets']} шт.</b>",
        parse_mode="HTML"
    )


# ----------------- ОПЛАТА TELEGRAM STARS (XTR) -----------------

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(lambda message: message.successful_payment is not None)
async def successful_payment(message: types.Message):
    payment = message.successful_payment
    user_id = message.from_user.id
    
    payload = payment.invoice_payload
    try:
        stars_amount = int(payload.split("_")[1])
    except Exception:
        stars_amount = payment.total_amount

    rub_added = stars_amount * STARS_DEPOSIT_RATE
    database.update_user_balance(user_id, rub_added)
    
    # Приветственный бонус +10 ₽ за первый депозит
    bonus = database.process_first_deposit_bonus(user_id)

    bonus_msg = f"\n🎁 <b>+10.00 ₽</b> бонуса за ваш первый депозит!" if bonus > 0 else ""

    await message.answer(
        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
        f"Зачислено за {stars_amount} ⭐: <b>+{rub_added:.2f} ₽</b>{bonus_msg}",
        parse_mode="HTML"
    )


# ----------------- ЖИЗНЕННЫЙ ЦИКЛ FASTAPI -----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if bot:
        try:
            await bot.set_webhook(
                url=f"{BASE_URL}/webhook", 
                drop_pending_updates=False,
                allowed_updates=["message", "callback_query", "pre_checkout_query"]
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
        logger.error(f"Ошибка апдейта Telegram: {e}")
    return {"ok": True}


# ----------------- LIVE-ЛЕНТА -----------------

@app.get("/api/live-feed")
def get_live_feed_endpoint():
    return database.get_live_feed()


# ----------------- ВЕБХУК ДЛЯ CRYPTOBOT -----------------

@app.post("/api/cryptobot/webhook")
async def cryptobot_webhook(request: Request):
    try:
        body_bytes = await request.body()
        signature = request.headers.get("crypto-pay-api-signature", "")
        
        if CRYPTOBOT_TOKEN and signature:
            secret = hashlib.sha256(CRYPTOBOT_TOKEN.encode()).digest()
            calculated_sig = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(calculated_sig, signature):
                raise HTTPException(status_code=400, detail="Invalid signature")

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
                rub_to_add = round(usdt_paid * 95.0, 2)
            
            if user_id_str and user_id_str.isdigit():
                user_id = int(user_id_str)
                database.update_user_balance(user_id, rub_to_add)
                
                # Приветственный бонус за первый депозит
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
async def create_stars_invoice(req: InvoiceRequest):
    if not bot:
        raise HTTPException(status_code=500, detail="Бот не инициализирован")
    if req.stars < 10:
        raise HTTPException(status_code=400, detail="Минимум 10 звезд")

    try:
        title = f"Пополнение на {req.stars} ⭐"
        description = f"Зачисление средств на игровой баланс Lucky Buy"
        payload = f"stars_{req.stars}"
        currency = "XTR"
        prices = [LabeledPrice(label="Telegram Stars", amount=req.stars)]

        invoice_link = await bot.create_invoice_link(
            title=title, description=description, payload=payload, currency=currency, prices=prices
        )
        return {"invoice_link": invoice_link}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Ошибка инвойса")


class CryptoBotRequest(BaseModel):
    user_id: int
    amount_rub: float

@app.post("/api/deposit/cryptobot")
async def create_cryptobot_invoice(req: CryptoBotRequest):
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
        "description": f"Пополнение Lucky Buy на {req.amount_rub:.0f} ₽",
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


# ----------------- API: ПОКУПКА В МАГАЗИНЕ ПОДАРКОВ -----------------

class ShopBuyRequest(BaseModel):
    user_id: int
    username: str
    gift_name: str
    ticket_cost: int

@app.post("/api/shop/buy")
async def buy_shop_gift(req: ShopBuyRequest):
    user = database.get_or_create_user(req.user_id, req.username)
    
    if user["tickets"] < req.ticket_cost:
        raise HTTPException(status_code=400, detail=f"Не хватает билетов! Нужно {req.ticket_cost} 🎟")

    database.add_user_tickets(req.user_id, -req.ticket_cost)
    updated_user = database.get_or_create_user(req.user_id)

    # Пушим покупку подарка в Live-ленту
    gift_icon = "🎁"
    if "Сердечко" in req.gift_name: gift_icon = "💖"
    elif "Мишка" in req.gift_name: gift_icon = "🧸"
    elif "Роза" in req.gift_name: gift_icon = "🌹"
    elif "Торт" in req.gift_name: gift_icon = "🎂"
    elif "Кольцо" in req.gift_name: gift_icon = "💍"
    
    database.log_live_drop(req.user_id, req.username, "buy_gift", req.gift_name, gift_icon)

    clean_user = req.username.strip().lstrip("@")
    if bot:
        for admin_id in ADMIN_IDS:
            try:
                alert_text = (
                    "🛍 <b>НОВЫЙ ЗАКАЗ В МАГАЗИНЕ ПОДАРКОВ!</b>\n\n"
                    f"👤 Игрок: <b>@{clean_user}</b> (ID: <code>{req.user_id}</code>)\n"
                    f"🎁 Товар: <b>{req.gift_name}</b>\n"
                    f"🎟 Списано билетов: <b>{req.ticket_cost} шт.</b>\n"
                    f"📊 Остаток: <b>{updated_user['tickets']} шт.</b>\n\n"
                    f"👉 <a href='https://t.me/{clean_user}'>Диалог с игроком</a>"
                )
                await bot.send_message(admin_id, alert_text, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                pass

    return {
        "success": True,
        "gift_name": req.gift_name,
        "new_tickets": updated_user["tickets"]
    }


# ----------------- API ДЛЯ РУЛЕТКИ -----------------

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
    
    # Списываем стоимость игры
    database.update_user_balance(req.user_id, -cost)
    
    # Начисляем рефереру 10% от ставки
    database.reward_referrer_on_bet(req.user_id, cost)
    
    roll = random.uniform(0, 100)
    is_win = roll <= req.chance
    
    tickets_won = 0
    payout_id = None
    
    if is_win:
        stars_won = req.stars
        # Пушим победу в Live-ленту
        database.log_live_drop(req.user_id, req.username, "win_stars", f"+{req.stars} ⭐", "⭐")

        # Создаем заявку на выплату админом
        with database.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO payout_requests (user_id, username, recipient, stars, cost, status)
                    VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING id;
                """, (req.user_id, req.username, req.recipient, stars_won, cost))
                payout_id = cur.fetchone()["id"]
                conn.commit()
    else:
        stars_won = 0
        tickets_won = 1
        database.add_user_tickets(req.user_id, 1)

    database.log_game(req.user_id, req.username, req.stars, req.chance, cost, is_win, req.recipient, tickets_won)
    
    # Уведомление админу
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
                    f"👤 Игрок: <b>@{clean_user}</b> (ID: <code>{req.user_id}</code>)\n"
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
# Добавь это к списку команд в main.py
@dp.message(Command("rules"))
async def rules_handler(message: types.Message):
    text = (
        "⚖️ <b>Пользовательское соглашение Lucky Buy</b>\n\n"
        "1. Сервис предоставляет услуги по приобретению цифрового контента (Telegram Stars) "
        "и подарков в интерактивном формате (программа лояльности).\n"
        "2. Пополняя баланс, вы приобретаете виртуальные баллы сервиса, которые не подлежат "
        "обмену на фиатные деньги (рубли, доллары и т.д.).\n"
        "3. Выигрыши выплачиваются исключительно в виде цифровых активов внутри Telegram.\n"
        "4. Все операции по пополнению являются финальными и не подлежат возврату.\n"
        "5. Участвуя в игре, вы подтверждаете, что вам исполнилось 18 лет.\n\n"
        "<i>Данный сервис не является азартной игрой в понимании законодательства РФ, "
        "так как не производит выплаты в денежной форме.</i>"
    )
    await message.answer(text, parse_mode="HTML")

# В функцию start_handler добавь в конец текста:
# "Начиная игру, вы принимаете условия /rules"
