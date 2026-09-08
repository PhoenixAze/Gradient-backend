import os
from datetime import datetime, timedelta
import bcrypt
from jose import jwt
from dotenv import load_dotenv
from fastapi import Request, HTTPException, status
from app.database import get_db

load_dotenv()

# Təhlükəsizlik Qeydi: JWT_SECRET_KEY mütləq .env faylından gəlməlidir.
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("CRITICAL ERROR: JWT_SECRET_KEY tapılmadı!")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 günlük sessiya

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """İstifadəçinin daxil etdiyi parolun bazadakı hash ilə uyğunluğunu yoxlayır."""
    password_bytes = plain_password.encode('utf-8')
    hash_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hash_bytes)

def get_password_hash(password: str) -> str:
    """Parolu geri qaytarıla bilməyən (irreversible) bcrypt hash-inə çevirir."""
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed_bytes = bcrypt.hashpw(password_bytes, salt)
    return hashed_bytes.decode('utf-8')

def create_access_token(data: dict) -> str:
    """İstifadəçi məlumatları əsasında JWT token yaradır."""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_current_user(request: Request):
    """HttpOnly Cookie-dən tokeni oxuyur və istifadəçini təsdiqləyir (Auth Guard)"""
    token = request.cookies.get("access_token")
    
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessiya tapılmadı.")
    
    try:
        # "Bearer <token>" formatından yalnız tokeni ayırırıq
        token = token.split(" ")[1] if " " in token else token
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Keçərsiz token.")
            
        db = get_db()
        user_res = db.table("users").select("id, role, first_name, last_name, balance").eq("id", user_id).execute()
        
        if len(user_res.data) == 0:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="İstifadəçi tapılmadı.")
            
        return user_res.data[0]
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessiyanın vaxtı bitib.")
    except jwt.JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Keçərsiz sessiya.")
