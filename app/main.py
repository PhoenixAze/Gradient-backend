from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.rate_limit import apply_rate_limits
from app.routers import auth, users, exams, debug, settings, analytics, tutor, tutor_group, plans

app = FastAPI(
    title="Gradient EdTech API",
    description="Zero-Trust Architecture Backend",
    version="1.0.0",
    docs_url=None, # Təhlükəsizlik: Production-da Swagger UI bağlıdır
    redoc_url=None
)

# Təhlükəsizlik Qeydi: Qəti CORS Siyasəti. Wildcard (*) qadağandır.
import os

ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
    "http://localhost:8158",
    "https://gradient.az",
    "https://www.gradient.az",
    "https://phoenixaze.github.io",
    "https://gradient-aze.vercel.app"
]

env_origins = os.getenv("ALLOWED_ORIGINS", "")
if env_origins:
    for o in env_origins.split(","):
        o_clean = o.strip()
        if o_clean and o_clean != "*" and o_clean not in ALLOWED_ORIGINS:
            ALLOWED_ORIGINS.append(o_clean)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https:\/\/ais-(dev|pre)-.*\.run\.app",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept", "X-Debug-Key", "x-refresh-token"],
)

# Router-ləri sistemə əlavə edirik
#
# QEYD (route collision qoruması): `tutor` router-i artıq `/api/v1/tutor`
# prefiksini tutur və frontend həmin yolları işlədir. `tutor_group` router-i
# eyni endpoint-ləri (`/dashboard`, `/students/add`, `/requests`, `/profile`)
# təkrarladığı üçün AYRI prefiksə qeyd olunur — beləliklə route collision
# yaranmır və mövcud frontend çağırışları qırılmır.
# Rate limit dekoratorlarını real FastAPI dependency-lərə çeviririk.
# Bu MÜHİM: FastAPI `Depends(...)` asılılıqlarını endpoint-dən əVVƏL icra edir,
# ona görə limit yalnız dekoratorun içində yoxlanılsa, autentifikasiya 401 atanda
# heç vaxt işə düşməzdi (DoS boşluğu). apply_rate_limits bunu dependency-ə çevirir.
_rate_limited_routes = apply_rate_limits(tutor_group.router)

# AI analiz endpoint-ləri `ai` bucket-ındadır (bahalı əməliyyat) — eyni
# mexanizm tətbiq olunmalıdır, əks halda `@rate_limit("ai")` dekoratoru
# heç vaxt icra olunmaz və limit işləməz.
_rate_limited_analytics_routes = apply_rate_limits(analytics.router)

# Debug konsolu endpoint-ləri də `@rate_limit` dekoratoru ilə qorunur.
# BU ÇAĞRI MÜHÜMDÜR: `apply_rate_limits` dekoratoru real `Depends(...)`
# asılılığına çevirir və limiti autentifikasiyadan ƏVVƏL yoxlayır.
# Çağrılmasa, `@rate_limit("read"/"write")` heç vaxt icra olunmaz —
# yəni bütün debug endpoint-ləri limitsiz və bloklayıcı cəzasız qalardı.
_rate_limited_debug_routes = apply_rate_limits(debug.router)

# Abunə planları endpoint-ləri (`/api/v1/plans`).
# BU ÇAĞRI MÜHİMDÜR: `apply_rate_limits` dekoratoru real `Depends(...)`
# asılılığına çevirir və limiti autentifikasiyadan ƏVVƏL yoxlayır.
# Çağrılmasa, `@rate_limit("read"/"write")` heç vaxt icra olunmaz —
# yəni plan endpoint-ləri limitsiz və bloklayıcı cəzasız qalardı.
_rate_limited_plans_routes = apply_rate_limits(plans.router)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(exams.router)
app.include_router(settings.router)
app.include_router(analytics.router)
app.include_router(tutor.router)
app.include_router(debug.router)
app.include_router(tutor_group.router, prefix="/api/v1/tutor-group", tags=["tutor-group"])
app.include_router(plans.router, prefix="/api/v1/plans", tags=["plans"])

@app.get("/api/health")
def health_check():
    """Serverin işlək vəziyyətdə olub-olmadığını yoxlamaq üçün endpoint."""
    return {"status": "ok", "message": "Gradient API is running securely."}
