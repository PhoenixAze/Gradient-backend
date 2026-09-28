# app/core/rate_limit.py
"""
Sliding-window rate limiting (.clinerules §1 — rate-limiting).

TƏHLÜKƏSİZLİK İZAHI:
  * Rate limit "məqsədli" deyil, hədəf seçir: IP üzrə avtomatik hədəf seçilmə
    üçün autentifikasiya olmuş istifadəçi ID-si prioritet kimi istifadə olunur.
    Bu, legitim istifadəçinin NAT arxasında əlaqələnməsi halında yalançı bloklanmanın
    qarşısını alır.
  * AI sorğuları `ai` bucket-ına gedir — bahalı əməliyyatlara ayrıca, daha ciddi
    bütövlük limiti tətbiq olunur.
  * Naməlum bucket → tətbiq sıradan çıxar (fail-closed): "limitsiz" fallback
    yoxdur, çünki bu, rate-limit boşluğu olardı.
  * Rate limit nəticəsi 429 + `Retry-After` başlığı ilə qaytarılır; istifadəçiyə
    daxili məlumat sızdırılmır.

KRİTİK TƏFSİL — niyə `apply_rate_limits` var:
  FastAPI endpoint funksiyasını çağırmadan ƏVVƏL bütün `Depends(...)`
  asılılıqlarını icra edir. Əgər limit yalnız dekoratorun gövdesində yoxlanılsaydı,
  autentifikasiya `require_tutor` 401 atanda dekorator HEÇ VAYT işə düşməzdi və
 limitsiz hədəfli sorğu mümkün olardı (DoS boşluğu).

  `apply_rate_limits()` dekoratoru real FastAPI dependency-ə çevirir — beləliklə
  limit AUTENTİFİKASİYADAN ƏVVƏL yoxlanılır. main.py bu funksiyanı router
  qeyd olunmadan əvvəl çağırır.

TİCARƏT QEYDİ (bilərəkdən):
  Render-də bu in-memory sayğac `instance` səviyyəsindədir. Yeni konteyner
  start edildikdə sayğac sıfırlanır. Bu, orta həcmli əməliyyatlar üçün qəbul
  edilə bilən trade-offdur; tam dəzgah üçün paylaşımlı store (Redis) tələb olunur.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from functools import wraps
from typing import Any, Callable

from fastapi import HTTPException, Request, status

from app.core.config import RateLimitBucket, settings

logger = logging.getLogger("gradient.rate_limit")

# (bucket, client_key) -> son sorğuların monotonik vaxt damğaları
_HITS: dict[tuple[str, str], deque[float]] = defaultdict(deque)
# In-memory store thread-safe olmalıdır (FastAPI sync endpoint-lər thread-pool-da çalışır)
_LOCK = threading.Lock()

# Uzunmüddətli burulmadan (memory leak) qorunmaq üçün nadir sıradan təmizləmə
_LAST_SWEEP = 0.0
_SWEEP_EVERY_SECONDS = 300.0


def _sweep(now: float) -> None:
    """Bütövlük boş olan bucket sətrini təmizləyir (request üzrə, arxa plan yox)."""
    global _LAST_SWEEP
    if now - _LAST_SWEEP < _SWEEP_EVERY_SECONDS:
        return
    _LAST_SWEEP = now
    for key in [k for k, hits in _HITS.items() if not hits]:
        _HITS.pop(key, None)


def _client_key(request: Request) -> str:
    """Sorumlu sorğu üçün stabil açar.

    İstifadəçi autentikasiya olunubsa onun ID-si, yoxsa IP üzrən qruplaşdırılır.
    Rate limit dependency kimi autentifikasiyadan əvvəl icra olunduğu üçün
    `request.state.user_id` hələ yoxdur → limitsiz hədəfli IP bloklanması isə
    autentikasiyadan sonra ID-yə keçir, yəni hədəf məqsədli olmur.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"

    client = getattr(request, "client", None)
    ip = getattr(client, "host", None) if client else None
    if not ip:
        return "ip:unknown"
    return f"ip:{ip}"


