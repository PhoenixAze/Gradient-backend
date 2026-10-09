# app/routers/exams.py
"""
Platforma sınaqları: siyahı, satın alma, başlatma, təhvil, cəhd tarixçəsi.

TƏKRAR CƏHD MƏNTİQİ (.clinerules §1 — Zero-Trust + Data integrity):
  Şagird bitirdiyi sınağı yenidən işləyə bilər. Amma:
    * ƏVVƏLKI (ilk) NƏTİCƏ QORUNUR — heç vaht üzerinə yazılmır.
    * TƏKRAR CƏHD ÜMUMİ STATİSTİKAYA TƏSİR ETMİR.
  Bunun üçün statistikanın mənbəyi ayrıca cədvəldir:
    `exam_results`  → yalnız 1-ci cəhd (əvvəl sxemada mövcud idi).
    `exam_attempts` → BÜTÜN cəhdlər (1-ci cəhd daxil), per-sual detalları,
                       AI analizləri.
  Beləliklə "hər ikisini də yaz" prensipi yoxdur — hər cəhd `exam_attempts`-ə
  bir sətir yazılır, `exam_results`-ə yalnız 1-ci dəfə yazılır. Yarışma
  (race condition) yarana bilmir, UPSERT yarışması da olmur.
"""

from typing import Dict, List, Optional

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.database import get_db
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/exams", tags=["Exams"])

# Təhlükəsizlik limitləri (gözə toxunmayan sabitlər — env ilə dəyişmək
# mümkündür, lakin default dəyər məqsədli hədəf seçməyə mane olur)
MAX_ATTEMPTS_PER_EXAM = 5
MAX_ANSWERS_PER_SUBMIT = 300
MAX_ANSWER_KEY_LEN = 8
MAX_ATTEMPT_HISTORY = 50


class ExamSubmitRequest(BaseModel):
    """Təhvil formu — yalnız cavab xaritası, heç nə digər."""

    answers: Dict[str, str] = Field(default_factory=dict)

    @field_validator("answers", mode="before")
    @classmethod
    def _sanitize_answers(cls, v):
        """
        Zero-Trust: istifadəçinin göndərdiyi obyektə YOXDAN İDARƏ OLUNMUR.
        Yalnız A–H aralığından ibarət variant açarı saxlanılır və say limiti
        tətbiq edilir → nəhəng JSON göndərilməsi (DoS) mümkün deyil.
        """
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("Cavablar obyekt formatında olmalıdır.")
        if len(v) > MAX_ANSWERS_PER_SUBMIT:
            raise ValueError("Cavab sayı həddi aşdı.")

        cleaned: Dict[str, str] = {}
        for raw_key, raw_value in v.items():
            key = str(raw_key)[:64].strip()
            value = str(raw_value or "").strip().upper()[:MAX_ANSWER_KEY_LEN]
            if not key or not value:
                continue
            if value not in ("A", "B", "C", "D", "E", "F", "G", "H"):
                continue
            cleaned[key] = value
        return cleaned


# ============================================================================
# SİYAHI
# ============================================================================

@router.get("/")
def get_all_exams(current_user: dict = Depends(get_current_user)):
    db = get_db()

    exams_res = db.table("exams").select(
        "id, title, subject, price, question_count, duration_minutes, is_active, created_at"
    ).order("created_at", desc=True).execute()

    raw_exams = exams_res.data or []
    exams = [e for e in raw_exams if e.get("is_active") is not False]

    # Statistika mənbəyi = 1-ci cəhd (`exam_results`).
    results_res = db.table("exam_results").select(
        "exam_id, score, incorrect_count, empty_count"
    ).eq("student_id", current_user["id"]).execute()

    completed_exams_data = {res["exam_id"]: res for res in (results_res.data or [])}

    # Təkrar cəhd sayı (yalnız UI üçün: "2 dəfə işlədiniz" badge-i).
    # `select("exam_id").eq("student_id", ...)` — ağır sütunlar (question_details)
    # çəkilmədiyi üçün bu sorğu yüngüldür.
    #
    # DEPLOY TƏHLÜKƏSİZLİĞİ: `exam_attempts` cədvəli SQL miqrasiyası ilə yaradılır.
    # Miqrasiya hələ icra edilməyibsə, səhifə ÇÖKMƏMƏLİDİR (fail-soft yalnız bu
    # əlavə xüsusiyyətdə) — əks halda "Yenidən işlə" funksiyası olmayan səhifə
    # istifadəçini sınaq zalından da qoparardı. Əsas statistika (`exam_results`)
    # isə heç vaxt bu yola toxunmur.
    attempts_count: Dict[str, int] = {}
    try:
        attempts_res = db.table("exam_attempts").select(
            "exam_id"
        ).eq("student_id", current_user["id"]).execute()
        for row in (attempts_res.data or []):
            exam_id = row.get("exam_id")
            if not exam_id:
                continue
            attempts_count[exam_id] = attempts_count.get(exam_id, 0) + 1
    except Exception:
        # Yalnız cədvəl mövcud deyilsə xəta verə bilər; səhva yoxdur.
        attempts_count = {}

    for exam in exams:
        exam_id = exam["id"]
        result = completed_exams_data.get(exam_id)
        if result:
            # TƏHLÜKƏSİZLİK/NULL-SAFE: köhnə sətirlərdə null ola bilər.
            exam["is_completed"] = True
            exam["correct_count"] = result.get("score") or 0
            exam["incorrect_count"] = result.get("incorrect_count") or 0
            exam["empty_count"] = result.get("empty_count") or 0
        else:
            exam["is_completed"] = False

        exam["attempt_count"] = attempts_count.get(exam_id, 1 if result else 0)
        exam["can_retake"] = exam["attempt_count"] < MAX_ATTEMPTS_PER_EXAM

    return exams


# ============================================================================
# SATIN ALMA
# ============================================================================

@router.post("/{exam_id}/purchase")
def purchase_exam(exam_id: str, current_user: dict = Depends(get_current_user)):
    """Pullu sınağın satın alınması (Zero-Trust balans yoxlanışı)."""
    db = get_db()

    exam_res = db.table("exams").select("id, price").eq("id", exam_id).execute()
    if not exam_res.data:
        raise HTTPException(status_code=404, detail="Sınaq tapılmadı.")

    exam = exam_res.data[0]
    price = float(exam.get("price", 0))

    # TƏKRAR CƏHD PUL TƏLƏB ETMİR: balans "bu sınaqı AL" üçün bir dəfə çıxılır,
    # təkrar işlətmə isə təkrarlama məqsədi ilə pulsuzdur (aşağıdakı yoxlama).
    first_attempt_res = db.table("exam_results").select("id").eq(
        "exam_id", exam_id
    ).eq("student_id", current_user["id"]).limit(1).execute()
    if first_attempt_res.data:
        return {"success": True, "message": "Sınaq artıq alınıb — təkrar işlətmə pulsuzdur."}

    if price <= 0:
        return {"success": True, "message": "Sınaq pulsuzdur."}

    user_res = db.table("users").select("id, balance").eq("id", current_user["id"]).execute()
    if not user_res.data:
        raise HTTPException(status_code=404, detail="İstifadəçi profili tapılmadı.")

    current_balance = float(user_res.data[0].get("balance", 0) or 0)

    if current_balance < price:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Balansınız kifayət etmir. Zəhmət olmasa balansı artırın."
        )

    new_balance = round(current_balance - price, 2)
    db.table("users").update({"balance": new_balance}).eq("id", current_user["id"]).execute()

    return {"success": True, "new_balance": new_balance, "message": "Sınaq uğurla satın alındı."}


# ============================================================================
# BAŞLATMA (təkrar cəhd dəstəyi)
# ============================================================================

@router.get("/{exam_id}/start")
def start_exam(exam_id: str, current_user: dict = Depends(get_current_user)):
    db = get_db()

    # Təkrar cəhd: eyni sınağı bir neçə dəfə işləmək olar, sadəcə limitli.
    # `exam_attempts` yoxdursa (miqrasiya icra edilməyib), səhifə yeni cəhdi
    # başlatmaq mümkün deyil — amma bu MÜHİM DEYİL, köhnə davranış qalır.
    try:
        attempts_res = db.table("exam_attempts").select(
            "id, attempt_no"
        ).eq("exam_id", exam_id).eq("student_id", current_user["id"]).execute()
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Təkrar cəhd xidməti hazırda əlçatan deyil. Zəhmət olmasa biraz sonra cəhd edin."
        )

    attempts = attempts_res.data or []
    attempt_no = len(attempts) + 1

    if attempt_no > MAX_ATTEMPTS_PER_EXAM:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Bu sınaq üçün maksimum {MAX_ATTEMPTS_PER_EXAM} cəhd limitinə "
                "çatmısınız. Əlavə cəhd üçün repetitorunuzla əlaqə saxlayın."
            )
        )

    exam_res = db.table("exams").select(
        "id, title, subject, question_count, duration_minutes, questions"
    ).eq("id", exam_id).execute()
    if not exam_res.data:
        raise HTTPException(status_code=404, detail="Sınaq tapılmadı.")

    exam_data = dict(exam_res.data[0])
    questions = exam_data.get("questions") or []

    # TƏHLÜKƏSİZLİK: düzgün cavablar (`correct_answer`) və izahlar
    # (`explanation`) frontend-ə HEÇ VAXT göndərilmir. Sabit allow-list —
    # JSONB-dən gələn "əlavə" açar (məsən daxili metadata) sızdırılmır.
    safe_questions = []
    for idx, q in enumerate(questions):
        if not isinstance(q, dict):
            continue
        options = q.get("options")
        q_id = q.get("q_id") if q.get("q_id") is not None else (idx + 1)
        safe_questions.append({
            "q_id": q_id,
            "text": q.get("text"),
            "options": options if isinstance(options, dict) else {},
        })

    exam_data["questions"] = safe_questions
    exam_data["attempt_no"] = attempt_no
    exam_data["is_retake"] = attempt_no > 1
    exam_data["max_attempts"] = MAX_ATTEMPTS_PER_EXAM
    # AI analiz yalnız TAMAMLANMIŞ cəhd üçün mövcuddur.
    exam_data["analysis_available"] = True

    return exam_data


# ============================================================================
# TƏHVİL VERİLMƏSİ
# ============================================================================

@router.post("/{exam_id}/submit")
def submit_exam(exam_id: str, payload: ExamSubmitRequest, current_user: dict = Depends(get_current_user)):
    db = get_db()

    exam_res = db.table("exams").select("title, subject, questions, question_count").eq("id", exam_id).execute()
    if not exam_res.data:
        raise HTTPException(status_code=404, detail="Sınaq tapılmadı.")

    exam = exam_res.data[0]
    questions = exam.get("questions") or []
    total_questions = int(exam.get("question_count") or len(questions))
    user_answers = payload.answers

    # ---- Cəhd nömrəsi: paralel submit yarışmasına qarşı atomik sayım ----
    try:
        attempts_res = db.table("exam_attempts").select("id, attempt_no").eq(
            "exam_id", exam_id
        ).eq("student_id", current_user["id"]).execute()
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Nəticəni qeyd etmək mümkün deyil. Zəhmət olmasa biraz sonra cəhd edin."
        )

    attempt_no = len(attempts_res.data or []) + 1
    if attempt_no > MAX_ATTEMPTS_PER_EXAM:
        raise HTTPException(
            status_code=403,
            detail="Bu sınaq üçün cəhd limiti dolub."
        )

    # ---- Yoxlama: yalnız BİR sətirlik nəticə (`exam_results`) ilə işləyən
    #      köhnə sütunlar varsa, ilk cəhd artıq yazılıb deməkdir. ----
    first_result_res = db.table("exam_results").select("id").eq(
        "exam_id", exam_id
    ).eq("student_id", current_user["id"]).limit(1).execute()
    is_first_attempt = not bool(first_result_res.data)

    correct_count = 0
    incorrect_count = 0
    question_details: List[dict] = []
    weak_topics: Dict[str, int] = {}

    for idx, q in enumerate(questions):
        if not isinstance(q, dict):
            continue

        q_id = str(q.get("q_id") if q.get("q_id") is not None else (idx + 1))
        correct_ans = str(q.get("correct_answer") or "").strip().upper()
        q_tag = str(q.get("q_tag") or "").strip() or "Qeyd olunmayan mövzu"
        chosen = user_answers.get(q_id, "") or user_answers.get(str(idx + 1), "") or user_answers.get(str(idx), "")

        if chosen and correct_ans and chosen == correct_ans:
            status_value = "correct"
            correct_count += 1
        elif chosen:
            status_value = "incorrect"
            incorrect_count += 1
            weak_topics[q_tag] = weak_topics.get(q_tag, 0) + 1
        else:
            status_value = "empty"
            weak_topics[q_tag] = weak_topics.get(q_tag, 0) + 1

        question_details.append({
            "q_id": q_id,
            "q_tag": q_tag,
            "status": status_value,
            "chosen": chosen or None,
            "correct": correct_ans or None,
            # AI üçün qısa kontekst (bütün mətn göndərilmir → token qənaəti
            # və məlumat minimallaşdırma prinsipi).
            "text_preview": str(q.get("text") or "")[:180],
        })

    empty_count = max(0, total_questions - (correct_count + incorrect_count))

    # `weak_topics` sətiri yalnız 1-ci cəhd üçün yazılır (statistikanın mənbəyi).
    weak_topics_list = sorted(
        [{"topic": t, "wrong": c} for t, c in weak_topics.items() if c > 0],
        key=lambda x: x["wrong"],
        reverse=True,
    )

    # ---- 1) BÜTÜN cəhdlər `exam_attempts`-ə yazılır ----
    attempt_row = {
        "exam_result_id": None,
        "student_id": current_user["id"],
        "exam_id": exam_id,
        "attempt_no": attempt_no,
        "is_primary": is_first_attempt,
        "score": correct_count,
        "incorrect_count": incorrect_count,
        "empty_count": empty_count,
        "total_questions": total_questions,
        "answers": user_answers,
        "question_details": question_details,
        "weak_topics": weak_topics_list,
    }

    # ---- 2) `exam_results` YALNIZ 1-ci cəhd üçün yazılır ----
    #     Beləliklə təkrar cəhd heç vaxt əvvəlki nəticəni "üzrə yazmır".
    if is_first_attempt:
        result_data = {
            "id": str(uuid.uuid4()),
            "student_id": current_user["id"],
            "exam_id": exam_id,
            "score": correct_count,
            "incorrect_count": incorrect_count,
            "empty_count": empty_count,
            "total_questions": total_questions,
            "weak_topics": weak_topics_list,
        }
        ins_res = db.table("exam_results").insert(result_data).execute()
        inserted = (ins_res.data or [None])[0]
        result_id = inserted.get("id") if inserted else result_data["id"]
        attempt_row["exam_result_id"] = result_id

    db.table("exam_attempts").insert(attempt_row).execute()

    return {
        "message": "Sınaq uğurla bitdi!",
        "score": correct_count,
        "incorrect": incorrect_count,
        "empty": empty_count,
        "total": total_questions,
        "attempt_no": attempt_no,
        "is_retake": attempt_no > 1,
        # Təkrar cəhd statistikanı dəyişmədiyini UI-da açıq bildiririk.
        "counts_towards_stats": is_first_attempt,
        "weak_topics": weak_topics_list[:5],
    }


# ============================================================================
# CƏHD TARİXÇƏSİ (həm bir sınaq üzrə, həm də bütün səhifə üzrə)
# ============================================================================

@router.get("/{exam_id}/attempts")
def get_exam_attempts(exam_id: str, current_user: dict = Depends(get_current_user)):
    """
    Bir sınaqın bütün cəhdləri (təkrar cəhdlər daxil).
    Zero-Trust: `.eq("student_id", current_user["id"])` → şagird yalnız ÖZ
    cəhdlərini görür. Başqa şagirdin sətrini sorğu ilə əldə etmək mümkündür.
    """
    db = get_db()

    exam_res = db.table("exams").select("id, title, subject").eq("id", exam_id).execute()
    if not exam_res.data:
        raise HTTPException(status_code=404, detail="Sınaq tapılmadı.")
    exam = exam_res.data[0]

    # DEPLOY TƏHLÜKƏSİZLİĞİ: `exam_attempts` SQL miqrasiyası ilə yaradılır.
    # Miqrasiya hələ icra edilməyibsə endpoint 500 atmasın — əksinə köhnə
    # `exam_results` cədvəlindən "sintetik" 1-ci cəhd qurulur ki, frontend-in
    # AI analiz düyməsi ölü qalmasın və istifadəçi boş paneldə qalmasın.
    try:
        attempts_res = db.table("exam_attempts").select(
            "id, exam_result_id, attempt_no, is_primary, score, incorrect_count, "
            "empty_count, total_questions, weak_topics, ai_analysis, created_at"
        ).eq("exam_id", exam_id).eq("student_id", current_user["id"]) \
            .order("attempt_no", desc=True).limit(MAX_ATTEMPT_HISTORY).execute()
        raw_attempts = attempts_res.data or []
    except Exception:
        raw_attempts = []
        fallback_res = db.table("exam_results").select(
            "id, score, incorrect_count, empty_count, total_questions, weak_topics, created_at"
        ).eq("exam_id", exam_id).eq("student_id", current_user["id"]) \
            .order("created_at", desc=True).limit(1).execute()

        fallback = (fallback_res.data or [None])[0]
        if fallback:
            raw_attempts = [{
                "id": fallback.get("id"),
                "exam_result_id": fallback.get("id"),
                "attempt_no": 1,
                "is_primary": True,
                "score": fallback.get("score") or 0,
                "incorrect_count": fallback.get("incorrect_count") or 0,
                "empty_count": fallback.get("empty_count") or 0,
                "total_questions": fallback.get("total_questions") or 0,
                "weak_topics": fallback.get("weak_topics") or [],
                "ai_analysis": None,
                "ai_model": None,
                "created_at": fallback.get("created_at"),
            }]

    attempts = []
    for a in raw_attempts:
        tq = a.get("total_questions") or 0
        sc = a.get("score") or 0
        attempts.append({
            "attempt_id": a.get("id"),
            "attempt_no": a.get("attempt_no"),
            "is_primary": bool(a.get("is_primary")),
            "score": sc,
            "incorrect_count": a.get("incorrect_count") or 0,
            "empty_count": a.get("empty_count") or 0,
            "total_questions": tq,
            "percentage": round(sc / tq * 100, 1) if tq > 0 else 0,
            "weak_topics": a.get("weak_topics") or [],
            "has_ai_analysis": bool(a.get("ai_analysis")),
            # `ai_model` (provider adı) cavaba daxil EDİLMİR — minimal data
            # prinsipi (.clinerules §1) və brendinq: texniki model adı
            # istifadəçi üçün məlumat deyil.
            "created_at": a.get("created_at"),
        })

    return {
        "exam": {
            "id": exam.get("id"),
            "title": exam.get("title"),
            "subject": exam.get("subject"),
        },
        "attempts": attempts,
        "max_attempts": MAX_ATTEMPTS_PER_EXAM,
        "can_retake": len(attempts) < MAX_ATTEMPTS_PER_EXAM,
    }


