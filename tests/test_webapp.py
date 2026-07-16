"""Tests der Web-App: Profil pro Konversation (Session-Trennung)."""

import os

os.environ.setdefault("OPENAI_API_KEY", "dummy-key-fuer-tests")

import json
from collections.abc import AsyncIterator

from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from starlette.testclient import TestClient

from advisor.agent import agent
from advisor.webapp import SessionStore, create_app


def test_session_store_trennt_und_verdraengt():
    store = SessionStore(max_sessions=2)
    a = store.get("chat-a")
    b = store.get("chat-b")
    assert a is not b
    assert store.get("chat-a") is a  # gleiche Konversation -> gleiches Objekt
    store.get("chat-c")  # verdrängt die älteste Sitzung (chat-b)
    assert len(store) == 2
    assert store.get("chat-b") is not b  # neu angelegt


def _chat_request(chat_id: str, text: str) -> dict:
    return {
        "trigger": "submit-message",
        "id": chat_id,
        "messages": [
            {"id": "m1", "role": "user", "parts": [{"type": "text", "text": text}]},
        ],
    }


def test_neue_konversation_bekommt_eigenes_profil():
    """Zwei Chats speichern unterschiedliche Anlageziele, ohne sich zu überschreiben."""

    async def skript(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        # Nach einem Tool-Aufruf (letzte Nachricht enthält tool-return) beenden.
        letzte = messages[-1]
        kinds = {getattr(p, "part_kind", "") for p in getattr(letzte, "parts", [])}
        if "tool-return" in kinds:
            yield "OK"
            return
        text = str(getattr(letzte, "parts", ""))
        ziel = next((z for z in ("Altersvorsorge", "Hauskauf") if z in text), None)
        if ziel:
            yield {
                0: DeltaToolCall(
                    name="speichere_profil",
                    json_args=json.dumps({"feld": "anlageziel", "wert": ziel}),
                )
            }
        else:
            yield "OK"

    app = create_app(agent)
    client = TestClient(app)

    with agent.override(model=FunctionModel(stream_function=skript)):
        r1 = client.post("/api/chat", json=_chat_request("chat-1", "Ich spare für die Altersvorsorge"))
        r2 = client.post("/api/chat", json=_chat_request("chat-2", "Ich spare für einen Hauskauf"))
    assert r1.status_code == 200
    assert r2.status_code == 200

    sessions: SessionStore = app.state.sessions
    assert sessions.get("chat-1").profile.anlageziel == "Altersvorsorge"
    assert sessions.get("chat-2").profile.anlageziel == "Hauskauf"


def test_health_und_configure_endpunkte():
    app = create_app(agent)
    client = TestClient(app)
    assert client.get("/api/health").json()["ok"] is True
    config = client.get("/api/configure").json()
    assert "models" in config and len(config["models"]) >= 1
