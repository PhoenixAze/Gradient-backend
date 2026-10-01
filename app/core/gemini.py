# app/core/gemini.py
"""
Server-only Gemini (Google Generative Language API) klienti.

TƏHLÜKƏSİZLİK İZAHI (.clinerules §1 — "Secrets" / "Zero-Trust Backend"):
  * Açar YALNIZCA server mühit dəyişənindən (Render ENV / lokal .env) oxunur və
    heç vaxt endpoint cavabında, logda və ya istisna mesajında görünmür.
  * Açar URL-in query hissəsidir (`?key=...`). Buna görə:
      - heç bir xəta mesajına upstream URL/response body daxil edilmir;
      - `logger` yalnız model adı + HTTP status yazır (açar YOXDUR).
  * Modul `import` olunanda açar yoxdursa heç nə atılmır — funksiya
    `None` qaytarır və endpoint 503 verir. Yəni "yarım konfiqurasiya ilə"
    sır vəziyyəti yaranmır, eyni zamanda AI olmadan da platforma işləyir.

STRUKTURLAŞDIRILMIŞ CAVAB:
  Analiz endpoint-ləri üçün mətn yox, **JSON** tələb olunur (frontend dəyişməz
  sxemə görə render edir). `_extract_json()` modelin bəzən JSON-u markdown
  kod bloku içində qaytarmasını tolerant şəkildə idarə edir.

MODEL STRATEGİYASI:
  Render-da şagird sayı artdıqça qlobal kvota problemi yarana bilər.
  `_generate_json()` siyahı halında modelləri sınayır: əvvəlcə ən ucuz/ən sürətli
  model, uğursuz olsa növbəti. Uğur model adı qaytarılır (frontend-də
  "Hansı model analiz etdi?" sualının cavabı — əlçatanlıq).
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("gradient.gemini")

# Render ENV-də bəzən fərqli adlarla verilir — hamısı yoxlanılır.
_API_KEY_ENV_NAMES: Tuple[str, ...] = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GEMINI_API_KEY",
    "GEMINI_KEY",
)

# Sıra = prioritet. Əvvəlcə bahalı deyil, sürətli model; sonra ehtiyat.
_MODELS: Tuple[str, ...] = (
    "gemini-2.0-flash",
    "gemini-2.5-flash-lite",
    "gemini-1.5-flash",
)

_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
_TIMEOUT_SECONDS = 25


def get_api_key() -> Optional[str]:
    """
    Gemini açarını oxuyur. Boş/boş-ağlı dəyər `None` qaytarır (fail-closed:
    "limitsiz fallback" yoxdur, AI sadəcə işləməz).
    """
    for name in _API_KEY_ENV_NAMES:
        raw = os.getenv(name)
        if raw and raw.strip():
            # .env fayllarında tək/ikiliqatlı dırnaq qalığı ola bilər
            return raw.strip().strip("'\"")
    return None


def is_configured() -> bool:
    return get_api_key() is not None


def _build_prompt(system_prompt: str, user_content: str) -> str:
    return f"{system_prompt.strip()}\n\n---\n{user_content.strip()}"


def _post(model: str, api_key: str, payload: Dict[str, Any]) -> Optional[str]:
    """Bir modelə sorğu göndərir; mətn uğursuz/boşdursa None qaytarır."""
    url = f"{_API_ROOT}/{model}:generateContent?key={api_key}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Gradient-AI/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return None
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # TƏHLÜKƏSİZLİK: yalnız status kodu loglanır. Upstream body (açar də
        # ola bilər) istifadəçiyə/loga sızdırılmır.
        logger.warning("gemini_http_error model=%s status=%s", model, exc.code)
        return None
    except Exception as exc:  # timeout, DNS, JSON xətası ...
        logger.warning("gemini_request_error model=%s type=%s", model, type(exc).__name__)
        return None

    candidates = data.get("candidates") or []
    if not candidates:
        return None
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    return text or None


def _extract_json(text: str) -> Optional[Any]:
    """
    Model cavabından JSON obyektini çıxarır.
    Gemini bəzən ```json ... ``` bloku və ya ətrafda izah mətni qaytarır —
    bu, tətbiq kodunun "kəsə yox" səhvinə çevrilməməsi üçün tolerant parser.
    """
    if not text:
        return None

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, flags=re.DOTALL | re.IGNORECASE)
    candidates: List[str] = []
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)

    for raw in candidates:
        cleaned = raw.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # İlk { ... } blokunu tap (izah mətni varsa)
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue

    logger.warning("gemini_json_parse_failed")
    return None


def generate_json(
    system_prompt: str,
    user_content: str,
    max_output_tokens: int = 2048,
    temperature: float = 0.3,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Gemini-dən strukturlaşdırılmış JSON alır.

    Returns:
        (data, model) — uğurlu olduqda (dict, model_adı).
        Uğursuz/çıxarsız halda (None, None).

    `temperature` 0.3-dir: analiz faktiki və təkrar edilə bilən olmalıdır,
    yaradıcı "mətn gəl-gəl" yox, struktur qurulmalıdır.
    """
    api_key = get_api_key()
    if not api_key:
        return None, None

    prompt = _build_prompt(system_prompt, user_content)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
        ],
    }

    for model in _MODELS:
        text = _post(model, api_key, payload)
        if not text:
            continue
        parsed = _extract_json(text)
        if isinstance(parsed, dict):
            logger.info("gemini_ok model=%s", model)
            return parsed, model
        if isinstance(parsed, list):
            # Model siyahı qaytardısa onu {"items": [...]} şəklində normallaşdırırıq
            logger.info("gemini_ok_list model=%s", model)
            return {"items": parsed}, model

    logger.warning("gemini_all_models_failed")
    return None, None


__all__ = ["generate_json", "get_api_key", "is_configured"]