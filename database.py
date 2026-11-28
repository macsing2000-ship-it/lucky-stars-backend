import os
from datetime import datetime, timedelta, timezone
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL", "")

# ---- Бонусы для привлечения игроков (всё настраивается через переменные окружения) ----
WELCOME_TICKETS = int(os.getenv("WELCOME_TICKETS", "3"))              # билеты новому игроку сразу
REF_TICKETS = int(os.getenv("REF_TICKETS", "1"))                      # билеты пригласившему за друга
REF_INVITEE_TICKETS = int(os.getenv("REF_INVITEE_TICKETS", "2"))      # доп. билеты приглашённому
FIRST_DEPOSIT_BONUS_PCT = float(os.getenv("FIRST_DEPOSIT_BONUS_PCT", "0.10"))  # % от первого пополнения
DEPOSIT_BONUS_PCT = float(os.getenv("DEPOSIT_BONUS_PCT", "0.05"))              # % от каждого следующего пополнения
FIRST_DEPOSIT_BONUS_MIN = float(os.getenv("FIRST_DEPOSIT_BONUS_MIN", "0"))     # минимум бонуса, ₽
FIRST_DEPOSIT_BONUS_MAX = float(os.getenv("FIRST_DEPOSIT_BONUS_MAX", "0"))     # максимум бонуса, ₽ (0 = без ограничения)

