import os
from datetime import datetime, timedelta
from passlib.context import CryptContext
from jose import jwt
from dotenv import load_dotenv

load_dotenv()

# Təhlükəsizlik Qeydi: JWT_SECRET_KEY mütləq .env faylından gəlməlidir. 
# Əgər yoxdursa, serverin işləməsinə icazə vermirik.
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("CRITICAL ERROR: JWT_SECRET_KEY tapılmadı!")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 günlük sessiya

# Bcrypt alqoritmi ilə parol hash-ləmə konfiqurasiyası
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """İstifadəçinin daxil etdiyi parolun bazadakı hash ilə uyğunluğunu yoxlayır."""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Parolu geri qaytarıla bilməyən (irreversible) bcrypt hash-inə çevirir."""
    return pwd_context.hash(password)

def create_access_token(data: dict) -> str:
    """İstifadəçi məlumatları əsasında JWT token yaradır."""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    
    # Tokeni imzalayırıq
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt
  from fastapi import Request, HTTPException, status
from app.database import get_db

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