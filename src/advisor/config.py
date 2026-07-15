"""Konfiguration: Modellauswahl und Provider-Anbindung.

Pattern übernommen aus dem chatbot-pydanticai-template:
- API-Keys und Einstellungen kommen aus der Umgebung bzw. einer lokalen `.env`.
- Standard ist die direkte Provider-Anbindung (z. B. OpenAI via OPENAI_API_KEY).
- Optional kann ein LiteLLM-Proxy verwendet werden (USE_LITELLM=1 bzw.
  LITELLM_SERVER_URL + LITELLM_API_KEY).
"""

import os
from typing import Any

import httpx
from dotenv import load_dotenv

# .env laden, bevor unten os.environ ausgewertet wird.
load_dotenv()

# Modell-Kennung im pydantic-ai-Format "<provider>:<modell>".
# Über die Umgebungsvariable ADVISOR_MODEL umstellbar, ohne Code zu ändern.
DEFAULT_MODEL = os.environ.get("ADVISOR_MODEL", "").strip() or "openai:gpt-4o-mini"

# --- Backend-Auswahl: Provider direkt vs. LiteLLM (aus dem Template übernommen) ---
USE_LITELLM_ENV = os.environ.get("USE_LITELLM", "").strip().lower() in ("1", "true", "yes")
LITELLM_SERVER_URL_ENV = os.environ.get("LITELLM_SERVER_URL", "").strip() or None
LITELLM_API_KEY_ENV = os.environ.get("LITELLM_API_KEY", "").strip() or None


def use_litellm() -> bool:
    """LiteLLM verwenden, wenn das Flag gesetzt ist oder URL + Key vorhanden sind."""
    if USE_LITELLM_ENV:
        return True
    return bool(LITELLM_SERVER_URL_ENV and LITELLM_API_KEY_ENV)


LITELLM_SERVER_URL = LITELLM_SERVER_URL_ENV or "http://localhost:4000"
LITELLM_API_KEY = LITELLM_API_KEY_ENV or ""
LITELLM_MODEL = os.environ.get("LITELLM_MODEL", "").strip() or "gpt-4o-mini"


def get_litellm_supported_models(timeout_s: float = 5.0) -> list[str]:
    """Modell-IDs vom konfigurierten LiteLLM-Server abrufen (GET /v1/models).

    Liefert eine leere Liste, wenn LiteLLM nicht konfiguriert ist oder die
    Anfrage fehlschlägt.
    """
    if not use_litellm():
        return []

    base = LITELLM_SERVER_URL.rstrip("/")
    api_base = base if base.endswith("/v1") else f"{base}/v1"
    url = f"{api_base}/models"

    headers: dict[str, str] = {}
    if LITELLM_API_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_API_KEY}"
        headers["x-api-key"] = LITELLM_API_KEY

    try:
        resp = httpx.get(url, headers=headers, timeout=timeout_s)
        resp.raise_for_status()
        payload: Any = resp.json()
    except Exception:
        return []

    data = payload.get("data")
    if not isinstance(data, list):
        return []

    models: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                models.append(model_id)

    return sorted(set(models))
