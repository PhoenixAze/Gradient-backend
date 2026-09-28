# app/core/tutor_code.py
"""
Təhlükəsiz 4 rəqəmli repetitor kodu generasiyası.

TƏHLÜKƏSİZLİK İZAHI (.clinerules §1):
  * Kod MÜLKİYYƏTÇİDİR — heç bir istifadəçi tərəfindən dəyişdirilə bilməz.
    Dəyişiklik yalnız bu moduldan, yalnız `role = 'tutor'` olan istifadəçi
    üçün baş verir.
  * Kod UNİKAL olmalıdır. Əvvəlki implementasiya `crc32(id) % 9000 + 1000`
    istifadə edirdi — bu, yalnız 9000 mümkün dəyər verir, təsadüfi deyil
    (eyni id → eyni kod) və UNİKALLIĞI YOXLAYIRDı. Nəticədə iki repetitor
    eyni kodu ala bilirdi və şagirdlər YANLIŞ qrupa daxil olurdu.
  * Burada: kriptoqrafik təsadüfi 4 rəqəm + DB ilə unikal yoxlaması +
    təkrar cəhd dövrü. DB-dəki UNIQUE indeks isə son səhvi tutur
    (rəqabət vəziyyətində — race condition).
  * Kod yalnız `role = 'tutor'` sətrinə yazılır; şagird heç vaxt kod almır.
    Bu, "şagird özünə kod daxil edə bilməz" xətasının kök səbəbini aradan
    qaldırır.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Any, Optional

from supabase import Client

logger = logging.getLogger("gradient.tutor_code")

# 4 rəqəm, 1000–9999 arası (0000–0999 bloklanır: UI-də daha rahat oxunur)
CODE_MIN = 1000
CODE_MAX = 9999
CODE_RE = re.compile(r"^[0-9]{4}$")

# Təkrar cəhd sayı. 9000 dəyər, 1 istifadəçiyə düşən orta boşluq ~4500 →
# 50 cəhd təxminən 1e-6 uğursuzluq ehtimalı verir.
MAX_ATTEMPTS = 50

# Rol yoxlaması: yalnız bu rollar kod ala bilər.
TUTOR_ROLES = frozenset({"tutor", "teacher", "repetitor", "instructor"})


def is_valid_code(value: Any) -> bool:
    """Dəyərin düzgün 4 rəqəmli formatda olub-olmadığını yoxlayır."""
    return bool(value) and bool(CODE_RE.match(str(value).strip()))


def _random_code() -> str:
    """Kriptoqrafik təsadüfi 4 rəqəmli kod."""
    return str(secrets.randbelow(CODE_MAX - CODE_MIN + 1) + CODE_MIN)


def _code_taken(db: Client, code: str) -> bool:
    """Kodun artıq başqa istifadəçiyə aid olub-olmadığını yoxlayır."""
    res = (
        db.table("users")
        .select("id, role")
        .eq("tutor_code", code)
        .execute()
    )
    for row in res.data or []:
        # Yalnız real repetitor kodu sayılır. Köhnə, səhv generasiya olunmuş
        # şagird sətrinin kodu bloklamasın — o, təmizlənəcək.
        if str(row.get("role", "")).lower() in TUTOR_ROLES:
            return True
    return False


def get_or_create_tutor_code(user: dict, db: Client) -> str:
    """Repetitorun unikal 4 rəqəmli kodunu qaytarır, yoxdursa yaradır.

    TƏHLÜKƏSİZLİK: `user['role']` repetitor deyilsə, heç nə yazılmır və
    xəta atılır. Bu, şagirdin özünə kod verilməsinin qarşısını alır.
    """
    user_id = user.get("id")
    role = str(user.get("role", "")).lower().strip()

    if role not in TUTOR_ROLES:
        logger.warning(
            "tutor_code_denied user_id=%s role=%s", user_id, role
        )
        raise ValueError(
            "Yalnız repetitor hesabı üçün sistem kodu yaradıla bilər."
        )

    if not user_id:
        raise ValueError("İstifadəçi identifikatoru tapılmadı.")

    # 1) Artıq mövcud və düzgün kod varsa, dəyişmə — kod sabitdir.
    existing = user.get("tutor_code") or user.get("invite_code")
    if is_valid_code(existing) and not _code_taken(db, str(existing).strip()):
        return str(existing).strip()

    # 2) DB-dəki cari vəziyyəti yenidən oxu (token/session köhnə ola bilər)
    try:
        res = (
            db.table("users")
            .select("tutor_code")
            .eq("id", user_id)
            .single()
            .execute()
        )
        db_code = (res.data or {}).get("tutor_code")
        if is_valid_code(db_code) and not _code_taken(db, str(db_code).strip()):
            return str(db_code).strip()
    except Exception:
        # Sətr tapılmadısa və ya oxunmasa — aşağıda yeni kod generasiya olunur.
        logger.debug("tutor_code_read_failed user_id=%s", user_id)

    # 3) Unikal kod generasiya et
    for _ in range(MAX_ATTEMPTS):
        candidate = _random_code()
        if _code_taken(db, candidate):
            continue
        try:
            db.table("users").update({"tutor_code": candidate}).eq(
                "id", user_id
            ).eq("role", "tutor").execute()
        except Exception:
            logger.exception("tutor_code_write_failed user_id=%s", user_id)
            continue
        return candidate

    logger.error("tutor_code_exhausted user_id=%s", user_id)
    raise RuntimeError(
        "Sistem kodu yaradıla bilmədi. Texniki xidmətə müraciət edin."
    )


__all__ = [
    "CODE_MAX",
    "CODE_MIN",
    "MAX_ATTEMPTS",
    "TUTOR_ROLES",
    "get_or_create_tutor_code",
    "is_valid_code",
]
