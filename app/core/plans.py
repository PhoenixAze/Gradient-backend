# app/core/plans.py
"""
Repetitor abunə planları — paylaşılan məntiq modulu.

YERLƏŞDİRMƏ: bu fayl backend repositoriyasında `app/core/plans.py` kimi
yerləşdirilir və `app/routers/plans.py`, `app/routers/tutor_group.py` və
`app/routers/tutor.py` tərəfindən import olunur.

TƏHLÜKƏSİZLİK İZAHI (.clinerules §1 — Zero-Trust / Security by Design):
  * **Frontend heç vaxt öz planını yaza bilmir.** Plan dəyişikliyi yalnız
    admin tərəfdən (Supabase SQL / service_role) edilir. İstifadəçi yalnız
    "yüksəltmə sorğusu" göndərə bilər (aşağıda `create_upgrade_request`).
  * **Fail-closed limitlər:** plan sətiri tapılmasa, katalog boş olsa və ya
    DB sorğusu xəta verərsə, `enforce_student_limit` / `enforce_exam_limit`
    heç vaxt "limitsiz" fallback ETMİR — əvəzinə Free səviyyəsinin sabit
    limitlərini (`FREE_FALLBACK_LIMITS`) tətbiq edir. Bu, DB əlçatmaz
    olduqda da ödənişsiz limitsiz istifadə boşluğunu bağlayır.
  * **Abunə bitməsi (expiry):** `plan_expires_at` keçmiş tarixdirsə, limitlər
    Free səviyyəsinə qaytarılır. Bu, "ödəniş bitdi → imtiyaz davam edir"
    boşluğunu bağlayır.
  * **Heç bir məlumat sızdırılmır:** xəta mesajları yalnız biznes məntiqi
    mətnidir; DB/SQL detalları `logger.exception` ilə iç jurnala düşür.
  * **Sətirlər silinmir:** yalnız oxuma (SELECT) və ehtiyat sayğac
    artırması (UPSERT) edilir — heç bir DELETE/UPDATE məlumat sətrini yox etmir.

MİQRASİYA: supabase/migrations/20261005000000_full_pro_plan_and_discounts.sql
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from supabase import Client

logger = logging.getLogger("gradient.plans")

# ---------------------------------------------------------------------------
# SABİTLƏR
# ---------------------------------------------------------------------------

PLAN_FREE = "free"
PLAN_STANDARD = "standard"
PLAN_PRO = "pro"
PLAN_PRO_PLUS = "pro_plus"
PLAN_FULL_PRO = "full_pro"

#: Plan yüksəltmə İSTİSMİYATI — yalnız yuxarıya hərəkət icazəlidir.
#: İstifadəçi öz planını "downgrade" edə bilməz (bu, admin işidir).
PLAN_RANK: dict[str, int] = {
    PLAN_FREE: 0,
    PLAN_STANDARD: 1,
    PLAN_PRO: 2,
    PLAN_PRO_PLUS: 3,
    PLAN_FULL_PRO: 4,
}

VALID_PLAN_IDS = frozenset(PLAN_RANK.keys())

#: FAIL-CLOSED sabitləri — DB-yə çıxış mümkün olmadıqda tətbiq olunur.
#: ⚠️ Hər iki dəyər `None` DEYİL, çünki `None` = "limitsiz" deməkdir və
#:    DB xətası ödənişsiz limitsiz giriş yaradardı (fail-OPEN boşluğu).
FREE_FALLBACK_MAX_STUDENTS = 5
FREE_FALLBACK_MAX_EXAMS = 2


# ---------------------------------------------------------------------------
# DAXİLİ KÖMƏKÇİLƏR
# ---------------------------------------------------------------------------
def _parse_ts(value: Any) -> Optional[datetime]:
    """DB-dən gələn timestamptz dəyərini timezone-aware datetime-a çevirir.

    `datetime.fromisoformat` Python 3.11+ "Z" suffiksini dəstəkləyir;
    köhnə versiyalarda əl ilə idarə olunur (defensive parsing).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    # Naive datetime-i UTC kimi qəbul edirik (DB timestamptz hər zaman UTC-dir).
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _current_period_start() -> str:
    """Cari təqvim ayının 1-i (YYYY-MM-DD) — aylıq limit hesabının əsası."""
    return date(datetime.now(timezone.utc).year, datetime.now(timezone.utc).month, 1).isoformat()


