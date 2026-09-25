# app/routers/analytics.py
from fastapi import APIRouter, Depends, HTTPException, status
from app.database import get_db
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics"])

@router.get("/me")
def get_my_analytics(current_user: dict = Depends(get_current_user)):
    """
    Şagirdin real sınaq nəticələrinə əsaslanan tam mövzu və fənn analitikası.
    Zero-Trust: Yalnız cari sessiyaya aid nəticələr bazadan çəkilir və hesablanır.
    """
    db = get_db()
    student_id = current_user["id"]

    # 1. Bütün bitmiş sınaq nəticələrini gətiririk
    results_res = db.table("exam_results").select(
        "id, exam_id, score, incorrect_count, empty_count, total_questions, weak_topics, created_at"
    ).eq("student_id", student_id).order("created_at", desc=True).execute()

    results = results_res.data or []

    if not results:
        return {
            "has_data": False,
            "total_exams": 0,
            "total_questions": 0,
            "correct_count": 0,
            "incorrect_count": 0,
            "empty_count": 0,
            "accuracy_pct": 0.0,
            "subject_stats": [],
            "history": [],
            "ai_diagnosis": None
        }

    # 2. Əlaqəli sınaqların məlumatlarını (fənn, başlıq) çəkirik
    exam_ids = list(set([r["exam_id"] for r in results if r.get("exam_id")]))
    exams_res = db.table("exams").select("id, title, subject, question_count").in_("id", exam_ids).execute()
    exams_map = {e["id"]: e for e in (exams_res.data or [])}

    total_exams = len(results)
    total_questions = 0
    total_correct = 0
    total_incorrect = 0
    total_empty = 0

    subjects_agg = {}
    history = []

    for r in results:
        exam = exams_map.get(r["exam_id"], {})
        exam_title = exam.get("title", "Sınaq")
        subject = exam.get("subject", "Digər")

        q_count = r.get("total_questions") or exam.get("question_count") or 0
        score = r.get("score") or 0
        inc = r.get("incorrect_count")
        emp = r.get("empty_count")

        # Null fallback
        if inc is None:
            inc = max(0, q_count - score)
        if emp is None:
            emp = max(0, q_count - (score + inc))

        total_questions += q_count
        total_correct += score
        total_incorrect += inc
        total_empty += emp

        # Fənn üzrə aqreqasiya
        if subject not in subjects_agg:
            subjects_agg[subject] = {
                "subject": subject,
                "total_questions": 0,
                "correct_count": 0,
                "incorrect_count": 0,
                "empty_count": 0,
                "exams_count": 0
            }
        subjects_agg[subject]["total_questions"] += q_count
        subjects_agg[subject]["correct_count"] += score
        subjects_agg[subject]["incorrect_count"] += inc
        subjects_agg[subject]["empty_count"] += emp
        subjects_agg[subject]["exams_count"] += 1

        history.append({
            "id": r["id"],
            "exam_id": r["exam_id"],
            "title": exam_title,
            "subject": subject,
            "score": score,
            "incorrect_count": inc,
            "empty_count": emp,
            "total_questions": q_count,
            "created_at": r["created_at"],
            "percentage": round((score / q_count * 100), 1) if q_count > 0 else 0
        })

    # Faizləri hesablamaq
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
            "exams_count": data["exams_count"],
            "accuracy_pct": acc
        })

    # Dəqiqlik faizinə görə sıralayırıq
    subject_stats.sort(key=lambda x: x["accuracy_pct"], reverse=True)

    overall_accuracy = round((total_correct / total_questions * 100), 1) if total_questions > 0 else 0.0

    # Real nəticələrə əsaslanan AI/Alqoritmik Diaqnoz
    ai_diagnosis = None
    if subject_stats:
        weakest = subject_stats[-1]
        strongest = subject_stats[0]

        if weakest["accuracy_pct"] < 60:
            ai_diagnosis = (
                f"Son sınaqların təhlili göstərir ki, ən çox xal itkisi '{weakest['subject']}' "
                f"fənnində qeydə alınıb ({weakest['accuracy_pct']}% dəqiqlik). "
                f"Bu fənn üzrə mövzuları təkrar etmək növbəti sınaqda balınızı əhəmiyyətli dərəcədə yüksəldəcək."
            )
        else:
            ai_diagnosis = (
                f"Ümumi nəticəniz sabitdir ({overall_accuracy}% dəqiqlik). "
                f"Ən yüksək göstəriciniz '{strongest['subject']}' ({strongest['accuracy_pct']}%) fənni üzrədir. "
                f"Nəticəni daha da yaxşılaşdırmaq üçün vaxt idarəetməsinə diqqət yetirin."
            )

    return {
        "has_data": True,
        "total_exams": total_exams,
        "total_questions": total_questions,
        "correct_count": total_correct,
        "incorrect_count": total_incorrect,
        "empty_count": total_empty,
        "accuracy_pct": overall_accuracy,
        "subject_stats": subject_stats,
        "history": history,
        "ai_diagnosis": ai_diagnosis
    }
