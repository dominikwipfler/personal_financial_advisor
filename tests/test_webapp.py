"""Tests der Web-App: Profil pro Konversation (Session-Trennung)."""

import os

os.environ.setdefault("OPENAI_API_KEY", "dummy-key-fuer-tests")

import json
from collections.abc import AsyncIterator

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from starlette.testclient import TestClient

import advisor.webapp as webapp
from advisor.agent import agent
from advisor.profile import AdvisorDeps, UserProfile
from advisor.risk import ermittle_risikoprofil
from advisor.strategy import erstelle_strategie
from advisor.webapp import SessionStore, _export_markdown, _session_state, create_app


@pytest.fixture(autouse=True)
def _isolierte_sqlite_datei(tmp_path, monkeypatch):
    """Jeder Test bekommt eine eigene, temporäre SQLite-Datei statt der echten
    `advisor_sessions.db` im Projektverzeichnis (siehe SessionStore/DB_PATH)."""
    monkeypatch.setattr(webapp, "DB_PATH", str(tmp_path / "test_sessions.db"))


def test_session_store_trennt_und_verdraengt():
    store = SessionStore(max_sessions=2)
    a = store.get("chat-a")
    b = store.get("chat-b")
    assert a is not b
    assert store.get("chat-a") is a  # gleiche Konversation -> gleiches Objekt
    store.get("chat-c")  # verdrängt die älteste Sitzung (chat-b)
    assert len(store) == 2
    assert store.get("chat-b") is not b  # neu angelegt


def test_session_store_uebersteht_sqlite_ueber_neue_instanz(tmp_path):
    """Eine zweite SessionStore-Instanz auf derselben Datei simuliert einen
    Server-Neustart: der In-Memory-Cache ist leer, die Daten müssen aus SQLite
    kommen."""
    db_path = str(tmp_path / "persist.db")

    store1 = SessionStore(db_path=db_path)
    deps = store1.get("chat-restart")
    deps.profile = deps.profile.model_copy(update={"anlageziel": "Altersvorsorge", "alter": 42})
    store1.speichere("chat-restart")

    store2 = SessionStore(db_path=db_path)  # frischer In-Memory-Cache, gleiche Datei
    geladen = store2.get("chat-restart")
    assert geladen is not deps
    assert geladen.profile.anlageziel == "Altersvorsorge"
    assert geladen.profile.alter == 42


def test_session_store_speichert_ohne_vorherigen_get_nichts():
    """`speichere()` für eine nie geladene Chat-ID ist ein No-Op (kein Fehler)."""
    store = SessionStore()
    store.speichere("nie-angefasst")  # darf nicht crashen


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


def test_export_dateiname_wird_bereinigt():
    """Regression: Die Chat-ID landet im Content-Disposition-Header.

    Anführungszeichen oder Zeilenumbrüche darin könnten den Header zerlegen
    bzw. weitere Header einschleusen.
    """
    app = create_app(agent)
    client = TestClient(app)
    antwort = client.get('/api/export/abc%22evil%22')
    disposition = antwort.headers["content-disposition"]
    assert disposition == 'attachment; filename="beratung-abcevil.md"'
    assert '"evil"' not in disposition


def test_druckansicht_escaped_html_aus_profilfeldern():
    """Profilangaben dürfen in der Druckansicht nicht als HTML ausgeführt werden."""
    app = create_app(agent)
    client = TestClient(app)
    chat = "escape-test"
    client.post(f"/api/profile/{chat}", json={"anlageziel": "<img src=x onerror=alert(1)>"})
    koerper = client.get(f"/api/export/{chat}?format=html").text.split("<body>")[1]
    assert "<img" not in koerper
    assert "&lt;img" in koerper


def test_health_und_configure_endpunkte():
    app = create_app(agent)
    client = TestClient(app)
    assert client.get("/api/health").json()["ok"] is True
    config = client.get("/api/configure").json()
    assert "models" in config and len(config["models"]) >= 1


def _vollstaendiges_profil() -> UserProfile:
    return UserProfile(
        anlageziel="Altersvorsorge",
        zeithorizont_jahre=30,
        alter=30,
        land_steuerkontext="Deutschland",
        anlageerfahrung="grundkenntnisse",
        vorhandene_anlagen="Tagesgeld 10k",
        depot_vorhanden=False,
        monatliche_sparrate_eur=400,
        einmalbetrag_eur=5000,
        schulden="keine",
        hat_konsumschulden=False,
        notgroschen_monatsausgaben=6,
        reaktion_kursverlust_20_prozent="gelassen_halten",
        max_akzeptierter_verlust_prozent=30,
    )