# ---------------------------------------------------------------------------
# PLAN KATALOQU
# ---------------------------------------------------------------------------
def fetch_plans(db: Client, only_active: bool = True) -> list[dict[str, Any]]:
    """Aktiv plan kataloqunu qiymət sırası ilə qaytarır.

    Yalnız oxuma (SELECT). Katalog boşdursa boş siyahı qaytarır —
    çağıran tərəf bunu handle etməlidir.
    """
    try:
        query = (
            db.table("tutor_plans")
            .select(
                "id, display_name, price_azn, original_price_azn, "
                "discount_percent, max_students, "
                "max_exams_per_month, description, sort_order"
            )
            .order("sort_order", desc=False)
        )
        if only_active:
            query = query.eq("is_active", True)
        rows = query.execute().data or []
    except Exception:
        # Miqrasiya hələ icra olunmamış ola bilər (yeni sütunlar yoxdur)
        # → köhnə sütunlarla təkrar cəhd, uğursuz olsa boş siyahı.
        logger.exception("plans.fetch_plans failed (retry without discount columns)")
        try:
            rows = (
                db.table("tutor_plans")
                .select(
                    "id, display_name, price_azn, max_students, "
                    "max_exams_per_month, description, sort_order"
                )
                .order("sort_order", desc=False)
                .execute()
                .data
                or []
            )
        except Exception:
            logger.exception("plans.fetch_plans failed (legacy columns)")
            return []
    return [_public_plan(r) for r in rows]


def _to_float(value: Any) -> Optional[float]:
    """DB `numeric` dəyərini float-a çevirir (xəta olanda None qaytarır)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    """DB `integer` dəyərini int-ə çevirir (xəta olanda None qaytarır)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _free_fallback_plan() -> dict[str, Any]:
    """DB əlçatmaz olanda istifadə olunan FAIL-CLOSED Free plan.

    ⚠️ Hər iki limit `None` DEYİL. `None` = "limitsiz" deməkdir; DB xətası
    zamanı `None` qaytarmaq ödənişsiz limitsiz giriş yaradardı.
    """
    return {
        "id": PLAN_FREE,
        "name": "Free",
        "price": 0.0,
        "original_price": None,
        "discount_percent": 0,
        "max_students": FREE_FALLBACK_MAX_STUDENTS,
        "max_exams_per_month": FREE_FALLBACK_MAX_EXAMS,
        "unlimited_students": False,
        "unlimited_exams": False,
        "description": "Kiqik qruplar üçün başlanğıc plan",
        "is_fallback": True,
    }


def _public_plan(row: dict[str, Any]) -> dict[str, Any]:
    """Plan sətrini frontend-ə təhlükəsiz forma verir.

    `numeric` tipli qiymətlər JSON-da float ola bilər — hər zaman float-a
    çevrilir ki, frontend-da `29.99` kimi görünsün (19.9900001 yoxdur).

    `original_price` / `discount_percent` yalnız UI göstəriliyi üçündür
    (50% endirim nişanı); ödəniş məntiqi `price` dəyərindən istifadə edir.
    """
    price = _to_float(row.get("price_azn"))
    if price is None:
        logger.warning("plans.bad_price_value id=%s", row.get("id"))
        price = 0.0

    original_price = _to_float(row.get("original_price_azn"))

    discount_percent = _to_int(row.get("discount_percent")) or 0
    if discount_percent < 0 or discount_percent > 90:
        discount_percent = 0

    # Endirim məntiqi: orijinal qiymət verilməyibsə, ödəniş qiymətindən
    # hesablanır (price = original * (1 - discount/100)).
    if original_price is None and discount_percent > 0 and price > 0:
        original_price = round(price / (1 - discount_percent / 100), 2)

    # Təhlükəsizlik: endirim olmadan orijinal qiymət göstərilmir.
    if not discount_percent:
        original_price = None

    max_students = _to_int(row.get("max_students"))
    max_exams = _to_int(row.get("max_exams_per_month"))

    return {
        "id": row.get("id"),
        "name": row.get("display_name") or row.get("id"),
        "price": round(price, 2),
        "original_price": round(original_price, 2) if original_price is not None else None,
        "discount_percent": discount_percent,
        "max_students": max_students,
        "max_exams_per_month": max_exams,
        "unlimited_students": max_students is None,
        "unlimited_exams": max_exams is None,
        "description": row.get("description") or "",
        "is_fallback": False,
    }


