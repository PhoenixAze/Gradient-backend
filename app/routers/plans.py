# app/routers/plans.py
"""
FASTAPI REFERANS MODULU — Repetitor abunə planları.

YERLƏŞDİRMƏ: bu fayl backend repositoriyasında `app/routers/plans.py`
kimi yerləşdirilir və `app/main.py` daxilində qeyd edilir:

    from app.routers import plans
    _rate_limited_plans_routes = apply_rate_limits(plans.router)
    app.include_router(plans.router, prefix="/api/v1/plans", tags=["plans"])

TƏHLÜKƏSİZLİK (.clinerules §1 — Zero-Trust Backend):
  * Bütün endpoint-lər autentifikasiya tələb edir (`require_tutor`).
  * PLAN MƏLUMATLARI `tutor_plans` cədvəlindən oxunur — frontend-də
    TİKİLİ (hardcoded) qiymət YOXDUR, beləliklə biznes qaydaları
    mərkəzləşdirilmişdir.
  * İstifadəçi planını ÖZÜ DƏYİŞDİRƏ BİLMİR — yalnız "yüksəltmə sorğusu"
    göndərə bilər (`POST /upgrade-request`). Real plan dəyişikliyi admin
    tərəfdən əl ilə edilir (privilege-escalation boşluğu yoxdur).
  * Pydantic ilə giriş sanizasiyası; rate limiting `read`/`write` bucket-ləri.
  * Xəta mesajları müştəriyə stack trace/DB detalları SIZDIRILMIR —
    iç səhv `logger.exception` ilə jurnala düşür.

ENDPOINT-LƏR:
  GET  /api/v1/plans                 → plan kataloqu (bütün 4 plan)
  GET  /api/v1/plans/me              → cari plan + limitlər + istifadə
  POST /api/v1/plans/upgrade-request → yüksəltmə sorğusu (WhatsApp mesajı üçün)

MİQRASİYA: supabase/migrations/20261003000000_tutor_subscription_plans.sql
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from supabase import Client

from app.core.plans import (
    PLAN_RANK,
    VALID_PLAN_IDS,
    get_usage_summary,
    is_upgrade,
)
from app.core.rate_limit import rate_limit
from app.core.security import get_supabase_admin, require_tutor

logger = logging.getLogger("gradient.plans_router")

router = APIRouter()

# WhatsApp mesajında istifadəçinin yazdığı mətnin uzunluq həddi.
CONTACT_HINT_MAX = 160
CONTACT_HINT_RE = re.compile(r"^[0-9a-zA-Z+()\s@._-]{0,160}$")


# ---------------------------------------------------------------------------
# Pydantic modelləri (giriş doğrulaması)
# ---------------------------------------------------------------------------
class UpgradeRequestIn(BaseModel):
    """Yüksəltmə sorğusunun gövdəsi."""

    desired_plan: Literal["standard", "pro", "pro_plus"]
    contact_hint: Optional[str] = Field(default=None, max_length=CONTACT_HINT_MAX)

    @field_validator("contact_hint")
    @classmethod
    def _sanitize_hint(cls, v: Optional[str]) -> Optional[str]:
        """Yalnız təhlükəsiz simvolları saxlayır (defensive validation).

        Bu mətn admin panelində göstəriləcək üçün HTML/meta simvolları
        filtrlənir (defense-in-depth: render zamanı da `textContent` istifadə
        olunacaq, lakin DB-yə təmiz məlumat yazmaq daha təhlükəsizdir).
        """
        if v is None:
            return None
        val = v.strip()
        if not val:
            return None
        if not CONTACT_HINT_RE.match(val):
            raise ValueError("Əlaqə məlumatında icazə olmayan simvollar var")
        return val


# ---------------------------------------------------------------------------
# DAXİLİ KÖMƏKÇİLƏR
# ---------------------------------------------------------------------------
def _fail(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    """Müştəriyə təhlükəsiz xəta qaytarır (DB detalları sızdırılmır)."""
    return HTTPException(status_code=code, detail=message)


# ---------------------------------------------------------------------------
# 1. PLAN KATALOQU
# ---------------------------------------------------------------------------
@router.get("")
@rate_limit("read")
async def list_plans(
    tutor: dict[str, Any] = Depends(require_tutor),
    db: Client = Depends(get_supabase_admin),
) -> dict[str, Any]:
    """Bütün aktiv planları qiymət sırası ilə qaytarır.

    `plans.html` bu cavabı oxuyur → plan adları, qiymətlər və limitlər
    mərkəzləşdirilmiş cədvəldən gəlir.
    """
    from app.core.plans import fetch_plans  # lokal import: circular yaranmasın

    try:
        plans = fetch_plans(db)
    except Exception:
        logger.exception("plans.list_plans failed")
        raise _fail("Plan məlumatları yüklənmədi", status.HTTP_500_INTERNAL_SERVER_ERROR)

    return {"plans": plans}


# ---------------------------------------------------------------------------
# 2. CARİ PLAN + İSTİFADƏ
# ---------------------------------------------------------------------------
@router.get("/me")
@rate_limit("read")
async def get_my_plan(
    tutor: dict[str, Any] = Depends(require_tutor),
    db: Client = Depends(get_supabase_admin),
) -> dict[str, Any]:
    """Cari plan, limitlər və faktiki istifadə.

    `plans.html`-də "Hazırda Free plandasınız" bloku və
    "Hazırkı limitləriniz" cədvəli bu cavabdan qurulur.
    `tutor-dashboard.js` isə banner-i yalnız Free planda göstərmək üçün
    `plan.id` sahəsindən istifadə edir.
    """
    tutor_id = tutor["id"]

    try:
        summary = get_usage_summary(db, tutor_id)
    except Exception:
        logger.exception("plans.get_my_plan failed tutor_id=%s", tutor_id)
        raise _fail("Plan məlumatınız yüklənmədi", status.HTTP_500_INTERNAL_SERVER_ERROR)

    plan = summary.get("plan") or {}
    return {
        "plan": plan,
        "is_free": plan.get("id") == "free",
        "students_used": summary["students_used"],
        "students_limit": summary["students_limit"],
        "students_remaining": summary["students_remaining"],
        "exams_used": summary["exams_used"],
        "exams_limit": summary["exams_limit"],
        "exams_remaining": summary["exams_remaining"],
        "can_add_student": summary["can_add_student"],
        "can_create_exam": summary["can_create_exam"],
        "period_start": summary["period_start"],
    }


# ---------------------------------------------------------------------------
# 3. PLAN YÜKSƏLTMƏ SORĞUSU
# ---------------------------------------------------------------------------
@router.post("/upgrade-request", status_code=status.HTTP_201_CREATED)
@rate_limit("write")
async def create_upgrade_request(
    payload: UpgradeRequestIn,
    tutor: dict[str, Any] = Depends(require_tutor),
    db: Client = Depends(get_supabase_admin),
) -> dict[str, Any]:
    """Plan yüksəltmə sorğusu qeyd edir (ödəniş YOXDUR).

    İş axını:
      1. Frontend istifadəçinin düyməsinə basanda WhatsApp-a yönləndirir
         (`wa.me` linki ilə, hazırlanmış mesajla).
      2. Eyni zamanda burada bir `plan_upgrade_requests` sətri yaranır ki,
         admin daha sonra hesabı əl ilə yüksətsin.

    TƏHLÜKƏSİZLİK:
      * İstifadəçi planı YALNIZ sorğu kimi göndərə bilər — `users.plan`
        sütunu bu endpoint-də heç vaxt yazılmır (privilege escalation yoxdur).
      * Yalnız öz planından YÜKSƏK plan tələb edə bilər (`is_upgrade`).
      * Eyni plan üçün təkrar sorğu yaratmağa icazə verilmir (DB-də
        yoxlanılır → rate-limit və spam qorunması).
    """
    tutor_id = tutor["id"]

    # Cari planı oxu (yoxlamalar üçün)
    from app.core.plans import get_effective_plan

    try:
        current = get_effective_plan(db, tutor_id)
    except Exception:
        logger.exception("plans.upgrade_request: current plan lookup failed")
        raise _fail("Plan məlumatınız yüklənmədi", status.HTTP_500_INTERNAL_SERVER_ERROR)

    current_plan = current.get("id") or "free"

    # Yalnız yüksəltmə icazəlidir (downgrade = admin işidir)
    if not is_upgrade(payload.desired_plan, current_plan):
        raise _fail(
            "Bu plan artıq mövcuddur və ya daha aşağıdır. Plan dəyişikliyi "
            "admin tərəfdən həyata keçirilir.",
            status.HTTP_400_BAD_REQUEST,
        )

    desired = payload.desired_plan
    if desired not in VALID_PLAN_IDS:  # defense-in-depth (Literal onsuz da yoxlayır)
        raise _fail("Naməlum plan seçildi", status.HTTP_400_BAD_REQUEST)

    # Təkrar sorğunun qarşısını al (spam/rate-limit müdafiəsi)
    try:
        pending = (
            db.table("plan_upgrade_requests")
            .select("id")
            .eq("tutor_id", tutor_id)
            .eq("desired_plan", desired)
            .eq("status", "pending")
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        logger.exception("plans.upgrade_request: pending check failed")
        raise _fail("Sorğu yaradıla bilmədi", status.HTTP_500_INTERNAL_SERVER_ERROR)

    if pending:
        # Təkrar səhifə açılışında eyni sorğu — heç nə yaratma, sadəcə xəbərdar et.
        return {
            "message": "Sorğunuz artıq qeyd edilib. Tezliklə əlaqə saxlayacağıq.",
            "already_pending": True,
            "current_plan": current_plan,
            "desired_plan": desired,
        }

    try:
        db.table("plan_upgrade_requests").insert(
            {
                "tutor_id": tutor_id,
                "current_plan": current_plan,
                "desired_plan": desired,
                "contact_hint": payload.contact_hint,
                "status": "pending",
            }
        ).execute()
    except Exception:
        logger.exception("plans.upgrade_request: insert failed tutor_id=%s", tutor_id)
        raise _fail("Sorğunu göndərmək mümkün olmadı", status.HTTP_500_INTERNAL_SERVER_ERROR)

    logger.info("plan_upgrade_requested tutor_id=%s from=%s to=%s",
                tutor_id, current_plan, desired)

    return {
        "message": "Sorğunuz qeydə alındı. Admin tərəfdən hesabınız əl ilə yüksəldiləcək.",
        "already_pending": False,
        "current_plan": current_plan,
        "desired_plan": desired,
    }


# ---------------------------------------------------------------------------
# 4. PLAN SIRASI YARDIMI (frontend üçün dəyişməz — test/UI logikası)
# ---------------------------------------------------------------------------
@router.get("/order")
@rate_limit("read")
async def get_plan_order(
    tutor: dict[str, Any] = Depends(require_tutor),
) -> dict[str, Any]:
    """Plan yüksəltmə sırasını qaytarır (frontend-də " daha yüksək plan"
    müqayisəsi üçün).

    Bu məlumat sabitdir amma backend-dən gəlməsi, frontend-də ikinci bir
    mənbə yaratmamaq (single source of truth) üçün faydalıdır.
    """
    order = sorted(PLAN_RANK.items(), key=lambda kv: kv[1])
    return {"order": [pid for pid, _ in order], "rank": dict(PLAN_RANK)}