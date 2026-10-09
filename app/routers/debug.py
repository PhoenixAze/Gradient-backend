# app/routers/debug.py
"""
Admin Debug Konsolu — Zero-Trust backend qatı (.clinerules §1).

TƏHLÜKƏSİZLİK İZAHI:
  * Autentifikasiya: `X-Debug-Key` başlığı ilə gələn açar `secrets.compare_digest()`
    ilə SABIIT VAXTLA müqayisə olunur → klassik timing attack mümkün deyil.
  * Açar `.env` / Render ENV-dən oxunur, heç vaxt cavabda qaytarılmır və
    loglanmır. Müəyyən deyilsə endpoint-lər "deaktiv" cavabı verir (fail-closed).
  * Rate limiting: bütun endpoint-lər `read`/`write` bucket-larına bağlıdır və
    limit AÇAR YOXLANILMASINDAN ƏVVƏL icra olunur (DoS boşluğu yoxdur).
  * Bütün giriş məlumatları Pydantic ilə tip/uzunluq/format üzrə yoxlanılır.
    Sual JSON-u ayrıca dərinlik və hədd yoxlamasından keçir (aşağıda).
  * Xəta mesajları: istifadəçiyə yalnız sanitizasiya edilmiş `detail` verilir;
    stack trace / DB detalları yalnız server loguna yazılır.
  * Sahə seçimi: `users` cədvəlindən yalnız AÇIQ sütunlar oxunur —
    `password_hash`, `refresh_token` və s. heç vaxt cavaba daxil edilmir.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import uuid
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field, field_validator

from app.core.rate_limit import rate_limit
from app.database import get_db

load_dotenv()

logger = logging.getLogger("gradient.debug")

router = APIRouter(prefix="/api/v1/debug", tags=["Debug"])

DEBUG_SECRET_KEY = os.getenv("DEBUG_SECRET_KEY", "").strip()
api_key_header = APIKeyHeader(name="X-Debug-Key", auto_error=False)

# Səhv cəhd cəzası (brute-force zəiflətmə). production-da sabit qalır.
FAILED_AUTH_DELAY_SECONDS = 3


async def verify_debug_key(request: Request, api_key: str = Security(api_key_header)) -> str:
    """Debug açarını SABIIT VAXTLA yoxlayır (timing attack müdafiəsi).

    `async` OLMASI MÜHÜMDÜR: `@rate_limit` dekoratoru endpoint-i async
    wrapper-a çevirib `await func(...)` edir. Sync (`def`) endpoint-lər bu
    zaman "object dict can't be used in 'await' expression" xətası verir və
    bütün debug endpoint-ləri 500 qaytarır. `await asyncio.sleep` ise event
    loop-u bloklamadan cəza tətbiq edir.
    """
    if not DEBUG_SECRET_KEY:
        # Konfiqurasiya boşdursa panel bağlıdır — "limitsiz" fallback YOXDUR.
        logger.error("debug_panel_disabled: DEBUG_SECRET_KEY mühit dəyişəni təyin edilməyib.")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Debug paneli deaktivdir.",
        )

    candidate = (api_key or "").strip()
    # compare_digest fərqli uzunluqda da sabit vaxtda işləyir (uzunluq sızıntısı yoxdur).
    if not secrets.compare_digest(candidate.encode("utf-8"), DEBUG_SECRET_KEY.encode("utf-8")):
        logger.warning(
            "debug_auth_failed ip=%s path=%s",
            getattr(request.client, "host", "unknown") if request.client else "unknown",
            request.url.path,
        )
        # Bloklayıcı `time.sleep` event loop-u dayandırırdı → async variant.
        await asyncio.sleep(FAILED_AUTH_DELAY_SECONDS)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="İcazə rədd edildi. Keçərsiz Debug açarı.",
        )
    return api_key


# ---------------------------------------------------------------- VALIDATION

# `identifier` yalnız e-poçt / telefon formatında ola bilər. Regex allowlist
# SQL injection və log injection mümkünlüyünü mənbədə bağlayır.
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9@._+\-\s]{3,120}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Sual JSON-u üçün hədlər (DoS/boş-doldurma müdafiəsi).
MAX_QUESTIONS = 200
MAX_QUESTION_TEXT = 2000
MAX_OPTION_TEXT = 500
MAX_RAW_JSON_BYTES = 512_000
ALLOWED_OPTION_KEYS = {"A", "B", "C", "D", "E", "F", "G", "H"}

# Mövzu teqinin (q_tag) hədləri.
MAX_TAG_LENGTH = 80
# Teq yalnız hərf/rəqəm/boşluq/sözdəki ayırıcılardan ibarət ola bilər —
# HTML/XML sətirləri (log injection, XSS) mənbədə kəsilir.
TAG_RE = re.compile(r"^[A-Za-z0-9ÇÖĞÜŞİçöğüşıİ\s\-_/,.()]{0,80}$")

# Frontend ilə eyni məqsəd: HTML/XML sətirləri əlavə olunmadan kəsilir.
# Backend regex-i son müdafiə xətti olaraq qalır (defense in depth).
UNSAFE_TAG_CHARS_RE = re.compile(r"[<>&\x60]")


class CheckUserRequest(BaseModel):
    identifier: str = Field(min_length=3, max_length=120)

    @field_validator("identifier")
    @classmethod
    def _check_identifier(cls, v: str) -> str:
        v = v.strip()
        if not IDENTIFIER_RE.match(v):
            raise ValueError("Format düzgün deyil.")
        return v


class BalanceUpdateRequest(BaseModel):
    identifier: str = Field(min_length=3, max_length=120)
    new_balance: float = Field(ge=0, le=1_000_000_000)

    @field_validator("identifier")
    @classmethod
    def _check_identifier(cls, v: str) -> str:
        v = v.strip()
        if not IDENTIFIER_RE.match(v):
            raise ValueError("Format düzgün deyil.")
        return v

    @field_validator("new_balance")
    @classmethod
    def _round(cls, v: float) -> float:
        return round(v, 2)


class ExamQuestion(BaseModel):
    """Tək sual. `correct_answer` yalnız serverdə saxlanılır.

    `q_tag` — sualın aid olduğu mövzu/teq (məs. "Triqonometriya").
    Analitika (`exam_results.weak_topics`) bu sətirlərdən qurulur: şagird
    mövzunu səhv cavablayanda həmin tez `weak_topics`-ə düşür.
    """

    text: str = Field(min_length=1, max_length=MAX_QUESTION_TEXT)
    options: Dict[str, str]
    correct_answer: str = Field(min_length=1, max_length=2)
    q_tag: str = Field(default="", max_length=MAX_TAG_LENGTH)
    explanation: Optional[str] = Field(default=None, max_length=MAX_QUESTION_TEXT)

    @field_validator("text")
    @classmethod
    def _strip_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Sual mətni boş ola bilməz.")
        return v

    @field_validator("options")
    @classmethod
    def _check_options(cls, v: Dict[str, str]) -> Dict[str, str]:
        if len(v) < 2:
            raise ValueError("Ən azı 2 variant tələb olunur.")
        cleaned: Dict[str, str] = {}
        for key, value in v.items():
            k = str(key).strip().upper()
            if k not in ALLOWED_OPTION_KEYS:
                raise ValueError("Variant açarı A–H aralığında olmalıdır.")
            text = str(value).strip()
            if not text:
                raise ValueError("Variant mətni boş ola bilməz.")
            if len(text) > MAX_OPTION_TEXT:
                raise ValueError("Variant mətni çox uzundur.")
            cleaned[k] = text
        return cleaned

    @field_validator("correct_answer")
    @classmethod
    def _check_answer(cls, v: str) -> str:
        v = str(v).strip().upper()
        if v not in ALLOWED_OPTION_KEYS:
            raise ValueError("Düzgün cavab A–H aralığında olmalıdır.")
        return v

    @field_validator("q_tag")
    @classmethod
    def _check_tag(cls, v: str) -> str:
        v = str(v or "").strip()
        if not v:
            return ""
        # 1) HTML/XML sətirləri kəsilir (log injection + downstream XSS).
        v = UNSAFE_TAG_CHARS_RE.sub("", v)
        # 2) Allowlist regex-i son təsdiq rolunu oynayır.
        if not TAG_RE.match(v):
            raise ValueError("Mövzu teqi yalnız hərf, rəqəm və boşluqdan ibarət ola bilər.")
        return v


class ExamUpsertRequest(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    subject: str = Field(min_length=2, max_length=80)
    questions: List[ExamQuestion] = Field(min_length=1, max_length=MAX_QUESTIONS)
    price: float = Field(default=0, ge=0, le=100_000)
    duration_minutes: int = Field(default=30, ge=1, le=600)
    is_active: bool = True

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Başlıq ən azı 3 simvol olmalıdır.")
        return v

    @field_validator("subject")
    @classmethod
    def _strip_subject(cls, v: str) -> str:
        return v.strip()

    @field_validator("price")
    @classmethod
    def _round_price(cls, v: float) -> float:
        return round(v, 2)

    @field_validator("questions")
    @classmethod
    def _check_questions(cls, v: List[ExamQuestion]) -> List[ExamQuestion]:
        if not v:
            raise ValueError("Ən azı 1 sual tələb olunur.")
        # Düz JSON ölçüsü həddi (yükləmə balastı).
        approx_bytes = sum(len(q.text) for q in v) + sum(
            len(k) + len(val) for q in v for k, val in q.options.items()
        )
        if approx_bytes > MAX_RAW_JSON_BYTES:
            raise ValueError("Sual məzumu həddi aşır.")
        return v

    def to_questions_json(self) -> List[Dict[str, Any]]:
        """`exams.questions` JSONB sütununa yazılacaq struktur.

        `q_id` frontend ilə uyğunluq üçün 1-dən başlayan sıra nömrəsidir
        (bax: app/routers/exams.py → submit_exam).
        """
        payload: List[Dict[str, Any]] = []
        for index, q in enumerate(self.questions, start=1):
            item: Dict[str, Any] = {
                "q_id": index,
                "q_tag": q.q_tag,
                "text": q.text,
                "options": dict(sorted(q.options.items())),
                "correct_answer": q.correct_answer,
            }
            if q.explanation:
                item["explanation"] = q.explanation.strip()
            payload.append(item)
        return payload


class ExamUpdateRequest(ExamUpsertRequest):
    """Redaktə üçün eyni gözləmə şərti (full replace — yarımçıq vəziyyət yoxdur)."""


# ------------------------------------------------------------------ ENDPOINTS


@router.post("/check-user")
@rate_limit("write")
async def check_user(payload: CheckUserRequest, request: Request, api_key: str = Depends(verify_debug_key)):
    """Mərhələ 1: e-poçt/nömrə ilə istifadəçini tapır və İZNLİ sahələri qaytarır.

    TƏHLÜKƏSİZLİK: yalnız AÇIQ sütunlar seçilir — `password_hash` və digər
    həssas sütunlar cavaba daxil edilmir.
    """
    db = get_db()
    try:
        user_res = (
            db.table("users")
            .select(
                "id, first_name, last_name, balance, role, identifier, grade, "
                "subject, tutor_id, tutor_code, created_at"
            )
            .eq("identifier", payload.identifier)
            .execute()
        )
        if not user_res.data:
            raise HTTPException(status_code=404, detail="Belə bir istifadəçi tapılmadı.")

        user = user_res.data[0]

        # Əlavə kontekst: repetitor və nəticə statistikası.
        tutor: Optional[Dict[str, Any]] = None
        if user.get("tutor_id"):
            tutor_res = (
                db.table("users")
                .select("first_name, last_name, identifier, tutor_code")
                .eq("id", user["tutor_id"])
                .execute()
            )
            if tutor_res.data:
                t = tutor_res.data[0]
                tutor = {
                    "first_name": t.get("first_name"),
                    "last_name": t.get("last_name"),
                    "identifier": t.get("identifier"),
                    "tutor_code": t.get("tutor_code"),
                }

        results_res = (
            db.table("exam_results")
            .select("id, score, incorrect_count, empty_count, total_questions, created_at")
            .eq("student_id", user["id"])
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        results = results_res.data or []
        total_attempts = len(results)
        avg_score = (
            round(sum(r.get("score", 0) for r in results) / total_attempts, 1)
            if total_attempts
            else 0
        )

        return {
            "id": user.get("id"),
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "balance": user.get("balance"),
            "role": user.get("role"),
            "identifier": user.get("identifier"),
            "grade": user.get("grade"),
            "subject": user.get("subject"),
            "tutor_code": user.get("tutor_code"),
            "created_at": user.get("created_at"),
            "tutor": tutor,
            "stats": {
                "recent_attempts": total_attempts,
                "average_score": avg_score,
            },
            "recent_results": [
                {
                    "score": r.get("score"),
                    "incorrect_count": r.get("incorrect_count"),
                    "empty_count": r.get("empty_count"),
                    "total_questions": r.get("total_questions"),
                    "created_at": r.get("created_at"),
                }
                for r in results
            ],
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("check_user_failed identifier=%s", payload.identifier)
        raise HTTPException(status_code=500, detail="Daxili sistem xətası baş verdi.")


@router.post("/update-balance")
@rate_limit("write")
async def update_user_balance(
    payload: BalanceUpdateRequest, request: Request, api_key: str = Depends(verify_debug_key)
):
    """Mərhələ 2: balansı dəyişir. Əməliyyat server loguna yazılır (audit izi)."""
    db = get_db()
    try:
        user_res = db.table("users").select("id, balance, first_name").eq(
            "identifier", payload.identifier
        ).execute()
        if not user_res.data:
            raise HTTPException(status_code=404, detail="İstifadəçi tapılmadı.")

        user_id = user_res.data[0]["id"]
        old_balance = user_res.data[0].get("balance", 0)

        db.table("users").update({"balance": payload.new_balance}).eq("id", user_id).execute()

        logger.info(
            "balance_updated user_id=%s old=%s new=%s",
            user_id,
            old_balance,
            payload.new_balance,
        )
        return {
            "message": f"Uğurlu! Yeni balans: {payload.new_balance:.2f} ₼",
            "previous_balance": old_balance,
            "new_balance": payload.new_balance,
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("update_balance_failed identifier=%s", payload.identifier)
        raise HTTPException(status_code=500, detail="Daxili sistem xətası baş verdi.")


@router.get("/exams")
@rate_limit("read")
async def list_exams(
    request: Request,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    api_key: str = Depends(verify_debug_key),
):
    """Sınaqların siyahısı. `questions` yüklənmir (ağır JSON sütunu).

    MÜHÜM — `is_active` FALLBACK:
      SQL miqrasiyası (`20260930000000_debug_exam_management.sql`) hələ
      icra edilməyibsə `exams.is_active` sütunu mövcud deyil və PostgREST
      sorğunu 400 ilə rədd edir → siyahı BOŞ görünür.
      Buna görə sütun seçimi ilə olmayan sorğu üçün təkrar cəhd edilir və
      cavabda `is_active: true` fərz edilir (mövcud sınaqlar aktivdir).
    """
    db = get_db()
    # Hədlər: istifadəçi sayfanı sonsuz böyüdə bilməz.
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    clean_search = ""
    if search:
        clean_search = re.sub(r"[%_]", "", str(search).strip())[:100]

    def _run(select_cols: str) -> List[Dict[str, Any]]:
        query = db.table("exams").select(select_cols)
        if clean_search:
            query = query.ilike("title", f"%{clean_search}%")
        query = query.order("created_at", desc=True).range(offset, offset + limit)
        res = query.execute()
        return res.data or []

    try:
        rows = _run(
            "id, title, subject, price, question_count, duration_minutes, is_active, created_at"
        )
    except Exception as exc:
        # Sütun yoxdur (42703/400) və ya sorğu dəstəklənmir → sətrsiz sorğu.
        logger.warning("list_exams_fallback triggered: %s", type(exc).__name__)
        try:
            rows = _run("id, title, subject, price, question_count, duration_minutes, created_at")
            for row in rows:
                row.setdefault("is_active", True)
        except Exception:
            logger.exception("list_exams_failed")
            raise HTTPException(status_code=500, detail="Sınaqlar yüklənə bilmədi.")

    return {"exams": rows, "limit": limit, "offset": offset}


@router.get("/exams/{exam_id}")
@rate_limit("read")
async def get_exam(exam_id: str, request: Request, api_key: str = Depends(verify_debug_key)):
    """Tək sınaqın tam JSON məzmunu (redaktə üçün)."""
    if not UUID_RE.match(exam_id or ""):
        raise HTTPException(status_code=400, detail="Sınaq ID formatı düzgün deyil.")

    db = get_db()
    try:
        res = db.table("exams").select("*").eq("id", exam_id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Sınaq tapılmadı.")
        return res.data[0]
    except HTTPException:
        raise
    except Exception:
        logger.exception("get_exam_failed exam_id=%s", exam_id)
        raise HTTPException(status_code=500, detail="Sınaq yüklənə bilmədi.")


@router.post("/exams")
@rate_limit("write")
async def create_exam(payload: ExamUpsertRequest, request: Request, api_key: str = Depends(verify_debug_key)):
    """Yeni sınaq yaradır. Sual JSON-u serverdə normallaşdırılır."""
    db = get_db()
    try:
        exam_id = str(uuid.uuid4())
        data = {
            "id": exam_id,
            "title": payload.title,
            "subject": payload.subject,
            "questions": payload.to_questions_json(),
            "question_count": len(payload.questions),
            "price": payload.price,
            "duration_minutes": payload.duration_minutes,
            "is_active": payload.is_active,
        }
        try:
            db.table("exams").insert(data).execute()
        except Exception as insert_err:
            logger.warning("create_exam: retry without is_active column: %s", insert_err)
            del data["is_active"]
            db.table("exams").insert(data).execute()
            data["is_active"] = True

        logger.info("exam_created exam_id=%s questions=%d", exam_id, len(payload.questions))
        return {"message": "Sınaq uğurla yaradıldı.", "exam": data}
    except Exception:
        logger.exception("create_exam_failed")
        raise HTTPException(status_code=500, detail="Sınaq yaradıla bilmədi.")


@router.put("/exams/{exam_id}")
@rate_limit("write")
async def update_exam(
    exam_id: str,
    payload: ExamUpdateRequest,
    request: Request,
    api_key: str = Depends(verify_debug_key),
):
    """Sınağı tam əvəzləyir (partial update yoxdur — yarımçıq JSON riski olmaz)."""
    if not UUID_RE.match(exam_id or ""):
        raise HTTPException(status_code=400, detail="Sınaq ID formatı düzgün deyil.")

    db = get_db()
    try:
        exists = db.table("exams").select("id").eq("id", exam_id).execute()
        if not exists.data:
            raise HTTPException(status_code=404, detail="Sınaq tapılmadı.")

        data = {
            "title": payload.title,
            "subject": payload.subject,
            "questions": payload.to_questions_json(),
            "question_count": len(payload.questions),
            "price": payload.price,
            "duration_minutes": payload.duration_minutes,
            "is_active": payload.is_active,
        }
        try:
            db.table("exams").update(data).eq("id", exam_id).execute()
        except Exception as update_err:
            logger.warning("update_exam: retry without is_active column: %s", update_err)
            del data["is_active"]
            db.table("exams").update(data).eq("id", exam_id).execute()
            data["is_active"] = True

        logger.info("exam_updated exam_id=%s", exam_id)
        return {"message": "Sınaq uğurla yeniləndi.", "exam": {"id": exam_id, **data}}
    except HTTPException:
        raise
    except Exception:
        logger.exception("update_exam_failed exam_id=%s", exam_id)
        raise HTTPException(status_code=500, detail="Sınaq yenilənə bilmədi.")


@router.delete("/exams/{exam_id}")
@rate_limit("write")
async def delete_exam(exam_id: str, request: Request, api_key: str = Depends(verify_debug_key)):
    """Sınağı silir.

    TƏHLÜKƏSİZLİK: FK zənciri qırılmasın deyə əvvəlcədən `exam_results`
    sətrləri yoxlanılır; nəticələri mövcuddursa silmə rədd edilir
    (məlumat itkisi məqsədli qadağan edilir).
    """
    if not UUID_RE.match(exam_id or ""):
        raise HTTPException(status_code=400, detail="Sınaq ID formatı düzgün deyil.")

    db = get_db()
    try:
        exists = db.table("exams").select("id, title").eq("id", exam_id).execute()
        if not exists.data:
            raise HTTPException(status_code=404, detail="Sınaq tapılmadı.")

        results = db.table("exam_results").select("id").eq("exam_id", exam_id).limit(1).execute()
        if results.data:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Bu sınaq şagirdlər tərəfindən həll olunub — silmə mümkün deyil. "
                "Əvvəlcə sınağı deaktiv edin.",
            )

        db.table("exams").delete().eq("id", exam_id).execute()
        logger.warning("exam_deleted exam_id=%s title=%s", exam_id, exists.data[0].get("title"))
        return {"message": "Sınaq silindi."}
    except HTTPException:
        raise
    except Exception:
        logger.exception("delete_exam_failed exam_id=%s", exam_id)
        raise HTTPException(status_code=500, detail="Sınaq silinə bilmədi.")


@router.get("/stats")
@rate_limit("read")
async def get_system_stats(request: Request, api_key: str = Depends(verify_debug_key)):
    """Sistemin sağlamlığını və Supabase metrikalarını qaytarır."""
    db = get_db()
    try:
        users_res = db.table("users").select("role").execute()
        users = users_res.data or []
        total_users = len(users)
        students = sum(1 for u in users if u.get("role") == "student")
        tutors = sum(1 for u in users if u.get("role") == "tutor")
        admins = sum(1 for u in users if u.get("role") == "admin")

        exams_res = db.table("exams").select("question_count").execute()
        exams = exams_res.data or []
        total_exams = len(exams)
        total_questions = sum(int(e.get("question_count") or 0) for e in exams)

        return {
            "status": "online",
            "database": "connected",
            "metrics": {
                "users": {
                    "total": total_users,
                    "students": students,
                    "tutors": tutors,
                    "admins": admins,
                },
                "exams": {
                    "total": total_exams,
                    "total_questions": total_questions,
                },
            },
        }
    except Exception:
        # Müştəriyyə DB detalları SIZDIRILMIR — yalnız ümumi mesaj.
        logger.exception("get_system_stats_failed")
        return {
            "status": "offline",
            "database": "error",
            "message": "Verilənlər bazası ilə əlaqə qurmaq mümkün olmadı.",
        }