def get_connection():
    """Подключение к облачной базе данных PostgreSQL (Neon)"""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    """Инициализация таблиц и авто-миграция новых колонок."""
    if not DATABASE_URL:
        print("⚠️ ВНИМАНИЕ: DATABASE_URL не задан в Render Environment!")
        return

    with get_connection() as conn:
        with conn.cursor() as cursor:
            # Таблица пользователей
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance REAL DEFAULT 0.0,
                    tickets INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Авто-миграция недостающих колонок для имеющихся таблиц
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT DEFAULT NULL;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_count INTEGER DEFAULT 0;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_earned REAL DEFAULT 0.0;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS has_deposited BOOLEAN DEFAULT FALSE;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily DATE;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_streak INTEGER DEFAULT 0;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS app_opened_at TIMESTAMP;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked BOOLEAN DEFAULT FALSE;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_daily BOOLEAN DEFAULT TRUE;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_case_last DATE;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_case_at TIMESTAMP;")  # момент последнего открытия: кейс снова доступен через 24 ч
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reminder_log (
                    day DATE PRIMARY KEY,
                    sent INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Таблица игр
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS games (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username TEXT,
                    stars INTEGER,
                    chance INTEGER,
                    cost REAL,
                    is_win INTEGER,
                    tickets_won INTEGER DEFAULT 0,
                    recipient TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Таблица заявок на выплату
            cursor.execute("""
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
            """)

            # Тип заявки: 'win' — выигрыш в рулетке, 'purchase' — покупка Stars за баланс
            cursor.execute("ALTER TABLE payout_requests ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'win';")

            # Таблица Live-ленты
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS live_drops (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username TEXT,
                    event_type TEXT,
                    title TEXT,
                    icon TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Промокоды: код, награда (билеты/₽), лимит использований, срок жизни
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS promo_codes (
                    code TEXT PRIMARY KEY,
                    tickets INTEGER DEFAULT 0,
                    rub REAL DEFAULT 0,
                    max_uses INTEGER DEFAULT 0,
                    used INTEGER DEFAULT 0,
                    expires_at TIMESTAMP,
                    active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS promo_uses (
                    code TEXT,
                    user_id BIGINT,
                    used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (code, user_id)
                );
            """)
            # Инвентарь подарков из бесплатного кейса: owned -> requested -> delivered | sold
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_gifts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    gift_name TEXT,
                    icon TEXT,
                    value_rub NUMERIC(10, 2),
                    status TEXT DEFAULT 'owned',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("ALTER TABLE user_gifts ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'free';")
            # NFT из ежедневного кейса: картинка и срок жизни (не вывел вовремя — приз сгорает)
            cursor.execute("ALTER TABLE user_gifts ADD COLUMN IF NOT EXISTS image TEXT;")
            cursor.execute("ALTER TABLE user_gifts ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP;")
            # Квалификация друга для вывода NFT: Telegram Premium + рейтинг (уровень) не ниже порога
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tg_level INTEGER;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_qualified BOOLEAN DEFAULT FALSE;")
            cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS level_checked_at TIMESTAMP;")

            # Таблица открытий кейсов (для статистики и контроля реального RTP)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS case_opens (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username TEXT,
                    case_id TEXT,
                    cost NUMERIC(10, 2),
                    prize_type TEXT,
                    prize_title TEXT,
                    prize_value NUMERIC(10, 2),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
    print("✅ Облачная база данных PostgreSQL успешно инициализирована и обновлена!")

def mask_username(username: str) -> str:
    clean = (username or "").strip().lstrip("@")
    if not clean or clean.startswith("id"):
        return "@user***"
    if len(clean) <= 3:
        return f"@{clean[0]}***"
    return f"@{clean[:3]}***"

def get_or_create_user(user_id: int, username: str = "", referrer_id: int = None):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, username, balance, tickets, referrer_id, ref_count, ref_earned, has_deposited FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            
            clean_user = username.strip().lstrip("@") if username else f"id{user_id}"
            
            if not row:
                actual_ref = None
                if referrer_id and referrer_id != user_id:
                    cursor.execute("SELECT user_id FROM users WHERE user_id = %s", (referrer_id,))
                    if cursor.fetchone():
                        actual_ref = referrer_id
                        cursor.execute("UPDATE users SET tickets = tickets + %s, ref_count = ref_count + 1 WHERE user_id = %s", (REF_TICKETS, referrer_id))

                # Новичок получает приветственные билеты, а пришедший по реферальной ссылке — ещё и бонус приглашённого.
                # Билеты тратятся только в магазине подарков (а вывод подарков закрыт условиями), так что фарм аккаунтов невыгоден.
                start_tickets = WELCOME_TICKETS + (REF_INVITEE_TICKETS if actual_ref else 0)
                cursor.execute(
                    "INSERT INTO users (user_id, username, balance, tickets, referrer_id) VALUES (%s, %s, 0.0, %s, %s)",
                    (user_id, clean_user, start_tickets, actual_ref)
                )
                conn.commit()
                return {
                    "user_id": user_id,
                    "username": f"@{clean_user}",
                    "balance": 0.0,
                    "tickets": start_tickets,
                    "referrer_id": actual_ref,
                    "ref_count": 0,
                    "ref_earned": 0.0,
                    "has_deposited": False
                }
            
            db_uname = row["username"] or ""
            if clean_user and db_uname.lower() != clean_user.lower():
                cursor.execute("UPDATE users SET username = %s WHERE user_id = %s", (clean_user, user_id))
                conn.commit()
                db_uname = clean_user
                
            return {
                "user_id": row["user_id"],
                "username": f"@{db_uname}",
                "balance": float(row["balance"]),
                "tickets": row["tickets"],
                "referrer_id": row.get("referrer_id"),
                "ref_count": row.get("ref_count") or 0,
                "ref_earned": float(row.get("ref_earned") or 0.0),
                "has_deposited": row.get("has_deposited") or False
            }

def deposit_bonus_amount(deposit_rub: float, first: bool) -> float:
    bonus = deposit_rub * (FIRST_DEPOSIT_BONUS_PCT if first else DEPOSIT_BONUS_PCT)
    bonus = max(FIRST_DEPOSIT_BONUS_MIN if first else 0.0, bonus)
    if FIRST_DEPOSIT_BONUS_MAX > 0:
        bonus = min(FIRST_DEPOSIT_BONUS_MAX, bonus)
    return round(bonus, 2)

def process_deposit_bonus(user_id: int, deposit_rub: float):
    """Бонус к пополнению: FIRST_DEPOSIT_BONUS_PCT на первое, DEPOSIT_BONUS_PCT на каждое следующее.
    Признак «первое» снимается атомарно. Возвращает (бонус, было_первым)."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET has_deposited = TRUE WHERE user_id = %s AND COALESCE(has_deposited, FALSE) = FALSE RETURNING user_id",
                (user_id,))
            first = cursor.fetchone() is not None
            bonus = deposit_bonus_amount(deposit_rub, first)
            if bonus > 0:
                cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (bonus, user_id))
            conn.commit()
            return bonus, first


def reward_referrer_on_bet(user_id: int, bet_cost: float, percent: float = 0.03):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT referrer_id FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if row and row["referrer_id"]:
                ref_id = row["referrer_id"]
                ref_cut = round(bet_cost * percent, 2)
                if ref_cut > 0:
                    cursor.execute(
                        "UPDATE users SET balance = balance + %s, ref_earned = ref_earned + %s WHERE user_id = %s",
                        (ref_cut, ref_cut, ref_id)
                    )
                    conn.commit()
                    return ref_id, ref_cut
            return None, 0.0

def log_live_drop(user_id: int, username: str, event_type: str, title: str, icon: str):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO live_drops (user_id, username, event_type, title, icon) VALUES (%s, %s, %s, %s, %s)",
                (user_id, username, event_type, title, icon)
            )
            cursor.execute("DELETE FROM live_drops WHERE id NOT IN (SELECT id FROM live_drops ORDER BY id DESC LIMIT 50)")
            conn.commit()

def get_live_feed(limit: int = 15):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, username, event_type, title, icon, created_at FROM live_drops ORDER BY id DESC LIMIT %s", (limit,))
            rows = cursor.fetchall()
            
            feed = []
            for r in rows:
                feed.append({
                    "id": r["id"],
                    "user": mask_username(r["username"]),
                    "event_type": r["event_type"],
                    "title": r["title"],
                    "icon": r["icon"]
                })
            # Только реальные события: выдуманные выигрыши вводят игроков в заблуждение
            return feed

def find_user(query: str):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            clean_query = query.strip().lstrip("@")
            
            if clean_query.isdigit():
                cursor.execute("SELECT user_id, username, balance, tickets, ref_count, ref_earned, created_at FROM users WHERE user_id = %s", (int(clean_query),))
                row = cursor.fetchone()
                if row:
                    return {"user_id": row["user_id"], "username": f"@{row['username']}", "balance": float(row["balance"]), "tickets": row["tickets"], "ref_count": row.get("ref_count") or 0, "ref_earned": float(row.get("ref_earned") or 0.0), "created_at": row["created_at"]}
            
            cursor.execute("SELECT user_id, username, balance, tickets, ref_count, ref_earned, created_at FROM users WHERE LOWER(TRIM(LEADING '@' FROM username)) = LOWER(%s)", (clean_query,))
            row = cursor.fetchone()
            if row:
                return {"user_id": row["user_id"], "username": f"@{row['username']}", "balance": float(row["balance"]), "tickets": row["tickets"], "ref_count": row.get("ref_count") or 0, "ref_earned": float(row.get("ref_earned") or 0.0), "created_at": row["created_at"]}
                
            return None

def update_user_balance(user_id: int, amount: float):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))
            conn.commit()

