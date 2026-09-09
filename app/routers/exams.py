from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Dict
from app.database import get_db
from app.security import get_current_user
import uuid

router = APIRouter(prefix="/api/v1/exams", tags=["Exams"])

# Tələbənin göndərəcəyi cavabların Pydantic modeli
class ExamSubmitRequest(BaseModel):
    answers: Dict[str, str]  # Nümunə: {"1": "A", "2": "C"}

@router.get("/")
def get_all_exams(current_user: dict = Depends(get_current_user)):
    """Bütün sınaqların siyahısını qaytarır (Suallar xaric)"""
    db = get_db()
    exams_res = db.table("exams").select("id, title, subject, price, question_count, created_at").order("created_at", desc=True).execute()
    return exams_res.data

@router.get("/{exam_id}/start")
def start_exam(exam_id: str, current_user: dict = Depends(get_current_user)):
    """Sınağı başladır və sualları qaytarır (DÜZGÜN CAVABLAR GİZLƏDİLİR)"""
    db = get_db()
    exam_res = db.table("exams").select("id, title, question_count, questions").eq("id", exam_id).execute()
    
    if len(exam_res.data) == 0:
        raise HTTPException(status_code=404, detail="Sınaq tapılmadı.")
        
    exam_data = exam_res.data[0]
    questions = exam_data.get("questions", [])
    
    # ZERO-TRUST: Düzgün cavabları frontend-ə getmədən əvvəl silirik!
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
    """Tələbənin cavablarını yoxlayır və nəticəni bazaya yazır"""
    db = get_db()
    exam_res = db.table("exams").select("questions, question_count").eq("id", exam_id).execute()
    
    if len(exam_res.data) == 0:
        raise HTTPException(status_code=404, detail="Sınaq tapılmadı.")
        
    questions = exam_res.data[0].get("questions", [])
    total_questions = exam_res.data[0].get("question_count", 0)
    
    correct_count = 0
    user_answers = payload.answers
    
    # Cavabları serverdə yoxlayırıq
    for q in questions:
        q_id = str(q.get("q_id"))
        correct_ans = q.get("correct_answer")
        
        if q_id in user_answers and user_answers[q_id] == correct_ans:
            correct_count += 1
            
    # Nəticəni bazaya yazırıq
    result_data = {
        "id": str(uuid.uuid4()),
        "student_id": current_user["id"],
        "exam_id": exam_id,
        "score": correct_count,
        "total_questions": total_questions
    }
    
    db.table("exam_results").insert(result_data).execute()
    
    return {"message": "Sınaq uğurla bitdi!", "score": correct_count, "total": total_questions}
