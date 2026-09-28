# app/core/config.py
"""
Mərkəzləşdirilmiş, server-only konfiqurasiya.

TƏHLÜKƏSİZLİK (.clinerules §1):
  * Bütün sirlər yalnız server mühitində (Render ENV) oxunur. `.env` yalnız
    lokal inkişaf üçün dəstəklənir.
  * Hər bir açar üçün "tapılmazsa dayan" davranışı — yarım konfiqurasiya ilə
    tətbiq qalxmasına icazə verilmir.
  * Heç bir açar burada `repr`/log zamanı göstərilmir: `SecretStr` işlədilir.
  * Açar dəyəri heç bir endpoint cavabında qaytarılmır.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    """Açarı oxuyur; yoxdursa tətbiqin qalxmasını dayandırır (fail-closed)."""
    value = os.getenv(name)
    if not value or not value.strip():
        raise ValueError(f"CRITICAL SECURITY ERROR: {name} mühit dəyişəni tapılmadı!")
    return value.strip()


def _optional_env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _positive_int_env(name: str, default: int) -> int:
    """Rate limit kimi sayğac dəyərləri mənfi/zero ola bilməz (fail-closed)."""
    raw = os.getenv(name)
    if not raw or not raw.strip():
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        raise ValueError(
            f"CRITICAL SECURITY ERROR: {name} rəqəm deyil (dəyər: {raw!r})!"
        )
    if parsed <= 0:
        raise ValueError(
            f"CRITICAL SECURITY ERROR: {name} müsbət rəqəm olmalıdır (dəyər: {parsed})!"
        )
    return parsed


@dataclass(frozen=True)
class RateLimitBucket:
    """Bir əməliyyat sinfi üçün rate limit parametrləri."""

    name: str
    max_requests: int
    window_seconds: int


@dataclass(frozen=True)
class Settings:
    """Tətbiqin bütün server-only konfiqurasiyası.

    frozen=True → dəyişkən dəyişdirilə bilməz, tətbiq boyu dəyişməz qalır.
    """

    # --- Supabase (service_role — yalnız server tərəfdə, §1) ---
    supabase_url: str = field(repr=False)
    supabase_service_key: str = field(repr=False)

    # --- JWT (HttpOnly + Secure + SameSite cookie-lər, §1) ---
    jwt_secret_key: str = field(repr=False)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30

    # --- Rate limit bucket-ləri (§1 rate-limiting) ---
    # "ai" bucket-i ayrıdır: AI sorğuları bahalıdır, ayrıca bütövlük təmin edir.
    read_bucket: RateLimitBucket = field(
        default_factory=lambda: RateLimitBucket(
            "read", _positive_int_env("RATE_LIMIT_READ_MAX", 60), 60
        )
    )
    write_bucket: RateLimitBucket = field(
        default_factory=lambda: RateLimitBucket(
            "write", _positive_int_env("RATE_LIMIT_WRITE_MAX", 20), 60
        )
    )
    ai_bucket: RateLimitBucket = field(
        default_factory=lambda: RateLimitBucket(
            "ai", _positive_int_env("RATE_LIMIT_AI_MAX", 5), 60
        )
    )

    def bucket(self, name: str) -> RateLimitBucket:
        """Bucket adını konfiqurasiya obyektinə çevirir."""
        buckets = {
            "read": self.read_bucket,
            "write": self.write_bucket,
            "ai": self.ai_bucket,
        }
        try:
            return buckets[name]
        except KeyError:
            # Naməlum bucket = səhv konfiqurasiya; heç vaxt "limitsiz" fallback etmirik.
            raise ValueError(
                f"CRITICAL SECURITY ERROR: naməlum rate limit bucket '{name}'!"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings obyektini bir dəfə yaradır və keşləyir."""
    return Settings(
        supabase_url=_require_env("SUPABASE_URL"),
        supabase_service_key=_require_env("SUPABASE_SERVICE_KEY"),
        jwt_secret_key=_require_env("JWT_SECRET_KEY"),
    )


settings = get_settings()
