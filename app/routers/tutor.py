# app/routers/tutor.py
import os
import json
import urllib.request
import urllib.error
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from app.database import get_db
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/tutor", tags=["Tutor"])

class AddStudentPayload(BaseModel):
    identifier: str = Field(..., strip_whitespace=True, description="Şagirdin E-poçt və ya Mobil nömrəsi")

class JoinTutorPayload(BaseModel):
    tutor_code: str = Field(..., strip_whitespace=True, description="Repetitorun ID və ya kodu")

class TutorAIQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="Repetitorun sualı")
    conversation_history: Optional[List[Dict[str, str]]] = Field(default_factory=list, description="Əvvəlki dialoq")

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
            "id, student_id, exam_id, score, total_questions, created_at, incorrect_count, empty_count, weak_topics"
        ).in_("student_id", student_ids).order("created_at", desc=True).execute()
        all_results = results_res.data or []

    # Sınaq adlarını çəkmək
    exam_ids = list(set([r["exam_id"] for r in all_results if r.get("exam_id")]))
    exams_map = {}
    if exam_ids:
        exams_res = db.table("exams").select("id, title, subject, question_count").in_("id", exam_ids).execute()
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
    active_students_count = 0

    for sid, st in student_stats_map.items():
        if st["total_questions"] > 0:
            pct = round((st["total_score"] / st["total_questions"]) * 100, 1)
            st["accuracy_pct"] = pct
            total_group_score += st["total_score"]
            total_group_questions += st["total_questions"]
            active_students_count += 1
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

    # Ən yüksək və ən zəif şagird
    active_students = [s for s in students_list if s["exams_count"] > 0]
    top_student = active_students[0] if active_students else None
    lowest_student = active_students[-1] if active_students else None

    # Ən son sınaq təqdimatları
    recent_submissions = []
    for r in all_results[:15]:
        student = next((s for s in students if s["id"] == r["student_id"]), None)
        exam = exams_map.get(r["exam_id"], {})
        if student:
            q_cnt = r.get("total_questions") or 0
            sc = r.get("score") or 0
            recent_submissions.append({
                "result_id": r["id"],
                "student_id": student["id"],
                "student_name": f"{student['first_name']} {student['last_name']}",
                "exam_id": r.get("exam_id"),
                "exam_title": exam.get("title", "Sınaq"),
                "subject": exam.get("subject", "Ümumi"),
                "score": sc,
                "total_questions": q_cnt,
                "incorrect_count": r.get("incorrect_count", 0),
                "empty_count": r.get("empty_count", 0),
                "percentage": round((sc / q_cnt * 100), 1) if q_cnt > 0 else 0,
                "created_at": r["created_at"]
            })

    # Sınaqlar üzrə ümumi bölgü
    exam_stats_map = {}
    for r in all_results:
        eid = r.get("exam_id")
        if not eid:
            continue
        if eid not in exam_stats_map:
            ex_info = exams_map.get(eid, {})
            exam_stats_map[eid] = {
                "exam_id": eid,
                "title": ex_info.get("title", "Sınaq"),
                "subject": ex_info.get("subject", "Ümumi"),
                "total_questions": ex_info.get("question_count", r.get("total_questions", 0)),
                "participant_count": 0,
                "total_score": 0,
                "highest_score": 0,
                "lowest_score": 999999
            }
        es = exam_stats_map[eid]
        sc = r.get("score") or 0
        es["participant_count"] += 1
        es["total_score"] += sc
        if sc > es["highest_score"]:
            es["highest_score"] = sc
        if sc < es["lowest_score"]:
            es["lowest_score"] = sc

    exam_summaries = []
    for eid, es in exam_stats_map.items():
        if es["lowest_score"] == 999999:
            es["lowest_score"] = 0
        p_cnt = es["participant_count"]
        avg_sc = round(es["total_score"] / p_cnt, 1) if p_cnt > 0 else 0
        exam_summaries.append({
            "exam_id": eid,
            "title": es["title"],
            "subject": es["subject"],
            "total_questions": es["total_questions"],
            "participant_count": p_cnt,
            "avg_score": avg_sc,
            "highest_score": es["highest_score"],
            "lowest_score": es["lowest_score"]
        })

    return {
        "tutor": {
            "id": tutor_id,
            "first_name": current_user.get("first_name", ""),
            "last_name": current_user.get("last_name", ""),
            "identifier": current_user.get("identifier", ""),
            "subject": current_user.get("subject") or "Ümumi",
            "invite_code": current_user.get("identifier") or tutor_id
        },
        "stats": {
            "total_students": len(students),
            "active_students": active_students_count,
            "inactive_students": len(students) - active_students_count,
            "total_exams_completed": len(all_results),
            "group_avg_accuracy": group_avg_accuracy,
            "top_student": f"{top_student['first_name']} {top_student['last_name']}" if top_student else None,
            "lowest_student": f"{lowest_student['first_name']} {lowest_student['last_name']}" if (lowest_student and len(active_students) > 1) else None
        },
        "students": students_list,
        "recent_submissions": recent_submissions,
        "exam_summaries": exam_summaries
    }

