"""End-to-End-Test des Agent-Tool-Loops mit FunctionModel (ohne echten LLM-Key).

Simuliert per Skript-Modell den kompletten Beratungsablauf: alle Profilfelder
speichern -> Risikoprofil berechnen -> Strategie berechnen. Verifiziert damit
Tool-Registrierung, Session-State-Mutation und die JSON-Schnittstellen.
"""

import json
import os

os.environ.setdefault("OPENAI_API_KEY", "dummy-key-fuer-tests")

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from advisor.agent import agent
from advisor.profile import AdvisorDeps

ANGABEN = [
    ("anlageziel", "Altersvorsorge"),
    ("zeithorizont_jahre", "30"),
    ("alter", "30"),
    ("land_steuerkontext", "Deutschland"),
    ("anlageerfahrung", "grundkenntnisse"),
    ("vorhandene_anlagen", "Tagesgeld 10k"),
    ("depot_vorhanden", "false"),
    ("monatliche_sparrate_eur", "400"),
    ("einmalbetrag_eur", "5000"),
    ("schulden", "keine"),
    ("hat_konsumschulden", "false"),
    ("notgroschen_monatsausgaben", "6"),
    ("reaktion_kursverlust_20_prozent", "gelassen_halten"),
    ("max_akzeptierter_verlust_prozent", "30"),
]


def test_kompletter_beratungs_tool_loop():
    schritt = {"i": 0}

    def skript(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        i = schritt["i"]
        schritt["i"] += 1
        if i < len(ANGABEN):
            feld, wert = ANGABEN[i]
            return ModelResponse(
                parts=[ToolCallPart("speichere_profil", {"feld": feld, "wert": wert})]
            )
        if i == len(ANGABEN):
            return ModelResponse(parts=[ToolCallPart("ermittle_risikoprofil_tool", {})])
        if i == len(ANGABEN) + 1:
            return ModelResponse(parts=[ToolCallPart("erstelle_strategie_tool", {})])
        return ModelResponse(parts=[TextPart("FERTIG")])

    deps = AdvisorDeps()
    with agent.override(model=FunctionModel(skript)):
        result = agent.run_sync("Ich möchte für die Rente vorsorgen.", deps=deps)

    p = deps.profile
    assert p.ist_vollstaendig(), f"Profil unvollständig: {p.fehlende_angaben()}"
    assert p.risikoklasse == 4

    # Risiko- und Strategie-Tool berechnen beide dasselbe Risiko erneut -> zwei
    # Einträge im Verlauf (Grundlage für das Verlaufs-Chart der Web-UI).
    assert [e["risikoklasse"] for e in deps.risiko_verlauf] == [4, 4]

    tool_returns = [
        part
        for m in result.all_messages()
        for part in getattr(m, "parts", [])
        if getattr(part, "part_kind", "") == "tool-return"
    ]
    strategie = json.loads(tool_returns[-1].content)
    assert abs(sum(strategie["allokation_prozent"].values()) - 100) < 0.2
    assert abs(sum(strategie["sparplan_aufteilung_eur"].values()) - 400) < 0.01
    assert result.output == "FERTIG"


def test_strategie_verweigert_bei_unvollstaendigem_profil():
    def skript(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("erstelle_strategie_tool", {})])
        return ModelResponse(parts=[TextPart("ENDE")])

    deps = AdvisorDeps()
    with agent.override(model=FunctionModel(skript)):
        result = agent.run_sync("Gib mir sofort eine Strategie!", deps=deps)

    tool_returns = [
        part
        for m in result.all_messages()
        for part in getattr(m, "parts", [])
        if getattr(part, "part_kind", "") == "tool-return"
    ]
    assert "unvollständig" in tool_returns[-1].content
