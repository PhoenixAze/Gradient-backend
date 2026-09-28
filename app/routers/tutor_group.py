"""
FASTAPI REFERANS MODULU — Repetitor qrup sistemi (4 rəqəmli kod + qurmaq)

YERLƏŞDİRMƏ: bu fayl backend repositoriyasında `app/routers/tutor_group.py`
kimi yerləşdirilir və `app/main.py` daxilində qeyd edilir:

    from app.routers import tutor_group
    app.include_router(tutor_group.router, prefix="/api/v1/tutor", tags=["tutor-group"])

TƏHLÜKƏSİZLİK (.clinerules §1 — Zero-Trust Backend):
  * Bütün endpoint-lər autentifikasiya tələb edir (`require_tutor`).
  * Bütün DB əməliyyatları supabase-py + service_role açarı ilə,
    yəni yalnız server tərəfdədir. Frontend heç vaxt cədvələ toxunmur.
  * Pydantic v2 ilə giriş sanizasiyası (uzunluq/format/trim).
  * Rate limiting: `ai` bucket-i olmayan ucuz əməliyyatlar üçün
    `write` bucket-i; AI əməliyyatları ayrıca `ai` bucket-ini saxlayır.
  * Xəta mesajları müştəriyə stack trace SIZDIRILMIR — yalnız
    biznes məntiqi mesajı. Bəs qəbərsə 500 qaytarılır və iç səhvi
    `logger.exception` ilə jurnala düşür (açar redaktə edilməklə).
  * Obyekt səviyyəsində giriş yoxlaması: repetitor yalnız öz qrupundakı
    şagirdi (users.tutor_id == current_user.id) görə/idarə edə bilər —
    bu, IDOR hücumunun qarşısını alır.

MİQRASİYA: supabase/migrations/20260101000000_tutor_group_system.sql
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from supabase import Client

from app.core.config import settings
from app.core.rate_limit import rate_limit
from app.core.security import get_supabase_admin, require_tutor, require_user

logger = logging.getLogger("gradient.tutor_group")

router = APIRouter()

TELEPHONE_RE = re.compile(r"^\+?994\d{9}$|^0\d{9}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
TUTOR_CODE_RE = re.compile(r"^[0-9]{4}$")


# ---------------------------------------------------------------------------
# Pydantic modelləri (giriş doğrulaması)
# ---------------------------------------------------------------------------
class AddStudentIn(BaseModel):
    """Şagirdi qrupa əlavə etmək üçün göndərilən gövdə."""

    identifier: str = Field(min_length=5, max_length=120)

    @field_validator("identifier")
    @classmethod
    def _validate_identifier(cls, v: str) -> str:
        val = v.strip()
        if not val:
            raise ValueError("Boş dəyər")
        if not (EMAIL_RE.match(val) or TELEPHONE_RE.match(val)):
            raise ValueError("E-poçt və ya mobil nömrə formatı düzgün deyil")
        return val.lower() if EMAIL_RE.match(val) else val


class JoinTutorIn(BaseModel):
    """Şagirdin repetitor kodu ilə qoşulma sorğusu."""

    tutor_code: str = Field(min_length=4, max_length=4)

    @field_validator("tutor_code")
    @classmethod
    def _validate_code(cls, v: str) -> str:
        val = v.strip()
        if not TUTOR_CODE_RE.match(val):
            raise ValueError("Kod 4 rəqəmdən ibarət olmalıdır")
        return val


class TutorProfileIn(BaseModel):
    first_name: str = Field(min_length=2, max_length=60)
    last_name: str = Field(min_length=2, max_length=60)
    subject: str = Field(min_length=2, max_length=80)


# ---------------------------------------------------------------------------
# DAXİLİ KÖMƏKÇİLƏR
# ---------------------------------------------------------------------------
def _fail(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    """Müştəriyə təhlükəsiz xəta qaytarır (DB detalları sızdırılmır)."""
    return HTTPException(status_code=code, detail=message)


def _user_public(row: dict[str, Any]) -> dict[str, Any]:
    """İstifadəçi sətrini frontend-ə təhlükəsiz forma verir.

    `password_hash` və daxili sütunlar heç vaxt qaytarılmır (.clinerules §1).
    """
    return {
        "id": row.get("id"),
        "first_name": row.get("first_name") or "",
        "last_name": row.get("last_name") or "",
        "identifier": row.get("identifier") or "",
        "grade": row.get("grade"),
        "subject": row.get("subject"),
        "tutor_code": row.get("tutor_code"),  # yalnız repetitor üçün doludur
    }


# ---------------------------------------------------------------------------
# 1. DASHBOARD — 4 rəqəmli kod buradan qaytarılır
# ---------------------------------------------------------------------------
@router.get("/dashboard")
@rate_limit("read")
async def get_dashboard(
    tutor: dict[str, Any] = Depends(require_tutor),
    db: Client = Depends(get_supabase_admin),
) -> dict[str, Any]:
    """Repetitor panelinin bütün məlumatı.

    Əsas düzəliş: cavabda `tutor.tutor_code` mütləq qaytarılır
    (əvvəlki versiya bu sahəni qaytarmırdı → dashboard "----" göstərirdi).
    """
    tutor_id = tutor["id"]

    try:
        tutor_row = (
            db.table("users")
            .select("id, first_name, last_name, identifier, subject, tutor_code")
            .eq("id", tutor_id)
            .single()
            .execute()
            .data
        )
        if not tutor_row:
            raise _fail("Repetitor profili tapılmadı", status.HTTP_404_NOT_FOUND)

        # Əgər köhnə sətirdə kod yoxdursa (trigger-dan əvvəl yaradılmış),
        # o zamanı bir dəfə generate edib yadda saxla.
        if not tutor_row.get("tutor_code"):
            tutor_row = (
                db.table("users")
                .select("id, first_name, last_name, identifier, subject, tutor_code")
                .eq("id", tutor_id)
                .update({"tutor_code": f"{__import__('random').randint(1000, 9999)}"})
                .single()
                .execute()
                .data
            )

        students = (
            db.table("users")
            .select("id, first_name, last_name, identifier, grade")
            .eq("tutor_id", tutor_id)
            .eq("role", "student")
            .order("created_at", desc=False)
            .execute()
            .data
            or []
        )

        submissions = (
            db.table("exam_results")
            .select("id, student_id, exam_id, score, total_questions, "
                    "incorrect_count, empty_count, created_at")
            .in_("student_id", [s["id"] for s in students] or ["00000000-0000-0000-0000-000000000000"])
            .order("created_at", desc=True)
            .limit(50)
            .execute()
            .data
            or []
        )

        summaries = (
            db.table("exams")
            .select("id, title, subject, total_questions, duration_minutes")
            .order("created_at", desc=True)
            .limit(25)
            .execute()
            .data
            or []
        )

    except HTTPException:
        raise
    except Exception:
        logger.exception("tutor.dashboard failed: tutor_id=%s", tutor_id)
        raise _fail("Dashboard məlumatları yüklənmədi", status.HTTP_500_INTERNAL_SERVER_ERROR)

    total_questions = sum(int(s.get("total_questions") or 0) for s in submissions)
    total_score = sum(int(s.get("score") or 0) for s in submissions)
    accuracy = round(total_score / total_questions * 100) if total_questions else 0

    return {
        # `tutor_code` — dashboard-un 4 rəqəmli kodunun mənbəyi
        "tutor": _user_public(tutor_row),
        "stats": {
            "total_students": len(students),
            "active_students": len({s["student_id"] for s in submissions}) if submissions else 0,
            "total_exams_completed": len(submissions),
            "group_avg_accuracy": accuracy,
            "top_student": _top_student(students, submissions),
            "lowest_student": _lowest_student(students, submissions),
        },
        "students": [_student_with_metrics(s, submissions) for s in students],
        "exam_summaries": summaries,
        "recent_submissions": submissions,
    }


def _student_with_metrics(
    student: dict[str, Any], submissions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Şagirdin nəticə statistikasını hesablayır (vahid məntiq)."""
    rows = [s for s in submissions if s.get("student_id") == student["id"]]
    total_q = sum(int(r.get("total_questions") or 0) for r in rows)
    total_s = sum(int(r.get("score") or 0) for r in rows)
    pct = round(total_s / total_q * 100) if total_q else 0

    if total_q == 0:
        status_label = "Sınaq işləməyib"
    elif pct >= 80:
        status_label = "Yaxşı"
    elif pct >= 50:
        status_label = "Orta"
    else:
        status_label = "Zəif"

    return {
        "id": student["id"],
        "first_name": student.get("first_name") or "",
        "last_name": student.get("last_name") or "",
        "identifier": student.get("identifier") or "",
        "grade": student.get("grade"),
        "exams_count": len(rows),
        "total_questions": total_q,
        "accuracy_pct": pct,
        "last_score": (
            f"{rows[0].get('score')}/{rows[0].get('total_questions')}" if rows else "-"
        ),
        "status": status_label,
    }