def set_user_balance(user_id: int, exact_balance: float):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET balance = %s WHERE user_id = %s", (exact_balance, user_id))
            conn.commit()

def add_user_tickets(user_id: int, count: int = 1):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET tickets = tickets + %s WHERE user_id = %s", (count, user_id))
            conn.commit()

def log_game(user_id: int, username: str, stars: int, chance: int, cost: float, is_win: bool, recipient: str, tickets_won: int = 0):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            clean_user = username.strip().lstrip("@")
            clean_rec = recipient.strip().lstrip("@")
            cursor.execute(
                "INSERT INTO games (user_id, username, stars, chance, cost, is_win, tickets_won, recipient) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (user_id, clean_user, stars, chance, cost, 1 if is_win else 0, tickets_won, clean_rec)
            )
            conn.commit()

def get_stats():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) as count FROM users")
            total_users = cursor.fetchone()["count"]

            cursor.execute("SELECT COUNT(*) as total_games, COALESCE(SUM(cost), 0) as turnover, COALESCE(SUM(is_win), 0) as wins, COALESCE(SUM(tickets_won), 0) as tickets FROM games")
            games_row = cursor.fetchone()

            return {
                "total_users": total_users,
                "total_games": games_row["total_games"],
                "total_turnover": round(float(games_row["turnover"]), 2),
                "total_wins": games_row["wins"],
                "total_tickets": games_row["tickets"]
            }


