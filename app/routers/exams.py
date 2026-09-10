from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Dict
from app.database import get_db
from app.security import get_current_user
import uuid

router = APIRouter(prefix="/api/v1/exams", tags=["Exams"])

class ExamSubmitRequest(BaseModel):
    answers: Dict[str, str]

@router.get("/")
def get_all_exams(current_user: dict = Depends(get_current_user)):
    db = get_db()
    
    # 1. Bütün sınaqları gətiririk
    exams_res = db.table("exams").select(
        "id, title, subject, price, question_count, duration_minutes, created_at"
    ).order("created_at", desc=True).execute()
    
    exams = exams_res.data

    # 2. Cari istifadəçinin nəticələrini gətiririk (YENİ SÜTUNLARLA BİRLİKDƏ)
    results_res = db.table("exam_results").select(
        "exam_id, score, incorrect_count, empty_count"
    ).eq("student_id", current_user["id"]).execute()
    
    # Sürətli axtarış üçün nəticələri dictionary-yə yığırıq: {exam_id: result_data}
    completed_exams_data = {res["exam_id"]: res for res in results_res.data}

    # 3. Hər bir sınaq üçün status və nəticələri təyin edirik
    for exam in exams:
        exam_id = exam["id"]
        if exam_id in completed_exams_data:
            exam["is_completed"] = True
            result = completed_exams_data[exam_id]
            exam["correct_count"] = result["score"]
            exam["incorrect_count"] = result.get("incorrect_count", 0)
            exam["empty_count"] = result.get("empty_count", 0)
        else:
            exam["is_completed"] = False

    return exams

@router.get("/{exam_id}/start")
def start_exam(exam_id: str, current_user: dict = Depends(get_current_user)):
    db = get_db()
    
    # TƏHLÜKƏSİZLİK: Şagirdin bu sınağı əvvəlcədən işləyib-işləmədiyini yoxlayırıq
    check_res = db.table("exam_results").select("id").eq("exam_id", exam_id).eq("student_id", current_user["id"]).execute()
    if len(check_res.data) > 0:
        raise HTTPException(status_code=403, detail="Siz artıq bu sınağı işləmisiniz. Yenidən cəhd edə bilməzsiniz.")

    exam_res = db.table("exams").select("id, title, question_count, duration_minutes, questions").eq("id", exam_id).execute()
    if len(exam_res.data) == 0:
        raise HTTPException(status_code=404, detail="Sınaq tapılmadı.")
        
    exam_data = exam_res.data[0]
    questions = exam_data.get("questions", [])
    
    # TƏHLÜKƏSİZLİK: Düzgün cavabları frontend-ə getmədən əvvəl silirik (Data Sanitization)
    safe_questions = []
    for q in questions:
        safe_q = {
            "q_id": q.get("q_id"),
            "text": q.get("text"),
            "options": q.get("options")
        }
        safe_questions.append(safe_q)
        
    exam_data["questions"] = safe_questions
    return exam_data

@router.post("/{exam_id}/submit")
def submit_exam(exam_id: str, payload: ExamSubmitRequest, current_user: dict = Depends(get_current_user)):
    db = get_db()
    
    # TƏHLÜKƏSİZLİK: İki dəfə göndərmənin qarşısını alırıq
    check_res = db.table("exam_results").select("id").eq("exam_id", exam_id).eq("student_id", current_user["id"]).execute()
    if len(check_res.data) > 0:
        raise HTTPException(status_code=400, detail="Bu sınağın nəticəsi artıq qeydə alınıb.")

    exam_res = db.table("exams").select("questions, question_count").eq("id", exam_id).execute()
    if len(exam_res.data) == 0:
        raise HTTPException(status_code=404, detail="Sınaq tapılmadı.")
        
    questions = exam_res.data[0].get("questions", [])
    total_questions = exam_res.data[0].get("question_count", 0)
    
    correct_count = 0
    incorrect_count = 0
    user_answers = payload.answers
    
    # TƏHLÜKƏSİZLİK: Cavabları server tərəfində yoxlayırıq
    for q in questions:
        q_id = str(q.get("q_id"))
        correct_ans = q.get("correct_answer")
        
        if q_id in user_answers:
            if user_answers[q_id] == correct_ans:
                correct_count += 1
            else:
                incorrect_count += 1
                
    empty_count = total_questions - (correct_count + incorrect_count)
            
    result_data = {
        "id": str(uuid.uuid4()),
        "student_id": current_user["id"],
        "exam_id": exam_id,
        "score": correct_count,
        "incorrect_count": incorrect_count,
        "empty_count": empty_count,
        "total_questions": total_questions
    }
    
    db.table("exam_results").insert(result_data).execute()
    
    return {
        "message": "Sınaq uğurla bitdi!", 
        "score": correct_count, 
        "incorrect": incorrect_count, 
        "empty": empty_count, 
        "total": total_questions
    }
