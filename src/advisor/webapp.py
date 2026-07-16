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

        return await VercelAIAdapter[AdvisorDeps, str].dispatch_request(
            request,
            agent=agent,
            model=model_ref,
            deps=deps,
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
