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

# Sıra = prioritet. Əvvəlcə sürətli model; sonra ehtiyat (ucuz) model.
#
# QEYD 1 (bu versiyanın səbəbi): dayandırılmış alias-lar
# (`gemini-flash-latest`, `gemini-2.0-flash`, `gemini-1.5-flash`) API tərəfindən
# 404 qaytarır → hər biri bir HTTP səhri + ~1s latency yalayır və siyahı
# sonuna qədər sınanır. Onlar siyahıdan TAMAMİLƏ çıxarıldı.
# QEYD 2: yalnız stabil, uzunmüddətli alias-lar (`-flash`) saxlanılır ki,
# Render-da "model yox oldu" xətası analizi dayandırmasın.
# QEYD 3 (thinking): 2.5 seriyasında "thinking" default ON-dur və
# `maxOutputTokens` bütçəsinin bir hissəsini "düşünməyə" sərf edir →
# uzun JSON cavabı KƏSİLİR və parse uğursuz olur (analiz "işləmir").
# `thinkingBudget: 0` ilə model yalnız cavab tokenlərinə görə işləyir.
_MODELS: Tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
)

_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
_TIMEOUT_SECONDS = 25

# Ümumi analiz (bütün sınaqlar) JSON-u cəhd analizindən XEYLİ böyükdür
# (fənn + mövzu + plan). Kəsilməmək üçün yuxarı bütçə; aşağıda
# `generate_json` uğursuz parse zamanı bir dəfə ikiqat artırır.
DEFAULT_MAX_OUTPUT_TOKENS = 4096
# Bir model iki dəfə sınanır: ikinci cəhd daha böyük bütçə ilə
# (kəsilmiş JSON'u tamamlamağa çalışır).
_MAX_RETRIES_PER_MODEL = 2


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


def _build_payload(
    max_output_tokens: int,
    temperature: float,
) -> Dict[str, Any]:
    """
    Təhlükəsiz/ödəksiz generation konfiqurasiyası.

    TƏHLÜKƏSİZLİK: `temperature` aşağıdır (0.3) və `responseMimeType=json`
    → model yalnız strukturlaşdırılmış mətn qaytarır, istənilən içəriyi yox.
    `thinkingBudget: 0` → "düşünmə" tokenləri cavab bütçəsini yemir.
    """
    return {
        "contents": [{"role": "user", "parts": [{"text": ""}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
        ],
    }


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
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    temperature: float = 0.3,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Gemini-dən strukturlaşdırılmış JSON alır.

    Returns:
        (data, model) — uğurlu olduqda (dict, model_adı).
        Uğursuz/çıxarsız halda (None, None).

    `temperature` 0.3-dir: analiz faktiki və təkrar edilə bilən olmalıdır,
    yaradıcı "mətn gəl-gəl" yox, struktur qurulmalıdır.

    İSO reliability: hər model iki dəfə sınanır; cavab JSON-u parse
    OLUNMASA (adətən `maxOutputTokens` kəsilməsi) bütçə ikiqat artırılıb
    təkrar sorğu göndərilir — uzun JSON-larda (ümumi analiz) endpoint-in
    "işləmir" görünməsinin əsas səbəbini aradan qaldırır.
    """
    api_key = get_api_key()
    if not api_key:
        return None, None

    prompt = _build_prompt(system_prompt, user_content)
    budget = max(1024, int(max_output_tokens))

    for model in _MODELS:
        current_budget = budget
        for attempt in range(1, _MAX_RETRIES_PER_MODEL + 1):
            payload = _build_payload(current_budget, temperature)
            payload["contents"] = [{"role": "user", "parts": [{"text": prompt}]}]

            text = _post(model, api_key, payload)
            if not text:
                # Boş HTTP/timeout nəticəsi → bütçəni artırmaq işə yaramır,
                # növbəti cəhdə keç.
                break

            parsed = _extract_json(text)
            if isinstance(parsed, dict):
                logger.info("gemini_ok model=%s attempt=%s", model, attempt)
                return parsed, model
            if isinstance(parsed, list):
                # Model siyahı qaytardısa onu {"items": [...]} şəklində normallaşdırırıq
                logger.info("gemini_ok_list model=%s attempt=%s", model, attempt)
                return {"items": parsed}, model

            # Parse uğursuz → daha böyük bütçə ilə təkrar cəhd
            logger.warning(
                "gemini_retry_larger_budget model=%s attempt=%s budget=%s",
                model, attempt, current_budget,
            )
            current_budget *= 2

    logger.warning("gemini_all_models_failed")
    return None, None


__all__ = ["generate_json", "get_api_key", "is_configured"]