from fastapi import APIRouter, Depends
from app.database import get_db
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/exams", tags=["Exams"])

@router.get("/")
def get_all_exams(current_user: dict = Depends(get_current_user)):
    """Bütün sınaqların siyahısını qaytarır (Suallar xaric)"""
    db = get_db()
    
    # Yalnız lazımi sütunları çəkirik (Zero-Trust)
    exams_res = db.table("exams").select("id, title, subject, price, question_count, created_at").order("created_at", desc=True).execute()
    
    return exams_res.data