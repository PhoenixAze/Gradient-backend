# app/routers/tutor.py
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from app.database import get_db
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/tutor", tags=["Tutor"])

class AddStudentPayload(BaseModel):
    identifier: str = Field(..., strip_whitespace=True, description="Şagirdin E-poçt və ya Mobil nömrəsi")

class JoinTutorPayload(BaseModel):
    tutor_code: str = Field(..., strip_whitespace=True, description="Repetitorun ID və ya kodu")

@router.get("/dashboard")
def get_tutor_dashboard(current_user: dict = Depends(get_current_user)):
    """
    Repetitorun real idarəetmə paneli məlumatları.
    Zero-Trust: Yalnız repetitor rolunda olan istifadəçilər icazə alır.
    Bütün şagirdlər və nəticələr bazadan real vaxt rejimində hesablanır.
    """
    if current_user.get("role") != "tutor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bu panelə yalnız repetitorlar daxil ola bilər."
        )

    db = get_db()
    tutor_id = current_user["id"]

    # 1. Repetitora bağlı olan real şagirdləri gətiririk
    students_res = db.table("users").select(
        "id, first_name, last_name, identifier, grade, created_at"
    ).eq("tutor_id", tutor_id).execute()

    students = students_res.data or []

    # 2. Şagirdlərin sınaq nəticələrini toplayırıq
    student_ids = [s["id"] for s in students]
    all_results = []
    if student_ids:
        results_res = db.table("exam_results").select(
            "id, student_id, exam_id, score, total_questions, created_at, incorrect_count, empty_count"
        ).in_("student_id", student_ids).order("created_at", desc=True).execute()
        all_results = results_res.data or []

    # Sınaq adlarını çəkmək
    exam_ids = list(set([r["exam_id"] for r in all_results if r.get("exam_id")]))
    exams_map = {}
    if exam_ids:
        exams_res = db.table("exams").select("id, title, subject").in_("id", exam_ids).execute()
        exams_map = {e["id"]: e for e in (exams_res.data or [])}

    # Hər şagird üzrə nəticələri qruplaşdırmaq
    student_stats_map = {}
    for s in students:
        student_stats_map[s["id"]] = {
            "id": s["id"],
            "first_name": s["first_name"],
            "last_name": s["last_name"],
            "identifier": s["identifier"],
            "grade": s.get("grade") or "Məlum deyil",
            "exams_count": 0,
            "total_score": 0,
            "total_questions": 0,
            "last_exam_date": None,
            "last_score": None,
            "accuracy_pct": 0.0,
            "status": "Aktiv deyil"
        }

    for r in all_results:
        sid = r["student_id"]
        if sid in student_stats_map:
            st = student_stats_map[sid]
            st["exams_count"] += 1
            st["total_score"] += (r.get("score") or 0)
            st["total_questions"] += (r.get("total_questions") or 0)
            if not st["last_exam_date"]:
                st["last_exam_date"] = r.get("created_at")
                st["last_score"] = f"{r.get('score', 0)}/{r.get('total_questions', 0)}"

    # Faizləri və statusları hesablamaq
    students_list = []
    total_group_score = 0
    total_group_questions = 0

    for sid, st in student_stats_map.items():
        if st["total_questions"] > 0:
            pct = round((st["total_score"] / st["total_questions"]) * 100, 1)
            st["accuracy_pct"] = pct
            total_group_score += st["total_score"]
            total_group_questions += st["total_questions"]
            if pct >= 80:
                st["status"] = "Yaxşı"
            elif pct >= 50:
                st["status"] = "Orta"
            else:
                st["status"] = "Zəif"
        else:
            st["status"] = "Sınaq işləməyib"

        students_list.append(st)

    # Nəticələrə görə çeşidləmək
    students_list.sort(key=lambda x: x["accuracy_pct"], reverse=True)

    group_avg_accuracy = (
        round((total_group_score / total_group_questions) * 100, 1)
        if total_group_questions > 0
        else 0.0
    )

    # Ən son sınaq təqdimatları
    recent_submissions = []
    for r in all_results[:10]:
        student = next((s for s in students if s["id"] == r["student_id"]), None)
        exam = exams_map.get(r["exam_id"], {})
        if student:
            q_cnt = r.get("total_questions") or 0
            sc = r.get("score") or 0
            recent_submissions.append({
                "result_id": r["id"],
                "student_name": f"{student['first_name']} {student['last_name']}",
                "exam_title": exam.get("title", "Sınaq"),
                "subject": exam.get("subject", "Ümumi"),
                "score": sc,
                "total_questions": q_cnt,
                "percentage": round((sc / q_cnt * 100), 1) if q_cnt > 0 else 0,
                "created_at": r["created_at"]
            })

    return {
        "tutor": {
            "id": tutor_id,
            "first_name": current_user.get("first_name", ""),
            "last_name": current_user.get("last_name", ""),
            "identifier": current_user.get("identifier", ""),
            "subject": current_user.get("subject", "Ümumi"),
            "invite_code": current_user.get("identifier", "") # Şagirdlər e-poçt və ya nömrə ilə qoşulur
        },
        "stats": {
            "total_students": len(students),
            "total_exams_completed": len(all_results),
            "group_avg_accuracy": group_avg_accuracy
        },
        "students": students_list,
        "recent_submissions": recent_submissions
    }

