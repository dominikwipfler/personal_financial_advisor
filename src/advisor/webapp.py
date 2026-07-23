"""Web-App mit Profil pro Konversation.

`agent.to_web()` verwendet EIN deps-Objekt für alle Requests – damit teilen
sich alle Chats dasselbe Nutzerprofil. Diese App bildet dieselben Endpunkte
mit dem darunterliegenden `VercelAIAdapter` nach, ordnet aber jeder
Konversation (Chat-ID aus dem Vercel-AI-Request) ein eigenes `AdvisorDeps` zu:

- Ein neuer Chat in der UI beginnt mit einem leeren Profil.
- Mehrere Personen können den Server gleichzeitig nutzen (je Chat ein Profil).
- `profil_zuruecksetzen` wirkt nur auf die aktuelle Konversation.

Die Profile liegen weiterhin im Arbeitsspeicher (kein Persistenz-Backend,
siehe LIMITATIONS.md); ein Server-Neustart leert sie.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from collections import OrderedDict
from collections.abc import Mapping

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from pydantic import BaseModel
from pydantic.alias_generators import to_camel
from pydantic_ai import Agent
from pydantic_ai.models import Model, infer_model

# Interner UI-Helfer aus pydantic-ai (laedt die Chat-UI vom CDN und cacht sie).
# Bewusst wiederverwendet statt kopiert; Version ist über uv.lock fixiert.
from pydantic_ai.ui._web.app import _get_ui_html  # pyright: ignore[reportPrivateUsage]
from pydantic_ai.ui.vercel_ai import VercelAIAdapter

from advisor.profile import AdvisorDeps

MAX_SESSIONS = 200
_CHAT_RETRY_DELAYS_S = (0.5, 1.5, 3.0)


class SessionStore:
    """Hält je Konversation (Chat-ID) ein eigenes AdvisorDeps-Objekt.

    Begrenzte Größe mit Verdrängung der ältesten Sitzung, damit ein lange
    laufender Server nicht unbegrenzt Profile ansammelt.
    """

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._sessions: OrderedDict[str, AdvisorDeps] = OrderedDict()
        self._max = max_sessions

    def get(self, chat_id: str) -> AdvisorDeps:
        if chat_id in self._sessions:
            self._sessions.move_to_end(chat_id)
            return self._sessions[chat_id]
        deps = AdvisorDeps()
        self._sessions[chat_id] = deps
        while len(self._sessions) > self._max:
            self._sessions.popitem(last=False)
        return deps

    def __len__(self) -> int:
        return len(self._sessions)


class _ModelInfo(BaseModel, alias_generator=to_camel, populate_by_name=True):
    id: str
    name: str
    builtin_tools: list[str] = []


class _ChatRequestExtra(BaseModel, extra="ignore", alias_generator=to_camel):
    model: str | None = None


def _inject_error_overlay(html: str) -> str:
        overlay = """
<style>
#advisor-error-banner {
    position: fixed;
    right: 16px;
    bottom: 16px;
    z-index: 9999;
    max-width: min(540px, calc(100vw - 32px));
    padding: 12px 14px;
    border-radius: 12px;
    border: 1px solid rgba(220, 38, 38, 0.35);
    background: rgba(127, 29, 29, 0.96);
    color: #fff;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.24);
    font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    display: none;
}
#advisor-error-banner strong { display: block; margin-bottom: 4px; }
#advisor-error-banner button {
    margin-top: 10px;
    padding: 6px 10px;
    border: 0;
    border-radius: 8px;
    background: rgba(255,255,255,0.18);
    color: inherit;
    cursor: pointer;
}
</style>
<div id="advisor-error-banner" role="alert" aria-live="assertive">
    <strong>Modellfehler</strong>
    <div id="advisor-error-banner-text"></div>
    <button type="button" onclick="document.getElementById('advisor-error-banner').style.display='none'">Schließen</button>
