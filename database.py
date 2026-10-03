import os
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

def reward_referrer_on_bet(user_id: int, bet_cost: float):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT referrer_id FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if row and row["referrer_id"]:
                ref_id = row["referrer_id"]
                ref_cut = round(bet_cost * 0.10, 2)
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
            
            if not feed:
                feed = [
                    {"id": 1, "user": "@rom***", "event_type": "win_stars", "title": "+300 ⭐", "icon": "⭐"},
                    {"id": 2, "user": "@art***", "event_type": "buy_gift", "title": "Мишка", "icon": "🧸"},
                    {"id": 3, "user": "@nik***", "event_type": "win_stars", "title": "+1 000 ⭐", "icon": "⭐"},
                    {"id": 4, "user": "@dmi***", "event_type": "buy_gift", "title": "Сердечко", "icon": "💖"},
                    {"id": 5, "user": "@ale***", "event_type": "win_stars", "title": "+500 ⭐", "icon": "⭐"}
                ]
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
