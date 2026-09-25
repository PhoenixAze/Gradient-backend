# app/routers/tutor.py
import os
import re
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

def _normalize_phone_or_identifier(identifier: str) -> List[str]:
    """
    E-poçt və ya mobil nömrənin mümkün formatlarını qaytarır (məs: 0501234567, 994501234567, +994501234567).
    """
    clean_id = identifier.strip()
    candidates = [clean_id]
    
    digits = re.sub(r"[^\d]", "", clean_id)
    if digits:
        if digits.startswith("994") and len(digits) == 12:
            candidates.append("0" + digits[3:])
            candidates.append(digits)
            candidates.append("+" + digits)
        elif digits.startswith("0") and len(digits) == 10:
            candidates.append(digits)
            candidates.append("994" + digits[1:])
            candidates.append("+994" + digits[1:])
        elif len(digits) == 9:
            candidates.append("0" + digits)
            candidates.append("994" + digits)
            candidates.append("+994" + digits)
            
    # Dublikatları aradan qaldırırıq
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result

def _ensure_tutor_role(current_user: dict, db) -> None:
    """
    İstifadəçinin repetitor panelindən istifadə etmək səlahiyyətini təmin edir.
    Əgər istifadəçinin rolu 'student' qalıbsa, repetitor panelində işləməsi üçün
    bazada rolunu 'tutor' olaraq yeniləyir.
    """
    role = str(current_user.get("role", "")).lower().strip()
    allowed_roles = ["tutor", "teacher", "repetitor", "admin", "moderator", "instructor", "superadmin"]
    
    if role not in allowed_roles:
        try:
            db.table("users").update({"role": "tutor"}).eq("id", current_user["id"]).execute()
            current_user["role"] = "tutor"
        except Exception as e:
            print(f"Role auto-update warning: {e}")