# ===================== АТОМАРНЫЕ СПИСАНИЯ =====================

def try_spend_balance(user_id: int, amount: float):
    """Списывает amount с баланса одним запросом. Возвращает новый баланс или None, если средств не хватает."""
    if amount <= 0:
        return None
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET balance = balance - %s WHERE user_id = %s AND balance >= %s RETURNING balance",
                (amount, user_id, amount)
            )
            row = cursor.fetchone()
            conn.commit()
            return float(row["balance"]) if row else None

def try_spend_tickets(user_id: int, count: int):
    """Списывает билеты одним запросом. Возвращает новый остаток или None, если билетов не хватает."""
    if count <= 0:
        return None
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET tickets = tickets - %s WHERE user_id = %s AND tickets >= %s RETURNING tickets",
                (count, user_id, count)
            )
            row = cursor.fetchone()
            conn.commit()
            return int(row["tickets"]) if row else None

_CLAIMABLE_TABLES = ("payout_requests", "shop_orders")

def claim_request(table: str, req_id: int) -> bool:
    """Атомарно переводит заявку pending -> processing. True, если заявку «захватили» именно мы
    (защита от двойного нажатия кнопок админом и двойной выплаты)."""
    if table not in _CLAIMABLE_TABLES:
        raise ValueError("bad table")
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET status = 'processing' WHERE id = %s AND status = 'pending' RETURNING id",
                (req_id,)
            )
            ok = cursor.fetchone() is not None
            conn.commit()
            return ok

def release_request(table: str, req_id: int):
    """Возвращает заявку из processing в pending (например, если выплата не прошла)."""
    if table not in _CLAIMABLE_TABLES:
        raise ValueError("bad table")
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET status = 'pending' WHERE id = %s AND status = 'processing'", (req_id,))
            conn.commit()

