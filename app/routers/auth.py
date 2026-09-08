import os
from fastapi import APIRouter, HTTPException, Response, status
from schemas import UserRegisterRequest, UserLoginRequest, TokenResponse
from database import get_db
from security import get_password_hash, verify_password, create_access_token
import uuid

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

# Təhlükəsizlik Qeydi: Localhost-da HTTP üzərindən işləyərkən Secure=True cookie-ləri bloklaya bilər.
# Production-da (HTTPS) bu mütləq True olmalıdır.
IS_PRODUCTION = os.getenv("ENVIRONMENT") == "production"

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(user_data: UserRegisterRequest):
    db = get_db()
    
    # 1. İstifadəçinin mövcudluğunu yoxla (Zero-Trust)
    existing_user = db.table("users").select("id").eq("identifier", user_data.identifier).execute()
    if len(existing_user.data) > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Bu E-poçt və ya Mobil nömrə artıq qeydiyyatdan keçib."
        )
    
    # 2. Parolu Hash-lə
    hashed_pw = get_password_hash(user_data.password)
    
    # 3. Bazaya yazılacaq məlumatları hazırla
    new_user = {
        "id": str(uuid.uuid4()),
        "role": user_data.role,
        "first_name": user_data.first_name,
        "last_name": user_data.last_name,
        "identifier": user_data.identifier,
        "password_hash": hashed_pw,
        "grade": user_data.grade,
        "subject": user_data.subject,
        "balance": 0.00
    }
    
    # 4. Supabase-ə insert et (service_role ilə)
    try:
        db.table("users").insert(new_user).execute()
    except Exception as e:
        # Təhlükəsizlik Qeydi: Daxili baza xətasını frontend-ə sızdırmırıq.
        print(f"DB Insert Error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Sistem xətası baş verdi. Zəhmət olmasa sonra yenidən cəhd edin."
        )
        
    return {"message": "Qeydiyyat uğurla tamamlandı."}

@router.post("/login", response_model=TokenResponse)
def login_user(credentials: UserLoginRequest, response: Response):
    db = get_db()
    
    # 1. İstifadəçini tap
    user_res = db.table("users").select("*").eq("identifier", credentials.identifier).execute()
    if len(user_res.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="İstifadəçi adı və ya şifrə yanlışdır." # Təhlükəsizlik: Hansının səhv olduğunu bildirmirik (Brute-force qarşısı)
        )
        
    user = user_res.data[0]
    
    # 2. Parolu yoxla
    if not verify_password(credentials.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="İstifadəçi adı və ya şifrə yanlışdır."
        )
        
    # 3. JWT Token yarat
    token_data = {"sub": user["id"], "role": user["role"]}
    access_token = create_access_token(data=token_data)
    
    # 4. HttpOnly Cookie təyin et (XSS Müdafiəsi)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        secure=IS_PRODUCTION, # Production-da mütləq True olacaq
        samesite="lax" if not IS_PRODUCTION else "strict", # Localhost üçün lax, production üçün strict
        max_age=60 * 24 * 7 * 60 # 7 gün (saniyə ilə)
    )
    
    return TokenResponse(message="Giriş uğurludur.", role=user["role"])

@router.post("/logout")
def logout_user(response: Response):
    """Sessiyanı bitirir və cookie-ni silir."""
    response.delete_cookie(key="access_token", httponly=True, secure=IS_PRODUCTION, samesite="strict")
    return {"message": "Uğurla çıxış edildi."}
