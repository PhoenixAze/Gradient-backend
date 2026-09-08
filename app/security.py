import os
from datetime import datetime, timedelta
import bcrypt
from jose import jwt
from dotenv import load_dotenv

load_dotenv()

# Təhlükəsizlik Qeydi: JWT_SECRET_KEY mütləq .env faylından gəlməlidir.
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("CRITICAL ERROR: JWT_SECRET_KEY tapılmadı!")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 günlük sessiya

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """İstifadəçinin daxil etdiyi parolun bazadakı hash ilə uyğunluğunu yoxlayır."""
    # bcrypt yalnız baytlarla (bytes) işləyir, ona görə encode edirik
    password_bytes = plain_password.encode('utf-8')
    hash_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(password_bytes, hash_bytes)

def get_password_hash(password: str) -> str:
    """Parolu geri qaytarıla bilməyən (irreversible) bcrypt hash-inə çevirir."""
    password_bytes = password.encode('utf-8')
    # Avtomatik duzlama (salt) və hash-ləmə
    salt = bcrypt.gensalt()
    hashed_bytes = bcrypt.hashpw(password_bytes, salt)
    # Bazada string kimi saxlamaq üçün decode edirik
    return hashed_bytes.decode('utf-8')

def create_access_token(data: dict) -> str:
    """İstifadəçi məlumatları əsasında JWT token yaradır."""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    
    # Tokeni imzalayırıq
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt
