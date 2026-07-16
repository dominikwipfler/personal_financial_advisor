"""Pydantic-AI-Agent: Verdrahtung von Modell, System-Prompt und Tools.

Aufbau nach dem Muster des chatbot-pydanticai-Templates (Modellauswahl direkt
vs. LiteLLM). Die Tools sind dünne Adapter um die Fachmodule `profile`, `risk`,
`strategy` und `research` – so bleibt die Fachlogik testbar und der Agent
austauschbar.
"""

from __future__ import annotations

import json
from typing import Any, cast

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModelSettings

from advisor import research
from advisor.config import (
    DEFAULT_MODEL,
    LITELLM_API_KEY,
    LITELLM_MODEL,
    LITELLM_SERVER_URL,
    MAX_TOKENS,
    REASONING_EFFORT,
    REQUEST_TIMEOUT_S,
    use_litellm,
)
from advisor.profile import AdvisorDeps, UserProfile
from advisor.prompts import SYSTEM_PROMPT
from advisor.rebalancing import Position, erstelle_umschichtungsplan
from advisor.risk import ermittle_risikoprofil
from advisor.strategy import erstelle_strategie

_model: str | Model

if use_litellm():
    # LiteLLM-Proxy verwenden (Muster aus dem Template übernommen).
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.litellm import LiteLLMProvider

    _provider = LiteLLMProvider(
        api_base=LITELLM_SERVER_URL, api_key=LITELLM_API_KEY or "litellm-placeholder"
    )
    _model = OpenAIChatModel(LITELLM_MODEL, provider=_provider)
else:
    # Provider direkt (z. B. OPENAI_API_KEY / ANTHROPIC_API_KEY aus .env).
    _model = DEFAULT_MODEL

# Zuverlässigkeits-Einstellungen:
# - timeout: hängende Anfragen brechen ab, statt die UI zu blockieren.
# - max_tokens: genug Raum für Reasoning + Tool-Aufrufe, damit Tool-Argumente
#   nicht mitten im JSON abgeschnitten werden (beobachtet mit gpt-oss-120b).
# - openai_reasoning_effort: "low" beschleunigt die vielen kleinen
#   Profil-Speicher-Runden deutlich; wird von Nicht-Reasoning-Modellen ignoriert.
_ALLOWED_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_effort = REASONING_EFFORT if REASONING_EFFORT in _ALLOWED_EFFORTS else "low"
_model_settings = OpenAIChatModelSettings(
    timeout=REQUEST_TIMEOUT_S,
    max_tokens=MAX_TOKENS,
)
# reasoning_effort nur an Modelle senden, die den Parameter sicher verstehen
# (OpenAI-/gpt-oss-Familie); andere Backends lehnen ihn teils mit Fehler ab.
_model_name = LITELLM_MODEL if use_litellm() else DEFAULT_MODEL
if "gpt-oss" in _model_name or _model_name.startswith(("openai:", "o1", "o3", "gpt-")):
    _model_settings["openai_reasoning_effort"] = cast(Any, _effort)

try:
    agent: Agent[AdvisorDeps] = Agent(
        _model,
        deps_type=AdvisorDeps,
        instructions=SYSTEM_PROMPT,
        model_settings=_model_settings,
        # Abgeschnittene/ungültige Tool-Argumente: bis zu 3 Korrekturversuche,
        # bevor ein Fehler an die UI durchschlägt.
        retries=3,
    )
except Exception as e:  # noqa: BLE001
    raise RuntimeError(
        f"Modell '{_model}' konnte nicht initialisiert werden: {e}\n"
        "Bitte `.env` anlegen (Vorlage: .env.example) und den API-Key des "
        "Providers setzen, z. B. OPENAI_API_KEY – oder LiteLLM konfigurieren."
    ) from e


@agent.instructions
def profil_status(ctx: RunContext[AdvisorDeps]) -> str:
    """Aktuellen Profilstand in jede Anfrage injizieren.

    Dadurch weiß der Agent ohne Tool-Aufruf, was schon erfasst ist, und
    stellt keine Frage doppelt – auch über Browser-Reloads hinweg.
    """
    p = ctx.deps.profile
    erfasst = p.model_dump(exclude_none=True)
    offen = p.fehlende_angaben()
    return (
        "# Aktueller Stand des Nutzerprofils (Session-State)\n"
        f"Bereits erfasst: {json.dumps(erfasst, ensure_ascii=False) if erfasst else 'noch nichts'}\n"
        f"Noch offen: {', '.join(offen) if offen else 'nichts – Profil vollständig'}\n"
        f"Aktuelle Fortschrittszeile: {p.fortschritt_zeile()}"
    )


# --------------------------- Profil-Tools ---------------------------------


@agent.tool
def speichere_profil(ctx: RunContext[AdvisorDeps], feld: str, wert: str) -> str:
    """Speichert EINE Nutzerangabe im Profil (Session-State).

    Args:
        feld: Feldname aus dem Nutzerprofil, z. B. "anlageziel",
            "zeithorizont_jahre", "alter", "land_steuerkontext",
            "anlageerfahrung" (keine|grundkenntnisse|fortgeschritten|sehr_erfahren),
            "vorhandene_anlagen", "depot_vorhanden" (true|false),
            "monatliche_sparrate_eur", "einmalbetrag_eur", "schulden",
            "hat_konsumschulden" (true|false), "notgroschen_monatsausgaben",
            "reaktion_kursverlust_20_prozent" (alles_verkaufen|teilweise_verkaufen|
            beunruhigt_halten|gelassen_halten|nachkaufen),
            "max_akzeptierter_verlust_prozent".
        wert: Der Wert als Text; Zahlen z. B. "150", Booleans "true"/"false".
    """
    p = ctx.deps.profile
    if feld not in type(p).model_fields:
        gueltig = ", ".join(type(p).model_fields.keys())
        return f"Unbekanntes Feld '{feld}'. Gültige Felder: {gueltig}"

    try:
        daten = p.model_dump()
        daten[feld] = wert
        # Validierung inkl. Typkonvertierung ("150" -> 150.0, "true" -> True).
        ctx.deps.profile = type(p).model_validate(daten)
    except Exception as e:  # noqa: BLE001
        return f"Wert '{wert}' für Feld '{feld}' ungültig: {e}"

    offen = ctx.deps.profile.fehlende_angaben()
    return (
        f"Gespeichert: {feld} = {wert}. "
        f"Noch offen: {', '.join(offen) if offen else 'nichts – Profil vollständig'}. "
        f"Fortschrittszeile: {ctx.deps.profile.fortschritt_zeile()}"
    )


@agent.tool
def speichere_profil_mehrere(ctx: RunContext[AdvisorDeps], angaben: UserProfile) -> str:
    """Speichert MEHRERE Nutzerangaben auf einmal im Profil (Session-State).

    Bevorzuge dieses Tool, wenn eine Nutzernachricht mehr als eine Angabe
    enthält: alle erkannten Angaben in EINEM Aufruf übergeben. Nur die Felder
    setzen, die der Nutzer tatsächlich genannt hat; alle übrigen weglassen.
    Das Feld `risikoklasse` niemals setzen (wird berechnet).

    Args:
        angaben: Die erkannten Profilfelder mit ihren Werten.
    """
    p = ctx.deps.profile
    neue_werte = angaben.model_dump(exclude_none=True)
    neue_werte.pop("risikoklasse", None)
    if not neue_werte:
        return "Keine Angaben übergeben – bitte die erkannten Felder setzen."

    daten = p.model_dump()
    daten.update(neue_werte)
    ctx.deps.profile = type(p).model_validate(daten)

    offen = ctx.deps.profile.fehlende_angaben()
    gespeichert = ", ".join(f"{k}={v}" for k, v in neue_werte.items())
    return (
        f"Gespeichert: {gespeichert}. "
        f"Noch offen: {', '.join(offen) if offen else 'nichts – Profil vollständig'}. "
        f"Fortschrittszeile: {ctx.deps.profile.fortschritt_zeile()}"
    )


@agent.tool
def zeige_profil(ctx: RunContext[AdvisorDeps]) -> str:
    """Zeigt das aktuell gespeicherte Nutzerprofil und offene Angaben."""
    p = ctx.deps.profile
    return json.dumps(
        {
            "profil": p.model_dump(exclude_none=True),
            "offene_angaben": p.fehlende_angaben(),
        },
        ensure_ascii=False,
        indent=1,
    )


@agent.tool
def profil_zuruecksetzen(ctx: RunContext[AdvisorDeps]) -> str:
    """Setzt das Nutzerprofil zurück (neue Beratung von vorn)."""
    ctx.deps.reset()
    return "Profil zurückgesetzt. Die Beratung beginnt von vorn."


# ----------------------- Risiko- und Strategie-Tools -----------------------


@agent.tool
def ermittle_risikoprofil_tool(ctx: RunContext[AdvisorDeps]) -> str:
    """Berechnet Risikoklasse und Aktienquote aus dem vollständigen Profil.

    Methodik: getrennte Scores für Risikobereitschaft und Risikotragfähigkeit
    (Minimum zählt), Aktienquote als Bernoulli-/Markowitz-Nutzenoptimum mit
    Kappungen (Zeithorizont, Notgroschen, Konsumschulden).
    """
    p = ctx.deps.profile
    offen = p.fehlende_angaben()
    if offen:
        return f"Profil noch unvollständig, bitte zuerst erfragen: {', '.join(offen)}"

    ergebnis = ermittle_risikoprofil(p)
    ctx.deps.profile = p.model_copy(update={"risikoklasse": ergebnis.risikoklasse})
    return json.dumps(ergebnis.__dict__, ensure_ascii=False, indent=1)


@agent.tool
def erstelle_strategie_tool(ctx: RunContext[AdvisorDeps]) -> str:
    """Berechnet Asset-Allokation (Prozent), Sparplan- und Einmalbetrags-Aufteilung.

    Liefert die Strategie-Basis als Zahlenwerk; die konkreten Produkte je
    Baustein ergänzt der Agent aus der aktuellen Web-Recherche.
    """
    p = ctx.deps.profile
    offen = p.fehlende_angaben()
    if offen:
        return f"Profil noch unvollständig, bitte zuerst erfragen: {', '.join(offen)}"

    risiko = ermittle_risikoprofil(p)
    strategie = erstelle_strategie(p, risiko)
    return json.dumps(strategie, ensure_ascii=False, indent=1)


@agent.tool
def erstelle_umschichtungsplan_tool(
    ctx: RunContext[AdvisorDeps],
    positionen: list[Position],
    gebuehr_prozent: float = 0.25,
    gebuehr_min_eur: float = 1.0,
) -> str:
    """Konkrete Kauf-/Verkaufsliste vom Ist-Depot zur Ziel-Allokation, mit Gebühren.

    Vorher mit dem Nutzer klären: (1) Aufschlüsselung der bestehenden Positionen
    (Name, Wert in EUR, Zuordnung zu einem Allokations-Baustein oder 'sonstiges'),
    (2) Ordergebühren des Brokers. Sind die Gebühren unbekannt, Standardwerte
    verwenden und das dem Nutzer sagen.

    Args:
        positionen: Bestehende Positionen mit Name, Wert und Kategorie.
        gebuehr_prozent: Ordergebühr in Prozent des Ordervolumens (z. B. 0.25).
        gebuehr_min_eur: Mindestgebühr pro Order in EUR (z. B. 1.0).
    """
    p = ctx.deps.profile
    offen = p.fehlende_angaben()
    if offen:
        return f"Profil noch unvollständig, bitte zuerst erfragen: {', '.join(offen)}"

    risiko = ermittle_risikoprofil(p)
    strategie = erstelle_strategie(p, risiko)
    plan = erstelle_umschichtungsplan(
        positionen=positionen,
        ziel_allokation_prozent=strategie["allokation_prozent"],
        neues_kapital_eur=p.einmalbetrag_eur or 0.0,
        gebuehr_prozent=gebuehr_prozent,
        gebuehr_min_eur=gebuehr_min_eur,
    )
    return json.dumps(plan, ensure_ascii=False, indent=1)


# ----------------------------- Recherche-Tools -----------------------------


@agent.tool_plain
def web_suche(suchbegriff: str, max_treffer: int = 8) -> str:
    """Websuche (DuckDuckGo) für aktuelle ETF-/Produkt- und Marktrecherche.

    Args:
        suchbegriff: Suchanfrage, z. B. "MSCI World UCITS ETF geringste TER 2026".
        max_treffer: Anzahl der Treffer (Standard 8).
    """
    return research.web_suche(suchbegriff, max_treffer)


@agent.tool_plain
def lese_webseite(url: str) -> str:
    """Ruft eine Webseite ab und liefert deren Textinhalt (gekürzt).

    Args:
        url: Vollständige URL aus den Suchtreffern.
    """
    return research.lese_webseite(url)


@agent.tool_plain
def marktdaten(symbole: str) -> str:
    """Kurse und Kennzahlen von Yahoo Finance (Rendite 1/3/5 Jahre, Volatilität, TER).

    Args:
        symbole: Kommagetrennte Yahoo-Finance-Ticker, z. B. "EUNL.DE, IS3N.DE, ^GSPC".
    """
    return research.marktdaten(symbole)
