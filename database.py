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
                tickets INTEGER DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                stars INTEGER,
                chance INTEGER,
                cost REAL,
                is_win INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

def get_or_create_user(user_id: int, username: str = ""):
    """Получить данные пользователя. Если новый — создать с тестовым балансом 150 руб."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, balance, tickets FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            initial_balance = 150.0  # Бонусный стартовый баланс на тест
            cursor.execute(
                "INSERT INTO users (user_id, username, balance, tickets) VALUES (?, ?, ?, ?)",
                (user_id, username, initial_balance, 2)
            )
            conn.commit()
            return {"user_id": user_id, "username": username, "balance": initial_balance, "tickets": 2}
        return {"user_id": row[0], "username": row[1], "balance": row[2], "tickets": row[3]}

def update_user_balance(user_id: int, amount: float):
    """Изменить баланс пользователя (+ или -)."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()

def log_game(user_id: int, stars: int, chance: int, cost: float, is_win: bool):
    """Записать результат игры в историю."""
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO games (user_id, stars, chance, cost, is_win) VALUES (?, ?, ?, ?, ?)",
            (user_id, stars, chance, cost, 1 if is_win else 0)
        )
        conn.commit()