def mark_app_opened(user_id: int):
    """Фиксирует, что игрок открыл Mini App (для воронки)."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET app_opened_at = CURRENT_TIMESTAMP WHERE user_id = %s AND app_opened_at IS NULL", (user_id,))
            conn.commit()


# ===================== ЕЖЕДНЕВНЫЙ БОНУС =====================

MSK = timezone(timedelta(hours=3))

def msk_today():
    return datetime.now(MSK).date()

def _daily_view(last_daily, streak, rewards):
    today = msk_today()
    streak = streak or 0
    n = len(rewards)
    if last_daily == today:
        can_claim = False
        current = streak
        done_days = ((streak - 1) % n) + 1 if streak > 0 else 0
    elif last_daily == today - timedelta(days=1):
        can_claim = True
        current = streak
        done_days = current % n
    else:
        can_claim = True
        current = 0
        done_days = 0
    return {
        "can_claim": can_claim,
        "streak": current,
        "done_days": done_days,
        "next_day": (done_days + 1) if can_claim else None,
        "rewards": [{"day": i + 1, "tickets": t, "rub": r} for i, (t, r) in enumerate(rewards)],
    }

def get_daily_status(user_id: int, rewards):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT last_daily, daily_streak FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if not row:
                return _daily_view(None, 0, rewards)
            return _daily_view(row["last_daily"], row["daily_streak"], rewards)

def claim_daily_bonus(user_id: int, rewards):
    """Выдаёт ежедневный бонус (билеты и/или рубли на баланс) не чаще раза в сутки по МСК.
    Серия дней подряд продвигает награду по кругу из len(rewards) дней."""
    today = msk_today()
    n = len(rewards)
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT last_daily, daily_streak FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
            row = cursor.fetchone()
            if not row:
                return {"success": False, "error": "Пользователь не найден"}

            last, streak = row["last_daily"], row["daily_streak"] or 0
            if last == today:
                return {"success": False, "error": "Сегодня бонус уже получен. Приходите завтра!"}

            streak = streak + 1 if last == today - timedelta(days=1) else 1
            tickets, rub = rewards[(streak - 1) % n]

            cursor.execute(
                "UPDATE users SET tickets = tickets + %s, balance = balance + %s, last_daily = %s, daily_streak = %s "
                "WHERE user_id = %s RETURNING balance, tickets",
                (tickets, rub, today, streak, user_id)
            )
            res = cursor.fetchone()
            conn.commit()
            return {
                "success": True,
                "day": ((streak - 1) % n) + 1,
                "streak": streak,
                "tickets": tickets,
                "rub": rub,
                "new_balance": round(float(res["balance"]), 2),
                "new_tickets": int(res["tickets"]),
            }


# ===================== КЕЙСЫ =====================

def log_case_open(user_id: int, username: str, case_id: str, cost: float,
                  prize_type: str, prize_title: str, prize_value: float):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO case_opens (user_id, username, case_id, cost, prize_type, prize_title, prize_value) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (user_id, username.strip().lstrip("@"), case_id, cost, prize_type, prize_title, prize_value)
            )
            conn.commit()

def get_case_stats():
    """Оборот кейсов и реальный RTP (сколько отдано от того, что принято) — по каждому кейсу и в сумме."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT case_id, COUNT(*) AS opens,
                       COALESCE(SUM(cost), 0) AS turnover,
                       COALESCE(SUM(prize_value), 0) AS paid
                FROM case_opens GROUP BY case_id ORDER BY case_id
            """)
            rows = cursor.fetchall()
            return [
                {"case_id": r["case_id"], "opens": r["opens"],
                 "turnover": round(float(r["turnover"]), 2), "paid": round(float(r["paid"]), 2)}
                for r in rows
            ]


# ===================== ЕЖЕДНЕВНЫЙ КЕЙС И ПОДАРКИ =====================

def claim_daily_case(user_id: int) -> bool:
    """Атомарно занимает открытие. Кейс доступен раз в 24 часа с момента прошлого открытия.
    Игроки, открывшие кейс по старому правилу (календарный день МСК), ждут до полуночи один раз."""
    today = msk_today()
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET daily_case_at = NOW() WHERE user_id = %s AND ("
                "(daily_case_at IS NOT NULL AND daily_case_at <= NOW() - INTERVAL '24 hours') OR "
                "(daily_case_at IS NULL AND (daily_case_last IS NULL OR daily_case_last < %s))) RETURNING user_id",
                (user_id, today)
            )
            ok = cursor.fetchone() is not None
            conn.commit()
            return ok

def release_daily_case(user_id: int):
    """Возвращает открытие, если приз выдать не удалось."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET daily_case_at = NULL, daily_case_last = NULL WHERE user_id = %s", (user_id,))
            conn.commit()

def get_daily_case_status(user_id: int):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT daily_case_last, EXTRACT(EPOCH FROM (daily_case_at + INTERVAL '24 hours' - NOW())) AS left_s "
                "FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
    if not row:
        return {"can_open": True, "seconds_left": 0}
    if row["left_s"] is not None:
        left = max(0, int(float(row["left_s"])))
        return {"can_open": left <= 0, "seconds_left": left}
    today, last = msk_today(), row["daily_case_last"]
    if last is not None and last >= today:   # открыт по старому правилу — ждём до полуночи МСК
        nxt = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=MSK)
        return {"can_open": False, "seconds_left": max(0, int((nxt - datetime.now(MSK)).total_seconds()))}
    return {"can_open": True, "seconds_left": 0}