@router.post("/students/add")
def add_student_to_group(payload: AddStudentPayload, current_user: dict = Depends(get_current_user)):
    """Repetitor şagirdi E-poçt və ya nömrəsinə əsasən öz qrupuna əlavə edir."""
    if current_user.get("role") != "tutor":
        raise HTTPException(status_code=403, detail="Yalnız repetitorlar şagird əlavə edə bilər.")

    db = get_db()
    identifier = payload.identifier.strip()

    # Şagirdi bazada axtarırıq
    user_res = db.table("users").select("id, role, first_name, last_name, identifier, grade, tutor_id").eq("identifier", identifier).execute()
    if not user_res.data:
        raise HTTPException(status_code=404, detail="Bu E-poçt və ya Mobil nömrəyə uyğun istifadəçi tapılmadı.")

    student = user_res.data[0]
    if student["role"] != "student":
        raise HTTPException(status_code=400, detail="Qeyd olunan istifadəçi şagird deyil.")

    if student.get("tutor_id") == current_user["id"]:
        raise HTTPException(status_code=400, detail="Bu şagird artıq sizin qrupunuzdadır.")

    # Şagirdi repetitora bağlayırıq
    db.table("users").update({"tutor_id": current_user["id"]}).eq("id", student["id"]).execute()

    return {
        "success": True,
        "message": f"Şagird {student['first_name']} {student['last_name']} uğurla qrupa əlavə edildi.",
        "student": {
            "id": student["id"],
            "first_name": student["first_name"],
            "last_name": student["last_name"],
            "identifier": student["identifier"],
            "grade": student.get("grade")
        }
    }

@router.delete("/students/{student_id}")
def remove_student_from_group(student_id: str, current_user: dict = Depends(get_current_user)):
    """Şagirdi repetitorun qrupundan çıxarır."""
    if current_user.get("role") != "tutor":
        raise HTTPException(status_code=403, detail="Yalnız repetitorlar şagird çıxara bilər.")

    db = get_db()
    db.table("users").update({"tutor_id": None}).eq("id", student_id).eq("tutor_id", current_user["id"]).execute()

    return {"success": True, "message": "Şagird qrupdan çıxarıldı."}

@router.post("/join")
def student_join_tutor(payload: JoinTutorPayload, current_user: dict = Depends(get_current_user)):
    """Şagird repetitor kodu (e-poçt və ya id) ilə repetitora qoşulur."""
    if current_user.get("role") != "student":
        raise HTTPException(status_code=400, detail="Yalnız şagirdlər repetitor qrupuna qoşula bilər.")

    db = get_db()
    code = payload.tutor_code.strip()

    # Repetitoru axtarırıq (identifier və ya id üzrə)
    tutor_res = db.table("users").select("id, first_name, last_name, subject, role").eq("identifier", code).execute()
    if not tutor_res.data:
        tutor_res = db.table("users").select("id, first_name, last_name, subject, role").eq("id", code).execute()

    if not tutor_res.data:
        raise HTTPException(status_code=404, detail="Qeyd olunan kod və ya e-poçta uyğun repetitor tapılmadı.")

    tutor = tutor_res.data[0]
    if tutor["role"] != "tutor":
        raise HTTPException(status_code=400, detail="Qeyd olunan istifadəçi repetitor deyil.")

    # Şagirdin profilinə tutor_id yazırıq
    db.table("users").update({"tutor_id": tutor["id"]}).eq("id", current_user["id"]).execute()

    return {
        "success": True,
        "message": f"Siz uğurla {tutor['first_name']} {tutor['last_name']} müəllimin qrupuna qoşuldunuz.",
        "tutor_name": f"{tutor['first_name']} {tutor['last_name']}",
        "subject": tutor.get("subject")
    }