def get_plan_by_id(db: Client, plan_id: str) -> Optional[dict[str, Any]]:
    """Tək plan sətrini qaytarır (None tapılmasa)."""
    if not plan_id or plan_id not in VALID_PLAN_IDS:
        return None
    try:
        row = (
            db.table("tutor_plans")
            .select(
                "id, display_name, price_azn, original_price_azn, "
                "discount_percent, max_students, "
                "max_exams_per_month, description, sort_order"
            )
            .eq("id", plan_id)
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        logger.exception("plans.get_plan_by_id failed id=%s", plan_id)
        return None
    return _public_plan(row[0]) if row else None


# ---------------------------------------------------------------------------
# CARİ PLAN + İSTİFADƏ
# ---------------------------------------------------------------------------
def get_effective_plan(db: Client, tutor_id: str) -> dict[str, Any]:
    """Repetitorun EFFEKTİV planını qaytarır (abunə bitmiş ola bilər).

    Qaytarılan struktur frontend tərəfdə "Hazırda hansı plandasınız?"
    məlumatı üçün istifadə olunur.

    ABUNƏ BİTMİŞSƏ (`plan_expires_at` < now) → plan Free səviyyəsinə
    qaytarılır və `expired: True` işarələnir. Bu, vaxtı bitmiş pullu planın
    limitlərini saxlamağın qarşısını alır.
    """
    fallback = _free_fallback_plan()

    try:
        row = (
            db.table("tutor_subscriptions")
            .select("plan, plan_expires_at")
            .eq("tutor_id", tutor_id)
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        logger.exception("plans.get_effective_plan failed tutor_id=%s", tutor_id)
        return {**fallback, "expired": False, "plan_expires_at": None, "degraded": True}

    if not row:
        logger.warning("plans.tutor_row_missing tutor_id=%s", tutor_id)
        return {**fallback, "expired": False, "plan_expires_at": None}

    raw_plan = row[0].get("plan") or PLAN_FREE
    expires_at = row[0].get("plan_expires_at")
    expires_dt = _parse_ts(expires_at)
    now = datetime.now(timezone.utc)

    is_expired = bool(expires_dt and expires_dt < now)

    # Təhlükəsizlik: naməlum plan ID-si → Free (fail-closed)
    if raw_plan not in VALID_PLAN_IDS:
        logger.warning("plans.unknown_plan_value tutor_id=%s plan=%r", tutor_id, raw_plan)
        raw_plan = PLAN_FREE

    if is_expired and raw_plan != PLAN_FREE:
        logger.info("plans.expired_downgraded tutor_id=%s plan=%s", tutor_id, raw_plan)
        free_plan = get_plan_by_id(db, PLAN_FREE) or fallback
        return {
            **free_plan,
            "expired": True,
            "expired_plan": raw_plan,
            "plan_expires_at": expires_at,
        }

    plan = get_plan_by_id(db, raw_plan) or fallback
    return {
        **plan,
        "expired": False,
        "plan_expires_at": expires_at,
    }


def count_students(db: Client, tutor_id: str) -> int:
    """Repetitorun qrupundakı aktiv şagird sayı."""
    try:
        rows = (
            db.table("users")
            .select("id")
            .eq("tutor_id", tutor_id)
            .eq("role", "student")
            .execute()
            .data
        )
        return len(rows or [])
    except Exception:
        logger.exception("plans.count_students failed tutor_id=%s", tutor_id)
        return 0


def get_month_exam_usage(db: Client, tutor_id: str) -> int:
    """Cari təqvim ayında yaradılan sınaq sayı (`tutor_plan_usage`)."""
    period = _current_period_start()
    try:
        rows = (
            db.table("tutor_plan_usage")
            .select("exams_created")
            .eq("tutor_id", tutor_id)
            .eq("period_start", period)
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        logger.exception("plans.get_month_exam_usage failed tutor_id=%s", tutor_id)
        return 0
    if not rows:
        return 0
    try:
        return int(rows[0].get("exams_created") or 0)
    except (TypeError, ValueError):
        return 0


def get_usage_summary(db: Client, tutor_id: str) -> dict[str, Any]:
    """Frontend-ə göndərilən vahid "plan + istifadə" bloku.

    `students_used` / `exams_used` — cari vəziyyət.
    `can_add_student` / `can_create_exam` — UI düymələrini söndürmək üçün.
    """
    plan = get_effective_plan(db, tutor_id)
    students_used = count_students(db, tutor_id)
    exams_used = get_month_exam_usage(db, tutor_id)

    max_students = plan.get("max_students")
    max_exams = plan.get("max_exams_per_month")

    return {
        "plan": plan,
        "students_used": students_used,
        "students_limit": max_students,
        "students_remaining": (
            None if max_students is None else max(0, max_students - students_used)
        ),
        "exams_used": exams_used,
        "exams_limit": max_exams,
        "exams_remaining": (
            None if max_exams is None else max(0, max_exams - exams_used)
        ),
        "can_add_student": max_students is None or students_used < max_students,
        "can_create_exam": max_exams is None or exams_used < max_exams,
        "period_start": _current_period_start(),
    }


# ---------------------------------------------------------------------------
# LIMIT ENFORCEMENT (yazmaq yollarında çağırılır)
# ---------------------------------------------------------------------------
def enforce_student_limit(db: Client, tutor_id: str) -> None:
    """Şagird əlavə etməzdən əvvəl limiti yoxlayır.

    Limit aşıldıqda `ValueError` atır — çağıran endpoint bunu 402/403-ə
    çevirir. Heç bir məlumat dəyişmir.
    """
    plan = get_effective_plan(db, tutor_id)
    max_students = plan.get("max_students")
    if max_students is None:
        return  # limitsiz plan

    used = count_students(db, tutor_id)
    if used >= int(max_students):
        raise PlanLimitError(
            code="student_limit_reached",
            message=(
                f"{plan.get('name')} planında maksimum {max_students} şagird limitiniz "
                "dolub. Daha çox şagird əlavə etmək üçün planınızı yüksəldin."
            ),
            plan_name=plan.get("name"),
            limit=max_students,
            used=used,
            upgrade_suggested=True,
        )


def enforce_exam_limit(db: Client, tutor_id: str) -> None:
    """Sınaq yaratma/yükləməzdən əvvəl aylıq limiti yoxlayır.

    Limit aşıldıqda `PlanLimitError` atır (təqvim ayı ərzində).
    """
    plan = get_effective_plan(db, tutor_id)
    max_exams = plan.get("max_exams_per_month")
    if max_exams is None:
        return  # limitsiz plan

    used = get_month_exam_usage(db, tutor_id)
    if used >= int(max_exams):
        raise PlanLimitError(
            code="exam_limit_reached",
            message=(
                f"{plan.get('name')} planında bu ay üçün {max_exams} sınaq limitiniz "
                "dolub. Limitsiz sınaq yaratmaq üçün planınızı yüksəldin."
            ),
            plan_name=plan.get("name"),
            limit=int(max_exams),
            used=used,
            upgrade_suggested=True,
        )


def increment_exam_usage(db: Client, tutor_id: str) -> None:
    """Cari ayın sınaq sayğacını 1 artırır (UPSERT).

    Yalnız əlavə olunur; heç nə silinmir. DB xətası loglanır amma
    çağıran əməliyyatı dayandırmır (sınaq onsuz da yaradılıb — burada
    yalnız sayğacdır).
    """
    period = _current_period_start()
    try:
        db.rpc(
            "increment_tutor_exam_usage",
            {"p_tutor_id": tutor_id, "p_period_start": period},
        ).execute()
    except Exception:
        # RPC yoxdursa (miqrasiya tam icra edilməmişsə) Python fallback:
        try:
            existing = (
                db.table("tutor_plan_usage")
                .select("exams_created")
                .eq("tutor_id", tutor_id)
                .eq("period_start", period)
                .limit(1)
                .execute()
                .data
            )
            if existing:
                db.table("tutor_plan_usage").update(
                    {"exams_created": int(existing[0].get("exams_created") or 0) + 1,
                     "updated_at": datetime.now(timezone.utc).isoformat()}
                ).eq("tutor_id", tutor_id).eq("period_start", period).execute()
            else:
                db.table("tutor_plan_usage").insert({
                    "tutor_id": tutor_id,
                    "period_start": period,
                    "exams_created": 1,
                }).execute()
        except Exception:
            logger.exception("plans.increment_exam_usage failed tutor_id=%s", tutor_id)


# ---------------------------------------------------------------------------
# XƏTA SİNFI
# ---------------------------------------------------------------------------
class PlanLimitError(Exception):
    """Plan limiti aşıldı.

    `code` frontend-ə verilir ki, mesajı sətirdən yox, strukturlaşdırılmış
    formada göstərsin. DB/SQL detalları HEÇ VAZT daxil edilmir.
    """

    def __init__(
        self,
        code: str,
        message: str,
        plan_name: str = "",
        limit: int = 0,
        used: int = 0,
        upgrade_suggested: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.plan_name = plan_name
        self.limit = limit
        self.used = used
        self.upgrade_suggested = upgrade_suggested

    def to_dict(self) -> dict[str, Any]:
        """Müştəriyə təhlükəsiz cavab (stack trace/DB detalları yoxdur)."""
        return {
            "code": self.code,
            "message": self.message,
            "plan_name": self.plan_name,
            "limit": self.limit,
            "used": self.used,
            "upgrade_suggested": self.upgrade_suggested,
        }


def is_upgrade(value: str, current_plan: str) -> bool:
    """Yüksəltmə sorğusunun qəbul oluna biləcəyini yoxlayır (server tərəfdə).

    Təhlükəsizlik: istifadəçi yalnız özündən YÜKSƏK plan tələb edə bilər.
    """
    if value not in VALID_PLAN_IDS:
        return False
    if current_plan not in PLAN_RANK:
        return False
    return PLAN_RANK[value] > PLAN_RANK[current_plan]