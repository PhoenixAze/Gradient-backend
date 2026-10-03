# app/routers/analytics.py
"""
Şagirdin analitika məlumatları və AI (Gemini) analizləri.

TƏHLÜKƏSİZLİK (.clinerules §1):
  * Bütün endpoint-lər `get_current_user` ilə qorunur və `.eq("student_id", ...)`
    filtrləri ilə yalnız CARİ sessiyanın məlumatını qaytarır (IDOR qoruması:
    klient `?student_id=` göndərə bilmir — parametr qəbul edilmir).
  * AI sorğuları `ai` rate-limit bucket-ına gedir (bahalı əməliyyat).
  * Gemini açarı yalnız serverdə oxunur; cavabda heç vaxt qaytarılmır.
  * AI cavabı JSON-dur; onu istifadəçiyə "mətn" kimi deyil, struktur kimi
    ötürürük (frontend yalnız `textContent` yazır → XSS mümkün deyil).
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.gemini import generate_json, is_configured
from app.core.rate_limit import rate_limit
from app.database import get_db
from app.security import get_current_user

logger = logging.getLogger("gradient.analytics")

router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics"])

MAX_TOPIC_ROWS = 40
MIN_SAMPLE_FOR_TOPIC = 1


# ============================================================================
# /me — Əsas analitika (statistika = yalnız 1-ci cəhdlər)
# ============================================================================

@router.get("/me")
def get_my_analytics(current_user: dict = Depends(get_current_user)):
    """
    Şagirdin real sınaq nəticələrinə əsaslanan tam mövzu və fənn analitikası.

    STATİSTİKA QAYDASI: `exam_results` cədvəli yalnız 1-ci cəhdləri saxlayır
    (`exam_attempts.is_primary = true` olanlar). Təkrar cəhdlər bu statistikaya
    qarışmır — əvvəlki nəticə qalır. Mövzu (q_tag) statistikası isə BÜTÜN
    cəhdlərdən yığılır ki, təkrar cəhd şagirdin göstəricisini "gizlətmiş" olmasın.
    """
    db = get_db()
    student_id = current_user["id"]

    # 1) Əsas statistika — 1-ci cəhdlər
    results_res = db.table("exam_results").select(
        "id, exam_id, score, incorrect_count, empty_count, total_questions, weak_topics, created_at"
    ).eq("student_id", student_id).order("created_at", desc=True).execute()

    results = results_res.data or []

    # Cəhd məlumatları (təkrar cəhdlər daxil) — mövzu statistikası üçün
    # DIQQET: `has_ai` adlı sütun YOXDUR — belə sütun seçilməsi PostgREST-də
    # 400 qaytarır. Mövcud `ai_analysis` sütunu seçilir (null ola bilər).
    #
    # DEPLOY TƏHLÜKƏSİZLİĞİ: `exam_attempts` SQL miqrasiyası ilə yaradılır.
    # Miqrasiya hələ icra edilməyibsə, analitika səhifəsi ÇÖKMƏMƏLİDİR —
    # mövcud (`exam_results`-ə əsaslanan) bütün statistika qalır, sadəcə yeni
    # cəhd/mövzu bölmələri boş göstərilir.
    try:
        attempts_res = db.table("exam_attempts").select(
            "id, exam_id, attempt_no, is_primary, score, incorrect_count, empty_count, "
            "total_questions, question_details, ai_analysis"
        ).eq("student_id", student_id).order("created_at", desc=True).execute()
        attempts = attempts_res.data or []
    except Exception:
        attempts = []

    if not results and not attempts:
        return _empty_payload()

    # 2) Sınaq meta məlumatları
    exam_ids = {r["exam_id"] for r in results if r.get("exam_id")}
    exam_ids |= {a["exam_id"] for a in attempts if a.get("exam_id")}
    exams_map: Dict[str, dict] = {}
    if exam_ids:
        exams_res = db.table("exams").select("id, title, subject, question_count").in_("id", list(exam_ids)).execute()
        exams_map = {e["id"]: e for e in (exams_res.data or [])}

    # 3) Cəhd indeksini qur (exam_id → attempt list) — sürətli UI render üçün
    attempts_by_exam: Dict[str, List[dict]] = {}
    for a in attempts:
        attempts_by_exam.setdefault(a.get("exam_id"), []).append(a)
    for lst in attempts_by_exam.values():
        lst.sort(key=lambda x: x.get("attempt_no") or 0)

    # 4) Fənn və mövzu aqreqasiyası (BÜTÜN cəhdlər üzrə)
    subjects_agg: Dict[str, Dict[str, Any]] = {}
    topics_agg: Dict[str, Dict[str, int]] = {}

    for a in attempts:
        exam = exams_map.get(a.get("exam_id"), {})
        subject = exam.get("subject") or "Digər"
        tq = a.get("total_questions") or 0
        sc = a.get("score") or 0
        inc = a.get("incorrect_count")
        emp = a.get("empty_count")
        if inc is None:
            inc = max(0, tq - sc)
        if emp is None:
            emp = max(0, tq - (sc + inc))

        s = subjects_agg.setdefault(subject, {
            "subject": subject,
            "total_questions": 0,
            "correct_count": 0,
            "incorrect_count": 0,
            "empty_count": 0,
            "attempts_count": 0,
        })
        s["total_questions"] += tq
        s["correct_count"] += sc
        s["incorrect_count"] += inc
        s["empty_count"] += emp
        s["attempts_count"] += 1

        # Per-sual detallarından mövzu (q_tag) statistikası
        details = a.get("question_details") or []
        for d in details:
            if not isinstance(d, dict):
                continue
            tag = str(d.get("q_tag") or "").strip() or "Qeyd olunmayan mövzu"
            st = d.get("status")
            t = topics_agg.setdefault(tag, {"correct": 0, "incorrect": 0, "empty": 0, "total": 0})
            t["total"] += 1
            if st == "correct":
                t["correct"] += 1
            elif st == "incorrect":
                t["incorrect"] += 1
            else:
                t["empty"] += 1

    # 5) Ümumi statistika — 1-ci cəhdlərdən (təkrar cəhd sayılmaz)
    total_exams = len(results)
    total_questions = 0
    total_correct = 0
    total_incorrect = 0
    total_empty = 0
    history: List[dict] = []

    for r in results:
        exam = exams_map.get(r["exam_id"], {})
        q_count = r.get("total_questions") or exam.get("question_count") or 0
        score = r.get("score") or 0
        inc = r.get("incorrect_count")
        emp = r.get("empty_count")
        if inc is None:
            inc = max(0, q_count - score)
        if emp is None:
            emp = max(0, q_count - (score + inc))

        total_questions += q_count
        total_correct += score
        total_incorrect += inc
        total_empty += emp

        exam_attempts = attempts_by_exam.get(r["exam_id"], [])
        primary = next((x for x in exam_attempts if x.get("is_primary")), None)
        retakes = [x for x in exam_attempts if not x.get("is_primary")]

        history.append({
            "id": r["id"],
            "exam_id": r["exam_id"],
            # `attempt_id` → frontend "AI Analiz" düyməsini bu ID ilə çağırır.
            # `exam_attempts` cədvəli mövcud deyilsə None qaytarılır (aşağıda
            # təhlükəsiz fallback sətri yaradılır).
            "attempt_id": (primary or {}).get("id"),
            "title": exam.get("title", "Sınaq"),
            "subject": exam.get("subject", "Digər"),
            "score": score,
            "incorrect_count": inc,
            "empty_count": emp,
            "total_questions": q_count,
            "created_at": r["created_at"],
            "percentage": round((score / q_count * 100), 1) if q_count > 0 else 0,
            "attempt_no": (primary or {}).get("attempt_no", 1),
            "has_ai_analysis": bool((primary or {}).get("ai_analysis")),
            "retake_count": len(retakes),
            # `exam_attempts` cədvəli yoxdursa frontend üçün cəhd məlumatı
            # lazımdır ki, AI düyməsi "tapılmadı" deyib ölü qalmasın.
            "attempts_known": bool(exam_attempts),
        })

    # 6) Fənn statistikası
    subject_stats = []
    for subj, data in subjects_agg.items():
        tq = data["total_questions"]
        acc = round((data["correct_count"] / tq * 100), 1) if tq > 0 else 0
        subject_stats.append({
            "subject": subj,
            "total_questions": tq,
            "correct_count": data["correct_count"],
            "incorrect_count": data["incorrect_count"],
            "empty_count": data["empty_count"],
            "attempts_count": data["attempts_count"],
            "accuracy_pct": acc,
        })
    subject_stats.sort(key=lambda x: x["accuracy_pct"], reverse=True)

    # 7) Mövzu (q_tag) statistikası — hansı mövzuda nə qədər çətinlik çəkilir
    topic_stats = []
    for tag, data in topics_agg.items():
        tot = data["total"]
        if tot < MIN_SAMPLE_FOR_TOPIC:
            continue
        acc = round((data["correct"] / tot * 100), 1)
        topic_stats.append({
            "topic": tag,
            "total_questions": tot,
            "correct_count": data["correct"],
            "incorrect_count": data["incorrect"],
            "empty_count": data["empty"],
            "accuracy_pct": acc,
        })
    topic_stats.sort(key=lambda x: (x["accuracy_pct"], -x["total_questions"]))
    topic_stats = topic_stats[:MAX_TOPIC_ROWS]

    overall_accuracy = round((total_correct / total_questions * 100), 1) if total_questions > 0 else 0.0

    # 8) Deterministik diaqnoz (AI olmadan da boş panel qalmasın)
    ai_diagnosis = _build_deterministic_diagnosis(
        overall_accuracy, subject_stats, topic_stats, total_exams
    )

    return {
        "has_data": True,
        "total_exams": total_exams,
        "total_attempts": len(attempts),
        "retake_attempts": max(0, len(attempts) - len(results)),
        "total_questions": total_questions,
        "correct_count": total_correct,
        "incorrect_count": total_incorrect,
        "empty_count": total_empty,
        "accuracy_pct": overall_accuracy,
        "subject_stats": subject_stats,
        "topic_stats": topic_stats,
        "history": history,
        "ai_diagnosis": ai_diagnosis,
        "ai_available": is_configured(),
    }


def _now_iso() -> str:
    """
    DB-yə yazılacaq timestamptz dəyəri.

    TƏHLÜKƏSİZLİK/KİMLİK: `now()` SQL funksiyası PostgREST JSON gövəsində
    CAST OLUNMUR — göndərilən "now()" mətni timestamptz sütununa yazılmağa
    çalışanda xəta verir və bütün analiz yazımı itir. Buna görə ISO-8601
    UTC dəyəri hesablanıb göndərilir.
    """
    return datetime.now(timezone.utc).isoformat()


def _empty_payload() -> dict:
    return {
        "has_data": False,
        "total_exams": 0,
        "total_attempts": 0,
        "retake_attempts": 0,
        "total_questions": 0,
        "correct_count": 0,
        "incorrect_count": 0,
        "empty_count": 0,
        "accuracy_pct": 0.0,
        "subject_stats": [],
        "topic_stats": [],
        "history": [],
        "ai_diagnosis": None,
        "ai_available": is_configured(),
    }


def _build_deterministic_diagnosis(
    accuracy: float,
    subject_stats: List[dict],
    topic_stats: List[dict],
    total_exams: int,
) -> Optional[str]:
    """
    Gemini çağırılmadan əvvəl göstərilən tez diaqnoz.
    Qəsdən heuristikdir: faktiki rəqəmlərə əsaslanır, "hallucinasiya" riski yoxdur.
    """
    if not subject_stats:
        return None

    weakest_subject = subject_stats[-1]
    strongest_subject = subject_stats[0]
    weak_topics = [t for t in topic_stats if t["accuracy_pct"] < 60][:3]

    parts: List[str] = []
    parts.append(
        f"{total_exams} sınaq, {accuracy}% ümumi dəqiqlik."
    )

    if weakest_subject["accuracy_pct"] < 60:
        parts.append(
            f"Ən çox xal itkisi '{weakest_subject['subject']}' fənnindədir "
            f"({weakest_subject['accuracy_pct']}%)."
        )
    else:
        parts.append(
            f"'{weakest_subject['subject']}' fənni ən zəif nöqtənizdir "
            f"({weakest_subject['accuracy_pct']}%)."
        )

    if weak_topics:
        names = ", ".join(f"{t['topic']} ({t['accuracy_pct']}%)" for t in weak_topics)
        parts.append(f"Təkrar cəhd tələb edən mövzular: {names}.")

    parts.append(
        f"Ən güclü tərəfiniz '{strongest_subject['subject']}' fənnidir "
        f"({strongest_subject['accuracy_pct']}%)."
    )

    parts.append("Aşağıdakı 'AI Analiz' düyməsi ilə hər sınaq üzrə ayrıca fərdi analiz yarada bilərsiniz.")
    return " ".join(parts)


# ============================================================================
# AI: HƏR BİR CƏHD ÜZRƏ FƏRDİ ANALİZ
# ============================================================================

_EXAM_SYSTEM_PROMPT = (
    "Sən Azərbaycan dili ilə danışan peşəkar imtahan müəllisisən. "
    "Sənə bir şagirdin BİR sınaq cəhdindəki (sual üzrə səhv etdiyi/yetmədiyi) "
    "məlumatları verilir. Sən MƏHDUD və DƏQİQ rəqəmlər əsasında analiz yazırsan. "
    "Qaydalar:\n"
    "1) HEÇ VAKT rəqəm uydurma. Verilməyən fəni uydurma.\n"
    "2) Şagirdə \"öyrənməlidir\" deyə konkret mövzu adları qeyd et.\n"
    "3) Müzakirəli/şəxsi fərziyyə yazma, faktiki nəticə yaz.\n"
    "4) Cavab tamamilə AZƏRBAYCAN DİLİNDƏ olsun.\n"
    "5) YALNIZ aşağıdakı JSON formatında cavab ver (markdown YOXDUR):\n"
    "{\n"
    '  "summary": "2-3 cümləlik ümumi qiymətləndirmə",\n'
    '  "headline": "bir cümləlik ənVacib nəticə (məs: \"Hərəkət problemləri ən zəif mövzunuzdur\")",\n'
    '  "strong_topics": [{"topic": "mövzu adı", "note": "niyə yaxşı"}],\n'
    '  "weak_topics": [{"topic": "mövzu adı", "note": "niyə problemli", "priority": "high|medium|low"}],\n'
    '  "mistakes": [{"topic": "mövzu", "issue": "tipik səhv nədir", "advice": " konkret tövsiyə"}],\n'
    '  "recommendations": ["3-5 qısa, konkret həftəlik tövsiyə (hər biri 1 cümlə)"],\n'
    '  "study_plan": ["3-6 addımlıq qısa öyrənmə planı"]\n'
    "}\n"
    "Hər massiv boş ola bilər (məs. bütün suallar düzgündürsə weak_topics boş olur)."
)


class ExamAiAnalysisRequest(BaseModel):
    """AI analiz çağırışı — gövdə yoxdur, yalnız cəhd seçilir."""
    force: bool = Field(default=False, description="true = cache-i nəzərə almadan yenidən yarat")


@router.post("/attempts/{attempt_id}/ai-analysis")
@rate_limit("ai")
async def analyze_attempt(
    attempt_id: str,
    payload: ExamAiAnalysisRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    Bir cəhdin (və ya ilk cəhdin) AI analizini yaradır.

    TƏHLÜKƏSİZLİK:
      * `attempt_id` yalnız cari şagirdin cəhdi ola bilər → `.eq("student_id")`.
      * `request: Request` parametri `rate_limit("ai")` dekoratoru üçün məcburidir.
    """
    db = get_db()
    student_id = current_user["id"]

    # Zero-Trust: başqa şagirdin attempt_id'si → 404 (varlığını açıqlamırıq)
    attempt_res = db.table("exam_attempts").select(
        "id, exam_id, attempt_no, score, incorrect_count, empty_count, total_questions, "
        "question_details, weak_topics, ai_analysis, created_at"
    ).eq("id", attempt_id).eq("student_id", student_id).limit(1).execute()

    if not attempt_res.data:
        raise HTTPException(status_code=404, detail="Analiz üçün cəhd tapılmadı.")

    attempt = attempt_res.data[0]

    # Cache: eyni cəhd üçün analiz artıq yaradılıbsa, yenidən API xərci etmirik
    if attempt.get("ai_analysis") and not payload.force:
        return {
            "attempt_id": attempt_id,
            "analysis": attempt["ai_analysis"],
            "cached": True,
            "ai_model": attempt.get("ai_model"),
        }

    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI analiz xidməti hazırda konfiqurasiya edilməyib.",
        )

    # Sınaq meta məlumatı (başlıq + fənn) — AI-ya kontekst verir
    exam_res = db.table("exams").select("id, title, subject, grade").eq("id", attempt["exam_id"]).limit(1).execute()
    exam = (exam_res.data or [{}])[0] or {}

    user_content = _build_attempt_prompt(attempt, exam)
    data, model = generate_json(_EXAM_SYSTEM_PROMPT, user_content)

    if data is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI xidməti hazırda cavob verə bilmədi. Bir az sonra yenidən cəhd edin.",
        )

    # DB-yə yazmaq UĞURSUZ olsa belə cavabı itirmirik (frontend göstərə bilir),
    # sadəcə loglayırıq — analiz "itə bilən" nadir hadisədir.
    # TƏHLÜKƏSİZLİK: yazma `.eq("student_id", student_id)` ilə məhdudlaşır →
    # klient başqa şagirdin cəhdinə yaza bilmir (IDOR qoruması).
    try:
        db.table("exam_attempts").update({
            "ai_analysis": data,
            "ai_model": model,
            "ai_generated_at": _now_iso(),
        }).eq("id", attempt_id).eq("student_id", student_id).execute()
    except Exception:
        # TƏHLÜKƏSİZLİK: xəta detalları (DB mesajı, sütun adları) cavabda
        # və istifadəçiyə sızdırılmaz — yalnız server logunda qalır.
        logger.warning("ai_analysis_cache_write_failed scope=attempt", exc_info=True)

    return {
        "attempt_id": attempt_id,
        "analysis": data,
        "cached": False,
        "ai_model": model,
    }


def _build_attempt_prompt(attempt: dict, exam: dict) -> str:
    """
    AI-ya göndərilən məlumat paketi.
    TƏHLÜKƏSİZLİK: buraya PII (ad, e-poçta, identifier) daxil EDİLMİR —
    modelə yalnız fənn/performans məlumatı gedir.
    """
    total = attempt.get("total_questions") or 0
    score = attempt.get("score") or 0
    pct = round(score / total * 100, 1) if total else 0

    lines = [
        f"SINAQ: {exam.get('title') or 'Sınaq'}",
        f"FƏNN: {exam.get('subject') or 'Müəyyən deyil'}",
        f"SİNIF: {exam.get('grade') or 'Müəyyən deyil'}",
        f"CƏHD NÖMRƏSİ: {attempt.get('attempt_no') or 1}",
        f"NƏTİCƏ: {score}/{total} ({pct}%) — səhv: {attempt.get('incorrect_count') or 0}, boş: {attempt.get('empty_count') or 0}",
        "",
        "SUAL ÜZRƏ NƏTİCƏLƏR:",
    ]

    details = attempt.get("question_details") or []
    if not details:
        # Köhnə nəticilərdə per-sual məlumat yoxdur → yalnız agregatdan analiz et
        lines.append("(per-sual məlumat yoxdur, yalnız ümumi göstəricilərə əsaslan)")
        for w in (attempt.get("weak_topics") or [])[:10]:
            if isinstance(w, dict):
                lines.append(f"- {w.get('topic')}: {w.get('wrong')} səhv")
    else:
        for i, d in enumerate(details, start=1):
            if not isinstance(d, dict):
                continue
            st = d.get("status")
            marker = {"correct": "DÜZGÜN", "incorrect": "SƏHV", "empty": "BOŞ"}.get(st, "BİLİNMƏYƏN")
            tag = d.get("q_tag") or "-"
            chosen = d.get("chosen") or "-"
            correct = d.get("correct") or "-"
            preview = str(d.get("text_preview") or "")[:120]
            lines.append(
                f"{i}. [{marker}] mövzu={tag} | şagird={chosen} | düzgün={correct} | sual: {preview}"
            )

    return "\n".join(lines)


# ============================================================================
# AI: BÜTÜN SINAQLAR ÜZRƏ ÜMUMİ ANALİZ
# ============================================================================

_OVERALL_SYSTEM_PROMPT = (
    "Sən Azərbaycan dili ilə danışan peşəkar təhsil analitikisən. "
    "Sənə bir şagirdin BÜTÜN sınaq nəticələri (fənn və mövzu üzrə aqreqatlar) verilir. "
    "Sən məqsədli, strukturlaşdırılmış, FAKTİKİ analiz yazırsan.\n"
    "Qaydalar:\n"
    "1) HEÇ VAXT rəqəm/mövzu adı uydurma — yalnız verilən məlumatı istifadə et.\n"
    "2) Ən çətin fəni, ən çətin mövzuları və ən güclü tərəfləri konkret göstər.\n"
    "3) Tövsiyələr ölçülə bilən olsun (nə çox, nə hansı sırayla).\n"
    "4) Cavab tamamilə AZƏRBAYCAN DİLİNDƏ olsun.\n"
    "5) YALNIZ aşağıdakı JSON formatında cavab ver (markdown YOXDUR):\n"
    "{\n"
    '  "summary": "3-4 cümləlik ümumi qiymətləndirmə",\n'
    '  "headline": "bir cümləlik ən güclü nəticə",\n'
    '  "focus_subjects": [{"subject": "fənn", "accuracy_pct": 0-100, "reason": "səbəb"}],\n'
    '  "focus_topics": [{"topic": "mövzu", "accuracy_pct": 0-100, "priority": "high|medium|low", "action": "nə etməli"}],\n'
    '  "strong_topics": [{"topic": "mövzu", "note": "niyə güclüdür"}],\n'
    '  "recommendations": ["4-6 konkret, prioritetləndirilmiş tövsiyə"],\n'
    '  "weekly_plan": ["həftəlik 3-6 addımlıq plan"]\n'
    "}\n"
)