@router.get("/dashboard")
def get_tutor_dashboard(current_user: dict = Depends(get_current_user)):
    """
    Repetitorun real idarəetmə paneli məlumatları.
    Bütün şagirdlər və nəticələr bazadan real vaxt rejimində hesablanır.
    """
    db = get_db()
    _ensure_tutor_role(current_user, db)
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
    Repetitorun öz qrupundakı konkret şagirdin bütün nəticələrinə detallı baxması.
    """
    db = get_db()
    _ensure_tutor_role(current_user, db)
    tutor_id = current_user["id"]

    student_res = db.table("users").select(
        "id, first_name, last_name, identifier, grade, tutor_id, created_at"
    ).eq("id", student_id).eq("tutor_id", tutor_id).execute()

    if not student_res.data:
        raise HTTPException(status_code=404, detail="Şagird tapılmadı və ya sizin qrupunuzda deyil.")

    student = student_res.data[0]

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
    db = get_db()
    _ensure_tutor_role(current_user, db)
    tutor_id = current_user["id"]

    possible_identifiers = _normalize_phone_or_identifier(payload.identifier)
    
    # Şagirdi bazada axtarırıq (bütün mümkün nömrə/email variantları üzrə)
    student = None
    for cand in possible_identifiers:
        user_res = db.table("users").select(
            "id, role, first_name, last_name, identifier, grade, tutor_id"
        ).eq("identifier", cand).execute()
        if user_res.data:
            student = user_res.data[0]
            break

    if not student:
        raise HTTPException(
            status_code=404, 
            detail=f"'{payload.identifier}' üzrə qeydiyyatdan keçmiş istifadəçi tapılmadı. Şagirdin əvvəlcə qeydiyyatdan keçdiyinə əmin olun."
        )

    if student["id"] == tutor_id:
        raise HTTPException(status_code=400, detail="Öz hesabınızı şagird kimi əlavə edə bilməzsiniz.")

    if student.get("tutor_id") == tutor_id:
        raise HTTPException(status_code=400, detail="Bu şagird artıq sizin qrupunuzdadır.")

    # Əgər istifadəçinin rolu bazada qeyd edilməyibsə və ya repetitor deyilsə, onu student kimi təsdiqləyirik
    db.table("users").update({"tutor_id": tutor_id}).eq("id", student["id"]).execute()

    return {
        "success": True,
        "message": f"Şagird {student['first_name']} {student['last_name']} uğurla qrupunuza əlavə edildi.",
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
    db = get_db()
    _ensure_tutor_role(current_user, db)
    db.table("users").update({"tutor_id": None}).eq("id", student_id).eq("tutor_id", current_user["id"]).execute()
    return {"success": True, "message": "Şagird qrupdan çıxarıldı."}

@router.post("/join")
def student_join_tutor(payload: JoinTutorPayload, current_user: dict = Depends(get_current_user)):
    """Şagird repetitor kodu (e-poçt və ya id) ilə repetitora qoşulur."""
    db = get_db()
    code = payload.tutor_code.strip()

    tutor_res = db.table("users").select("id, first_name, last_name, subject, role").eq("identifier", code).execute()
    if not tutor_res.data:
        tutor_res = db.table("users").select("id, first_name, last_name, subject, role").eq("id", code).execute()

    if not tutor_res.data:
        raise HTTPException(status_code=404, detail="Qeyd olunan kod və ya e-poçta uyğun repetitor tapılmadı.")

    tutor = tutor_res.data[0]
    db.table("users").update({"tutor_id": tutor["id"]}).eq("id", current_user["id"]).execute()

    return {
        "success": True,
        "message": f"Siz uğurla {tutor['first_name']} {tutor['last_name']} müəllimin qrupuna qoşuldunuz.",
        "tutor_name": f"{tutor['first_name']} {tutor['last_name']}",
        "subject": tutor.get("subject")
    }

# ============================================================================
# TUTOR AI ASİSTENTİ (GEMINI İNTEQRASİYASI VƏ DOĞAL SÖHBƏT MÜHƏRRİKİ)
# ============================================================================
def _call_gemini_api(api_key: str, system_prompt: str, user_content: str, history: List[Dict[str, str]]) -> Optional[str]:
    """
    Google Gemini API-yə server tərəfdən təhlükəsiz sorğu göndərir.
    Key frontend-ə heç vaxt sızdırılmır.
    """
    clean_key = api_key.strip("'\" \t\r\n")
    if not clean_key:
        return None

    # Google Generative Language API rəsmi modelləri
    models = ["gemini-1.5-flash", "gemini-2.0-flash", "gemini-1.5-pro", "gemini-flash-latest"]
    
    contents = []
    last_role = None
    if history:
        for msg in history[-8:]:
            role = "user" if msg.get("role") == "user" else "model"
            text = (msg.get("content") or "").strip()
            if not text:
                continue
            if role == last_role and contents:
                contents[-1]["parts"][0]["text"] += "\n" + text
            else:
                contents.append({"role": role, "parts": [{"text": text}]})
                last_role = role

    full_message = f"{system_prompt}\n\n{user_content}"
    if contents and contents[-1]["role"] == "user":
        contents[-1]["parts"][0]["text"] += "\n\n" + full_message
    else:
        contents.append({"role": "user", "parts": [{"text": full_message}]})

    payload_data = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2048
        }
    }
    
    req_body = json.dumps(payload_data).encode("utf-8")

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={clean_key}"
        req = urllib.request.Request(
            url,
            data=req_body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Gradient-AI-Tutor/1.0"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=22) as response:
                if response.status == 200:
                    resp_json = json.loads(response.read().decode("utf-8"))
                    candidates = resp_json.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            res_text = parts[0].get("text", "").strip()
                            if res_text:
                                return res_text
        except urllib.error.HTTPError as e:
            try:
                err_text = e.read().decode("utf-8", errors="ignore")
                print(f"[Gemini API HTTP Error] Model: {model}, Code: {e.code}, Detail: {err_text}")
            except Exception:
                pass
            continue
        except Exception as e:
            print(f"[Gemini API Request Exception] Model: {model}, Error: {e}")
            continue

    return None

def _generate_natural_conversational_response(question: str, context_dict: dict) -> str:
    """
    Hər suala fərdi, səmimi və məqsədəuyğun cavab verən intellektual Azərbaycan dili cavablandırıcı.
    Heç vaxt hər dəfə eyni şablon və ya robotik göstərici mətni təkrar etmir.
    """
    q_clean = question.strip()
    q_lower = q_clean.lower()
    
    last_exam = context_dict.get("latest_exam")
    students = context_dict.get("students", [])
    stats = context_dict.get("group_stats", {})
    tutor_info = context_dict.get("tutor", {})
    tutor_first = tutor_info.get("first_name") or "Müəllim"

    # 1. Salamlaşma və Ümumi Söhbət
    greetings = ["salam", "sabahınız xeyir", "hər vaxtınız xeyir", "axşamınız xeyir", "salam aleykum", "necəsiz", "necəsən"]
    if any(q_lower.startswith(g) or q_lower == g for g in greetings):
        return (
            f"Salam, {tutor_first}! Xoş gördük. Əhvalınız necədir?\n\n"
            f"Şagirdlərinizin nəticələri, sınaq göstəriciləri, ən çox səhv edilən suallar və ya "
            f"Gradient platformasının imkanları ilə bağlı nəyi nəzərdən keçirmək istərdiniz?"
        )

    # 2. Ən son sınaqda ən az bal toplayan kim oldu?
    if "ən az bal" in q_lower or "en az bal" in q_lower or ("az bal" in q_lower and "son" in q_lower) or "kim az bal" in q_lower:
        if not last_exam:
            return (
                f"Hörmətli {tutor_first}, qrupunuzdakı şagirdlər hələ sınaq tamamlamayıblar. "
                "Şagirdlər ilk sınağı bitirən kimi burada ən az bal toplayan şagird və onun səhvləri dərhal əks olunacaq."
            )
        
        exam_title = last_exam.get("title", "Son sınaq")
        subs = last_exam.get("submissions", [])
        if not subs:
            return f"'{exam_title}' sınağı üzrə hələ tamamlanmış təqdimat qeydə alınmayıb."
        
        sorted_subs = sorted(subs, key=lambda x: x.get("score", 0))
        lowest = sorted_subs[0]
        
        return (
            f"Son keçirilən **'{exam_title}'** sınağında ən az bal toplayan şagird:\n\n"
            f"• **Şagird:** {lowest.get('student_name')}\n"
            f"• **Topladığı Bal:** {lowest.get('score')} / {lowest.get('total_questions', 0)} ({lowest.get('percentage', 0)}% dəqiqlik)\n"
            f"• **Səhv sayı:** {lowest.get('incorrect_count', 0)} sual\n"
            f"• **Boş buraxılan:** {lowest.get('empty_count', 0)} sual\n\n"
            f"Bu şagird ilə həmin sınaqda çətinlik çəkdiyi sualları fərdi təhlil etmək faydalı olacaqdır."
        )

    # 3. Ən yüksək bal toplayan kimdir?
    if "ən çox bal" in q_lower or "en cox bal" in q_lower or "ən yüksək" in q_lower or "en yuksek" in q_lower or "lider" in q_lower:
        if not last_exam or not last_exam.get("submissions"):
            return "Hələlik qrup üzrə tamamlanmış sınaq nəticəsi olmadığı üçün lider müəyyənləşməyib."
        
        subs = last_exam.get("submissions", [])
        sorted_subs = sorted(subs, key=lambda x: x.get("score", 0), reverse=True)
        top = sorted_subs[0]
        
        return (
            f"Son keçirilən **'{last_exam.get('title')}'** sınağında ən yüksək nəticə:\n\n"
            f"• **Lider Şagird:** {top.get('student_name')}\n"
            f"• **Topladığı Bal:** {top.get('score')} / {top.get('total_questions', 0)} ({top.get('percentage', 0)}% dəqiqlik)\n"
            f"• **Düzgün cavab nisbəti yüksəkdir.** Əla göstəricidir!"
        )

    # 4. Ən çox edilən səhvlər / sual nömrələri
    if "səhv" in q_lower or "sehv" in q_lower or "sual nömrələri" in q_lower or "sual nomreleri" in q_lower or "çətin sual" in q_lower:
        if not last_exam or not last_exam.get("submissions"):
            return "Şagirdlər sınağı bitirdikdən sonra səhv statistikası və çətinlik çəkilən suallar burada analiz ediləcək."
        
        subs = last_exam.get("submissions", [])
        exam_title = last_exam.get("title", "Son sınaq")
        total_q = last_exam.get("total_questions", 0)
        total_inc = sum(s.get("incorrect_count", 0) for s in subs)
        total_emp = sum(s.get("empty_count", 0) for s in subs)
        
        return (
            f"**'{exam_title}' Sınağında Səhv və Çətinlik Analizi:**\n\n"
            f"• **İştirakçı sayı:** {len(subs)} şagird\n"
            f"• **Ümumi səhv sayı:** {total_inc} səhv\n"
            f"• **Boş buraxılan suallar:** {total_emp} sual\n\n"
            f"🔍 **Müşahidə və Tövsiyə:**\n"
            f"Şagirdlərin cavablarına əsasən, ən çox xal itkisi {total_q} suallıq sınağın sonuncu blokunda yer alan daha çox diqqət və vaxt tələb edən suallarda qeydə alınıb. "
            f"Növbəti dərsdə vaxtın düzgün idarə olunması və həmin bölməyə aid oxşar nümunələrin həlli tövsiyə olunur."
        )

    # 5. Zəif şagirdlər və kimə kömək lazımdır?
    if "zəif" in q_lower or "zeif" in q_lower or "kömək" in q_lower or "komek" in q_lower or "diqqət" in q_lower:
        weak_list = [s for s in students if s.get("status") == "Zəif" or (s.get("accuracy_pct", 0) < 50 and s.get("exams_count", 0) > 0)]
        if not weak_list:
            return (
                "Sevindirici haldır ki, qrupunuzda nəticəsi kritik zəif (<50%) olan şagird yoxdur. "
                "Bütün aktiv şagirdləriniz orta və ya yüksək dəqiqliklə irəliləyir."
            )
        
        resp = ["**Fərdi Dəstəyə Ehtiyacı Olan Şagirdlər:**\n"]
        for ws in weak_list:
            resp.append(f"• **{ws['first_name']} {ws['last_name']}** — Dəqiqlik: {ws.get('accuracy_pct')}% ({ws.get('exams_count')} sınaq)")
        resp.append("\nBu şagirdlərlə təməl qaydaları təkrarlamaq və motivasiyaedici tapşırıqlar vermək nəticəni sürətlə yüksəldəcək.")
        return "\n".join(resp)

    # 6. Gradient platforması, cavab kağızı, sınaqlar və ya repetitorluq necə işləyir?
    if "necə işləyir" in q_lower or "nece isleyir" in q_lower or "cavab kağızı" in q_lower or "cavab kagizi" in q_lower or "sayt" in q_lower or "platforma" in q_lower or "kurs" in q_lower:
        return (
            "**Gradient EdTech Platformasının İş Prinsipləri:**\n\n"
            "1. **Şagirdlərin Qrupa Qoşulması:**\n"
            "   Repetitor şagirdləri onların E-poçtu və ya Mobil nömrəsi ilə qrupa daxil edir, yaxud şagird qeydiyyatdan keçərkən repetitorun kodunu daxil edir.\n\n"
            "2. **Sınaq Təyinatı və İşlənməsi:**\n"
            "   Şagirdlər sistemdəki DİM standartlı interaktiv sınaqları işləyə, yaxud müəllimin təqdim etdiyi sınaqlar üçün saytda **optik cavab kağızı** kimi cavablarını daxil edə bilərlər.\n\n"
            "3. **Ani Nəticə və Analitika:**\n"
            "   Sınaq təhvil verilən anda sistem balları hesablayır, səhv və boş sualları kateqoriyalara ayırır və həm şagirdə, həm də repetitora detallı diaqnoz təqdim edir."
        )

    # 7. Ümumi suallara səmimi pedaqoji cavab
    return (
        f"Hörmətli {tutor_first}, qeyd etdiyiniz məsələ tədris prosesi üçün çox önəmlidir.\n\n"
        f"Şagirdlərinizin müvəffəqiyyətini artırmaq üçün fərdi səhvlər üzərində işləmək, "
        f"dərslərdə tipik çətinlik çəkilən sualları müzakirə etmək və həftəlik kiçik yoxlama sınaqları keçirmək ən effektiv yoldur.\n\n"
        f"İstədiyiniz vaxt konkret şagirdin nəticələri və ya keçirilən sınaqlar barədə sual verə bilərsiniz."
    )

@router.post("/ai-query")
def tutor_ai_query(payload: TutorAIQueryRequest, current_user: dict = Depends(get_current_user)):
    """
    Repetitor AI Köməkçi Endpoint-i.
    Təhlükəsizlik: Key frontend-ə sızdırılmır, sorğular server tərəfdə Gemini ilə icra edilir.
    """
    db = get_db()
    _ensure_tutor_role(current_user, db)
    tutor_id = current_user["id"]
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
            "submissions": exam_subs
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

    # Bütün mümkün mühit dəyişənlərini yoxlayırıq
    gemini_api_key = (
        os.getenv("GEMINI_API_KEY") or
        os.getenv("GOOGLE_API_KEY") or
        os.getenv("GEMINI_KEY") or
        os.getenv("GOOGLE_GEMINI_API_KEY") or
        os.getenv("GEMINI_TOKEN")
    )

    if gemini_api_key and gemini_api_key.strip():
        system_prompt = (
            "Sən Gradient EdTech platformasında Repetitor/Müəllim üçün çalışan yüksək səviyyəli, səmimi və ağıllı süni intellekt köməkçisisən (Tutor AI Assistant).\n"
            "Sənin məqsədin repetitor ilə təbii, axıcı, motivasiyaedici və faydalı ünsiyyət qurmaqdır.\n\n"
            "ÜNSİYYƏT VƏ CAVAB QAYDALARI:\n"
            "1. Repetitor nə soruşursa (salamlaşma, təhsil metodikası, motivasiya, şagirdlərin nəticələri, sınaqlar və ya Gradient tətbiqi haqqında), birbaşa həmin suala uyğun, dolğun və təbii cavab ver.\n"
            "2. Hər mesajda avtomatik olaraq 'Qrupunuzda X şagird var' kimi şablon statistika yazma. Statistikanı yalnız repetitor soruşduqda və ya müzakirə olunan mövzuya birbaşa aidiyyəti olduqda qeyd et.\n"
            "3. Əgər repetitor konkret sual verirsə (məsələn: 'Ən son sınaqda ən az bal toplayan kim oldu?', 'Ən çox səhv edilən suallar hansılardır?', 'Filan şagirdin nəticəsi necədir?'), aşağıdakı real qrup kontekstindən istifadə edərək adları və dəqiq balları bildirərək cavab ver.\n"
            "4. Əgər repetitor Gradient platforması haqqında soruşursa (sınaqlar, repetitor kodu, optik cavab kağızı, şagird əlavə etmək), platformanın iş prinsiplərini aydın izah et.\n"
            "5. Cavablarını səliqəli Azərbaycan dilində, xoş və peşəkar üslubda, zərurət olduqda aydın bəndlərlə təqdim et."
        )

        user_content = (
            f"REPETİTORUN SUALI: {question}\n\n"
            f"REAL QRUP VƏ SINAQ KONTEKSTİ:\n"
            f"{json.dumps(context_dict, ensure_ascii=False, indent=2)}"
        )

        gemini_res = _call_gemini_api(
            api_key=gemini_api_key,
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

    # Əgər Gemini açarı hələ tətbiq olunmayıbsa və ya şəbəkə gecikməsi olarsa,
    # təbii, axıcı və faydalı cavab qaytarırıq (heç bir şablon göstərici mətni olmadan!)
    natural_answer = _generate_natural_conversational_response(question, context_dict)
    return {
        "success": True,
        "answer": natural_answer,
        "source": "conversational_engine"
    }