def create_user_gift(user_id: int, name: str, icon: str, value: float, source: str = "free",
                     image: str = None, ttl_hours: float = None) -> int:
    """ttl_hours задаёт срок жизни подарка: NFT из ежедневного кейса сгорает, если его не вывели вовремя."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO user_gifts (user_id, gift_name, icon, value_rub, source, image, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, "
                "CASE WHEN %s::float IS NULL THEN NULL ELSE NOW() + %s::float * INTERVAL '1 hour' END) RETURNING id",
                (user_id, name, icon, value, source, image, ttl_hours, ttl_hours)
            )
            gid = cursor.fetchone()["id"]
            conn.commit()
            return gid


def expire_overdue_gifts(user_id: int = None):
    """Сжигает просроченные подарки (owned -> expired). Возвращает список сгоревших."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            q = ("UPDATE user_gifts SET status = 'expired' WHERE status = 'owned' "
                 "AND expires_at IS NOT NULL AND expires_at <= NOW()")
            args = ()
            if user_id is not None:
                q += " AND user_id = %s"
                args = (user_id,)
            cursor.execute(q + " RETURNING id, user_id, gift_name, icon, value_rub", args)
            rows = [dict(r) for r in cursor.fetchall()]
            conn.commit()
            return rows

_GIFT_COLS = ("id, gift_name, icon, value_rub, status, source, image, "
              "CASE WHEN expires_at IS NULL THEN NULL "
              "ELSE GREATEST(0, EXTRACT(EPOCH FROM (expires_at - NOW()))) END AS seconds_left")

def _gift_row(r):
    left = r["seconds_left"]
    return {"id": r["id"], "name": r["gift_name"], "icon": r["icon"], "value": float(r["value_rub"]),
            "status": r["status"], "source": r["source"] or "free", "image": r["image"],
            "seconds_left": int(left) if (left is not None and r["status"] == "owned") else None}

def list_user_gifts(user_id: int):
    expire_overdue_gifts(user_id)
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT {_GIFT_COLS} FROM user_gifts "
                "WHERE user_id = %s AND status IN ('owned', 'requested') ORDER BY id DESC LIMIT 50",
                (user_id,)
            )
            return [_gift_row(r) for r in cursor.fetchall()]


def get_user_gift(gift_id: int, user_id: int):
    """Подарок игрока (для проверки источника, статуса и срока перед продажей/выводом)."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT {_GIFT_COLS} FROM user_gifts WHERE id = %s AND user_id = %s",
                (gift_id, user_id)
            )
            r = cursor.fetchone()
            return _gift_row(r) if r else None


def take_gift(gift_id: int, from_status: str, to_status: str, user_id: int = None):
    """Атомарно переводит подарок из одного статуса в другой. Возвращает строку подарка или None
    (защита от двойной продажи/вывода и от чужих подарков, если задан user_id).
    Просроченный подарок (сгорающий NFT) из 'owned' уже не забрать."""
    alive = " AND (expires_at IS NULL OR expires_at > NOW())" if from_status == "owned" else ""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            if user_id is None:
                cursor.execute(
                    "UPDATE user_gifts SET status = %s WHERE id = %s AND status = %s" + alive +
                    " RETURNING id, user_id, gift_name, icon, value_rub",
                    (to_status, gift_id, from_status)
                )
            else:
                cursor.execute(
                    "UPDATE user_gifts SET status = %s WHERE id = %s AND user_id = %s AND status = %s" + alive +
                    " RETURNING id, user_id, gift_name, icon, value_rub",
                    (to_status, gift_id, user_id, from_status)
                )
            row = cursor.fetchone()
            conn.commit()
            return dict(row) if row else None


def set_premium_flag(user_id: int, is_premium: bool):
    """Запоминаем наличие Telegram Premium из подписанных initData (обновляется при каждом заходе)."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET is_premium = %s WHERE user_id = %s AND is_premium IS DISTINCT FROM %s",
                (is_premium, user_id, is_premium)
            )
            conn.commit()