def test_state_endpunkt_spiegelt_beratungsphase():
    app = create_app(agent)
    client = TestClient(app)

    # Neue Konversation: Profil noch leer -> Phase "profil".
    state = client.get("/api/state/chat-neu").json()
    assert state["phase"] == "profil"
    assert state["profilFortschritt"] == {"erfasst": 0, "gesamt": 13}
    assert state["risiko"] is None
    assert state["strategie"] is None

    # Vollständiges Profil, aber Risiko/Strategie noch nicht berechnet.
    sessions: SessionStore = app.state.sessions
    deps = sessions.get("chat-voll")
    deps.profile = _vollstaendiges_profil()
    state = client.get("/api/state/chat-voll").json()
    assert state["phase"] == "risiko"

    # Risiko berechnet, Strategie noch offen.
    risiko = ermittle_risikoprofil(deps.profile)
    deps.profile = deps.profile.model_copy(update={"risikoklasse": risiko.risikoklasse})
    deps.letztes_risiko = risiko.__dict__
    state = client.get("/api/state/chat-voll").json()
    assert state["phase"] == "strategie"
    assert state["risiko"]["risikoklasse"] == risiko.risikoklasse

    # Strategie berechnet -> abgeschlossen.
    deps.letzte_strategie = erstelle_strategie(deps.profile, risiko)
    state = client.get("/api/state/chat-voll").json()
    assert state["phase"] == "abgeschlossen"
    assert "aktien_welt_industrielaender" in state["strategie"]["allokation_prozent"]
    assert abs(sum(state["strategie"]["allokation_prozent"].values()) - 100) < 0.2


def test_state_endpunkt_liefert_risiko_verlauf_fuers_chart():
    """Jede Risikoberechnung hängt einen Snapshot an risiko_verlauf an (Grundlage
    für das Verlaufs-Chart im Beratungsstatus-Panel)."""
    app = create_app(agent)
    client = TestClient(app)
    sessions: SessionStore = app.state.sessions
    deps = sessions.get("chat-verlauf")
    deps.profile = _vollstaendiges_profil()

    assert client.get("/api/state/chat-verlauf").json()["risikoVerlauf"] == []

    risiko = ermittle_risikoprofil(deps.profile)
    deps.risiko_verlauf.append({"risikoklasse": risiko.risikoklasse, "aktienquote_empfohlen": risiko.aktienquote_empfohlen})
    deps.risiko_verlauf.append({"risikoklasse": risiko.risikoklasse, "aktienquote_empfohlen": risiko.aktienquote_empfohlen})
    verlauf = client.get("/api/state/chat-verlauf").json()["risikoVerlauf"]
    assert len(verlauf) == 2
    assert verlauf[0]["risikoklasse"] == risiko.risikoklasse

    deps.reset()
    assert client.get("/api/state/chat-verlauf").json()["risikoVerlauf"] == []


def test_profil_uebersteht_simulierten_serverneustart_ueber_api():
    """Zwei unabhängige `create_app`-Instanzen auf derselben (per Fixture
    isolierten) DB_PATH simulieren einen Server-Neustart über die echten
    HTTP-Endpunkte: Formular-Übernahme in "Prozess 1", Auslesen in "Prozess 2"."""
    client1 = TestClient(create_app(agent))
    r = client1.post(
        "/api/profile/chat-neustart",
        json={"anlageziel": "Altersvorsorge", "alter": 42},
    )
    assert r.status_code == 200

    client2 = TestClient(create_app(agent))  # neue App = neuer In-Memory-Cache
    state = client2.get("/api/state/chat-neustart").json()
    assert state["profil"]["Anlageziel"] == "Altersvorsorge"
    assert state["profil"]["Alter"] == 42