def _top_student(
    students: list[dict[str, Any]], submissions: list[dict[str, Any]]
) -> Optional[str]:
    if not students:
        return None
    best = max(
        students,
        key=lambda s: sum(
            int(r.get("score") or 0) for r in submissions if r.get("student_id") == s["id"]
        ),
    )
    return f"{best.get('first_name') or ''} {best.get('last_name') or ''}".strip() or None


def _lowest_student(
    students: list[dict[str, Any]], submissions: list[dict[str, Any]]
) -> Optional[str]:
    if not students:
        return None
    worst = min(
        students,
        key=lambda s: sum(
            int(r.get("score") or 0) for r in submissions if r.get("student_id") == s["id"]
        ),
    )
    return f"{worst.get('first_name') or ''} {worst.get('last_name') or ''}".strip() or None


# ---------------------------------------------------------------------------
# 2. ŞAGİRD ƏLAVƏ ETMƏ / ÇIXARMAQ
# ---------------------------------------------------------------------------
@router.post("/students/add", status_code=status.HTTP_201_CREATED)
@rate_limit("write")
async def add_student(
    payload: AddStudentIn,
    tutor: dict[str, Any] = Depends(require_tutor),
    db: Client = Depends(get_supabase_admin),
) -> dict[str, Any]:
    """Şagirdi repetitorun qrupuna əlavə edir (users.tutor_id yazılır).

    Əsas düzəliş: əvvəlki versiya əlaqəni bazada saxlamırdı və
    şagird səhifəni yenilədikdə yoxa çıxırdı.
    """
    tutor_id = tutor["id"]

    try:
        student = (
            db.table("users")
            .select("id, first_name, last_name, identifier, grade, tutor_id")
            .eq("identifier", payload.identifier)
            .eq("role", "student")
            .single()
            .execute()
            .data
        )
    except Exception:
        student = None

    if not student:
        raise _fail("Bu E-poçt/mobil ilə qeydiyyatdan keçmiş şagird tapılmadı", 404)

    if student.get("tutor_id") == tutor_id:
        raise _fail("Şagird artıq bu qrupdadır", 409)

    try:
        # Köhnə qrupdakı pending istəkləri ləğv edilir
        db.table("tutor_join_requests").update(
            {"status": "cancelled", "resolved_at": "now()"}
        ).eq("student_id", student["id"]).eq("status", "pending").execute()

        db.table("users").update({"tutor_id": tutor_id}).eq("id", student["id"]).execute()
    except Exception:
        logger.exception("tutor.add_student failed: student_id=%s", student["id"])
        raise _fail("Şagirdi əlavə etmək mümkün olmadı", 500)

    return {
        "message": (
            f"{student.get('first_name') or ''} {student.get('last_name') or ''}".strip()
            or "Şagird"
        )
        + " uğurla qrupa əlavə edildi!",
        "student": _user_public(student),
    }


