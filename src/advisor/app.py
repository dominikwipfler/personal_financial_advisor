"""Web-App: offizielle Pydantic AI Chat UI über `agent.to_web()`.

Start:  uv run uvicorn advisor.app:app --reload
Danach: http://localhost:8000

Muster aus dem chatbot-pydanticai-template übernommen (inkl. optionaler
Modellliste vom LiteLLM-Server). Das AdvisorDeps-Objekt hält das Nutzerprofil
als Session-State über alle Requests des Serverprozesses hinweg.
"""

from __future__ import annotations

from advisor.agent import agent
from advisor.config import (
    DEFAULT_MODEL,
    LITELLM_API_KEY,
    LITELLM_SERVER_URL,
    get_litellm_supported_models,
    use_litellm,
)
from advisor.profile import AdvisorDeps

if use_litellm():
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.litellm import LiteLLMProvider

    _provider = LiteLLMProvider(
        api_base=LITELLM_SERVER_URL,
        api_key=LITELLM_API_KEY or "litellm-placeholder",
    )
    _web_models = {
        model_id: OpenAIChatModel(model_id, provider=_provider)
        for model_id in get_litellm_supported_models()
        if model_id != "all-proxy-models"
    }
else:
    _web_models = {DEFAULT_MODEL.split(":", 1)[-1]: DEFAULT_MODEL}

# Session-State: ein Profil pro Serverprozess (lokale Einzelnutzer-App).
deps = AdvisorDeps()

app = agent.to_web(
    models=_web_models,
    deps=deps,
)
