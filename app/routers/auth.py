import os
from fastapi import APIRouter, HTTPException, Response, Request, status
from app.schemas import UserRegisterRequest, UserLoginRequest, TokenResponse
from app.database import get_db
from app.security import (
    get_password_hash, verify_password, 
    create_access_token, create_refresh_token, 
    SECRET_KEY, ALGORITHM
)
from jose import jwt
import uuid

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

IS_PRODUCTION = os.getenv("ENVIRONMENT") == "production"

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register_user(user_data: UserRegisterRequest):
    try:
        db = get_db()
        existing_user = db.table("users").select("id").eq("identifier", user_data.identifier).execute()
        if len(existing_user.data) > 0:
            raise HTTPException(status_code=400, detail="Bu E-poçt və ya Mobil nömrə artıq qeydiyyatdan keçib.")
        
        hashed_pw = get_password_hash(user_data.password)
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
        db.table("users").insert(new_user).execute()
        return {"message": "Qeydiyyat uğurla tamamlandı."}
    except HTTPException:
        raise 
    except Exception as e:
        print(f"CRITICAL REGISTER ERROR: {e}")
        raise HTTPException(status_code=500, detail=f"Sistem xətası: {str(e)}")

@router.post("/login", response_model=TokenResponse)
def login_user(credentials: UserLoginRequest, response: Response):
    db = get_db()
    user_res = db.table("users").select("*").eq("identifier", credentials.identifier).execute()
    
    if len(user_res.data) == 0 or not verify_password(credentials.password, user_res.data[0]["password_hash"]):
        raise HTTPException(status_code=401, detail="İstifadəçi adı və ya şifrə yanlışdır.")
        
    user = user_res.data[0]
    token_data = {"sub": user["id"], "role": user["role"]}
    
    access_token = create_access_token(data=token_data)
    refresh_token = create_refresh_token(data=token_data)
    
    # Access Token (15 dəqiqə)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        secure=True,
        samesite="none",
        max_age=15 * 60
    )
    
    # Refresh Token (7 gün) - Brauzer bağlananda silinməməsi üçün max_age dəqiq verilir
    response.set_cookie(
        key="refresh_token",
        value=f"Bearer {refresh_token}",
        httponly=True,
        secure=True,
        samesite="none",
        max_age=7 * 24 * 60 * 60
    )
    
    return TokenResponse(message="Giriş uğurludur.", role=user["role"])

@router.post("/refresh")
def refresh_token(request: Request, response: Response):
    """Vaxtı bitmiş access_token-i yeniləyir"""
    token = request.cookies.get("refresh_token")
    
    if not token:
        raise HTTPException(status_code=401, detail="Refresh token tapılmadı. Yenidən giriş edin.")
        
    try:
        token = token.split(" ")[1] if " " in token else token
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Yanlış token növü.")
            
        user_id = payload.get("sub")
        role = payload.get("role")
        
        # Zero-Trust: İstifadəçinin hələ də bazada aktiv olduğunu yoxlayırıq
        db = get_db()
        user_res = db.table("users").select("id").eq("id", user_id).execute()
        if len(user_res.data) == 0:
            raise HTTPException(status_code=401, detail="İstifadəçi tapılmadı və ya silinib.")
            
        # Yeni Access Token yaradırıq
        new_access_token = create_access_token(data={"sub": user_id, "role": role})
        
        response.set_cookie(
            key="access_token",
            value=f"Bearer {new_access_token}",
            httponly=True,
            secure=True,
            samesite="none",
            max_age=15 * 60
        )
        return {"message": "Token uğurla yeniləndi."}
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sessiyanın vaxtı tamamilə bitib. Yenidən giriş edin.")
    except jwt.JWTError:
        raise HTTPException(status_code=401, detail="Keçərsiz refresh token.")

@router.post("/logout")
def logout_user(response: Response):
    """Hər iki tokeni silir"""
    response.delete_cookie(key="access_token", httponly=True, secure=True, samesite="none")
    response.delete_cookie(key="refresh_token", httponly=True, secure=True, samesite="none")
    return {"message": "Uğurla çıxış edildi."}