@router.delete("/students/{student_id}")
@rate_limit("write")
async def remove_student(
    student_id: str,
    tutor: dict[str, Any] = Depends(require_tutor),
    db: Client = Depends(get_supabase_admin),
) -> dict[str, Any]:
    """Şagirdi qrupdan çıxarır. Hesabı SİLİNMİR (tələb + məlumat qorunur)."""
    tutor_id = tutor["id"]

    try:
        # OBYEKT SƏVİYYƏSİNDƏ GİRİŞ YOXLAMASI (IDOR qoruması):
        # repetitor yalnız öz qrupundakı şagirdi çıxara bilər.
        updated = (
            db.table("users")
            .update({"tutor_id": None})
            .eq("id", student_id)
            .eq("tutor_id", tutor_id)
            .execute()
            .data
        )
    except Exception:
        logger.exception("tutor.remove_student failed: student_id=%s", student_id)
        raise _fail("Şagirdi çıxarmaq mümkün olmadı", 500)

    if not updated:
        raise _fail("Şagird bu qrupda deyil və ya tapılmadı", 404)

    return {"message": "Şagird qrupdan çıxarıldı"}


# ---------------------------------------------------------------------------
# 3. QRUPA QOŞULMA İSTƏKLƏRİ
# ---------------------------------------------------------------------------
@router.post("/requests", status_code=status.HTTP_201_CREATED)
@rate_limit("write")
async def create_join_request(
    payload: JoinTutorIn,
    student: dict[str, Any] = Depends(require_user),
    db: Client = Depends(get_supabase_admin),
) -> dict[str, Any]:
    """Şagird 4 rəqəmli kodu daxil edib qoşulma istəyi göndərir.

    Endpoint `js/exam.js` tərəfindən `POST /api/v1/tutor/requests`
    göndərilən `{ "tutor_code": "1234" }` gövdesi ilə çağırılır.
    """
    student_id = student["id"]

    # DB RPC-si bütün invariantları (tək pending, özünə qoşulma qadağanı,
    # əvvəlki istəklərin ləğvi) atomik şəkildə idarə edir.
    # ⚠️ RPC student_id-ni parametr kimi alır — service_role-da `auth.uid()`
    #    NULL qaytarır, ona görə kimlik burada ötürülür.
    try:
        result = db.rpc(
            "submit_tutor_join_request",
            {"p_student_id": student_id, "p_tutor_code": payload.tutor_code},
        ).execute()
    except Exception as exc:
        logger.exception("tutor.create_join_request failed: student_id=%s", student_id)
        # DB-nin biznes qaydası xətalarını istifadəçiyə tərcüman edirik,
        # texniki detalları (SQL, stack) jurnala yazılır, müştəriyə çıxmır.
        message = str(getattr(exc, "message", "") or "")
        if "tutor_not_found" in message:
            raise _fail("Bu kodla repetitor tapılmadı. Kodu yoxlayın.", 404)
        if "self_join" in message:
            raise _fail("Bu kod özünüzə aiddir", 400)
        if "not_a_student" in message:
            raise _fail("Bu əməliyyat yalnız şagird hesabı üçün mümkündür", 403)
        raise _fail("İstək göndərilə bilmədi", 500)

    return {"message": "Qoşulma istəyiniz repetitora göndərildi.", **(result.data or {})}


