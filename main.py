import random
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import database

# Инициализируем БД
database.init_db()

app = FastAPI(title="Lucky Stars API")

# Разрешаем веб-запросы (CORS) с Netlify и локального компьютера
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STAR_PRICE_RUB = 1.95  # Стоимость 1 звезды в рублях

class SpinRequest(BaseModel):
    user_id: int
    username: str
    stars: int
    chance: int
    recipient: str

@app.get("/")
def home():
    return {"status": "running", "message": "Lucky Stars Backend is live!"}

@app.get("/api/user/{user_id}")
def get_user(user_id: int, username: str = ""):
    """Получение баланса и билетов пользователя"""
    try:
        user_data = database.get_or_create_user(user_id, username)
        return user_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/spin")
def make_spin(req: SpinRequest):
    """Безопасный расчет броска на стороне сервера"""
    # 1. Получаем профиль из БД
    user = database.get_or_create_user(req.user_id, req.username)
    
    # 2. Вычисляем стоимость согласно формуле (с маржой 5%)
    full_cost = req.stars * STAR_PRICE_RUB
    cost = (full_cost * (req.chance / 100.0)) * 1.05
    
    # 3. Проверка баланса
    if user["balance"] < cost:
        raise HTTPException(status_code=400, detail="Недостаточно средств на балансе")
    
    # 4. Списание средств
    database.update_user_balance(req.user_id, -cost)
    
    # 5. Розыгрыш на сервере
    roll = random.uniform(0, 100)
    is_win = roll <= req.chance
    
    # 6. Запись игры в лог
    database.log_game(req.user_id, req.stars, req.chance, cost, is_win)
    
    # Получаем обновленный баланс
    updated_user = database.get_or_create_user(req.user_id)
    
    return {
        "success": True,
        "is_win": is_win,
        "new_balance": round(updated_user["balance"], 2),
        "stars_won": req.stars if is_win else 0,
        "cost_paid": round(cost, 2)
    }
