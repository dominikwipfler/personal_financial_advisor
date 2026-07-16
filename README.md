# Persönlicher Finanzberater-Chatbot

Ein deutschsprachiger Chatbot, der Nutzer per Dialog profiliert (Ziele, Kapital,
Risikobereitschaft, Rahmendaten), aktuell per Web-Recherche passende, breit
gestreute Anlageprodukte identifiziert und daraus eine konkrete, nachvollziehbare
und personalisierte Anlagestrategie ableitet – inklusive Asset-Allokation in
Prozent, Produktvorschlägen, Sparplan-Aufteilung und Begründung je Baustein.

> **Wichtiger Hinweis:** Dieses Projekt ist ein Hochschulprojekt und **keine
> zugelassene Anlage-, Steuer- oder Rechtsberatung**. Alle Ausgaben des Bots
> sind allgemeine Informationen zur eigenen Entscheidungsfindung, ohne
> Garantien oder Renditeversprechen. Kapitalanlagen können zu Verlusten führen.

## Features

- **Nutzer-Profiling per Dialog** – der Bot fragt Schritt für Schritt (nicht
  alles auf einmal): Anlageziel und Zeithorizont, vorhandene Anlagen und Depot,
  monatliche Sparrate und Einmalbetrag, Schulden und Notgroschen, Alter,
  Land/Steuerkontext und Anlageerfahrung.
- **Szenariobasierte Risikoprofilierung** – statt „hoch/mittel/niedrig“ fragt
  der Bot u. a. die Reaktion auf einen hypothetischen 20-%-Kursverlust und die
  maximal tragbare zwischenzeitliche Verlusthöhe ab. Risikobereitschaft
  (subjektiv) und Risikotragfähigkeit (objektiv) werden getrennt bewertet;
  die schwächere Dimension begrenzt (Vorsichtsprinzip).
- **Session-State** – das Profil wird serverseitig gespeichert; bereits
  beantwortete Fragen werden nicht erneut gestellt, und der Bot baut im
  weiteren Gespräch darauf auf.
- **Aktuelle Web-Recherche** – Websuche (DuckDuckGo, ohne API-Key),
  Seiten-Abruf und Marktdaten von Yahoo Finance (Kurse, Rendite-Historie,
  Volatilität, Kosten) zur Auswahl kostengünstiger, breit gestreuter Produkte.
- **Quantitative Strategie-Engine** – Aktienquote als Bernoulli-/Markowitz-
  Nutzenoptimum `U = E(x) − a·Var(x)` mit horizont- und liquiditätsabhängigen
  Kappungen; daraus Asset-Allokation in Prozent, Sparplan-Aufteilung der
  Monatsrate (mit Mindestraten) und Aufteilung des Einmalbetrags.
