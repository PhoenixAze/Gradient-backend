import os
from fastapi import APIRouter, HTTPException, Security, status, Depends
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from app.database import get_db
from dotenv import load_dotenv

load_dotenv()

router = APIRouter(prefix="/api/v1/debug", tags=["Debug"])

DEBUG_SECRET_KEY = os.getenv("DEBUG_SECRET_KEY", "menim_gizli_debug_acarim_123")
api_key_header = APIKeyHeader(name="X-Debug-Key", auto_error=True)

def verify_debug_key(api_key: str = Security(api_key_header)):
    if api_key != DEBUG_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="İcazə rədd edildi. Keçərsiz Debug Açarı."
        )
    return api_key

# --- SCHEMAS ---
class CheckUserRequest(BaseModel):
    identifier: str

class BalanceUpdateRequest(BaseModel):
    identifier: str
    new_balance: float

# --- ENDPOINTS ---

@router.post("/check-user")
def check_user(payload: CheckUserRequest, api_key: str = Depends(verify_debug_key)):
    """Mərhələ 1: E-poçt və ya nömrəyə əsasən istifadəçini tapır"""
    db = get_db()
    try:
        user_res = db.table("users").select("id, first_name, last_name, balance").eq("identifier", payload.identifier).execute()
        if len(user_res.data) == 0:
            raise HTTPException(status_code=404, detail="Belə bir istifadəçi tapılmadı.")
        
        user = user_res.data[0]
        return {
            "first_name": user["first_name"],
            "last_name": user["last_name"],
            "balance": user["balance"]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sistem xətası: {str(e)}")

@router.post("/update-balance")
def update_user_balance(payload: BalanceUpdateRequest, api_key: str = Depends(verify_debug_key)):
    """Mərhələ 2: Təsdiqləndikdən sonra balansı yeniləyir"""
    db = get_db()
    try:
        user_res = db.table("users").select("id").eq("identifier", payload.identifier).execute()
        if len(user_res.data) == 0:
            raise HTTPException(status_code=404, detail="İstifadəçi tapılmadı.")
        
        user_id = user_res.data[0]["id"]
        db.table("users").update({"balance": payload.new_balance}).eq("id", user_id).execute()
        
        return {"message": f"Uğurlu! Yeni balans: {payload.new_balance} ₼"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sistem xətası: {str(e)}")

@router.get("/stats")
def get_system_stats(api_key: str = Depends(verify_debug_key)):
    """Sistemin sağlamlığını və Supabase metrikalarını qaytarır"""
    db = get_db()
    try:
        users_res = db.table("users").select("role").execute()
        users = users_res.data
        total_users = len(users)
        students = sum(1 for u in users if u.get("role") == "student")
        tutors = sum(1 for u in users if u.get("role") == "tutor")
        admins = sum(1 for u in users if u.get("role") == "admin")

        exams_res = db.table("exams").select("question_count").execute()
        exams = exams_res.data
        total_exams = len(exams)
        total_questions = sum(e.get("question_count", 0) for e in exams)

        return {
            "status": "online",
            "database": "connected",
            "metrics": {
                "users": {
                    "total": total_users,
                    "students": students,
                    "tutors": tutors,
                    "admins": admins
                },
                "exams": {
                    "total": total_exams,
                    "total_questions": total_questions
                }
            }
        }
    except Exception as e:
        return {
            "status": "offline",
            "database": "error",
            "error_detail": str(e)
        }