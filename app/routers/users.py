from fastapi import APIRouter, Depends
from app.security import get_current_user

router = APIRouter(prefix="/api/v1/users", tags=["Users"])

@router.get("/me")
def get_my_profile(current_user: dict = Depends(get_current_user)):
    """Cari istifadəçinin məlumatlarını qaytarır (Auth Guard ilə qorunur)"""
    return current_user