- **Umschichtungsplan mit Gebühren** – für Nutzer mit Bestandsdepot berechnet
  der Bot konkrete Kauf- und Verkaufsempfehlungen vom Ist-Depot zur
  Ziel-Allokation: „neues Geld zuerst" (minimiert Verkäufe, Gebühren und
  Steuerrealisierung), Verkäufe nur oberhalb einer Abweichungsschwelle,
  Ordergebühren (Prozent + Mindestgebühr) je Trade ausgewiesen,
  Steuerhinweis bei realisierten Gewinnen; Fremdpositionen („sonstiges")
  werden nie automatisch zum Verkauf gesetzt.
- **Verständliche Erklärungen** – Diversifikation, Risiko/Rendite, Zeithorizont
  und Rebalancing werden begründet und auf Deutsch erklärt.
- **Disclaimer eingebaut** – zu Gesprächsbeginn und am Ende jeder Strategie.

## Architektur

Basis ist das **chatbot-pydanticai-template** (Begründung siehe
[Entscheidungen](#entscheidungen)): ein Pydantic-AI-Agent, der über
`agent.to_web()` die offizielle Pydantic AI Chat UI ausliefert.

```mermaid
flowchart TB
    subgraph Browser
        UI["Pydantic AI Chat UI<br/>(React, via CDN, Streaming)"]
    end

    subgraph Server["Starlette-App (uvicorn) – advisor.app"]
        AGENT["Pydantic-AI-Agent<br/>advisor.agent<br/>System-Prompt: advisor.prompts<br/>(Phasen: Profiling → Risiko → Recherche → Strategie)"]
        DEPS["AdvisorDeps (Session-State)<br/>UserProfile – advisor.profile"]

        subgraph Tools
            T1["speichere_profil /<br/>zeige_profil /<br/>profil_zuruecksetzen"]
            T2["ermittle_risikoprofil_tool<br/>(advisor.risk)"]
            T3["erstelle_strategie_tool<br/>(advisor.strategy)"]
            T4["web_suche / lese_webseite /<br/>marktdaten (advisor.research)"]
        end
    end

    subgraph Extern
        LLM["LLM-Provider<br/>(OpenAI direkt oder LiteLLM-Proxy)"]
        WEB["Web (DuckDuckGo,<br/>justETF & Co.)"]
        YF["Yahoo Finance"]
    end

    UI <-->|"Vercel AI Data Stream<br/>/api/chat"| AGENT
    AGENT <--> LLM
    AGENT --> T1 & T2 & T3 & T4
    T1 <--> DEPS
    T2 <--> DEPS
    T3 <--> DEPS
    T4 --> WEB
    T4 --> YF
```

**UI-Fluss / Beratungsprozess** (angelehnt an den Portfolio-Management-Prozess
aus dem Finanzmanagement-Skript, Kap. 1&2):

1. **Begrüßung + Disclaimer** → 2. **Profilierung** (eine Frage pro Nachricht;
   jede Antwort wird sofort via `speichere_profil` in den Session-State
   geschrieben; der aktuelle Profilstand wird dem Agenten in jede Anfrage
   injiziert, sodass nichts doppelt gefragt wird) → 3. **Risikoprofil**
   (Berechnung + verständliche Erklärung, Bestätigung durch Nutzer) →
   4. **Recherche** (aktuelle ETFs/Produkte, Kosten, Marktdaten) →
   5. **Strategie** (Allokation, Produkte, Sparplan, Begründungen, Hinweise,
   Disclaimer) → 6. **Rückfragen/Anpassungen** (Profilupdates → Neuberechnung).

### Modulübersicht

| Modul | Aufgabe |
|---|---|
| `src/advisor/config.py` | Modell-/Provider-Konfiguration aus `.env` (Pattern aus dem Template: OpenAI direkt oder LiteLLM-Proxy) |
| `src/advisor/prompts.py` | Deutscher System-Prompt: Rolle, Phasenmodell, Regeln (Disclaimer, keine Garantien, keine erfundenen ISINs) |
| `src/advisor/profile.py` | `UserProfile` (Pydantic) + `AdvisorDeps` (Session-State) |
| `src/advisor/risk.py` | Risikoprofilierung: Scores, Risikoklasse 1–5, Risikoaversionsparameter `a`, nutzenoptimale Aktienquote mit Kappungen |
| `src/advisor/strategy.py` | Strategische + taktische Asset-Allokation, Sparplan- und Einmalbetrags-Aufteilung, Hinweise (Notgroschen, Tilgung, Rebalancing) |
| `src/advisor/rebalancing.py` | Umschichtungsplan: Ist-Depot → Ziel-Allokation mit Ordergebühren, Handels-Schwellen und „neues Geld zuerst"-Prinzip |
| `src/advisor/research.py` | Websuche (ddgs), Seitenabruf (httpx + BeautifulSoup), Marktdaten (yfinance) |
| `src/advisor/agent.py` | Agent-Verdrahtung: Modell, Instructions, Tool-Registrierung (dünne Adapter um die Fachmodule) |
| `src/advisor/app.py` | Starlette-App via `agent.to_web()` – serviert Chat-UI und Streaming-API |

**Designprinzip:** Das Zahlenwerk (Risikoklasse, Allokation, Sparplan) wird
**deterministisch in Python** berechnet – das LLM interpretiert, erklärt und
recherchiert, erfindet aber keine Prozentsätze. Konkrete Produktvorschläge
(Stufe „Titelauswahl“) kommen ausschließlich aus der aktuellen Web-Recherche.

## Setup

Voraussetzungen: Python ≥ 3.10, [uv](https://docs.astral.sh/uv/), ein API-Key
für den gewählten LLM-Provider.

```bash
git clone https://github.com/dominikwipfler/personal_financial_advisor.git
cd personal_financial_advisor

# Abhängigkeiten installieren
uv sync

# API-Key konfigurieren (Pattern aus dem Template)
cp .env.example .env
# .env editieren: OPENAI_API_KEY=sk-... (und optional ADVISOR_MODEL)

# Starten
uv run uvicorn advisor.app:app --reload
```

Danach <http://localhost:8000> öffnen – die Chat-UI lädt beim ersten Aufruf
vom CDN und wird lokal gecacht.

### Nutzung mit dem HKA-LLM-Server (empfohlen)

Der HKA-Server <https://llm.hka-cloud.de> ist ein LiteLLM-Proxy und wird vom
Projekt direkt unterstützt:

1. Unter <https://llm.hka-cloud.de/ui/> anmelden und einen **Virtual Key**
   anlegen (beginnt mit `sk-`).
2. In der `.env` eintragen:

   ```env
   USE_LITELLM=1
   LITELLM_SERVER_URL=https://llm.hka-cloud.de
   LITELLM_API_KEY=sk-...
   LITELLM_MODEL=gpt-4o        # eine Modell-ID aus der Liste des Servers
   ```

3. Verfügbare Modelle prüfen (UI → „Models“ oder):

   ```bash
   curl -s https://llm.hka-cloud.de/v1/models -H "Authorization: Bearer sk-..."
   ```

   Alle vom Key erlaubten Modelle erscheinen zusätzlich automatisch im
   Modell-Selector der Chat-UI.

**Modell-Empfehlung für dieses Projekt:** Der Berater braucht zuverlässiges
mehrstufiges Tool-Calling (14+ Profil-Speicherungen, Recherche-Ketten) und
gutes Deutsch. In dieser Reihenfolge wählen, je nachdem was der Server
anbietet: `gpt-4o` bzw. `gpt-4.1` oder ein `claude-sonnet-*` (beste
Dialog-/Tool-Qualität) → `gpt-4o-mini` / `gpt-4.1-mini` (günstiger, für Tests
ausreichend, überspringt aber eher mal Phasen). Kleine lokale Modelle
(z. B. 7B-Klasse) sind für die Tool-Ketten nicht zuverlässig genug.

### Modell/Provider wechseln

- `ADVISOR_MODEL` in `.env` setzt das Modell im pydantic-ai-Format
  `<provider>:<modell>`, z. B. `openai:gpt-4o`, `anthropic:claude-sonnet-4-5`
  (dann `ANTHROPIC_API_KEY` setzen). Standard: `openai:gpt-4o-mini`.
- Alternativ **LiteLLM-Proxy**: `USE_LITELLM=1`, `LITELLM_SERVER_URL`,
  `LITELLM_API_KEY`, `LITELLM_MODEL` – identisch zum Template; die vom Proxy
  unterstützten Modelle erscheinen im Modell-Selector der UI.

### Nutzung

Einfach das Gespräch beginnen („Hallo, ich möchte Geld anlegen“). Der Bot
stellt seine Profilfragen nacheinander; Angaben können jederzeit korrigiert
werden („meine Sparrate ist doch 300 €“) – die Strategie wird dann neu
berechnet. „Fang bitte von vorn an“ setzt das Profil zurück.

Tipp: Die Tool-Aufrufe (Profil speichern, Recherche, Strategie-Berechnung)
sind in der Chat-UI einsehbar – nützlich zum Nachvollziehen der Beratung.

## Fachliche Grundlagen aus den Vorlesungsunterlagen

Die Beratungslogik setzt Prinzipien aus dem Finanzmanagement-Skript von
Prof. Dr. Andrea Wirth (HKA) um. Die Skript-PDFs liegen aus
**Urheberrechtsgründen nicht im Repository** – sie wurden einmalig
ausgewertet; die Prinzipien sind fest in Code und System-Prompt eingebaut
(der Bot liest die PDFs zur Laufzeit nicht). Herkunft je Prinzip:

| Prinzip | Quelle (Dokument) | Umsetzung im Code |
|---|---|---|
| Zielgrößen **Rendite, Risiko, Liquidität, Zeithorizont** („magisches Dreieck/Viereck“) als Zielsystem jeder Anlageentscheidung | `FM_kap1aamp;2_wirth_online.pdf` (Abschn. 2.1 Zielgrößen) | Profilfragen decken alle vier Größen ab; Liquidität (Notgroschen) und Zeithorizont kappen die Aktienquote (`risk.py`) |
| **Portfolio-Management-Prozess**: Ertrags-/Risikoziele → Asset-Allokation → Prognose → Performance-Monitoring → Revision | `FM_kap1aamp;2_wirth_online.pdf` (Abschn. 2.1, SAP-Portfolioanalyse) | Phasenmodell des Dialogs in `prompts.py`; Rebalancing-Hinweis in jeder Strategie |
| **Investmentfonds: Grundsatz der Risikostreuung** | `FM_kap1aamp;2_wirth_online.pdf` (Abschn. 2.5 Investmentfonds) | Bausteine der Allokation sind marktbreite Fonds/ETFs, keine Einzeltitel (`strategy.py`, `prompts.py`) |
| **Markowitz-Portfoliotheorie und Diversifikation**: Korrelation < 1 senkt das Portfoliorisiko; unsystematische (titelspezifische) Risiken sind wegdiversifizierbar, systematische nicht | `FM_kap3_wirth_online.pdf` (Abschn. 3.2) | Mehrere schwach korrelierte Anlageklassen (Aktien Welt/EM, Anleihen, Geldmarkt, ggf. Gold); Erklärtexte des Bots |
| **Bernoulli-Ansatz / Risiko-Nutzenfunktion** `U(x) = E(x) − a·Var(x)`: individuelle Risikoaversion `a` bestimmt das optimale Portfolio; Formel für die optimale Mischung zweier Anlagen mit Korrelation | `FM_kap3_wirth_online.pdf` (Abschn. 3.2, „Optimales Portfolio“) | `risk.py::optimale_aktienquote()` implementiert exakt diese Formel; Risikoklasse 1–5 → Parameter `a` |
| **Stresstest-Gedanke**: Schockszenarien (z. B. Aktienkursrückgang von 12–20 %) prüfen die Risikotragfähigkeit | `FM_kap3_wirth_online.pdf` (Abschn. 3.2 Stresstests) | Szenariofrage „Was tust du bei −20 %?“ statt Selbsteinschätzung „hoch/mittel/niedrig“ |
| **Dreistufige Asset-Allokation**: strategisch (Anlageklassen) → taktisch (Regionen, Branchen, Laufzeiten) → Titelauswahl | `FM_kap3_wirth_online.pdf` (Abschn. 3.3 Vorgehensweise der Asset Allokation) | `strategy.py`: Stufe 1+2 deterministisch berechnet, Stufe 3 (Produkte) per aktueller Web-Recherche |
| **Beschränkungen der Asset-Allokation**: Datenqualität der Inputs, kein statisches Buy-and-Hold → laufende Überwachung und Revision | `FM_kap3_wirth_online.pdf` (Abschn. 3.3 Beschränkungen) | Konservative, dokumentierte Kapitalmarktannahmen; jährlicher Rebalancing-Hinweis in jeder Strategie |
| **Risikoklassifikation und Risikoprozess**; Liquiditätsrisiko als eigene Risikoart | `FM_kap5_wirth_online.pdf` | Getrennte Bewertung von Risikobereitschaft und -tragfähigkeit; Notgroschen-Regel (erst Liquiditätsreserve, dann investieren) |
| **Liquiditätsplanung/Finanzdisposition** | `FM_kap7_wirth.pdf` | Notgroschen von 3–6 Monatsausgaben als Voraussetzung; kurzfristiger Bedarf bleibt im Geldmarkt/Tagesgeld |

Kapitel 4 (Unternehmensbewertung) und 8/9 (Unternehmenssteuerung) betreffen
Corporate Finance und fließen bewusst nicht in die Privatanleger-Logik ein.

## Entscheidungen

### Template-Wahl: `chatbot-pydanticai-template`

Beide Templates nutzen **Pydantic AI** als Agent-Framework; unterschieden haben
sie sich im Frontend und im Reifegrad der Verdrahtung:

| Kriterium | chatbot-pydanticai-template | chatbot-copilotkit-template |
|---|---|---|
| Agent-Framework | Pydantic AI (v1.81) | Pydantic AI |
| UI | Offizielle Pydantic AI Chat UI via `agent.to_web()` (Streaming, Tool-Visualisierung, Modell-Selector) | Eigenes Next.js-Frontend mit selbstgebautem SSE-Protokoll |
| Tool-Integration | `@agent.tool` nativ, Tool-Aufrufe in der UI sichtbar | Tools als Dict definiert, aber nicht an den Agenten angebunden |
| Session-/State-Handling | `to_web(deps=...)` erlaubt typisierte Dependencies (hier: `UserProfile`) | Conversation-Store vorhanden, aber ohne Agent-Anbindung für strukturierten State |
| Betrieb | Ein Prozess (`uvicorn`), reines Python | Zwei Prozesse (uvicorn + npm), zusätzlicher TypeScript-Stack |
| Key-Management | `.env` + python-dotenv, LiteLLM optional | `.env`, nur OpenAI direkt |

**Entscheidung:** pydanticai-Template. Für dieses Projekt zählt die Qualität
der Agent-/Tool-Logik (Profiling, Recherche, Strategie), nicht ein eigenes
Frontend. Die offizielle Chat-UI liefert Streaming und Tool-Transparenz ohne
eigenen Frontend-Code, und die native Tool-/Deps-Integration von Pydantic AI
trägt das Session-State-Konzept direkt. Übernommen wurden Ordnerstruktur
(`src/`-Layout), uv-Setup, ruff/ty-Konfiguration und das komplette
Konfigurations-/Key-Management-Pattern (inkl. optionalem LiteLLM-Betrieb).

### Datenquellen/Recherche-Tools

- **DuckDuckGo (`ddgs`)** für die Websuche: ohne API-Key nutzbar → keine
  zusätzliche Key-Verwaltung, reproduzierbares Setup für Korrektoren.
- **Yahoo Finance (`yfinance`)** für Kurse/Kennzahlen: ebenfalls schlüssellos;
  liefert Rendite-Historie und daraus berechnete Volatilität (Risikomaß gemäß
  Skript Kap. 1&2).
- Provider-seitige Built-in-Websuche (z. B. OpenAI WebSearchTool) wurde bewusst
  nicht verwendet, damit die Recherche unabhängig vom gewählten LLM-Provider
  funktioniert (auch über LiteLLM).

### Weitere Entscheidungen

- **Deterministisches Zahlenwerk statt LLM-Rechnen:** Risikoklasse, Quoten und
  Sparplan-Beträge berechnet Python-Code; das LLM darf Zahlen nur übernehmen
  und erklären. Das verhindert halluzinierte Prozentsätze.
- **Vorsichtsprinzip:** Risikoklasse = Minimum aus Bereitschaft und
  Tragfähigkeit; zusätzlich harte Kappungen (Horizont, Notgroschen,
  Konsumschulden) – angelehnt an die Zielgrößen-Logik des Skripts und gängige
  Geeignetheitsprüfungen.
- **Kapitalmarktannahmen** (`risk.py`): bewusst konservative, gerundete
  Langfristwerte (Aktien 7 % p. a. / σ 16 %, Anleihen 2,5 % p. a. / σ 5 %,
  ρ = 0,2), im Code dokumentiert und leicht änderbar; siehe auch
  [LIMITATIONS.md](LIMITATIONS.md).

## Nicht umgesetzt / Einschränkungen

Siehe [LIMITATIONS.md](LIMITATIONS.md) – u. a. kein Bank-Connector (bewusst,
Regulatorik/Sicherheit), keine Zulassung als Anlageberatung, Session-State pro
Serverprozess, Grenzen der schlüssellosen Datenquellen.

## Lizenz / Kontext

Hochschulprojekt (HKA). Die referenzierten Vorlesungsunterlagen
(© Prof. Dr. Andrea Wirth) sind nicht Teil des Repositories.
