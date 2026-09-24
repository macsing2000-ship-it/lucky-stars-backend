import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL", "")

def get_connection():
    """Подключение к облачной базе данных PostgreSQL (Neon)"""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    """Инициализация таблиц в PostgreSQL при первом запуске."""
    if not DATABASE_URL:
        print("⚠️ ВНИМАНИЕ: DATABASE_URL не задан! База данных не подключена.")
        return

    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance REAL DEFAULT 0.0,
                    tickets INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
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
                )
            """)
            conn.commit()
    print("✅ Облачная база данных PostgreSQL успешно инициализирована!")

def get_or_create_user(user_id: int, username: str = ""):
    """Получить данные пользователя. Если новый — создать."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT user_id, username, balance, tickets FROM users WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            
            clean_user = username.strip().lstrip("@") if username else f"id{user_id}"
            
            if not row:
                cursor.execute(
                    "INSERT INTO users (user_id, username, balance, tickets) VALUES (%s, %s, %s, %s)",
                    (user_id, clean_user, 0.0, 0)
                )
                conn.commit()
                return {"user_id": user_id, "username": f"@{clean_user}", "balance": 0.0, "tickets": 0}
            
            if clean_user and row["username"].lower() != clean_user.lower():
                cursor.execute("UPDATE users SET username = %s WHERE user_id = %s", (clean_user, user_id))
                conn.commit()
                
            return {"user_id": row["user_id"], "username": f"@{row['username']}", "balance": row["balance"], "tickets": row["tickets"]}

def find_user(query: str):
    """Поиск пользователя по user_id или username (работает и с @, и без @)."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            clean_query = query.strip().lstrip("@")
            
            # 1. Поиск по ID
            if clean_query.isdigit():
                cursor.execute("SELECT user_id, username, balance, tickets, created_at FROM users WHERE user_id = %s", (int(clean_query),))
                row = cursor.fetchone()
                if row:
                    return {"user_id": row["user_id"], "username": f"@{row['username']}", "balance": row["balance"], "tickets": row["tickets"], "created_at": row["created_at"]}
            
            # 2. Поиск по username (без учета регистра)
            cursor.execute("SELECT user_id, username, balance, tickets, created_at FROM users WHERE LOWER(username) = LOWER(%s)", (clean_query,))
            row = cursor.fetchone()
            if row:
                return {"user_id": row["user_id"], "username": f"@{row['username']}", "balance": row["balance"], "tickets": row["tickets"], "created_at": row["created_at"]}
                
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
            cursor.execute("SELECT COUNT(*) FROM users")
            total_users = cursor.fetchone()["count"]

            cursor.execute("SELECT COUNT(*), COALESCE(SUM(cost), 0), COALESCE(SUM(is_win), 0), COALESCEC(SUM(tickets_won), 0) FROM games")
            games_row = cursor.fetchone()
            # В psycopg2 ключи могут отличаться в зависимости от версии, собираем безопасно:
            vals = list(games_row.values()) if isinstance(games_row, dict) else games_row
            
            return {
                "total_users": total_users,
                "total_games": vals[0],
                "total_turnover": round(float(vals[1]), 2),
                "total_wins": vals[2],
                "total_tickets": vals[3]
            }