def test_chat_tool_ergebnis_uebersteht_simulierten_serverneustart():
    """Auch vom Agenten (Tool-Aufruf) gesetzte Profilfelder müssen nach dem
    Chat-Request persistiert sein, nicht nur die Formular-Übernahme."""

    async def skript(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        letzte = messages[-1]
        kinds = {getattr(p, "part_kind", "") for p in getattr(letzte, "parts", [])}
        if "tool-return" in kinds:
            yield "OK"
            return
        yield {0: DeltaToolCall(name="speichere_profil", json_args=json.dumps({"feld": "alter", "wert": "34"}))}

    client1 = TestClient(create_app(agent))
    with agent.override(model=FunctionModel(stream_function=skript)):
        r = client1.post("/api/chat", json=_chat_request("chat-tool-neustart", "Ich bin 34."))
    assert r.status_code == 200

    client2 = TestClient(create_app(agent))
    state = client2.get("/api/state/chat-tool-neustart").json()
    assert state["profil"]["Alter"] == 34


def test_profile_endpunkt_uebernimmt_formular_felder_ohne_llm():
    app = create_app(agent)
    client = TestClient(app)

    r = client.post(
        "/api/profile/chat-formular",
        json={
            "anlageziel": "Altersvorsorge",
            "alter": 42,
            "depot_vorhanden": False,
            "monatliche_sparrate_eur": 250,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["profil"]["Anlageziel"] == "Altersvorsorge"
    assert body["profil"]["Alter"] == 42
    assert body["profil"]["Depot vorhanden"] is False

    sessions: SessionStore = app.state.sessions
    deps = sessions.get("chat-formular")
    assert deps.profile.alter == 42
    assert deps.profile.monatliche_sparrate_eur == 250


def test_profile_endpunkt_uebernimmt_risiko_slider_felder():
    """Die beiden Slider-Felder aus Schritt 2 des Willkommens-Formulars werden
    genauso wie die übrigen Formularfelder validiert und übernommen."""
    app = create_app(agent)
    client = TestClient(app)

    r = client.post(
        "/api/profile/chat-slider",
        json={
            "anlageziel": "Altersvorsorge",
            "reaktion_kursverlust_20_prozent": "gelassen_halten",
            "max_akzeptierter_verlust_prozent": 25,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["profil"]["Reaktion auf −20 % Kursverlust"] == "gelassen_halten"
    assert body["profil"]["Max. akzeptierter Verlust (%)"] == 25

    sessions: SessionStore = app.state.sessions
    deps = sessions.get("chat-slider")
    assert deps.profile.reaktion_kursverlust_20_prozent == "gelassen_halten"
    assert deps.profile.max_akzeptierter_verlust_prozent == 25


def test_profile_endpunkt_lehnt_ungueltige_reaktion_ab():
    app = create_app(agent)
    client = TestClient(app)
    r = client.post(
        "/api/profile/chat-invalid-reaktion",
        json={"reaktion_kursverlust_20_prozent": "panik"},
    )
    assert r.status_code == 400


def test_profile_endpunkt_ignoriert_client_seitige_risikoklasse():
    app = create_app(agent)
    client = TestClient(app)
    client.post("/api/profile/chat-x", json={"alter": 30, "risikoklasse": 5})
    sessions: SessionStore = app.state.sessions
    assert sessions.get("chat-x").profile.risikoklasse is None


def test_profile_endpunkt_lehnt_ungueltige_werte_ab():
    app = create_app(agent)
    client = TestClient(app)
    r = client.post("/api/profile/chat-invalid", json={"anlageerfahrung": "quatsch"})
    assert r.status_code == 400


def test_export_liefert_markdown_mit_profil_und_strategie():
    deps = AdvisorDeps()
    deps.profile = _vollstaendiges_profil()
    risiko = ermittle_risikoprofil(deps.profile)
    deps.letztes_risiko = risiko.__dict__
    deps.letzte_strategie = erstelle_strategie(deps.profile, risiko)

    markdown = _export_markdown(deps)
    assert "Altersvorsorge" in markdown
    assert "Risikoklasse" in markdown
    assert "Strategie: Asset-Allokation" in markdown
    assert "keine zugelassene Anlage-, Steuer- oder Rechtsberatung" in markdown


def test_export_formatiert_booleans_und_enums_menschenlesbar():
    """Rohwerte (True/False, Enum-Literale) dürfen im Export nicht durchschlagen,
    sondern als Ja/Nein bzw. Klartext erscheinen."""
    deps = AdvisorDeps()
    deps.profile = _vollstaendiges_profil().model_copy(
        update={
            "depot_vorhanden": False,
            "hat_konsumschulden": True,
            "reaktion_kursverlust_20_prozent": "gelassen_halten",
            "anlageerfahrung": "grundkenntnisse",
        }
    )
    markdown = _export_markdown(deps)
    assert "**Depot vorhanden:** Nein" in markdown
    assert "**Konsumschulden vorhanden:** Ja" in markdown
    assert "**Reaktion auf −20 % Kursverlust:** Gelassen halten" in markdown
    assert "**Anlageerfahrung:** Grundkenntnisse" in markdown
    # Rohe Literale/Booleans dürfen nicht mehr auftauchen.
    assert "gelassen_halten" not in markdown
    assert " True" not in markdown and " False" not in markdown


def test_export_endpunkt_liefert_download_und_druckansicht():
    app = create_app(agent)
    client = TestClient(app)
    sessions: SessionStore = app.state.sessions
    sessions.get("chat-export").profile = _vollstaendiges_profil()

    r_md = client.get("/api/export/chat-export")
    assert r_md.status_code == 200
    assert "markdown" in r_md.headers["content-type"]
    assert "attachment" in r_md.headers["content-disposition"]
    assert "Altersvorsorge" in r_md.text

    r_html = client.get("/api/export/chat-export?format=html")
    assert r_html.status_code == 200
    assert "text/html" in r_html.headers["content-type"]
    assert "window.print()" in r_html.text


def test_profil_zuruecksetzen_loescht_auch_strategie_state():
    deps = AdvisorDeps()
    deps.profile = _vollstaendiges_profil()
    risiko = ermittle_risikoprofil(deps.profile)
    deps.letztes_risiko = risiko.__dict__
    deps.letzte_strategie = erstelle_strategie(deps.profile, risiko)

    deps.reset()

    assert deps.letztes_risiko is None
    assert deps.letzte_strategie is None
    assert deps.letzter_umschichtungsplan is None
    assert _session_state(deps)["phase"] == "profil"