def _check_and_record(bucket: RateLimitBucket, key: str) -> int | None:
    """Sliding window: icazə verirsə `None`, blokludursa `retry_after` qaytarır."""
    now = time.monotonic()
    window_start = now - bucket.window_seconds
    hits_key = (bucket.name, key)

    with _LOCK:
        _sweep(now)
        hits = _HITS[hits_key]
        # Pəncərədən çıxmış köhnə damğaları təmizlə
        while hits and hits[0] < window_start:
            hits.popleft()

        if len(hits) >= bucket.max_requests:
            retry_after = max(1, int(hits[0] + bucket.window_seconds - now))
            return retry_after

        hits.append(now)
    return None


def _enforce(request: Request, bucket: RateLimitBucket) -> None:
    """Rate limit qaydasını tətbiq edir; aşıldıqsa 429 atır."""
    key = _client_key(request)
    retry_after = _check_and_record(bucket, key)

    if retry_after is not None:
        logger.warning("rate_limit_exceeded bucket=%s key=%s", bucket.name, key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Çox sayda sorğu göndərildi. Bir az gözləyin və yenidən cəhd edin.",
            headers={"Retry-After": str(retry_after)},
        )

    # Dependency artıq bu sorğunu saydı; dekorator təkrar saymamalıdır.
    request.state.rate_limit_done = True


def _enforce_dependency(bucket: RateLimitBucket) -> Callable[..., None]:
    """FastAPI dependency üçün qapalı funksiya yaradır (bucket captured)."""

    def _dependency(request: Request) -> None:
        _enforce(request, bucket)

    return _dependency


def rate_limit(bucket_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Dekorator: endpoint-i seçilmiş bucket-a görə rate limit-ləyir.

    İstifadə:
        @router.get("/dashboard")
        @rate_limit("read")
        async def get_dashboard(...): ...

    Endpoint `Request` parametri qəbul etməlidir (FastAPI inject edir); əks halda
    aydın səvh atılır — sessiz şəkildə rate limit-i söndürməkdən təhlükəsizdir.
    """
    # Bucket adı dekoratorun yaradılma vaxtında yoxlanılır (fail-closed).
    bucket = settings.bucket(bucket_name)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            request = kwargs.get("request")
            if request is None:
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break

            if not isinstance(request, Request):
                raise RuntimeError(
                    f"rate_limit: '{func.__name__}' endpoint-i 'request: Request' "
                    "parametri qəbul etməlidir (FastAPI inject edir)."
                )

            # `apply_rate_limits` bu endpoint üçün dependency əlavə edibsə,
            # sorğu artıq sayılıb — təkrar saymamaq üçün keçirik.
            if not getattr(request.state, "rate_limit_done", False):
                _enforce(request, bucket)

            return await func(*args, **kwargs)

        # `apply_rate_limits` bu markeri oxuyur.
        wrapper.__rate_limit_bucket__ = bucket  # type: ignore[attr-defined]
        return wrapper

    return decorator


def apply_rate_limits(router: Any) -> int:
    """Router-dakı `@rate_limit` dekoratorunu real dependency-ə çevirir.

    Limitin autentifikasiyadan əvvəl icra olunmasını təmin edir (modul
    docstring-ində izah olunan DoS boşluğunun düzgün həlli). Dəyişdirilmiş
    route sayını qaytarır.
    """
    from fastapi import Depends
    from fastapi.dependencies.utils import get_dependant

    updated = 0
    for route in router.routes:
        endpoint = getattr(route, "endpoint", None)
        bucket = getattr(endpoint, "__rate_limit_bucket__", None)
        if not isinstance(bucket, RateLimitBucket):
            continue
        # Eyni route üçün iki dəfə tətbiq olunmasının qarşısını alır
        if getattr(route, "_rate_limited", False):
            continue

        dependency = Depends(_enforce_dependency(bucket))
        route.dependencies.append(dependency)

        # FastAPI dependenti route qurulduqda yaradır; dəyişiklik effektli olsun
        # deyə dependant-i dependency əlavə edərək yenidən qururuq.
        dependant = get_dependant(path=route.path_format, call=route.endpoint)
        dependant.dependencies.insert(
            0, get_dependant(path=route.path_format, call=dependency.dependency)
        )
        route.dependant = dependant
        route._rate_limited = True  # type: ignore[attr-defined]
        updated += 1

    return updated


__all__ = ["apply_rate_limits", "rate_limit"]