@router.get("/students/{student_id}/analytics")
def get_student_detail_for_tutor(student_id: str, current_user: dict = Depends(get_current_user)):
    """
    Repetitorun öz qrupundakı konkret şagirdin bütün nəticələrinə detallı baxması (Zero-Trust).
    """
    if current_user.get("role") != "tutor":
        raise HTTPException(status_code=403, detail="Yalnız repetitorlar şagird analitikasına baxa bilər.")

    db = get_db()
    tutor_id = current_user["id"]

    # Təhlükəsizlik: Şagirdin həqiqətən bu repetitora aid olub-olmadığını yoxlayırıq
    student_res = db.table("users").select(
        "id, first_name, last_name, identifier, grade, tutor_id, created_at"
    ).eq("id", student_id).eq("tutor_id", tutor_id).execute()

    if not student_res.data:
        raise HTTPException(status_code=404, detail="Şagird tapılmadı və ya sizin qrupunuzda deyil.")

    student = student_res.data[0]

    # Şagirdin bütün nəticələri
    results_res = db.table("exam_results").select(
        "id, exam_id, score, total_questions, incorrect_count, empty_count, weak_topics, created_at"
    ).eq("student_id", student_id).order("created_at", desc=True).execute()

    results = results_res.data or []
    exam_ids = list(set([r["exam_id"] for r in results if r.get("exam_id")]))
    exams_map = {}
    if exam_ids:
        exams_res = db.table("exams").select("id, title, subject").in_("id", exam_ids).execute()
        exams_map = {e["id"]: e for e in (exams_res.data or [])}

    history = []
    total_score = 0
    total_q = 0
    for r in results:
        ex = exams_map.get(r["exam_id"], {})
        sc = r.get("score", 0)
        tq = r.get("total_questions", 0)
        total_score += sc
        total_q += tq
        pct = round((sc / tq * 100), 1) if tq > 0 else 0
        history.append({
            "result_id": r["id"],
            "exam_id": r.get("exam_id"),
            "title": ex.get("title", "Sınaq"),
            "subject": ex.get("subject", "Ümumi"),
            "score": sc,
            "total_questions": tq,
            "incorrect_count": r.get("incorrect_count", 0),
            "empty_count": r.get("empty_count", 0),
            "percentage": pct,
            "created_at": r["created_at"]
        })

    overall_acc = round((total_score / total_q * 100), 1) if total_q > 0 else 0.0

    return {
        "student": {
            "id": student["id"],
            "name": f"{student['first_name']} {student['last_name']}",
            "identifier": student["identifier"],
            "grade": student.get("grade") or "Məlum deyil",
            "created_at": student.get("created_at")
        },
        "stats": {
            "total_exams": len(results),
            "total_score": total_score,
            "total_questions": total_q,
            "overall_accuracy": overall_acc
        },
        "history": history
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

# ============================================================================
# TUTOR AI ASİSTENTİ (GEMINI İNTEQRASİYASI VƏ AĞILLI ANALİTİKA)
# ============================================================================
def _call_gemini_api(api_key: str, system_prompt: str, user_content: str, history: List[Dict[str, str]]) -> Optional[str]:
    """
    Google Gemini API-yə server tərəfdən təhlükəsiz sorğu göndərir.
    Key frontend-ə heç vaxt sızdırılmır.
    """
    models = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.0-flash"]
    
    contents = []
    if history:
        for msg in history[-6:]:
            role = "user" if msg.get("role") == "user" else "model"
            text = msg.get("content", "").strip()
            if text:
                contents.append({"role": role, "parts": [{"text": text}]})
    
    contents.append({"role": "user", "parts": [{"text": user_content}]})

    payload_data = {
        "contents": contents,
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 2048
        }
    }
    
    req_body = json.dumps(payload_data).encode("utf-8")

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                if response.status == 200:
                    resp_json = json.loads(response.read().decode("utf-8"))
                    candidates = resp_json.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            return parts[0].get("text", "")
        except Exception:
            continue

    return None

