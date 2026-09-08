from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import auth

app = FastAPI(
    title="Gradient EdTech API",
    description="Zero-Trust Architecture Backend",
    version="1.0.0",
    docs_url=None, # Təhlükəsizlik: Production-da Swagger UI-ı bağlayırıq (istəyə görə aça bilərsən)
    redoc_url=None
)

# Təhlükəsizlik Qeydi: Qəti CORS Siyasəti. Wildcard (*) qadağandır.
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "http://localhost:8158",
    "https://gradient.az",
    "https://www.gradient.az"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True, # Cookie-lərin (HttpOnly) gedib-gəlməsi üçün mütləq True olmalıdır
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept"],
)

# Router-ləri sistemə əlavə edirik
app.include_router(auth.router)

@app.get("/api/health")
def health_check():
    """Serverin işlək vəziyyətdə olub-olmadığını yoxlamaq üçün endpoint."""
    return {"status": "ok", "message": "Gradient API is running securely."}