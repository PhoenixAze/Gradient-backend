import os
from fastapi import APIRouter, HTTPException, Response, status
from app.schemas import UserRegisterRequest, UserLoginRequest, TokenResponse
from app.database import get_db
from app.security import get_password_hash, verify_password, create_access_token
import uuid

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

# Təhlükəsizlik Qeydi: Localhost-da HTTP üzərindən işləyərkən Secure=True cookie-ləri bloklaya bilər.
# Production-da (HTTPS) bu mütləq True olmalıdır.
IS_PRODUCTION = os.getenv("ENVIRONMENT") == "production"

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(user_data: UserRegisterRequest):
    try:
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
        
        # 4. Supabase-ə insert et
        db.table("users").insert(new_user).execute()
        
        return {"message": "Qeydiyyat uğurla tamamlandı."}
        
    except HTTPException:
        # Əgər bizim bilərəkdən verdiyimiz xətadırsa (məs: nömrə mövcuddur), olduğu kimi qaytar
        raise 
    except Exception as e:
        # TƏHLÜKƏSİZLİK VƏ DİAQNOSTİKA: Serverin çökməsinin qarşısını alırıq
        print(f"CRITICAL REGISTER ERROR: {e}")
        # Müvəqqəti olaraq əsl xətanı frontend-ə göndəririk ki, problemi görək
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"Sistem xətası: {str(e)}"
        )

@router.post("/login", response_model=TokenResponse)
def login_user(credentials: UserLoginRequest, response: Response):
    db = get_db()
    
    # 1. İstifadəçini tap
    user_res = db.table("users").select("*").eq("identifier", credentials.identifier).execute()
    if len(user_res.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="İstifadəçi adı və ya şifrə yanlışdır."
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
    # DÜZƏLİŞ: Fərqli domenlər (GitHub Pages və Render) üçün SameSite="none" olmalıdır!
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        secure=True,         # Mütləq True olmalıdır (HTTPS tələb edir)
        samesite="none",     # Fərqli domenlər arasında cookie göndərmək üçün
        max_age=60 * 24 * 7 * 60
    )
    
    return TokenResponse(message="Giriş uğurludur.", role=user["role"])

@router.post("/logout")
def logout_user(response: Response):
    """Sessiyanı bitirir və cookie-ni silir."""
    response.delete_cookie(key="access_token", httponly=True, secure=IS_PRODUCTION, samesite="strict")
    return {"message": "Uğurla çıxış edildi."}
