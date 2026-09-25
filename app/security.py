import os
from datetime import datetime, timedelta
import bcrypt
from jose import jwt
from dotenv import load_dotenv
from fastapi import Request, HTTPException, status
from app.database import get_db

load_dotenv()

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("CRITICAL ERROR: JWT_SECRET_KEY tapılmadı!")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30  # 30 dəqiqə
REFRESH_TOKEN_EXPIRE_DAYS = 30    # 30 gün (istifadəçinin təkrar giriş etmədən rahat qalması üçün)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    password_bytes = plain_password.encode("utf-8")
    hash_bytes = hashed_password.encode("utf-8")
    return bcrypt.checkpw(password_bytes, hash_bytes)

def get_password_hash(password: str) -> str:
    password_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt()
    hashed_bytes = bcrypt.hashpw(password_bytes, salt)
    return hashed_bytes.decode("utf-8")

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(request: Request):
    # 1. HttpOnly Cookie-dən və ya Authorization Header-dən oxuma
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
        if auth_header:
            token = auth_header
        
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessiya tapılmadı.")
        
    try:
        # Bearer və ya URL encoded prefix-ləri təmizləyirik
        if " " in token:
            token = token.split(" ")[1]
        elif token.startswith("Bearer%20"):
            token = token[9:]
        elif token.startswith("Bearer"):
            token = token[6:]

        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            
        # TƏHLÜKƏSİZLİK: Yalnız access_token qəbul edilir
        if payload.get("type") != "access":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Yanlış token növü.")
                
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Keçərsiz token.")
                
        db = get_db()
        user_res = db.table("users").select(
            "id, role, first_name, last_name, identifier, grade, subject, balance, tutor_id"
        ).eq("id", user_id).execute()
            
        if not user_res.data or len(user_res.data) == 0:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="İstifadəçi tapılmadı.")
                
        return user_res.data[0]
            
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessiyanın vaxtı bitib.")
    except jwt.JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Keçərsiz sessiya.")