</div>
<script>
(function () {
    const originalFetch = window.fetch;
    function showError(message) {
        const banner = document.getElementById('advisor-error-banner');
        const text = document.getElementById('advisor-error-banner-text');
        if (!banner || !text) return;
        text.textContent = message;
        banner.style.display = 'block';
    }
    window.fetch = async function (...args) {
        try {
            const response = await originalFetch.apply(this, args);
            const request = args[0];
            const url = typeof request === 'string' ? request : request && request.url ? request.url : '';
            if (url.includes('/api/chat') && !response.ok) {
                const clone = response.clone();
                let message = `Fehler ${response.status}`;
                try {
                    const payload = await clone.json();
                    message = payload.error || payload.detail || message;
                } catch (error) {
                    try {
                        message = await clone.text();
                    } catch (_) {}
                }
                showError(message);
            }
            return response;
        } catch (error) {
            showError('Netzwerkfehler oder Modell-Endpoint nicht erreichbar.');
            throw error;
        }
    };
})();
</script>
"""

        marker = "</body>"
        if marker not in html:
                return html + overlay
        return html.replace(marker, overlay + marker, 1)


def create_app(
    agent: Agent[AdvisorDeps],
    models: Mapping[str, Model | str] | None = None,
) -> Starlette:
    """Starlette-App mit Chat-UI, /api-Endpunkten und Profil pro Konversation."""
    sessions = SessionStore()

    # Modell-Auswahl für die UI (Logik analog zu pydantic-ai to_web):
    # Agent-Modell zuerst, dann die übergebenen Modelle, Duplikate entfernt.
    model_id_to_ref: dict[str, Model | str] = {}
    model_infos: list[_ModelInfo] = []
    all_models: list[tuple[str | None, Model | str]] = []
    if agent.model is not None:
        all_models.append((None, agent.model))
    all_models.extend((label, ref) for label, ref in (models or {}).items())

    for label, ref in all_models:
        model = infer_model(ref)
        model_id = ref if isinstance(ref, str) else model.model_id
        if model_id in model_id_to_ref:
            continue
        model_id_to_ref[model_id] = ref
        model_infos.append(_ModelInfo(id=model_id, name=label or model.label))

    async def index(request: Request) -> Response:
        content = await _get_ui_html(None)
        content = _inject_error_overlay(content)
        return HTMLResponse(content=content, headers={"Cache-Control": "public, max-age=3600"})

    async def configure_frontend(request: Request) -> Response:
        return JSONResponse(
            {
                "models": [m.model_dump(by_alias=True) for m in model_infos],
                "builtinTools": [],
            }
        )

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "sessions": len(sessions)})

    async def options_chat(request: Request) -> Response:
        return Response()

    async def post_chat(request: Request) -> Response:
        adapter = await VercelAIAdapter[AdvisorDeps, str].from_request(request, agent=agent)
        # Chat-ID des Vercel-AI-Requests = Konversation -> eigenes Profil.
        chat_id = getattr(adapter.run_input, "id", None) or "default"
        deps = sessions.get(chat_id)

        extra = _ChatRequestExtra.model_validate(adapter.run_input.__pydantic_extra__ or {})
        if extra.model and extra.model not in model_id_to_ref:
            return JSONResponse(
                {"error": f'Modell "{extra.model}" ist nicht in der erlaubten Liste'},
                status_code=400,
            )
        model_ref = model_id_to_ref.get(extra.model) if extra.model else None

        last_error: Exception | None = None
        for attempt, delay_s in enumerate((0.0, *_CHAT_RETRY_DELAYS_S), start=1):
            if delay_s:
                await asyncio.sleep(delay_s)
            try:
                return await VercelAIAdapter[AdvisorDeps, str].dispatch_request(
                    request,
                    agent=agent,
                    model=model_ref,
                    deps=deps,
                )
            except Exception as e:  # noqa: BLE001
                last_error = e
                err_str = str(e)

                retryable = any(
                    needle in err_str.lower()
                    for needle in (
                        "timed out",
                        "timeout",
                        "temporarily unavailable",
                        "rate limit",
                        "429",
                        "502",
                        "503",
                        "504",
                    )
                )
                if attempt < 4 and retryable:
                    print(f"Model request failed (attempt {attempt}); retrying...", file=sys.stderr)
                    print(traceback.format_exc(), file=sys.stderr)
                    continue

                # Mapping technischer Provider-Fehler auf aussagekräftige UI-Antworten.
                status = 502
                user_msg = "Fehler beim Modellzugriff. Bitte später erneut versuchen."

                if "Missing Authentication header" in err_str or "401" in err_str:
                    status = 401
                    user_msg = (
                        "Authentifizierungsfehler beim Modell-Provider: "
                        "Bitte OPENAI_API_KEY / LITELLM_API_KEY prüfen."
                    )
                elif "timed out" in err_str.lower() or "timeout" in err_str.lower():
                    status = 504
                    user_msg = "Anfrage an Modell hat zu lange gedauert. Bitte erneut versuchen."
                elif "BadRequestError" in err_str or "400" in err_str:
                    status = 400
                    user_msg = "Ungültige Anfrage an das Modell (400). Bitte Eingabe prüfen."

                print("Model proxy error:", file=sys.stderr)
                print(traceback.format_exc(), file=sys.stderr)

                return JSONResponse({"error": user_msg, "detail": err_str}, status_code=status)

        if last_error is not None:
            print("Unexpected model error fallback:", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            return JSONResponse(
                {"error": "Unbekannter Modellfehler.", "detail": str(last_error)},
                status_code=502,
            )

    api = Starlette(
        routes=[
            Route("/chat", options_chat, methods=["OPTIONS"]),
            Route("/chat", post_chat, methods=["POST"]),
            Route("/configure", configure_frontend, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
        ]
    )

    app = Starlette(routes=[Mount("/api", app=api)])
    app.router.add_route("/", index, methods=["GET"])
    app.router.add_route("/{id}", index, methods=["GET"])
    app.state.sessions = sessions
    return app
