"""Web-App mit Profil pro Konversation.

`agent.to_web()` verwendet EIN deps-Objekt für alle Requests – damit teilen
sich alle Chats dasselbe Nutzerprofil. Diese App bildet dieselben Endpunkte
mit dem darunterliegenden `VercelAIAdapter` nach, ordnet aber jeder
Konversation (Chat-ID aus dem Vercel-AI-Request) ein eigenes `AdvisorDeps` zu:

- Ein neuer Chat in der UI beginnt mit einem leeren Profil.
- Mehrere Personen können den Server gleichzeitig nutzen (je Chat ein Profil).
- `profil_zuruecksetzen` wirkt nur auf die aktuelle Konversation.

Die Profile werden zusätzlich in einer SQLite-Datei gespiegelt (siehe
`SessionStore`), damit ein Server-Neustart sie nicht mehr löscht; im laufenden
Betrieb bleibt ein In-Memory-Cache (LRU, `MAX_SESSIONS`) die schnelle
Zugriffsebene.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import traceback
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route

from pydantic import BaseModel
from pydantic.alias_generators import to_camel
from pydantic_ai import Agent
from pydantic_ai.models import Model, infer_model

# Interner UI-Helfer aus pydantic-ai (laedt die Chat-UI vom CDN und cacht sie).
# Bewusst wiederverwendet statt kopiert; Version ist über uv.lock fixiert.
from pydantic_ai.ui._web.app import _get_ui_html  # pyright: ignore[reportPrivateUsage]
from pydantic_ai.ui.vercel_ai import VercelAIAdapter

from advisor.config import DB_PATH
from advisor.profile import PFLICHTANGABEN, AdvisorDeps, UserProfile

MAX_SESSIONS = 200
_CHAT_RETRY_DELAYS_S = (0.5, 1.5, 3.0)

# Menschenlesbare Feldnamen für Status-Panel und Export (siehe UserProfile).
_PROFIL_LABELS: dict[str, str] = {
    "anlageziel": "Anlageziel",
    "zeithorizont_jahre": "Zeithorizont (Jahre)",
    "vorhandene_anlagen": "Vorhandene Anlagen",
    "depot_vorhanden": "Depot vorhanden",
    "monatliche_sparrate_eur": "Monatliche Sparrate (EUR)",
    "einmalbetrag_eur": "Einmalbetrag (EUR)",
    "reaktion_kursverlust_20_prozent": "Reaktion auf −20 % Kursverlust",
    "max_akzeptierter_verlust_prozent": "Max. akzeptierter Verlust (%)",
    "schulden": "Schulden",
    "hat_konsumschulden": "Konsumschulden vorhanden",
    "notgroschen_monatsausgaben": "Notgroschen (Monatsausgaben)",
    "alter": "Alter",
    "land_steuerkontext": "Land / Steuerkontext",
    "anlageerfahrung": "Anlageerfahrung",
}


def _session_state(deps: AdvisorDeps) -> dict:
    """Kompakter Session-Status für das Status-Panel der Web-UI (siehe _UI_ENHANCEMENTS)."""
    p = deps.profile
    offen = p.fehlende_angaben()
    erfasst = len(PFLICHTANGABEN) - len(offen)

    if offen:
        phase = "profil"
    elif p.risikoklasse is None:
        phase = "risiko"
    elif deps.letzte_strategie is None:
        phase = "strategie"
    else:
        phase = "abgeschlossen"

    return {
        "phase": phase,
        "profil": {
            _PROFIL_LABELS.get(k, k): v
            for k, v in p.model_dump(exclude_none=True).items()
            if k != "risikoklasse"
        },
        "profilFortschritt": {"erfasst": erfasst, "gesamt": len(PFLICHTANGABEN)},
        "risiko": deps.letztes_risiko,
        "risikoVerlauf": deps.risiko_verlauf,
        "strategie": deps.letzte_strategie,
        "umschichtungsplan": deps.letzter_umschichtungsplan,
    }


def _export_markdown(deps: AdvisorDeps) -> str:
    """Baut die Beratungszusammenfassung als Markdown (Export-Button der Web-UI)."""
    p = deps.profile
    zeilen: list[str] = [
        "# Persönliche Anlagestrategie – Zusammenfassung",
        "",
        f"_Erstellt am {datetime.now().strftime('%d.%m.%Y %H:%M')} Uhr_",
        "",
        "> Hochschulprojekt – keine zugelassene Anlage-, Steuer- oder "
        "Rechtsberatung. Allgemeine Informationen ohne Garantien oder "
        "Renditeversprechen. Kapitalanlagen können zu Verlusten führen.",
        "",
        "## Profil",
    ]

    profil_werte = p.model_dump(exclude_none=True)
    for feld, label in _PROFIL_LABELS.items():
        if feld in profil_werte:
            zeilen.append(f"- **{label}:** {profil_werte[feld]}")
    if not any(feld in profil_werte for feld in _PROFIL_LABELS):
        zeilen.append("- _Noch keine Angaben erfasst._")

    risiko = deps.letztes_risiko
    if risiko:
        zeilen += [
            "",
            "## Risikoprofil",
            f"- **Risikoklasse:** {risiko.get('risikoklasse')} "
            f"({risiko.get('klassen_name')})",
            f"- **Aktienquote (nutzenoptimal):** "
            f"{round((risiko.get('aktienquote_unbegrenzt') or 0) * 100, 1)} %",
            f"- **Aktienquote (empfohlen, nach Kappungen):** "
            f"{round((risiko.get('aktienquote_empfohlen') or 0) * 100, 1)} %",
        ]
        for begrenzung in risiko.get("begrenzungen") or []:
            zeilen.append(f"  - {begrenzung}")

    if len(deps.risiko_verlauf) > 1:
        verlauf = " → ".join(str(v["risikoklasse"]) for v in deps.risiko_verlauf)
        zeilen.append(f"- **Verlauf der Risikoklasse im Gespräch:** {verlauf}")

    strategie = deps.letzte_strategie
    if strategie:
        zeilen += ["", "## Strategie: Asset-Allokation"]
        for baustein, prozent in strategie.get("allokation_prozent", {}).items():
            zeilen.append(f"- {baustein.replace('_', ' ')}: {prozent} %")

        sparplan = strategie.get("sparplan_aufteilung_eur") or {}
        if sparplan:
            zeilen += ["", "## Sparplan-Aufteilung (monatlich)"]
            for baustein, betrag in sparplan.items():
                zeilen.append(f"- {baustein.replace('_', ' ')}: {betrag} €")

        einmal = strategie.get("einmalbetrag_aufteilung_eur") or {}
        if einmal:
            zeilen += ["", "## Einmalbetrag-Aufteilung"]
            for baustein, betrag in einmal.items():
                zeilen.append(f"- {baustein.replace('_', ' ')}: {betrag} €")

        hinweise = strategie.get("hinweise") or []
        if hinweise:
            zeilen += ["", "## Hinweise"]
            for hinweis in hinweise:
                zeilen.append(f"- {hinweis}")

    plan = deps.letzter_umschichtungsplan
    if plan and "fehler" not in plan:
        zeilen += [
            "",
            "## Umschichtungsplan",
            f"- Handelsvolumen: {plan.get('handelsvolumen_eur')} €",
            f"- Gebühren gesamt: {plan.get('gebuehren_summe_eur')} €",
            f"- Geschätzte Steuer: {plan.get('geschaetzte_steuer_summe_eur')} €",
        ]
        for kauf in plan.get("kaeufe") or []:
            zeilen.append(f"  - Kauf {kauf.get('kategorie')}: {kauf.get('betrag_eur')} €")
        for verkauf in plan.get("verkaeufe") or []:
            zeilen.append(f"  - Verkauf {verkauf.get('kategorie')}: {verkauf.get('betrag_eur')} €")

    return "\n".join(zeilen) + "\n"


def _dateiname(chat_id: str) -> str:
    """Chat-ID auf einen unbedenklichen Dateinamen reduzieren.

    Die ID kommt aus der URL und landet im `Content-Disposition`-Header.
    Ohne Filterung könnten Anführungszeichen den Header zerlegen bzw.
    Zeilenumbrüche weitere Header einschleusen (Response-Splitting).
    """
    sauber = "".join(z for z in chat_id if z.isalnum() or z in "-_")
    return sauber[:60] or "export"


def _export_print_html(markdown: str) -> str:
    """Druckfreundliche HTML-Ansicht der Export-Zusammenfassung ('Als PDF speichern')."""
    escaped = (
        markdown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>Anlagestrategie – Zusammenfassung</title>
<style>
  body {{
      font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
      max-width: 760px; margin: 32px auto; padding: 0 24px; color: #111;
      white-space: pre-wrap;
  }}
  @media print {{ body {{ margin: 0; }} }}
</style>
</head>
<body>{escaped}
<script>window.onload = function () {{ window.print(); }};</script>
</body>
</html>"""


class SessionStore:
    """Hält je Konversation (Chat-ID) ein eigenes AdvisorDeps-Objekt.

    Zwei Ebenen: ein In-Memory-LRU-Cache (`_sessions`, begrenzt auf
    `max_sessions`, damit ein lange laufender Server nicht unbegrenzt viele
    Objekte im Arbeitsspeicher hält) vor einer SQLite-Datei, die dieselben
    Daten dauerhaft hält – ein Server-Neustart verliert dadurch keine Profile
    mehr; `speichere()` muss nach jeder Mutation eines `AdvisorDeps` explizit
    aufgerufen werden (siehe `post_chat`/`post_profile`), da die Objekte selbst
    nichts von der Persistenz wissen.
    """

    def __init__(self, max_sessions: int = MAX_SESSIONS, db_path: str | None = None) -> None:
        self._sessions: OrderedDict[str, AdvisorDeps] = OrderedDict()
        self._max = max_sessions
        # Erst hier (statt als Default-Argument) aufgelöst, damit Tests
        # `advisor.webapp.DB_PATH` per monkeypatch auf eine temporäre Datei
        # umbiegen können, ohne die reale Datei im Projektverzeichnis zu berühren.
        self._db_path = db_path or DB_PATH
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "chat_id TEXT PRIMARY KEY, daten TEXT NOT NULL, aktualisiert_am TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

    def _aus_db_laden(self, chat_id: str) -> AdvisorDeps | None:
        conn = self._connect()
        try:
            zeile = conn.execute(
                "SELECT daten FROM sessions WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        finally:
            conn.close()
        if zeile is None:
            return None
        try:
            return AdvisorDeps.from_dict(json.loads(zeile[0]))
        except Exception:  # noqa: BLE001
            # Beschädigter/inkompatibler Datensatz (z. B. nach Schema-Änderung):
            # lieber mit leerem Profil neu starten als die ganze App blockieren.
            print(f"Konnte Session '{chat_id}' nicht aus SQLite laden – starte leer.", file=sys.stderr)
            return None

    def get(self, chat_id: str) -> AdvisorDeps:
        if chat_id in self._sessions:
            self._sessions.move_to_end(chat_id)
            return self._sessions[chat_id]
        deps = self._aus_db_laden(chat_id) or AdvisorDeps()
        self._sessions[chat_id] = deps
        while len(self._sessions) > self._max:
            self._sessions.popitem(last=False)
        return deps

    def speichere(self, chat_id: str) -> None:
        """Schreibt den aktuellen Stand einer Konversation nach SQLite.

        Muss nach jeder Anfrage aufgerufen werden, die `AdvisorDeps` mutiert
        haben könnte (Chat-Turn mit Tool-Aufrufen, Formular-Übernahme) – die
        Mutation selbst passiert direkt auf dem Objekt, ohne dass diese Klasse
        davon erfährt.
        """
        deps = self._sessions.get(chat_id)
        if deps is None:
            return
        payload = json.dumps(deps.to_dict(), ensure_ascii=False)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO sessions (chat_id, daten, aktualisiert_am) VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(chat_id) DO UPDATE SET daten = excluded.daten, aktualisiert_am = excluded.aktualisiert_am",
                (chat_id, payload),
            )
            conn.commit()
        finally:
            conn.close()

    def __len__(self) -> int:
        return len(self._sessions)


class _ModelInfo(BaseModel, alias_generator=to_camel, populate_by_name=True):
    id: str
    name: str
    builtin_tools: list[str] = []


class _ChatRequestExtra(BaseModel, extra="ignore", alias_generator=to_camel):
    model: str | None = None


def _inject_ui_enhancements(html: str) -> str:
    """Schleust additive Status-/Fehler-/Beratungs-UI vor `</body>` ein.

    Die eigentliche Chat-UI kommt fertig gebündelt vom CDN (siehe Docstring
    oben) – ihre React-Interna sind hier unbekannt und nicht Teil dieses
    Repos. Alles unten ist daher bewusst additiv (eigene, fest positionierte
    Elemente) statt in die bestehende Oberfläche integriert: robust gegen
    CDN-Updates, aber optisch nicht nahtlos. Schnellwahl-Buttons versuchen
    per Best-Effort (nativer Value-Setter + Enter-Keydown), das gefundene
    `<textarea>` der Chat-UI zu befüllen und abzusenden; schlägt das fehl,
    bleibt der Text zum manuellen Absenden stehen.
    """
    overlay = """
<style>
/* Die Chat-UI zentriert Nachrichtenbereich + Eingabezeile bei einer leeren
   Konversation vertikal (Tailwind-Klasse "justify-center" auf dem Wrapper),
   statt die Eingabe unten anzulegen. Gezielte Überschreibung nur für exakt
   diese Klassenkombination (in der aktuellen CDN-Version einmalig für diesen
   Wrapper); trifft die Chat-UI keine der Klassen mehr (CDN-Update), greift
   die Regel einfach nicht mehr – kein hartes position:fixed, das mit dem
   internen Scroll-/Sticky-Verhalten der Chat-UI kollidieren könnte. */
.flex.flex-col.justify-center.flex-1.h-screen.overflow-hidden { justify-content: flex-end !important; }

#advisor-status-pill {
    position: fixed; top: 12px; left: 12px; z-index: 9998;
    display: flex; align-items: center; gap: 7px;
    padding: 6px 12px; border-radius: 999px;
    background: rgba(30, 30, 35, 0.85); color: #fff;
    font: 13px/1.3 system-ui, -apple-system, "Segoe UI", sans-serif;
    box-shadow: 0 4px 16px rgba(0,0,0,0.18);
}
#advisor-status-pill .advisor-dot {
    width: 8px; height: 8px; border-radius: 50%; background: #9ca3af; flex: none;
}
#advisor-status-pill[data-state="idle"] .advisor-dot { background: #22c55e; }
#advisor-status-pill[data-state="working"] .advisor-dot { background: #3b82f6; animation: advisor-pulse 1s infinite; }
#advisor-status-pill[data-state="error"] .advisor-dot { background: #ef4444; }
#advisor-status-pill[data-state="loading"] .advisor-dot { background: #f59e0b; }
@keyframes advisor-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

#advisor-error-banner {
    position: fixed; right: 16px; bottom: 16px; z-index: 9999;
    max-width: min(540px, calc(100vw - 32px));
    padding: 12px 14px; border-radius: 12px;
    border: 1px solid rgba(220, 38, 38, 0.35);
    background: rgba(127, 29, 29, 0.96); color: #fff;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.24);
    font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    display: none;
}
#advisor-error-banner strong { display: block; margin-bottom: 4px; }
#advisor-error-banner .advisor-actions { display: flex; gap: 8px; margin-top: 10px; }
#advisor-error-banner button {
    padding: 6px 10px; border: 0; border-radius: 8px;
    background: rgba(255,255,255,0.18); color: inherit; cursor: pointer;
}
#advisor-error-banner button:hover { background: rgba(255,255,255,0.28); }

#advisor-panel-tab {
    position: fixed; right: 0; top: 45%; transform: translateY(-50%);
    z-index: 9990; writing-mode: vertical-rl; text-orientation: mixed;
    padding: 10px 6px; border-radius: 10px 0 0 10px; cursor: pointer;
    background: #111827; color: #fff; font: 12px/1 system-ui, sans-serif;
    box-shadow: -4px 0 12px rgba(0,0,0,0.15); letter-spacing: 0.02em;
}
#advisor-panel {
    position: fixed; top: 0; right: 0; height: 100vh; width: min(340px, 92vw);
    z-index: 9991; background: var(--popover, #fff); color: var(--popover-foreground, #111827);
    border-left: 1px solid var(--border, rgba(120,120,120,0.2));
    box-shadow: -4px 0 16px rgba(0,0,0,0.08);
    transform: translateX(100%); transition: transform 0.25s ease;
    overflow-y: auto; font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 16px;
}
#advisor-panel.advisor-open { transform: translateX(0); }
#advisor-panel h3 { margin: 0 0 12px; font-size: 15px; }
#advisor-panel h4 { margin: 0 0 8px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; opacity: 0.7; }
#advisor-panel-close { position: absolute; top: 12px; right: 14px; cursor: pointer; background: none; border: 0; font-size: 16px; color: inherit; }

.advisor-stepper { display: flex; justify-content: space-between; margin: 8px 0 18px; }
.advisor-step { flex: 1; text-align: center; font-size: 11px; opacity: 0.55; position: relative; }
.advisor-step span { display: block; width: 10px; height: 10px; border-radius: 50%; background: #9ca3af; margin: 0 auto 4px; }
.advisor-step-active { opacity: 1; font-weight: 600; }
.advisor-step-active span { background: var(--primary, #3b82f6); }
.advisor-step-done { opacity: 0.9; }
.advisor-step-done span { background: #22c55e; }

.advisor-card {
    border: 1px solid var(--border, rgba(120,120,120,0.25)); border-radius: calc(var(--radius, 10px) - 2px);
    padding: 10px 12px; margin-bottom: 10px;
}
.advisor-bar { height: 6px; border-radius: 999px; background: var(--muted, rgba(120,120,120,0.25)); overflow: hidden; margin-bottom: 6px; }
.advisor-bar-fill { height: 100%; background: var(--primary, #3b82f6); }
.advisor-kv { display: flex; justify-content: space-between; gap: 8px; padding: 2px 0; }
.advisor-kv span { opacity: 0.65; }
.advisor-kv b { text-align: right; }
.advisor-card-export button {
    display: block; width: 100%; margin-top: 6px; padding: 8px 10px;
    border: 1px solid rgba(120,120,120,0.35); border-radius: 8px;
    background: transparent; color: inherit; cursor: pointer; font: inherit;
}
#advisor-footer-info { margin-top: 6px; font-size: 11px; opacity: 0.6; }

/* Farben/Radius werden von der echten Chat-UI übernommen (dieselben CSS-Variablen,
   von ihr auf :root/.dark gesetzt) statt eigener Werte zu raten – dadurch passt sich
   die Maske automatisch an Light/Dark-Umschaltung *innerhalb* der Chat-UI an, nicht
   nur an die Betriebssystem-Einstellung. Position/Breite werden per JS (siehe
   positionEmptyOverlay) auf die tatsächliche Chat-Spalte ausgerichtet, damit die
   Maske wie ein Teil davon wirkt statt lose darüber zu schweben. */
#advisor-empty-overlay {
    position: fixed; top: 64px; left: 50%; transform: translateX(-50%);
    z-index: 9989; width: min(640px, calc(100vw - 32px));
    max-height: calc(100vh - 140px); overflow-y: auto;
    background: var(--popover, #fff); color: var(--popover-foreground, #111827);
    border: 1px solid var(--border, rgba(120,120,120,0.2));
    border-radius: var(--radius, 10px);
    box-shadow: 0 4px 16px rgba(0,0,0,0.08); padding: 18px 20px;
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.advisor-hidden { display: none !important; }
#advisor-empty-overlay h2 { grid-column: 1 / -1; margin: 0 0 6px; font-size: 17px; }
#advisor-empty-overlay p { grid-column: 1 / -1; margin: 0 0 12px; opacity: 0.75; }
#advisor-empty-overlay .advisor-hint { font-size: 12px; opacity: 0.6; margin-top: 8px; }
#advisor-empty-overlay .advisor-close {
    position: absolute; top: 10px; right: 12px; background: none; border: 0;
    cursor: pointer; font-size: 15px; color: inherit; opacity: 0.6;
}
#advisor-step-1 {
    display: grid; grid-template-columns: 1fr 1fr; gap: 4px 14px; margin-top: 4px;
}
#advisor-step-1 label {
    display: flex; flex-direction: column; gap: 3px; font-size: 12px; opacity: 0.85; margin-bottom: 8px;
}
#advisor-profile-form input, #advisor-profile-form select {
    font: inherit; font-size: 13px; padding: 7px 8px; border-radius: calc(var(--radius, 8px) - 2px);
    border: 1px solid var(--input, rgba(120,120,120,0.35)); background: transparent; color: inherit;
}
#advisor-profile-form input[type="range"] {
    width: 100%; padding: 0; border: 0; background: transparent; accent-color: var(--primary, #3b82f6); cursor: pointer;
}
#advisor-profile-form .advisor-form-actions { grid-column: 1 / -1; display: flex; align-items: center; gap: 12px; margin-top: 4px; }
#advisor-profile-form .advisor-primary-btn {
    padding: 9px 16px; border-radius: calc(var(--radius, 8px) - 2px); border: 0; cursor: pointer; font: inherit; font-weight: 600;
    background: var(--primary, #3b82f6); color: var(--primary-foreground, #fff);
}
#advisor-profile-form .advisor-primary-btn:hover { filter: brightness(0.92); }
#advisor-profile-form .advisor-skip-link {
    background: none; border: 0; color: var(--muted-foreground, inherit); opacity: 0.85; cursor: pointer; font: inherit; text-decoration: underline;
}
#advisor-step-2 { margin-top: 4px; }
#advisor-step-2 p { margin-bottom: 16px; }
.advisor-slider-field { margin: 0 0 18px; }
.advisor-slider-field label { display: block; font-size: 13px; margin-bottom: 8px; }
.advisor-slider-scale { display: flex; justify-content: space-between; font-size: 11px; opacity: 0.55; margin-top: 2px; }
.advisor-slider-value { margin-top: 6px; font-size: 13px; font-weight: 600; min-height: 18px; }
.advisor-slider-value.advisor-slider-untouched { font-weight: 400; opacity: 0.55; font-style: italic; }

/* ---- Visualisierungen im Beratungsstatus-Panel (Risiko-Skala, Verlauf,
   Portfolio-Balken) -----------------------------------------------------
   Datenfarben folgen der validierten Standard-Palette des dataviz-Skills
   (ordinal/kategorial, per Skript gegen Kontrast/CVD geprüft) statt der
   Marken-Akzentfarbe der Chat-UI – das ist bei Diagrammfarben bewusst so
   getrennt von den übrigen UI-Elementen (die var(--primary) etc. nutzen). */
.viz-gauge-title { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.viz-gauge-track { display: flex; gap: 2px; height: 16px; border-radius: 4px; overflow: hidden; }
.viz-gauge-seg { flex: 1; }
.viz-gauge-seg.viz-gauge-current { outline: 2px solid var(--popover-foreground, #111); outline-offset: -2px; }
.viz-gauge-ticks { display: flex; margin-top: 4px; }
.viz-gauge-ticks span { flex: 1; text-align: center; font-size: 10px; opacity: 0.55; }
.viz-gauge-ticks span.viz-tick-active { font-weight: 700; opacity: 1; }

.viz-history-empty { font-size: 12px; opacity: 0.6; font-style: italic; }
.viz-history-svg { width: 100%; height: 70px; display: block; }
.viz-grid { stroke: var(--border, #e1e0d9); stroke-width: 1; }
.viz-axis-label { font-size: 7px; fill: var(--muted-foreground, #898781); }

.viz-alloc-track { display: flex; gap: 2px; height: 22px; border-radius: 4px; overflow: hidden; margin-bottom: 8px; }
.viz-alloc-seg { display: flex; align-items: center; justify-content: center; }
.viz-alloc-seg span { font-size: 10px; color: #fff; text-shadow: 0 1px 1px rgba(0,0,0,0.35); }
.viz-legend { display: flex; flex-direction: column; gap: 4px; }
.viz-legend-row { display: flex; align-items: center; gap: 6px; font-size: 12px; }
.viz-legend-row span:nth-child(2) { flex: 1; opacity: 0.85; }
.viz-legend-row b { font-weight: 600; }
.viz-swatch { width: 10px; height: 10px; border-radius: 2px; flex: none; }
</style>

<div id="advisor-status-pill" data-state="loading"><span class="advisor-dot"></span><span id="advisor-status-text">Modell lädt…</span></div>

<div id="advisor-error-banner" role="alert" aria-live="assertive">
    <strong>Modellfehler</strong>
    <div id="advisor-error-banner-text"></div>
    <div class="advisor-actions">
        <button type="button" id="advisor-error-model-hint">Modell wechseln</button>
        <button type="button" onclick="document.getElementById('advisor-error-banner').style.display='none'">Schließen</button>
    </div>
</div>

<div id="advisor-panel-tab">Beratungsstatus</div>
<aside id="advisor-panel">
    <button id="advisor-panel-close" aria-label="Schließen">×</button>
    <h3>Beratungsstatus</h3>
    <div id="advisor-stepper" class="advisor-stepper"></div>
    <div id="advisor-cards"></div>
    <div id="advisor-footer-info"></div>
</aside>

<div id="advisor-empty-overlay" class="advisor-hidden">
    <button class="advisor-close" aria-label="Schließen">×</button>
    <form id="advisor-profile-form">
      <div id="advisor-step-1">
        <h2>Willkommen 👋</h2>
        <p>Trag hier kurz deine Eckdaten ein – im nächsten Schritt fragen wir deine Risikoeinstellung per Regler ab; alles Weitere klärt der Chat direkt im Anschluss mit dir.</p>
        <label>Anlageziel
            <input name="anlageziel" type="text" placeholder="z. B. Altersvorsorge, Vermögensaufbau">
        </label>
        <label>Zeithorizont (Jahre)
            <input name="zeithorizont_jahre" type="number" min="0" step="1">
        </label>
        <label>Alter
            <input name="alter" type="number" min="0" step="1">
        </label>
        <label>Land / Steuerkontext
            <input name="land_steuerkontext" type="text" value="Deutschland">
        </label>
        <label>Anlageerfahrung
            <select name="anlageerfahrung">
                <option value="">– bitte wählen –</option>
                <option value="keine">Keine</option>
                <option value="grundkenntnisse">Grundkenntnisse</option>
                <option value="fortgeschritten">Fortgeschritten</option>
                <option value="sehr_erfahren">Sehr erfahren</option>
            </select>
        </label>
        <label>Vorhandene Anlagen
            <input name="vorhandene_anlagen" type="text" placeholder="z. B. keine / ETF-Depot 10k">
        </label>
        <label>Depot vorhanden?
            <select name="depot_vorhanden">
                <option value="">– bitte wählen –</option>
                <option value="true">Ja</option>
                <option value="false">Nein</option>
            </select>
        </label>
        <label>Monatliche Sparrate (EUR)
            <input name="monatliche_sparrate_eur" type="number" min="0" step="1">
        </label>
        <label>Einmalbetrag (EUR)
            <input name="einmalbetrag_eur" type="number" min="0" step="1">
        </label>
        <label>Schulden
            <input name="schulden" type="text" placeholder="z. B. keine">
        </label>
        <label>Konsumschulden vorhanden?
            <select name="hat_konsumschulden">
                <option value="">– bitte wählen –</option>
                <option value="true">Ja</option>
                <option value="false">Nein</option>
            </select>
        </label>
        <label>Notgroschen (Monatsausgaben)
            <input name="notgroschen_monatsausgaben" type="number" min="0" step="1">
        </label>
        <div class="advisor-form-actions">
            <button type="submit" class="advisor-primary-btn">Weiter</button>
            <button type="button" id="advisor-skip-form" class="advisor-skip-link">Ohne Formular direkt im Chat starten</button>
        </div>
      </div>

      <div id="advisor-step-2" class="advisor-hidden">
        <h2>Risikoeinschätzung</h2>
        <p>Stell dir vor, dein Depot verliert innerhalb weniger Monate 20 % an Wert – 10.000 € wären dann noch 8.000 €. Positioniere dich mit den Reglern; nicht bewegte Regler beantwortet der Chat im Anschluss mit dir.</p>

        <div class="advisor-slider-field">
            <label for="advisor-slider-reaktion">Wie würdest du reagieren?</label>
            <input type="range" id="advisor-slider-reaktion" min="0" max="4" step="1" value="2">
            <div class="advisor-slider-scale"><span>Alles verkaufen</span><span>Nachkaufen</span></div>
            <div class="advisor-slider-value advisor-slider-untouched" id="advisor-slider-reaktion-label">Noch nicht ausgewählt</div>
        </div>

        <div class="advisor-slider-field">
            <label for="advisor-slider-verlust">Welchen zwischenzeitlichen Wertverlust könntest du emotional noch aushalten?</label>
            <input type="range" id="advisor-slider-verlust" min="0" max="80" step="5" value="20">
            <div class="advisor-slider-value advisor-slider-untouched" id="advisor-slider-verlust-label">Noch nicht ausgewählt</div>
        </div>

        <input type="hidden" name="reaktion_kursverlust_20_prozent" id="advisor-hidden-reaktion" value="">
        <input type="hidden" name="max_akzeptierter_verlust_prozent" id="advisor-hidden-verlust" value="">

        <div class="advisor-form-actions">
            <button type="button" id="advisor-step2-submit" class="advisor-primary-btn">Beratung starten</button>
            <button type="button" id="advisor-step2-back" class="advisor-skip-link">Zurück</button>
            <button type="button" id="advisor-step2-skip" class="advisor-skip-link">Diese Fragen lieber im Chat beantworten</button>
        </div>
      </div>
    </form>
</div>

<script>
(function () {
    const originalFetch = window.fetch;
    let currentChatId = 'default';
    let lastUsedModel = null;
    let modelCount = null;
    let pendingProfile = null;

    // Chat-ID grob aus der URL raten (Client-Routing der Chat-UI nutzt /{id});
    // wird beim ersten echten /api/chat-Request unten ohnehin durch die
    // tatsächliche ID aus dem Request-Body überschrieben/bestätigt.
    const pfadId = location.pathname.replace(/^\\/+/, '').trim();
    if (pfadId) currentChatId = pfadId;

    // ---- Status-Pille ---------------------------------------------------
    function setStatus(state, text) {
        const pill = document.getElementById('advisor-status-pill');
        if (!pill) return;
        pill.dataset.state = state;
        document.getElementById('advisor-status-text').textContent = text;
    }

    // ---- Fehler-Banner ----------------------------------------------------
    function showError(message) {
        const banner = document.getElementById('advisor-error-banner');
        const text = document.getElementById('advisor-error-banner-text');
        if (!banner || !text) return;
        text.textContent = message;
        banner.style.display = 'block';
        setStatus('error', 'Fehler');
        setTimeout(function () { setStatus('idle', 'Verbunden'); }, 8000);
    }

    function highlightModelSelector() {
        const candidate = document.querySelector(
            '[data-testid*="model" i], select, [role="combobox"], button[aria-haspopup="listbox"]'
        );
        if (candidate) {
            candidate.scrollIntoView({ behavior: 'smooth', block: 'center' });
            const prevOutline = candidate.style.outline;
            candidate.style.outline = '2px solid #3b82f6';
            setTimeout(function () { candidate.style.outline = prevOutline; }, 2500);
        } else {
            showError('Modell-Auswahl nicht gefunden – bitte oben in der Chat-UI prüfen, ob ein anderes Modell verfügbar ist.');
        }
    }

    // ---- Eingabehinweis (Platzhaltertext) ---------------------------------
    let hintApplied = false;
    function applyInputHint() {
        if (hintApplied) return;
        const ta = document.querySelector('textarea');
        if (ta) {
            ta.placeholder = 'z. B. "500 € monatlich" oder "10.000 € Einmalbetrag"';
            hintApplied = true;
        }
    }

    // ---- Willkommens-Maske an der echten Chat-Spalte ausrichten -----------
    // Statt über die gesamte Seite (inkl. Sidebar) zu zentrieren, wird die
    // Breite/Position der Eingabezeile gemessen (einziger stabiler Anker, den
    // wir kennen) und die Maske darauf ausgerichtet – dadurch wirkt sie wie
    // Teil der Chat-Spalte statt lose darüber zu schweben, und überlappt
    // nicht mehr mit Eingabefeld/Modell-Auswahl dahinter.
    function positionEmptyOverlay() {
        const overlay = document.getElementById('advisor-empty-overlay');
        const ta = document.querySelector('textarea');
        if (!overlay || !ta || overlay.classList.contains('advisor-hidden')) return;
        const anker = ta.closest('form') || ta;
        const rect = anker.getBoundingClientRect();
        if (rect.width < 200) return;
        overlay.style.left = rect.left + 'px';
        overlay.style.transform = 'none';
        overlay.style.width = rect.width + 'px';
        const top = 64;
        overlay.style.top = top + 'px';
        overlay.style.maxHeight = Math.max(200, rect.top - top - 16) + 'px';
    }
    window.addEventListener('resize', positionEmptyOverlay);

    // ---- Leerzustand / Schnellwahl -----------------------------------------
    // Chat-IDs, für die die Maske in dieser Seiten-Session nicht mehr gezeigt
    // werden soll (Nachricht gesendet ODER laut Server-Status nicht mehr leer
    // ODER vom Nutzer explizit geschlossen/übersprungen). Wird PRO Chat-ID
    // geführt statt global, damit ein neuer Chat trotzdem die Maske zeigt.
    const OHNE_FORMULAR = new Set();

    function setOverlaySichtbar(sichtbar) {
        const overlay = document.getElementById('advisor-empty-overlay');
        if (!overlay) return;
        if (sichtbar) {
            overlay.classList.remove('advisor-hidden');
            positionEmptyOverlay();
        } else {
            overlay.classList.add('advisor-hidden');
        }
    }

    function hideEmptyOverlay() {
        OHNE_FORMULAR.add(currentChatId);
        setOverlaySichtbar(false);
    }

    // Die Maske startet server-seitig versteckt (siehe HTML) und wird nur
    // eingeblendet, wenn die AKTUELL angezeigte Konversation laut Session-State
    // noch kein Profil hat. Die Chat-UI ist eine SPA: Zwischen Konversationen in
    // der Sidebar zu wechseln lädt die Seite nicht neu, daher muss diese Prüfung
    // jedes Mal erneut laufen, wenn sich `currentChatId` ändert (siehe Poller
    // unten) – nicht nur einmal beim initialen Laden. Ein globales
    // "einmal gesehen, nie wieder"-Flag (z. B. via localStorage) würde hier
    // fälschlich auch neue Chats unterdrücken, sobald irgendeine Konversation
    // jemals begonnen wurde.
    function aktualisiereEmptyOverlayFuerAktuellenChat() {
        if (OHNE_FORMULAR.has(currentChatId)) {
            setOverlaySichtbar(false);
            return;
        }
        originalFetch('/api/state/' + encodeURIComponent(currentChatId))
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (state) {
                const hatFortschritt = !!(state && state.profilFortschritt && state.profilFortschritt.erfasst > 0);
                if (hatFortschritt) {
                    OHNE_FORMULAR.add(currentChatId);
                    setOverlaySichtbar(false);
                } else {
                    setOverlaySichtbar(true);
                }
            })
            .catch(function () { setOverlaySichtbar(true); });
    }
    aktualisiereEmptyOverlayFuerAktuellenChat();

    // ---- Chat-Wechsel per SPA-Navigation erkennen --------------------------
    // Ein Klick auf eine andere Konversation in der Sidebar ändert die URL
    // (Client-Routing), lädt die Seite aber NICHT neu – ohne diese Prüfung
    // würden sowohl die Leerzustand-Maske als auch das Beratungsstatus-Panel
    // (falls offen) weiter die Daten der vorher aktiven Konversation zeigen.
    let letzterPfad = location.pathname;
    setInterval(function () {
        applyInputHint();
        positionEmptyOverlay();
        const pfad = location.pathname.replace(/^\\/+/, '').trim();
        if (pfad === letzterPfad.replace(/^\\/+/, '').trim()) return;
        letzterPfad = location.pathname;
        currentChatId = pfad || 'default';
        aktualisiereEmptyOverlayFuerAktuellenChat();
        const offenesPanel = document.getElementById('advisor-panel');
        if (offenesPanel && offenesPanel.classList.contains('advisor-open')) {
            refreshStatePanel();
        }
    }, 1000);

    function setNativeValue(el, value) {
        const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(el, value);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function fillAndSubmit(text) {
        const ta = document.querySelector('textarea');
        if (!ta) return;
        ta.focus();
        setNativeValue(ta, text);
        setTimeout(function () {
            const form = ta.closest('form');
            if (form && form.requestSubmit) {
                try { form.requestSubmit(); return; } catch (e) {}
            }
            ta.dispatchEvent(new KeyboardEvent('keydown', {
                key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true
            }));
        }, 50);
    }

    // Zahlen-/Bool-Felder passend zum UserProfile-Schema konvertieren (siehe profile.py);
    // Freitext-Felder unverändert, leere Felder werden weggelassen (nur Angegebenes senden).
    const NUMMERN_FELDER = ['zeithorizont_jahre', 'alter', 'monatliche_sparrate_eur', 'einmalbetrag_eur', 'notgroschen_monatsausgaben', 'max_akzeptierter_verlust_prozent'];
    const BOOL_FELDER = ['depot_vorhanden', 'hat_konsumschulden'];

    function serializeProfileForm(form) {
        const daten = {};
        new FormData(form).forEach(function (wert, feld) {
            if (wert === '') return;
            if (NUMMERN_FELDER.indexOf(feld) !== -1) daten[feld] = Number(wert);
            else if (BOOL_FELDER.indexOf(feld) !== -1) daten[feld] = wert === 'true';
            else daten[feld] = wert;
        });
        return daten;
    }

    const profileForm = document.getElementById('advisor-profile-form');
    const step1El = document.getElementById('advisor-step-1');
    const step2El = document.getElementById('advisor-step-2');
    function goToStep2() { step1El.classList.add('advisor-hidden'); step2El.classList.remove('advisor-hidden'); positionEmptyOverlay(); }
    function goToStep1() { step2El.classList.add('advisor-hidden'); step1El.classList.remove('advisor-hidden'); positionEmptyOverlay(); }

    // Schritt 1 -> Schritt 2 (noch kein Absenden an den Chat).
    profileForm.addEventListener('submit', function (e) {
        e.preventDefault();
        goToStep2();
    });

    function finalizeAndSend() {
        // Die zwei Risiko-Felder kommen über die verborgenen Inputs der Slider mit
        // (siehe unten) und landen dadurch automatisch mit im selben FormData-Pass;
        // ein unberührter Slider hinterlässt dort einen leeren Wert und wird von
        // serializeProfileForm() genau wie jedes andere leere Feld übersprungen.
        const daten = serializeProfileForm(profileForm);
        hideEmptyOverlay();
        if (Object.keys(daten).length === 0) {
            fillAndSubmit('Hallo, ich möchte eine Anlageberatung starten.');
            return;
        }
        // Wird erst nach dem Absenden an /api/profile/{chat_id} übertragen, sobald die
        // eigentliche Chat-Anfrage die echte Konversations-ID preisgibt (siehe Fetch-Interception).
        pendingProfile = daten;
        const hatRisikoAntwort = ('reaktion_kursverlust_20_prozent' in daten) || ('max_akzeptierter_verlust_prozent' in daten);
        const text = hatRisikoAntwort
            ? 'Ich habe meine Eckdaten im Formular eingetragen und meine Risikoeinstellung per Regler angegeben. Bitte bestätige kurz meine Risikoeinschätzung bzw. frag gezielt nach, falls dir dazu noch etwas unklar ist, und mach dann mit der Beratung weiter.'
            : 'Ich habe meine Eckdaten gerade im Formular eingetragen. Bitte geh kurz die verbleibenden Punkte durch und mach dann mit der Beratung weiter.';
        fillAndSubmit(text);
    }

    // ---- Risiko-Slider (Schritt 2) -----------------------------------------
    // Reihenfolge/Werte exakt wie ReaktionKursverlust in profile.py und die
    // Punktetabelle in risk.py::_score_risikobereitschaft.
    const REAKTION_OPTIONEN = [
        { wert: 'alles_verkaufen', label: 'Alles verkaufen' },
        { wert: 'teilweise_verkaufen', label: 'Einen Teil verkaufen' },
        { wert: 'beunruhigt_halten', label: 'Beunruhigt halten' },
        { wert: 'gelassen_halten', label: 'Gelassen halten' },
        { wert: 'nachkaufen', label: 'Nachkaufen' },
    ];
    const reaktionSlider = document.getElementById('advisor-slider-reaktion');
    const reaktionHidden = document.getElementById('advisor-hidden-reaktion');
    const reaktionLabel = document.getElementById('advisor-slider-reaktion-label');
    reaktionSlider.addEventListener('input', function () {
        const opt = REAKTION_OPTIONEN[Number(reaktionSlider.value)];
        reaktionHidden.value = opt.wert;
        reaktionLabel.textContent = opt.label;
        reaktionLabel.classList.remove('advisor-slider-untouched');
    });

    const verlustSlider = document.getElementById('advisor-slider-verlust');
    const verlustHidden = document.getElementById('advisor-hidden-verlust');
    const verlustLabel = document.getElementById('advisor-slider-verlust-label');
    verlustSlider.addEventListener('input', function () {
        verlustHidden.value = verlustSlider.value;
        verlustLabel.textContent = verlustSlider.value + ' %';
        verlustLabel.classList.remove('advisor-slider-untouched');
    });

    document.getElementById('advisor-step2-submit').addEventListener('click', finalizeAndSend);
    document.getElementById('advisor-step2-back').addEventListener('click', goToStep1);
    document.getElementById('advisor-step2-skip').addEventListener('click', function () {
        // Angefasste, aber bewusst übersprungene Slider nicht mitsenden.
        reaktionHidden.value = '';
        verlustHidden.value = '';
        finalizeAndSend();
    });

    document.getElementById('advisor-skip-form').addEventListener('click', function () {
        hideEmptyOverlay();
    });
    document.querySelector('#advisor-empty-overlay .advisor-close').addEventListener('click', hideEmptyOverlay);
    document.getElementById('advisor-error-model-hint').addEventListener('click', highlightModelSelector);

    // ---- Beratungsstatus-Panel ---------------------------------------------
    const PHASEN = [
        { key: 'profil', label: 'Profil' },
        { key: 'risiko', label: 'Risiko' },
        { key: 'strategie', label: 'Strategie' },
        { key: 'abgeschlossen', label: 'Ergebnis' },
    ];

    function esc(v) {
        return String(v).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function renderStepper(phase) {
        const idx = PHASEN.findIndex(function (p) { return p.key === phase; });
        return PHASEN.map(function (p, i) {
            const state = i < idx ? 'done' : (i === idx ? 'active' : 'todo');
            return '<div class="advisor-step advisor-step-' + state + '"><span></span>' + p.label + '</div>';
        }).join('');
    }

    // ---- Diagramm-Farben (dataviz-Skill, validiert per validate_palette.js) ----
    function istDarkModus() { return document.documentElement.classList.contains('dark'); }

    // Ordinale Rampe (Risikoklasse 1-5, EINE Farbfamilie, monoton heller->dunkler),
    // getrennt nach Modus optimiert (siehe Kontrast-/CVD-Vorgaben je Oberfläche).
    const RISIKO_RAMPE_HELL = ['#86b6ef', '#5598e7', '#2a78d6', '#1c5cab', '#0d366b'];
    const RISIKO_RAMPE_DUNKEL = ['#b7d3f6', '#6da7ec', '#3987e5', '#256abf', '#184f95'];
    const RISIKO_KLASSEN = [
        { n: 1, label: 'Sehr defensiv' },
        { n: 2, label: 'Defensiv' },
        { n: 3, label: 'Ausgewogen' },
        { n: 4, label: 'Wachstumsorientiert' },
        { n: 5, label: 'Offensiv' },
    ];

    // Kategoriale Slots (Identität der Anlageklasse), feste Reihenfolge je Baustein
    // -- Farbe folgt der Kategorie, nicht ihrem Anteil, auch wenn sich die Werte
    // bei einer Neuberechnung ändern.
    const ALLOKATION_SLOTS = {
        aktien_welt_industrielaender: { hell: '#2a78d6', dunkel: '#3987e5', label: 'Aktien Welt (Industrieländer)' },
        aktien_schwellenlaender: { hell: '#eb6834', dunkel: '#d95926', label: 'Aktien Schwellenländer' },
        anleihen_eur_investment_grade: { hell: '#1baf7a', dunkel: '#199e70', label: 'Anleihen (EUR, Investment Grade)' },
        geldmarkt_tagesgeld: { hell: '#eda100', dunkel: '#c98500', label: 'Geldmarkt / Tagesgeld' },
        gold: { hell: '#e87ba4', dunkel: '#d55181', label: 'Gold' },
    };
    const ALLOKATION_REIHENFOLGE = Object.keys(ALLOKATION_SLOTS);

    function renderRiskGauge(risiko) {
        const rampe = istDarkModus() ? RISIKO_RAMPE_DUNKEL : RISIKO_RAMPE_HELL;
        const aktuelle = risiko ? risiko.risikoklasse : null;
        const segs = RISIKO_KLASSEN.map(function (k) {
            const aktiv = aktuelle === k.n;
            return '<div class="viz-gauge-seg' + (aktiv ? ' viz-gauge-current' : '') + '" style="background:' + rampe[k.n - 1] + '" title="Klasse ' + k.n + ' – ' + k.label + '"></div>';
        }).join('');
        const ticks = RISIKO_KLASSEN.map(function (k) {
            return '<span class="' + (aktuelle === k.n ? 'viz-tick-active' : '') + '">' + k.n + '</span>';
        }).join('');
        const aktuelleKlasse = RISIKO_KLASSEN.find(function (k) { return k.n === aktuelle; });
        const titel = aktuelleKlasse ? ('Klasse ' + aktuelle + ' – ' + esc(aktuelleKlasse.label)) : 'Noch nicht berechnet';
        return (
            '<div class="viz-gauge-title">' + titel + '</div>' +
            '<div class="viz-gauge-track">' + segs + '</div>' +
            '<div class="viz-gauge-ticks">' + ticks + '</div>'
        );
    }

    function renderRiskHistory(verlauf) {
        if (!verlauf || verlauf.length === 0) return '';
        if (verlauf.length === 1) {
            return '<div class="viz-history-empty">Noch keine Änderung im bisherigen Gesprächsverlauf.</div>';
        }
        const w = 260, h = 70, padL = 18, padR = 8, padT = 8, padB = 10;
        const plotW = w - padL - padR, plotH = h - padT - padB;
        const n = verlauf.length;
        const farbe = istDarkModus() ? '#3987e5' : '#2a78d6';
        const oberflaeche = istDarkModus() ? '#1a1a19' : '#fcfcfb';
        const punkte = verlauf.map(function (v, i) {
            const x = padL + (n === 1 ? 0 : (i / (n - 1)) * plotW);
            const y = padT + plotH - ((v.risikoklasse - 1) / 4) * plotH;
            return { x: x, y: y, v: v };
        });
        const pfad = punkte.map(function (p, i) { return (i === 0 ? 'M' : 'L') + p.x.toFixed(1) + ',' + p.y.toFixed(1); }).join(' ');
        const gitter = [1, 2, 3, 4, 5].map(function (k) {
            const y = padT + plotH - ((k - 1) / 4) * plotH;
            return '<line x1="' + padL + '" y1="' + y.toFixed(1) + '" x2="' + (w - padR) + '" y2="' + y.toFixed(1) + '" class="viz-grid" />' +
                '<text x="0" y="' + (y + 2.5).toFixed(1) + '" class="viz-axis-label">' + k + '</text>';
        }).join('');
        const punkteHtml = punkte.map(function (p, i) {
            const letzter = i === punkte.length - 1;
            const titel = 'Berechnung ' + (i + 1) + ': Klasse ' + p.v.risikoklasse + (p.v.zeitpunkt ? (' um ' + esc(p.v.zeitpunkt)) : '');
            return '<circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) + '" r="' + (letzter ? 5 : 4) + '" fill="' + farbe + '" stroke="' + oberflaeche + '" stroke-width="2"><title>' + titel + '</title></circle>';
        }).join('');
        return (
            '<svg class="viz-history-svg" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="xMidYMid meet">' +
            gitter +
            '<path d="' + pfad + '" fill="none" stroke="' + farbe + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />' +
            punkteHtml +
            '</svg>'
        );
    }

    function renderAllocationBar(allokation) {
        if (!allokation) return '';
        const dunkel = istDarkModus();
        const keys = ALLOKATION_REIHENFOLGE.filter(function (k) { return allokation[k] != null && allokation[k] > 0; });
        if (!keys.length) return '';
        const segs = keys.map(function (k) {
            const pct = allokation[k];
            const farbe = dunkel ? ALLOKATION_SLOTS[k].dunkel : ALLOKATION_SLOTS[k].hell;
            const beschriftung = pct >= 12 ? ('<span>' + Math.round(pct) + ' %</span>') : '';
            return '<div class="viz-alloc-seg" style="flex:' + pct + '; background:' + farbe + '" title="' + esc(ALLOKATION_SLOTS[k].label) + ': ' + pct + ' %">' + beschriftung + '</div>';
        }).join('');
        const legende = keys.map(function (k) {
            const farbe = dunkel ? ALLOKATION_SLOTS[k].dunkel : ALLOKATION_SLOTS[k].hell;
            return '<div class="viz-legend-row"><span class="viz-swatch" style="background:' + farbe + '"></span>' +
                '<span>' + esc(ALLOKATION_SLOTS[k].label) + '</span><b>' + allokation[k] + ' %</b></div>';
        }).join('');
        return '<div class="viz-alloc-track">' + segs + '</div><div class="viz-legend">' + legende + '</div>';
    }

    function renderCards(state) {
        const parts = [];
        const fp = state.profilFortschritt || { erfasst: 0, gesamt: 0 };
        const pct = fp.gesamt ? Math.round((fp.erfasst / fp.gesamt) * 100) : 0;
        const profilZeilen = Object.entries(state.profil || {}).map(function (kv) {
            return '<div class="advisor-kv"><span>' + esc(kv[0]) + '</span><b>' + esc(kv[1]) + '</b></div>';
        }).join('');
        parts.push(
            '<div class="advisor-card"><h4>Profil</h4>' +
            '<div class="advisor-bar"><div class="advisor-bar-fill" style="width:' + pct + '%"></div></div>' +
            '<div class="advisor-kv"><span>Fortschritt</span><b>' + fp.erfasst + ' / ' + fp.gesamt + '</b></div>' +
            profilZeilen + '</div>'
        );

        if (state.risiko) {
            const r = state.risiko;
            parts.push(
                '<div class="advisor-card"><h4>Risiko</h4>' +
                renderRiskGauge(r) +
                '<div class="advisor-kv" style="margin-top:8px"><span>Aktienquote empfohlen</span><b>' + Math.round((r.aktienquote_empfohlen || 0) * 100) + ' %</b></div>' +
                '</div>'
            );
        }

        if (state.risikoVerlauf && state.risikoVerlauf.length > 1) {
            parts.push(
                '<div class="advisor-card"><h4>Risiko im Gesprächsverlauf</h4>' +
                renderRiskHistory(state.risikoVerlauf) +
                '</div>'
            );
        }

        if (state.strategie) {
            parts.push(
                '<div class="advisor-card"><h4>Portfolio</h4>' +
                renderAllocationBar(state.strategie.allokation_prozent) +
                '</div>'
            );
        }

        if (state.umschichtungsplan && !state.umschichtungsplan.fehler) {
            const u = state.umschichtungsplan;
            parts.push(
                '<div class="advisor-card"><h4>Umschichtungsplan</h4>' +
                '<div class="advisor-kv"><span>Handelsvolumen</span><b>' + esc(u.handelsvolumen_eur) + ' €</b></div>' +
                '<div class="advisor-kv"><span>Gebühren</span><b>' + esc(u.gebuehren_summe_eur) + ' €</b></div>' +
                '<div class="advisor-kv"><span>Geschätzte Steuer</span><b>' + esc(u.geschaetzte_steuer_summe_eur) + ' €</b></div>' +
                '</div>'
            );
        }

        if (state.phase === 'abgeschlossen') {
            parts.push(
                '<div class="advisor-card advisor-card-export"><h4>Export</h4>' +
                '<button id="advisor-export-md">Als Markdown herunterladen</button>' +
                '<button id="advisor-export-pdf">Als PDF speichern</button>' +
                '</div>'
            );
        }
        return parts.join('');
    }

    function renderFooterInfo() {
        const footer = document.getElementById('advisor-footer-info');
        if (!footer) return;
        const modellZeile = lastUsedModel ? ('Modell: ' + lastUsedModel) : 'Modell: Server-Standard';
        const anzahlZeile = (modelCount != null) ? (modelCount + ' Modell(e) verfügbar') : '';
        footer.textContent = [modellZeile, anzahlZeile].filter(Boolean).join(' · ');
    }

    async function refreshStatePanel() {
        try {
            const res = await originalFetch('/api/state/' + encodeURIComponent(currentChatId));
            if (!res.ok) return;
            const state = await res.json();
            document.getElementById('advisor-stepper').innerHTML = renderStepper(state.phase);
            document.getElementById('advisor-cards').innerHTML = renderCards(state);
            renderFooterInfo();

            const exportMd = document.getElementById('advisor-export-md');
            if (exportMd) exportMd.addEventListener('click', function () {
                const a = document.createElement('a');
                a.href = '/api/export/' + encodeURIComponent(currentChatId);
                a.download = 'beratung.md';
                document.body.appendChild(a);
                a.click();
                a.remove();
            });
            const exportPdf = document.getElementById('advisor-export-pdf');
            if (exportPdf) exportPdf.addEventListener('click', function () {
                window.open('/api/export/' + encodeURIComponent(currentChatId) + '?format=html', '_blank');
            });
        } catch (e) {}
    }

    const tab = document.getElementById('advisor-panel-tab');
    const panel = document.getElementById('advisor-panel');
    tab.addEventListener('click', function () {
        panel.classList.add('advisor-open');
        refreshStatePanel();
    });
    document.getElementById('advisor-panel-close').addEventListener('click', function () {
        panel.classList.remove('advisor-open');
    });

    originalFetch('/api/configure').then(function (r) { return r.json(); }).then(function (cfg) {
        modelCount = (cfg.models || []).length;
        setStatus('idle', 'Verbunden');
        renderFooterInfo();
    }).catch(function () { setStatus('idle', 'Verbunden'); });

    // ---- Fetch-Interception: Chat-ID, Status, Fehler, Panel-Refresh -------
    window.fetch = async function (...args) {
        const request = args[0];
        const init = args[1] || {};
        const url = typeof request === 'string' ? request : (request && request.url) || '';
        const isChat = url.includes('/api/chat');

        if (isChat) {
            try {
                const bodyText = typeof init.body === 'string' ? init.body : null;
                if (bodyText) {
                    const parsed = JSON.parse(bodyText);
                    if (parsed && parsed.id) currentChatId = parsed.id;
                    if (parsed && parsed.model) lastUsedModel = parsed.model;
                }
            } catch (e) {}
            if (pendingProfile) {
                const formData = pendingProfile;
                pendingProfile = null;
                try {
                    await originalFetch('/api/profile/' + encodeURIComponent(currentChatId), {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(formData),
                    });
                } catch (e) {}
            }
            hideEmptyOverlay();
            setStatus('working', 'Arbeitet…');
        }

        try {
            const response = await originalFetch.apply(this, args);
            if (isChat) {
                if (!response.ok) {
                    const clone = response.clone();
                    let message = `Fehler ${response.status}`;
                    try {
                        const payload = await clone.json();
                        message = payload.error || payload.detail || message;
                    } catch (error) {
                        try { message = await clone.text(); } catch (_) {}
                    }
                    showError(message);
                } else {
                    setStatus('idle', 'Verbunden');
                    // Antwort-Stream im Hintergrund mitlesen (eigene Kopie, stört
                    // die App nicht), um nach Abschluss den Status zu aktualisieren.
                    response.clone().body?.getReader && (async function () {
                        try {
                            const reader = response.clone().body.getReader();
                            while (true) {
                                const chunk = await reader.read();
                                if (chunk.done) break;
                            }
                        } catch (e) {}
                        refreshStatePanel();
                    })();
                }
            }
            return response;
        } catch (error) {
            if (isChat) showError('Netzwerkfehler oder Modell-Endpoint nicht erreichbar.');
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
        content = (await _get_ui_html(None)).decode("utf-8")
        content = _inject_ui_enhancements(content)
        # kein Caching: der HTML-Wrapper enthält unseren eigenen, sich häufig
        # ändernden UI-Code (siehe _inject_ui_enhancements); die eigentlichen
        # CDN-Assets (JS/CSS) sind ohnehin über ihre eigenen, inhaltsadressierten
        # URLs dauerhaft cachebar und von dieser Einstellung unberührt.
        return HTMLResponse(content=content, headers={"Cache-Control": "no-cache"})

    async def configure_frontend(request: Request) -> Response:
        return JSONResponse(
            {
                "models": [m.model_dump(by_alias=True) for m in model_infos],
                "builtinTools": [],
            }
        )

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "sessions": len(sessions)})

    async def get_state(request: Request) -> Response:
        deps = sessions.get(request.path_params["chat_id"])
        return JSONResponse(_session_state(deps))

    async def post_profile(request: Request) -> Response:
        """Übernimmt mehrere Profilfelder auf einmal (Formular der Web-UI, siehe
        `_inject_ui_enhancements`); validiert wie `speichere_profil_mehrere` in
        agent.py, aber ohne den Umweg über das LLM."""
        deps = sessions.get(request.path_params["chat_id"])
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "Ungültiges JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "Erwarte ein JSON-Objekt mit Profilfeldern"}, status_code=400)

        payload.pop("risikoklasse", None)  # wird berechnet, nie vom Client gesetzt
        daten = deps.profile.model_dump()
        daten.update(payload)
        try:
            deps.profile = UserProfile.model_validate(daten)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Ungültige Profildaten: {e}"}, status_code=400)

        sessions.speichere(request.path_params["chat_id"])
        return JSONResponse(_session_state(deps))

    async def get_export(request: Request) -> Response:
        chat_id = request.path_params["chat_id"]
        deps = sessions.get(chat_id)
        markdown = _export_markdown(deps)
        if request.query_params.get("format") == "html":
            return HTMLResponse(_export_print_html(markdown))
        return PlainTextResponse(
            markdown,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="beratung-{_dateiname(chat_id)}.md"'
            },
        )

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
                response = await VercelAIAdapter[AdvisorDeps, str].dispatch_request(
                    request,
                    agent=agent,
                    model=model_ref,
                    deps=deps,
                )
                # Die Antwort streamt: der eigentliche Agenten-Lauf (inkl. Tool-Aufrufen,
                # die `deps` mutieren) passiert erst, während Starlette den Body NACH
                # dieser Funktion ausliefert – ein `finally` hier würde zu früh
                # persistieren. Ein Background-Task läuft dagegen garantiert erst,
                # nachdem die komplette Antwort gesendet wurde.
                vorheriger_task = response.background

                async def _persistieren_nach_stream(vorheriger: Any = vorheriger_task) -> None:
                    if vorheriger is not None:
                        await vorheriger()
                    sessions.speichere(chat_id)

                response.background = BackgroundTask(_persistieren_nach_stream)
                return response
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
                        # Bekannter, intermittierender Gemini-Fehler bei mehrstufigen
                        # Tool-Calls über LiteLLM/Vertex AI (v. a. Preview-Modelle):
                        # dieselbe Anfrage schlägt mal fehl, mal nicht – ein erneuter
                        # Versuch behebt es oft, ist aber kein sicherer Fix.
                        "thought signature",
                    )
                )
                if attempt <= len(_CHAT_RETRY_DELAYS_S) and retryable:
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
                elif "thought signature" in err_str.lower():
                    status = 502
                    user_msg = (
                        "Bekanntes, unregelmäßiges Problem des Gemini-Modells bei mehrstufigen "
                        "Tool-Aufrufen (auch nach mehreren Versuchen). Bitte erneut senden oder "
                        "auf ein anderes Modell wechseln."
                    )
                elif "BadRequestError" in err_str or "400" in err_str:
                    status = 400
                    user_msg = "Ungültige Anfrage an das Modell (400). Bitte Eingabe prüfen."

                print("Model proxy error:", file=sys.stderr)
                print(traceback.format_exc(), file=sys.stderr)

                # Nicht-streamende Fehlerantwort: der Agenten-Lauf ist an dieser
                # Stelle bereits vollständig beendet (fehlgeschlagen), etwaige
                # Tool-Mutationen vor dem Fehler liegen also schon vor.
                sessions.speichere(chat_id)
                return JSONResponse({"error": user_msg, "detail": err_str}, status_code=status)

        # Praktisch unerreichbar (die Schleife kehrt in jedem Zweig zurück), aber
        # als Sicherheitsnetz: die Route muss immer eine Response liefern.
        print("Unexpected model error fallback:", file=sys.stderr)
        sessions.speichere(chat_id)
        return JSONResponse(
            {
                "error": "Unbekannter Modellfehler.",
                "detail": str(last_error) if last_error else "keine Antwort vom Modell",
            },
            status_code=502,
        )

    api = Starlette(
        routes=[
            Route("/chat", options_chat, methods=["OPTIONS"]),
            Route("/chat", post_chat, methods=["POST"]),
            Route("/configure", configure_frontend, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/state/{chat_id}", get_state, methods=["GET"]),
            Route("/export/{chat_id}", get_export, methods=["GET"]),
            Route("/profile/{chat_id}", post_profile, methods=["POST"]),
        ]
    )

    app = Starlette(routes=[Mount("/api", app=api)])
    app.router.add_route("/", index, methods=["GET"])
    app.router.add_route("/{id}", index, methods=["GET"])
    app.state.sessions = sessions
    return app
