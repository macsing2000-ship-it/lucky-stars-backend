import sqlite3

DB_NAME = "game.db"

def init_db():
    """Инициализация таблиц базы данных при первом запуске."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0.0,
                tickets INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
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

def get_or_create_user(user_id: int, username: str = ""):
    """Получить данные пользователя. Если новый — создать с балансом 0 руб и 0 билетов."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, balance, tickets FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            clean_username = username.strip() if username else f"id{user_id}"
            cursor.execute(
                "INSERT INTO users (user_id, username, balance, tickets) VALUES (?, ?, ?, ?)",
                (user_id, clean_username, 0.0, 0)
            )
            conn.commit()
            return {"user_id": user_id, "username": clean_username, "balance": 0.0, "tickets": 0}
        
        # Обновляем username если он изменился
        if username and row[1] != username:
            cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username.strip(), user_id))
            conn.commit()
            
        return {"user_id": row[0], "username": row[1], "balance": row[2], "tickets": row[3]}

def find_user(query: str):
    """Поиск пользователя по user_id или @username."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        query = query.strip().replace("@", "")
        if query.isdigit():
            cursor.execute("SELECT user_id, username, balance, tickets, created_at FROM users WHERE user_id = ?", (int(query),))
        else:
            cursor.execute("SELECT user_id, username, balance, tickets, created_at FROM users WHERE LOWER(username) = LOWER(?) OR LOWER(username) = LOWER(?)", (query, f"@{query}"))
        
        row = cursor.fetchone()
        if row:
            return {"user_id": row[0], "username": row[1], "balance": row[2], "tickets": row[3], "created_at": row[4]}
        return None

def update_user_balance(user_id: int, amount: float):
    """Изменить баланс пользователя (+ или -)."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()

def set_user_balance(user_id: int, exact_balance: float):
    """Установить точный баланс пользователя."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (exact_balance, user_id))
        conn.commit()

def add_user_tickets(user_id: int, count: int = 1):
    """Добавить или списать билеты пользователю."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET tickets = tickets + ? WHERE user_id = ?", (count, user_id))
        conn.commit()

def log_game(user_id: int, username: str, stars: int, chance: int, cost: float, is_win: bool, recipient: str, tickets_won: int = 0):
    """Записать результат игры в историю."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO games (user_id, username, stars, chance, cost, is_win, tickets_won, recipient) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, stars, chance, cost, 1 if is_win else 0, tickets_won, recipient)
        )
        conn.commit()

def get_stats():
    """Получение глобальной статистики для админа."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*), COALESCE(SUM(cost), 0), COALESCE(SUM(is_win), 0), COALESCE(SUM(tickets_won), 0) FROM games")
        games_row = cursor.fetchone()
        total_games = games_row[0]
        total_turnover = games_row[1]
        total_wins = games_row[2]
        total_tickets = games_row[3]

        return {
            "total_users": total_users,
            "total_games": total_games,
            "total_turnover": round(total_turnover, 2),
            "total_wins": total_wins,
            "total_tickets": total_tickets
        }