class OverallAiRequest(BaseModel):
    force: bool = Field(default=False)


@router.post("/overall/ai-analysis")
@rate_limit("ai")
async def analyze_overall(
    request: Request,
    payload: OverallAiRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Bütün sınaqlar üzrə ümumi AI analiz.
    Nəticə `student_ai_insights` cədvəlində saxlanılır (upsert) ki, sonrakı
    səhifə açılışlarında təkrar AI xərci olmasın (qənaət + sürət).
    """
    db = get_db()
    student_id = current_user["id"]

    stats = get_my_analytics(current_user)

    if not stats.get("has_data"):
        raise HTTPException(status_code=404, detail="Analiz üçün heç bir sınaq nəticəsi yoxdur.")

    # Cache
    # DEPLOY TƏHLÜKƏSİZLİĞİ: cədvəl miqrasiya ilə yaradılır; yoxdursa analiz
    # sadəcə cache-siz işləyir (aşağıda Gemini çağırılır, yazma isə `try` ilə).
    try:
        existing_res = db.table("student_ai_insights").select(
            "id, summary, headline, focus_subjects, focus_topics, strong_topics, "
            "recommendations, ai_model, version, updated_at"
        ).eq("student_id", student_id).limit(1).execute()
        existing = (existing_res.data or [None])[0]
    except Exception:
        existing = None

    if existing and existing.get("summary") and not payload.force:
        return {
            "has_analysis": True,
            "analysis": {
                "summary": existing.get("summary"),
                "headline": existing.get("headline"),
                "focus_subjects": existing.get("focus_subjects") or [],
                "focus_topics": existing.get("focus_topics") or [],
                "strong_topics": existing.get("strong_topics") or [],
                "recommendations": existing.get("recommendations") or [],
            },
            "cached": True,
            "ai_model": existing.get("ai_model"),
            "version": existing.get("version"),
            "updated_at": existing.get("updated_at"),
        }

    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI analiz xidməti hazırda konfiqurasiya edilməyib.",
        )

    user_content = _build_overall_prompt(stats)
    data, model = generate_json(_OVERALL_SYSTEM_PROMPT, user_content)

    if data is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI xidməti hazırda cavab verə bilmədi. Bir az sonra yenidən cəhd edin.",
        )

    row = {
        "student_id": student_id,
        "scope": "overall",
        "summary": _clip(data.get("summary"), 2000),
        "headline": _clip(data.get("headline"), 300),
        "focus_subjects": _as_list(data.get("focus_subjects"), 10),
        "focus_topics": _as_list(data.get("focus_topics"), 15),
        "strong_topics": _as_list(data.get("strong_topics"), 15),
        "recommendations": _as_list(data.get("recommendations"), 10),
        "stats_snapshot": {
            "accuracy_pct": stats.get("accuracy_pct"),
            "total_exams": stats.get("total_exams"),
            "total_attempts": stats.get("total_attempts"),
            "total_questions": stats.get("total_questions"),
        },
        "ai_model": model,
        "version": int((existing or {}).get("version") or 0) + 1,
        "updated_at": _now_iso(),
    }

    try:
        # Təhlükəsiz yazma: upsert yalnız öz sətrinə (student_id) — IDOR yoxdur.
        db.table("student_ai_insights").upsert(row, on_conflict="student_id").execute()
    except Exception:
        logger.warning("ai_analysis_cache_write_failed scope=overall", exc_info=True)

    return {
        "has_analysis": True,
        "analysis": data,
        "cached": False,
        "ai_model": model,
        "version": row["version"],
    }


def _build_overall_prompt(stats: dict) -> str:
    lines = [
        f"ÜMUMİ NƏTİCƏ: {stats.get('accuracy_pct')}% dəqiqlik",
        f"Sınaq sayı: {stats.get('total_exams')} (təkrar cəhdlər: {stats.get('retake_attempts')})",
        f"Ümumi sual sayı: {stats.get('total_questions')}",
        "",
        "FƏNLƏR ÜZRƏ (dəqiqlik azdan çoxa):",
    ]
    for s in (stats.get("subject_stats") or []):
        lines.append(
            f"- {s.get('subject')}: {s.get('accuracy_pct')}% "
            f"({s.get('correct_count')}/{s.get('total_questions')}, cəhd: {s.get('attempts_count')})"
        )

    lines.append("")
    lines.append("MÖVZULAR ÜZRƏ (ən çətinlərdən):")
    for t in (stats.get("topic_stats") or [])[:20]:
        lines.append(
            f"- {t.get('topic')}: {t.get('accuracy_pct')}% "
            f"(səhv {t.get('incorrect_count')}, boş {t.get('empty_count')}, cəmi {t.get('total_questions')})"
        )

    if not (stats.get("topic_stats") or []):
        lines.append("(mövzu teqi olmayan köhnə sınaqlar)")
        for w in (stats.get("history") or [])[:10]:
            lines.append(f"- {w.get('title')}: {w.get('percentage')}%")

    return "\n".join(lines)


@router.get("/overall/ai-analysis")
def get_cached_overall(current_user: dict = Depends(get_current_user)):
    """Saxlanmış ümumi analizi oxuyur (AI xərci etmədən)."""
    db = get_db()
    try:
        res = db.table("student_ai_insights").select(
            "id, summary, headline, focus_subjects, focus_topics, strong_topics, "
            "recommendations, stats_snapshot, ai_model, version, updated_at"
        ).eq("student_id", current_user["id"]).limit(1).execute()
        row = (res.data or [None])[0]
    except Exception:
        row = None

    if not row:
        return {"has_analysis": False}

    return {
        "has_analysis": True,
        "analysis": {
            "summary": row.get("summary"),
            "headline": row.get("headline"),
            "focus_subjects": row.get("focus_subjects") or [],
            "focus_topics": row.get("focus_topics") or [],
            "strong_topics": row.get("strong_topics") or [],
            "recommendations": row.get("recommendations") or [],
        },
        "stats_snapshot": row.get("stats_snapshot") or {},
        "ai_model": row.get("ai_model"),
        "version": row.get("version"),
        "updated_at": row.get("updated_at"),
    }


# ============================================================================
# KÖMƏKÇİ: cavabın uzunluğunu məhdudlaşdırmaq (DoS/məlumat keyfiyyəti)
# ============================================================================

def _clip(value: Any, max_len: int) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        value = str(value)
    text = str(value).strip()
    return text[:max_len] if text else None


def _as_list(value: Any, max_items: int) -> list:
    """
    Model JSON-u bəzən dict/list qarışıq qaytarır.
    Burada yalnız MAZMUNun ölçüsü və tipi normalize edilir; mətn istifadəçiyə
    `textContent` ilə çatdırılacağı üçün HTML riski yoxdur (frontend).
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return [{"text": value[:500]}]
        value = parsed
    if not isinstance(value, list):
        return []
    return value[:max_items]