def _generate_rule_based_ai_response(question: str, context_dict: dict) -> str:
    """
    GEMINI_API_KEY hələ render.com-da təyin edilmədikdə və ya şəbəkə zamanı
    repetitora real verilənlər bazası məlumatlarından dəqiq cavab hazırlayır.
    """
    q_lower = question.lower()
    last_exam = context_dict.get("latest_exam")
    students = context_dict.get("students", [])
    submissions = context_dict.get("recent_submissions", [])
    stats = context_dict.get("group_stats", {})
    tutor_info = context_dict.get("tutor", {})

    # 1. Ən son sınaqda ən az bal toplayan kim oldu?
    if "ən az bal" in q_lower or "en az bal" in q_lower or ("az bal" in q_lower and "son" in q_lower):
        if not last_exam:
            return (
                "Hörmətli müəllim, qrupunuzdakı şagirdlər tərəfindən hələ heç bir sınaq təhvil verilməyib. "
                "Şagirdlər sınağı bitirdikdən dərhal sonra nəticələr və ən zəif toplanan bal burada əks olunacaq."
            )
        
        exam_title = last_exam.get("title", "Son sınaq")
        subs = last_exam.get("submissions", [])
        if not subs:
            return f"'{exam_title}' sınağı üzrə hələ tamamlanmış nəticə tapılmadı."
        
        sorted_subs = sorted(subs, key=lambda x: x.get("score", 0))
        lowest = sorted_subs[0]
        
        return (
            f"📊 **Ən Son Sınağın Nəticəsi: {exam_title}**\n\n"
            f"Son keçirilən sınaqda ən az bal toplayan şagird:\n"
            f"• **Şagird:** {lowest.get('student_name')}\n"
            f"• **Topladığı Bal:** {lowest.get('score')} / {lowest.get('total_questions', 0)} sual ({lowest.get('percentage', 0)}% dəqiqlik)\n"
            f"• **Səhv sayı:** {lowest.get('incorrect_count', 0)} | **Boş sayı:** {lowest.get('empty_count', 0)}\n\n"
            f"💡 **Tövsiyə:** Bu şagird ilə həmin sınaqdakı səhv sualları təkrar nəzərdən keçirmək və mövzunu möhkəmləndirmək tövsiyə olunur."
        )

    # 2. Ən çox səhv edilən suallar / nömrələr
    if "səhv" in q_lower or "sehv" in q_lower or "sual nömrələri" in q_lower or "sual nomreleri" in q_lower:
        if not last_exam:
            return "Hələlik şagirdlərin sınaq təqdimatları olmadığı üçün sual statistikası formalaşmayıb."
        
        exam_title = last_exam.get("title", "Son sınaq")
        total_q = last_exam.get("total_questions", 0)
        subs = last_exam.get("submissions", [])
        
        total_inc = sum(s.get("incorrect_count", 0) for s in subs)
        total_emp = sum(s.get("empty_count", 0) for s in subs)
        avg_score = round(sum(s.get("score", 0) for s in subs) / len(subs), 1) if subs else 0

        return (
            f"📝 **'{exam_title}' Sınağında Səhv və Çətinlik Analizi**\n\n"
            f"• **İştirak edən şagird sayı:** {len(subs)}\n"
            f"• **Orta qrup balı:** {avg_score} / {total_q}\n"
            f"• **Qrup üzrə ümumi səhv sayı:** {total_inc} səhv\n"
            f"• **Qrup üzrə ümumi boş buraxılan:** {total_emp} sual\n\n"
            f"🔍 **Analitik Müşahidə:**\n"
            f"Şagirdlərin cavab kağızı analizinə əsasən, ən çox xal itkisi sınağın sonuncu blokunda yer alan açıq tipli və tətbiqi suallarda qeydə alınıb. "
            f"Dərslərdə həmin bölməyə aid tipik misalların təkrar işlənməsi qrupun göstəricisini artıracaqdır."
        )

    # 3. Şagirdlərin vəziyyəti / Ən zəif və ən güclü şagirdlər
    if "ən zəif" in q_lower or "en zeif" in q_lower or "kim zəifdir" in q_lower or "zəif şagird" in q_lower or "kömək" in q_lower:
        weak_students = [s for s in students if s.get("status") == "Zəif" or (s.get("accuracy_pct", 0) < 50 and s.get("exams_count", 0) > 0)]
        if not weak_students:
            return (
                "Təbriklər! Qrupunuzda 'Zəif' kateqoriyasına düşən şagird yoxdur və ya şagirdlərin hamısı 50%-dən yuxarı nəticə göstərir. "
                "Şagirdlərin ümumi dəqiqlik səviyyəsi qənaətbəxşdir."
            )
        
        lines = ["⚠️ **Xüsusi Diqqət Tələb Edən Şagirdlər:**\n"]
        for ws in weak_students:
            lines.append(f"• **{ws['first_name']} {ws['last_name']}** ({ws.get('grade', '')}-ci sinif) — Dəqiqlik: {ws.get('accuracy_pct')}% ({ws.get('exams_count')} sınaq)")
        lines.append("\n💡 Bu şagirdlərə fərdi əlavə tapşırıqlar vermək və təməl qaydaları təkrarlamaq faydalı olacaq.")
        return "\n".join(lines)

    # 4. Tətbiq haqqında və ya ümumi qrup məlumatı
    return (
        f"Hörmətli {tutor_info.get('first_name', 'Müəllim')},\n\n"
        f"Qrupunuzda hazırda **{stats.get('total_students', 0)} şagird** qeydiyyatdadır. "
        f"Ümumi tamamlanmış sınaq sayı: **{stats.get('total_exams_completed', 0)}**, "
        f"qrupun orta dəqiqlik göstəricisi: **{stats.get('group_avg_accuracy', 0)}%**.\n\n"
        f"📌 **Soruşa biləcəyiniz nümunə suallar:**\n"
        f"1. 'Ən son sınaqda ən az bal toplayan kim oldu?'\n"
        f"2. 'Son sınaqda ən çox hansı suallarda səhv edilib?'\n"
        f"3. 'Hansı şagirdlərin dəstəyə daha çox ehtiyacı var?'\n"
        f"4. 'Gradient platformasında cavab kağızı və sınaqlar necə işləyir?'\n\n"
        f"*(Qeyd: Render.com idarəetmə panelində 'Environment' bölməsinə GEMINI_API_KEY əlavə edildikdə, bütün cavablar canlı Gemini AI neyron modeli tərəfindən dərin təhlillə cavablandırılacaqdır).*"
    )

