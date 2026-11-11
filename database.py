import os
from datetime import datetime, timedelta, timezone
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL", "")

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

            # Таблица заявок на ручное пополнение по СБП
            cursor.execute("""
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
                        cursor.execute("UPDATE users SET tickets = tickets + 1, ref_count = ref_count + 1 WHERE user_id = %s", (referrer_id,))

                cursor.execute(
                    "INSERT INTO users (user_id, username, balance, tickets, referrer_id) VALUES (%s, %s, 0.0, 0, %s)",
                    (user_id, clean_user, actual_ref)
                )
                conn.commit()
                return {
                    "user_id": user_id,
                    "username": f"@{clean_user}",
                    "balance": 0.0,
                    "tickets": 0,
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

def process_first_deposit_bonus(user_id: int) -> float:
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT has_deposited FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if row and not row["has_deposited"]:
                bonus = 10.0
                cursor.execute("UPDATE users SET balance = balance + %s, has_deposited = TRUE WHERE user_id = %s", (bonus, user_id))
                conn.commit()
                return bonus
            return 0.0

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

_CLAIMABLE_TABLES = ("payout_requests", "shop_orders", "sbp_deposits")

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