def count_qualified_friends(user_id: int) -> int:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM users WHERE referrer_id = %s AND ref_qualified = TRUE", (user_id,))
            return int(cursor.fetchone()["n"])

def list_friends_to_check(referrer_id: int, limit: int = 30, recheck_minutes: int = 10):
    """Друзья с Premium, чей уровень ещё не подтверждён (не чаще раза в recheck_minutes на друга)."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM users WHERE referrer_id = %s AND is_premium = TRUE "
                "AND COALESCE(ref_qualified, FALSE) = FALSE "
                "AND (level_checked_at IS NULL OR level_checked_at < NOW() - %s * INTERVAL '1 minute') "
                "ORDER BY user_id LIMIT %s",
                (referrer_id, recheck_minutes, limit)
            )
            return [r["user_id"] for r in cursor.fetchall()]

def save_level_check(user_id: int, level, qualified: bool):
    """Квалификация необратима: однажды засчитанный друг остаётся засчитанным."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE users SET tg_level = %s, level_checked_at = NOW(), "
                "ref_qualified = (COALESCE(ref_qualified, FALSE) OR %s) WHERE user_id = %s",
                (level, bool(qualified), user_id)
            )
            conn.commit()

# ===================== ПРОМОКОДЫ =====================

def create_promo(code: str, tickets: int, rub: float, max_uses: int, days: int = None):
    code = code.strip().upper()
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO promo_codes (code, tickets, rub, max_uses, expires_at) "
                "VALUES (%s, %s, %s, %s, CASE WHEN %s::int IS NULL THEN NULL ELSE NOW() + %s::int * INTERVAL '1 day' END) "
                "ON CONFLICT (code) DO UPDATE SET tickets = EXCLUDED.tickets, rub = EXCLUDED.rub, "
                "max_uses = EXCLUDED.max_uses, expires_at = EXCLUDED.expires_at, active = TRUE",
                (code, tickets, rub, max_uses, days, days)
            )
            conn.commit()

def set_promo_active(code: str, active: bool) -> bool:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE promo_codes SET active = %s WHERE code = %s RETURNING code", (active, code.strip().upper()))
            ok = cursor.fetchone() is not None
            conn.commit()
            return ok

def list_promos(limit: int = 20):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT code, tickets, rub, max_uses, used, active, expires_at FROM promo_codes ORDER BY created_at DESC LIMIT %s",
                (limit,)
            )
            return [dict(r) for r in cursor.fetchall()]

def redeem_promo(user_id: int, code: str) -> dict:
    """Активация промокода: один раз на игрока, с лимитом использований и сроком. Всё в одной транзакции."""
    code = (code or "").strip().upper()
    if not code or len(code) > 40:
        return {"success": False, "error": "Введите промокод"}
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT code, tickets, rub, max_uses, used, active, "
                "(expires_at IS NOT NULL AND expires_at <= NOW()) AS expired "
                "FROM promo_codes WHERE code = %s FOR UPDATE", (code,)
            )
            p = cursor.fetchone()
            if not p or not p["active"]:
                return {"success": False, "error": "Промокод не найден"}
            if p["expired"]:
                return {"success": False, "error": "Срок действия промокода истёк"}
            if p["max_uses"] and p["used"] >= p["max_uses"]:
                return {"success": False, "error": "Промокод уже закончился"}
            cursor.execute(
                "INSERT INTO promo_uses (code, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING code",
                (code, user_id)
            )
            if cursor.fetchone() is None:
                return {"success": False, "error": "Вы уже активировали этот промокод"}
            cursor.execute("UPDATE promo_codes SET used = used + 1 WHERE code = %s", (code,))
            cursor.execute(
                "UPDATE users SET tickets = tickets + %s, balance = balance + %s WHERE user_id = %s RETURNING balance, tickets",
                (p["tickets"] or 0, p["rub"] or 0, user_id)
            )
            u = cursor.fetchone()
            if not u:
                conn.rollback()
                return {"success": False, "error": "Пользователь не найден"}
            conn.commit()
            return {"success": True, "tickets": p["tickets"] or 0, "rub": float(p["rub"] or 0),
                    "new_balance": round(float(u["balance"]), 2), "new_tickets": int(u["tickets"])}


