# app/core/security.py
"""
Autentifikasiya və avtorizasiya asılılıqları (Zero-Trust, .clinerules §1).

TəHLÜKƏSİZLİK İZAHI:
  * JWT yalnız server tərəfdə imzalanır/dekriptlənir (`app.core.config` sırrı).
  * Token yalnız HttpOnly cookie və ya Authorization header-dən oxunur; frontend
    JavaScript token saxlamır (§1 Autentifikasiya).
  * `require_user` / `require_tutor` hər endpoint üçün *obyekt səviyyəsində*
    yoxlama aparır: DB-dən mövcudluğu və ROLU təsdiqlənir, yəni istifadəçi
    öz rolunu dəyişə bilmir (token içindəki rola etibar etmirik).
  * Xəta mesajları istifadəçiyə yalnız biznes məntiqi mətnini verir; stack
    trace və DB detalları sızdırılmır. Ətraflı məlumat daxili logger-a düşür.
  * Rol yoxlaması "fail-closed" prinsipi ilə işləyir: rol qeyd deyilsə və ya
    uyğun gəlmirsə → 403 (böyük səhvin görünməməsi üçün 401 deyil).
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, status
from supabase import Client

from app.core.config import settings
from app.database import get_db
from app.security import ALGORITHM, SECRET_KEY
from jose import jwt

logger = logging.getLogger("gradient.security")

# Təhlükəsizlik üçün istifadəçiyə qaytarıla bilən sütunlar.
# `password_hash` və daxili sütunlar HEÇ VAQT qaytarılmır (§1: DB detalları sızmır).
USER_PUBLIC_FIELDS = (
    "id, role, first_name, last_name, identifier, grade, subject, balance, tutor_id"
)


def _unauthorized(detail: str) -> HTTPException:
    """401 qaytarır; iç səbəb daxili loga düşür, istifadəçiyə sızmır."""
    logger.warning("auth_unauthorized reason=%s", detail)
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _extract_token(request: Request) -> str | None:
    """Cookie və ya Authorization header-dən JWT-ni çıxarır.

    Defensive parsing: "Bearer <token>" / "Bearer%20<token>" / URL-kodlaşdırılmış
    formaları dəstəkləyir. Token boşdursa None qaytarır.
    """
    token = request.cookies.get("access_token")
    if not token:
        auth_header = (
            request.headers.get("authorization")
            or request.headers.get("Authorization")
        )
        if auth_header:
            token = auth_header

    if not token or not token.strip():
        return None

    token = token.strip()
    # URL-kodlaşdırılmış "Bearer%20" formatı
    if token.startswith("Bearer%20"):
        token = token[9:]
    elif token.lower().startswith("bearer "):
        token = token[7:]
    elif " " in token:
        # "Bearer" sözü olmayan, amma boşluqlu sətri yalnız son segmenti götürülür
        token = token.split(" ")[-1]

    return token.strip() or None


def _decode_access_token(request: Request) -> str:
    """Tokeni dekriptləyib `sub` (user_id) qaytarır; uğursuzluqda 401 atır."""
    token = _extract_token(request)
    if not token:
        raise _unauthorized("Sessiya tapılmadı.")

    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Sessiyanın vaxtı bitib. Yenidən giriş edin.")
    except jwt.JWTError:
        raise _unauthorized("Keçərsiz sessiya.")
    except Exception:
        # Gözlənilməz hallarda da məlumat sızdırmadan 401 qaytarılır.
        logger.exception("token_decode_unexpected_error")
        raise _unauthorized("Sessiyanı yoxlamaq mümkün olmadı.")

    # Yalnız access token qəbul edilir — refresh token bu yerdə keçərsizdir.
    if payload.get("type") != "access":
        raise _unauthorized("Yanlış token növü.")

    user_id = payload.get("sub")
    if not user_id:
        raise _unauthorized("Keçərsiz token.")

    return user_id


def get_supabase_admin() -> Client:
    """DB dependency — service_role ilə Supabase client (yalnız server tərəfdə).

    §1 Supabase İzolasiyası: açarlar yalnız burada, serverdə istifadə olunur;
    frontend heç vaxt cədvələ toxunmur.
    """
    return get_db()


def require_user(
    request: Request, db: Client = Depends(get_supabase_admin)
) -> dict:
    """İstənilən autentifikasiya olmuş istifadəçi (hər rol).

    DB sorğusu vasitəsilə rol TƏSDİQLƏNİR — token daxilindəki rola etibar
    edilmir (token forged olsa belə, DB-də real vəziyyətə baxılır).
    """
    user_id = _decode_access_token(request)

    user_res = db.table("users").select(USER_PUBLIC_FIELDS).eq("id", user_id).execute()
    if not user_res.data or len(user_res.data) == 0:
        raise _unauthorized("İstifadəçi tapılmadı və ya silinib.")

    return user_res.data[0]


def require_tutor(
    request: Request, db: Client = Depends(get_supabase_admin)
) -> dict:
    """Yalnız `role == 'tutor'` olan istifadəçi üçün endpoint.

    Rol DB-dən oxunur (fail-closed). Şagird və ya admin bu endpoint-lərə
    daxil ola bilmir → IDOR/privilege-escalation hücumlarının qarşısı alınır.
    """
    user = require_user(request, db)

    if user.get("role") != "tutor":
        logger.warning(
            "role_denied user_id=%s role=%s required=tutor",
            user.get("id"),
            user.get("role"),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bu əməliyyat üçün repetitor səlahiyyəti tələb olunur.",
        )

    return user


__all__ = [
    "USER_PUBLIC_FIELDS",
    "get_supabase_admin",
    "require_tutor",
    "require_user",
]