@router.get("/attempts/all")
def get_all_attempts(
    current_user: dict = Depends(get_current_user),
    limit: int = 100,
):
    """
    Şagirdin BÜTÜN sınaqları üzrə bütün cəhdləri (tarix sırası ilə).
    Analytics səhifəsində "hər sınağın cəhdləri" siyahısını qurmaq üçün.
    `limit` yoxlanılır (klient nəzarətsiz `?limit=999999` göndərə bilər).
    """
    db = get_db()
    safe_limit = max(1, min(int(limit or 100), MAX_ATTEMPT_HISTORY * 2))

    attempts_res = db.table("exam_attempts").select(
        "id, exam_id, attempt_no, is_primary, score, incorrect_count, empty_count, "
        "total_questions, weak_topics, ai_analysis, ai_generated_at, created_at"
    ).eq("student_id", current_user["id"]) \
        .order("created_at", desc=True).limit(safe_limit).execute()

    exam_ids = list({a.get("exam_id") for a in (attempts_res.data or []) if a.get("exam_id")})
    exams_map: Dict[str, dict] = {}
    if exam_ids:
        ex_res = db.table("exams").select("id, title, subject").in_("id", exam_ids).execute()
        exams_map = {e["id"]: e for e in (ex_res.data or [])}

    out = []
    for a in (attempts_res.data or []):
        ex = exams_map.get(a.get("exam_id"), {})
        tq = a.get("total_questions") or 0
        sc = a.get("score") or 0
        out.append({
            "attempt_id": a.get("id"),
            "exam_id": a.get("exam_id"),
            "title": ex.get("title", "Sınaq"),
            "subject": ex.get("subject", "Digər"),
            "attempt_no": a.get("attempt_no") or 1,
            "is_primary": bool(a.get("is_primary")),
            "score": sc,
            "incorrect_count": a.get("incorrect_count") or 0,
            "empty_count": a.get("empty_count") or 0,
            "total_questions": tq,
            "percentage": round(sc / tq * 100, 1) if tq > 0 else 0,
            "has_ai_analysis": bool(a.get("ai_analysis")),
            "created_at": a.get("created_at"),
        })

    return {"attempts": out, "count": len(out)}