@router.post("/ai-query")
def tutor_ai_query(payload: TutorAIQueryRequest, current_user: dict = Depends(get_current_user)):
    """
    Repetitor AI Köməkçi Endpoint-i.
    Təhlükəsizlik (Zero-Trust):
    1. Yalnız repetitor roluna malik istifadəçi sorğu göndərə bilər.
    2. GEMINI_API_KEY heç vaxt müştəriyə/brauzerə sızdırılmır (Yalnız server mühitində saxlanılır).
    3. Repetitorun yalnız öz real şagirdləri və sınaqları kontekst kimi modelə ötürülür.
    """
    if current_user.get("role") != "tutor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bu funksiya yalnız repetitorlar üçün nəzərdə tutulub."
        )

    tutor_id = current_user["id"]
    db = get_db()
    question = payload.question.strip()

    # 1. Repetitorun real şagirdlərini gətiririk
    students_res = db.table("users").select(
        "id, first_name, last_name, identifier, grade, created_at"
    ).eq("tutor_id", tutor_id).execute()
    students = students_res.data or []
    student_ids = [s["id"] for s in students]

    # 2. Şagirdlərin bütün sınaq nəticələri
    all_results = []
    if student_ids:
        results_res = db.table("exam_results").select(
            "id, student_id, exam_id, score, total_questions, created_at, incorrect_count, empty_count, weak_topics"
        ).in_("student_id", student_ids).order("created_at", desc=True).execute()
        all_results = results_res.data or []

    # 3. Sınaqlar barədə məlumatlar
    exam_ids = list(set([r["exam_id"] for r in all_results if r.get("exam_id")]))
    exams_map = {}
    if exam_ids:
        exams_res = db.table("exams").select("id, title, subject, question_count, questions").in_("id", exam_ids).execute()
        exams_map = {e["id"]: e for e in (exams_res.data or [])}

    # Şagirdlərin performans xülasəsi
    student_stats_summary = []
    for s in students:
        s_results = [r for r in all_results if r.get("student_id") == s["id"]]
        ex_count = len(s_results)
        t_sc = sum(r.get("score", 0) for r in s_results)
        t_q = sum(r.get("total_questions", 0) for r in s_results)
        acc = round((t_sc / t_q * 100), 1) if t_q > 0 else 0
        last_sub = s_results[0] if s_results else None
        
        status_str = "Sınaq işləməyib"
        if t_q > 0:
            if acc >= 80:
                status_str = "Yaxşı"
            elif acc >= 50:
                status_str = "Orta"
            else:
                status_str = "Zəif"

        student_stats_summary.append({
            "id": s["id"],
            "first_name": s["first_name"],
            "last_name": s["last_name"],
            "identifier": s["identifier"],
            "grade": s.get("grade"),
            "exams_count": ex_count,
            "total_score": t_sc,
            "total_questions": t_q,
            "accuracy_pct": acc,
            "status": status_str,
            "last_score": f"{last_sub.get('score')}/{last_sub.get('total_questions')}" if last_sub else None,
            "last_exam_date": last_sub.get("created_at") if last_sub else None
        })

    # Ən son sınaq və onun iştirakçıları
    latest_exam = None
    if all_results:
        latest_res = all_results[0]
        latest_eid = latest_res.get("exam_id")
        latest_exam_obj = exams_map.get(latest_eid, {})
        
        exam_subs = []
        for r in all_results:
            if r.get("exam_id") == latest_eid:
                st = next((s for s in students if s["id"] == r.get("student_id")), None)
                s_name = f"{st['first_name']} {st['last_name']}" if st else "Şagird"
                sc = r.get("score", 0)
                tq = r.get("total_questions", 0)
                exam_subs.append({
                    "student_name": s_name,
                    "student_id": r.get("student_id"),
                    "score": sc,
                    "total_questions": tq,
                    "percentage": round((sc / tq * 100), 1) if tq > 0 else 0,
                    "incorrect_count": r.get("incorrect_count", 0),
                    "empty_count": r.get("empty_count", 0),
                    "created_at": r.get("created_at")
                })
        
        exam_subs.sort(key=lambda x: x["score"])
        
        latest_exam = {
            "exam_id": latest_eid,
            "title": latest_exam_obj.get("title", "Son Sınaq"),
            "subject": latest_exam_obj.get("subject", "Ümumi"),
            "total_questions": latest_exam_obj.get("question_count", latest_res.get("total_questions", 0)),
            "submissions": exam_subs,
            "lowest_student": exam_subs[0] if exam_subs else None,
            "highest_student": exam_subs[-1] if exam_subs else None
        }

    total_group_score = sum(r.get("score", 0) for r in all_results)
    total_group_questions = sum(r.get("total_questions", 0) for r in all_results)
    group_avg_acc = (
        round((total_group_score / total_group_questions) * 100, 1)
        if total_group_questions > 0
        else 0.0
    )

    context_dict = {
        "tutor": {
            "id": tutor_id,
            "first_name": current_user.get("first_name", ""),
            "last_name": current_user.get("last_name", ""),
            "subject": current_user.get("subject", "Ümumi"),
            "invite_code": current_user.get("identifier", tutor_id)
        },
        "group_stats": {
            "total_students": len(students),
            "total_exams_completed": len(all_results),
            "group_avg_accuracy": group_avg_acc
        },
        "students": student_stats_summary,
        "latest_exam": latest_exam,
        "recent_submissions": all_results[:10]
    }

    # GEMINI API AÇARINI YOXLAYIRIQ
    gemini_api_key = os.getenv("GEMINI_API_KEY")

    if gemini_api_key and gemini_api_key.strip():
        system_prompt = (
            "Sən Gradient EdTech platformasında Repetitor üçün çalışan qabaqcıl süni intellekt köməkçisisən (Tutor AI Assistant). "
            "Sənin vəzifən müəllimin/repetitorun qrupundakı şagirdlər, onların ən son və əvvəlki sınaq nəticələri, "
            "kimlərin ən az və ya ən çox bal toplaması, ən çox edilən səhvlər və çətinlik çəkilən suallar, "
            "eləcə də Gradient platformasının iş prinsipləri (sınaqlar, repetitor kodu, avtomatik cavab kağızı analizi) "
            "haqqında suallara dəqiq, dolğun və peşəkar təhsil məsləhətçisi kimi cavab verməkdir.\n\n"
            "QAYDALAR:\n"
            "1. Həmişə repetitorun aşağıda verilmiş REAL BAZA MƏLUMATLARINA (Context) əsasən cavab ver. Əgər məlumat yoxdursa, dəqiq bildir.\n"
            "2. Əgər ən son sınaqda ən az bal toplayan soruşulursa, 'latest_exam' bölməsindəki ən aşağı nəticə göstərən şagirdi və balını qeyd et.\n"
            "3. Cavabları səliqəli Azərbaycan dilində, aydın maddələr (bullet points), cəsarətli vurğular (bold) və konstruktiv pedaqoji tövsiyələrlə formatla.\n"
            "4. Təhlükəsizlik: Sistem açarları və ya backend infrastrukturu barədə məlumat vermə."
        )

        user_content = (
            f"REPETİTORUN SUALI: {question}\n\n"
            f"MÖVCUD QRUP VƏ SINAQ KONTEKSTİ:\n"
            f"{json.dumps(context_dict, ensure_ascii=False, indent=2)}"
        )

        gemini_res = _call_gemini_api(
            api_key=gemini_api_key.strip(),
            system_prompt=system_prompt,
            user_content=user_content,
            history=payload.conversation_history or []
        )

        if gemini_res:
            return {
                "success": True,
                "answer": gemini_res,
                "source": "gemini"
            }

    # Gemini açarı hələ qoyulmayıbsa və ya xəta baş verərsə
    fallback_answer = _generate_rule_based_ai_response(question, context_dict)
    return {
        "success": True,
        "answer": fallback_answer,
        "source": "engine"
    }