@router.get("/requests")
@rate_limit("read")
async def list_join_requests(
    tutor: dict[str, Any] = Depends(require_tutor),
    db: Client = Depends(get_supabase_admin),
) -> list[dict[str, Any]]:
    """Repetitorun gözləyən istəkləri (şagird məlumatları ilə birlikdə)."""
    try:
        requests = (
            db.table("tutor_join_requests")
            .select("id, student_id, created_at")
            .eq("tutor_id", tutor["id"])
            .eq("status", "pending")
            .order("created_at", desc=True)
            .execute()
            .data
            or []
        )
        if not requests:
            return []

        student_ids = [r["student_id"] for r in requests]
        students = (
            db.table("users")
            .select("id, first_name, last_name, identifier, grade")
            .in_("id", student_ids)
            .execute()
            .data
            or []
        )
        by_id = {s["id"]: s for s in students}
    except Exception:
        logger.exception("tutor.list_join_requests failed")
        raise _fail("İstəklər yüklənmədi", 500)

    return [
        {
            "id": r["id"],
            "student_id": r["student_id"],
            "student_name": " ".join(
                filter(
                    None,
                    [
                        by_id.get(r["student_id"], {}).get("first_name"),
                        by_id.get(r["student_id"], {}).get("last_name"),
                    ],
                )
            )
            or "Şagird",
            "student_identifier": by_id.get(r["student_id"], {}).get("identifier") or "",
            "student_grade": by_id.get(r["student_id"], {}).get("grade"),
            "created_at": r.get("created_at"),
        }
        for r in requests
    ]


