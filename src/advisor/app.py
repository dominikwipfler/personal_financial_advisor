"""Web-App: offizielle Pydantic AI Chat UI mit Profil pro Konversation.

Start:  uv run uvicorn advisor.app:app --reload
Danach: http://localhost:8000

Für weitere Nutzer im selben Netzwerk (z. B. zweite Person am Laptop):
        uv run uvicorn advisor.app:app --host 0.0.0.0
Dann ist der Bot unter http://<IP-dieses-Rechners>:8000 erreichbar;
jeder Chat hat sein eigenes Profil (siehe advisor/webapp.py).
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
from advisor.webapp import create_app

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

app = create_app(agent, models=_web_models)