def count_deposited_friends(user_id: int) -> int:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM users WHERE referrer_id = %s AND has_deposited = TRUE", (user_id,))
            return int(cursor.fetchone()["n"])


# ===================== ВОРОНКА ДЛЯ /stats =====================

def get_funnel():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE app_opened_at IS NOT NULL) AS opened,
                    COUNT(*) FILTER (WHERE has_deposited) AS deposited,
                    COUNT(*) FILTER (WHERE referrer_id IS NOT NULL) AS referred,
                    COUNT(*) FILTER (WHERE last_daily IS NOT NULL) AS daily_users,
                    COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '1 day') AS new_24h,
                    COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '7 days') AS new_7d
                FROM users
            """)
            u = cursor.fetchone()

            cursor.execute("""
                SELECT
                    COUNT(*) AS played,
                    COUNT(*) FILTER (WHERE n >= 2) AS played_2plus,
                    COUNT(*) FILTER (WHERE d >= 2) AS returned
                FROM (
                    SELECT user_id, COUNT(*) AS n, COUNT(DISTINCT created_at::date) AS d
                    FROM games GROUP BY user_id
                ) t
            """)
            g = cursor.fetchone()

            return {
                "total": u["total"], "opened": u["opened"], "deposited": u["deposited"],
                "referred": u["referred"], "daily_users": u["daily_users"],
                "new_24h": u["new_24h"], "new_7d": u["new_7d"],
                "played": g["played"] or 0, "played_2plus": g["played_2plus"] or 0,
                "returned": g["returned"] or 0,
            }


# ===================== РАССЫЛКИ И НАПОМИНАНИЯ =====================

def get_broadcast_recipients():
    """Все, кто запускал бота и не заблокировал его."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id FROM users WHERE blocked IS NOT TRUE ORDER BY user_id")
            return [r["user_id"] for r in cursor.fetchall()]

def set_blocked(user_id: int, blocked: bool):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET blocked = %s WHERE user_id = %s", (blocked, user_id))
            conn.commit()

def set_notify_daily(user_id: int, on: bool):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET notify_daily = %s WHERE user_id = %s", (on, user_id))
            conn.commit()

def get_reminder_targets():
    """Кому напомнить про ежедневный бонус: брали его за последние 7 дней, но сегодня ещё не брали.
    Те, кто игнорирует напоминания дольше недели, выпадают из списка сами (без спама)."""
    today = msk_today()
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT user_id, daily_streak, last_daily FROM users
                WHERE blocked IS NOT TRUE AND notify_daily IS NOT FALSE
                  AND last_daily IS NOT NULL AND last_daily < %s AND last_daily >= %s
                """,
                (today, today - timedelta(days=7))
            )
            return [
                {"user_id": r["user_id"], "streak": r["daily_streak"] or 0,
                 "at_risk": r["last_daily"] == today - timedelta(days=1)}
                for r in cursor.fetchall()
            ]

def claim_reminder_day(day) -> bool:
    """True, если напоминания за этот день ещё не отправлялись (защита от дублей)."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO reminder_log (day) VALUES (%s) ON CONFLICT DO NOTHING RETURNING day", (day,))
            ok = cursor.fetchone() is not None
            conn.commit()
            return ok

def finish_reminder_day(day, sent: int):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE reminder_log SET sent = %s WHERE day = %s", (sent, day))
            conn.commit()