@router.post("/requests/{request_id}/accept")
@rate_limit("write")
async def accept_join_request(
    request_id: str,
    tutor: dict[str, Any] = Depends(require_tutor),
    db: Client = Depends(get_supabase_admin),
) -> dict[str, Any]:
    """İstəyi qəbul edir. Bütün əlaqə dəyişiklikləri DB RPC-si ilə
    atomik şəkildə baş verir (status və users.tutor_id ayrılmaz)."""
    try:
        row = (
            db.table("tutor_join_requests")
            .select("tutor_id, status")
            .eq("id", request_id)
            .single()
            .execute()
            .data
        )
    except Exception:
        row = None

    if not row or row.get("tutor_id") != tutor["id"]:
        raise _fail("Tələb tapılmadı", 404)

    try:
        db.rpc("accept_tutor_join_request", {"p_request_id": request_id}).execute()
    except Exception:
        logger.exception("tutor.accept failed: request_id=%s", request_id)
        raise _fail("İstəyi qəbul etmək mümkün olmadı", 500)

    return {"message": "Şagird qrupa qəbul edildi"}


@router.post("/requests/{request_id}/reject")
@rate_limit("write")
async def reject_join_request(
    request_id: str,
    tutor: dict[str, Any] = Depends(require_tutor),
    db: Client = Depends(get_supabase_admin),
) -> dict[str, Any]:
    try:
        row = (
            db.table("tutor_join_requests")
            .select("tutor_id, status")
            .eq("id", request_id)
            .single()
            .execute()
            .data
        )
    except Exception:
        row = None

    if not row or row.get("tutor_id") != tutor["id"]:
        raise _fail("Tələb tapılmadı", 404)

    try:
        db.rpc("reject_tutor_join_request", {"p_request_id": request_id}).execute()
    except Exception:
        logger.exception("tutor.reject failed: request_id=%s", request_id)
        raise _fail("İstəyi rədd etmək mümkün olmadı", 500)

    return {"message": "İstək rədd edildi"}


# ---------------------------------------------------------------------------
# 4. PROFİL
# ---------------------------------------------------------------------------
@router.put("/profile")
@rate_limit("write")
async def update_profile(
    payload: TutorProfileIn,
    tutor: dict[str, Any] = Depends(require_tutor),
    db: Client = Depends(get_supabase_admin),
) -> dict[str, Any]:
    """Profil məlumatlarını yeniləyir.

    `tutor_code` burada dəyişdirilə bilməz — o yalnız sistem tərəfindən
    generasiya olunur (miqrasiya trigger-i). Bu, 4 rəqəmli kodun
    təxrib edilməsinin qarşısını alır.
    """
    try:
        row = (
            db.table("users")
            .update(
                {
                    "first_name": payload.first_name.strip(),
                    "last_name": payload.last_name.strip(),
                    "subject": payload.subject.strip(),
                }
            )
            .eq("id", tutor["id"])
            .select("id, first_name, last_name, identifier, subject, tutor_code")
            .single()
            .execute()
            .data
        )
    except Exception:
        logger.exception("tutor.update_profile failed")
        raise _fail("Məlumatları saxlamaq mümkün olmadı", 500)

    return {"message": "Profil yeniləndi", "tutor": _user_public(row)}


# NOTE: `require_user` artıq yuxarıda `require_tutor` ilə birlikdə import olunur
# (sətir 39). Əvvəlki versiya bu import-u faylın SONUNA qoymuşdu, lakin
# `create_join_request` funksiyasının imzası icra vaxtında qiymətləndirildiyi
# üçün bu, import mərhələsində NameError verirdi.
