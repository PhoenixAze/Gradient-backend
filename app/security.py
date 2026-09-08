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
