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


def _code_taken(db: Client, code: str, exclude_user_id: str | None = None) -> bool:
    """Kodun BAŞQA bir istifadəçiyə aid olub-olmadığını yoxlayır.

    TƏHLÜKƏSİZLİK: `exclude_user_id` VACİBDİR. Əks halda repetitorun ÖZ kodu
    "tutulmuş" sayılır və funksiya hər panel açılanda YENİ kod yaradıb
    üstünü yazır — yəni sistem kodu sabit qalmır.
    """
    res = (
        db.table("users")
        .select("id, role")
        .eq("tutor_code", code)
        .execute()
    )
    for row in res.data or []:
        # Öz sətri hesaba katlanmır — repetitor öz kodunu saxlayır.
        if exclude_user_id and str(row.get("id")) == str(exclude_user_id):
            continue
        # Yalnız real repetitor kodu sayılır. Köhnə, səhv generasiya olunmuş
        # şagird sətrinin kodu bloklamasın.
        if str(row.get("role", "")).lower() in TUTOR_ROLES:
            return True
    return False


def get_or_create_tutor_code(user: dict, db: Client) -> str:
    """Repetitorun unikal 4 rəqəmli kodunu qaytarır, yoxdursa yaradır.

    TƏHLÜKƏSİZLİK: `user['role']` repetitor deyilsə, heç nə yazılmır və
    xəta atılır. Bu, şagirdin özünə kod verilməsinin qarşısını alır.
    """
    user_id = user.get("id")
    if not user_id:
        raise ValueError("İstifadəçi identifikatoru tapılmadı.")

    # Rol: çağıran dict-də ola bilər, olmaya da bilər (bəzi endpoint-lər
    # `role` sütununu seçmir). Yoxdursa bazadan oxuyuruq — beləliklə rol
    # yoxlaması heç vaxt "boş" deyə keçmir (fail-closed).
    role = str(user.get("role") or "").lower().strip()
    if not role:
        try:
            res_role = (
                db.table("users").select("role").eq("id", user_id).single().execute()
            )
            role = str((res_role.data or {}).get("role") or "").lower().strip()
        except Exception:
            role = ""

    if role not in TUTOR_ROLES:
        logger.warning(
            "tutor_code_denied user_id=%s role=%s", user_id, role or "(yoxdur)"
        )
        raise ValueError(
            "Yalnız repetitor hesabı üçün sistem kodu yaradıla bilər."
        )

    # 1) DB-dəki cari vəziyyəti oxu (token/session köhnə ola bilər)
    try:
        res = (
            db.table("users")
            .select("tutor_code")
            .eq("id", user_id)
            .single()
            .execute()
        )
        db_code = (res.data or {}).get("tutor_code")
    except Exception:
        db_code = None
        logger.debug("tutor_code_read_failed user_id=%s", user_id)

    # 2) KOD SABİTDİR — bir dəfə verilən kod HEÇ VAKT dəyişmir.
    #    `exclude_user_id` vacibdir: repetitorun öz sətri "tutulmuş" sayılmasın.
    #    Əks halda hər panel açılanda yeni kod yaradılıb üstü yazılırdı və
    #    şagird əvvəlki kodu ilə artıq repetitorun qrupuna qoşula bilmirdi.
    if is_valid_code(db_code) and not _code_taken(db, str(db_code).strip(), exclude_user_id=user_id):
        return str(db_code).strip()

    # 3) Sessiyada mövcud ola bilən kod (DB oxunmasa da)
    existing = user.get("tutor_code") or user.get("invite_code")
    if is_valid_code(existing) and not _code_taken(db, str(existing).strip(), exclude_user_id=user_id):
        try:
            db.table("users").update({"tutor_code": str(existing).strip()}).eq(
                "id", user_id
            ).eq("role", "tutor").execute()
        except Exception:
            logger.exception("tutor_code_write_failed user_id=%s", user_id)
        return str(existing).strip()

    # 4) Yalnız həqiqətən kodu OLANDA yeni unikal kod generasiya et
    for _ in range(MAX_ATTEMPTS):
        candidate = _random_code()
        if _code_taken(db, candidate, exclude_user_id=user_id):
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
