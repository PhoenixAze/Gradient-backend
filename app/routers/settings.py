from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])

@router.get("/contact")
def get_contact_settings():
    """Platformanın əlaqə və WhatsApp dəstək məlumatlarını qaytarır."""
    return {
        "whatsapp_url": "https://wa.me/994505975697",
        "email": "support@gradient.az",
        "phone": "+994 50 597 56 97"
    }
