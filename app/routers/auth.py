import os
from fastapi import APIRouter, HTTPException, Response, Request, status
from app.schemas import UserRegisterRequest, UserLoginRequest, TokenResponse
from app.database import get_db
from app.security import (
    get_password_hash, verify_password, 
    create_access_token, create_refresh_token, 
    ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS,
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
        tutor_id = None
        if user_data.role == "student" and user_data.tutor_code:
            code = user_data.tutor_code.strip()
            t_res = db.table("users").select("id").eq("identifier", code).eq("role", "tutor").execute()
            if not t_res.data:
                t_res = db.table("users").select("id").eq("id", code).eq("role", "tutor").execute()
            if t_res.data:
                tutor_id = t_res.data[0]["id"]

        new_user = {
            "id": str(uuid.uuid4()),
            "role": user_data.role,
            "first_name": user_data.first_name,
            "last_name": user_data.last_name,
            "identifier": user_data.identifier,
            "password_hash": hashed_pw,
            "grade": user_data.grade,
            "subject": user_data.subject,
            "balance": 0.00,
            "tutor_id": tutor_id
        }
        db.table("users").insert(new_user).execute()
        return {"message": "Qeydiyyat uğurla tamamlandı."}
    except HTTPException:
        raise 
    except Exception as e:
        print(f"CRITICAL REGISTER ERROR: {e}")
        raise HTTPException(status_code=500, detail="Qeydiyyat zamanı xəta baş verdi. Zəhmət olmasa bir az sonra yenidən cəhd edin.")

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
    
    # Access Token (30 dəqiqə)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )
    
    # Refresh Token (30 gün) - Brauzer bağlansa belə istifadəçi sistemdə qalsın
    response.set_cookie(
        key="refresh_token",
        value=f"Bearer {refresh_token}",
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    )
    
    return TokenResponse(
        message="Giriş uğurludur.",
        role=user["role"],
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        user={
            "id": user["id"],
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "identifier": user.get("identifier"),
            "role": user.get("role"),
            "grade": user.get("grade"),
            "subject": user.get("subject")
        }
    )

@router.post("/refresh")
def refresh_token(request: Request, response: Response):
    """Vaxtı bitmiş access_token-i yeniləyir"""
    token = request.cookies.get("refresh_token")
    if not token:
        token = request.headers.get("x-refresh-token") or request.headers.get("authorization") or request.headers.get("Authorization")
    if not token:
        raise HTTPException(status_code=401, detail="Refresh token tapılmadı. Yenidən giriş edin.")
        token = token.split(" ")[1] if " " in token else token
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Yanlış token növü.")
            
        user_id = payload.get("sub")
        role = payload.get("role")
        
        # Zero-Trust: İstifadəçinin hələ də bazada aktiv olduğunu yoxlayırıq
        db = get_db()
        user_res = db.table("users").select("id, role, first_name, last_name, identifier, grade, subject, balance, tutor_id").eq("id", user_id).execute()
        if len(user_res.data) == 0:
            raise HTTPException(status_code=401, detail="İstifadəçi tapılmadı və ya silinib.")
            
        user = user_res.data[0]
        # Yeni Access Token və uzadılmış Refresh Token yaradırıq (Sliding session)
        new_access_token = create_access_token(data={"sub": user["id"], "role": user["role"]})
        new_refresh_token = create_refresh_token(data={"sub": user["id"], "role": user["role"]})
        
        response.set_cookie(
            key="access_token",
            value=f"Bearer {new_access_token}",
            httponly=True,
            secure=True,
            samesite="none",
            path="/",
            max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60
        )
        response.set_cookie(
            key="refresh_token",
            value=f"Bearer {new_refresh_token}",
            httponly=True,
            secure=True,
            samesite="none",
            path="/",
            max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
        )
        return {
            "message": "Token uğurla yeniləndi.",
            "role": user["role"],
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "bearer",
            "user": {
                "id": user["id"],
                "first_name": user.get("first_name"),
                "last_name": user.get("last_name"),
                "identifier": user.get("identifier"),
                "role": user.get("role")
            }
        }
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sessiyanın vaxtı tamamilə bitib. Yenidən giriş edin.")
    except jwt.JWTError:
        raise HTTPException(status_code=401, detail="Keçərsiz refresh token.")

@router.post("/logout")
def logout_user(response: Response):
    """Hər iki tokeni silir"""
    response.delete_cookie(key="access_token", path="/", httponly=True, secure=True, samesite="none")
    response.delete_cookie(key="refresh_token", path="/", httponly=True, secure=True, samesite="none")
    return {"message": "Uğurla çıxış edildi."}