"""Recherche-Tools: Websuche, Seitenabruf und Marktdaten.

Alle Funktionen sind bewusst schlüssellos nutzbar:
- Websuche über DuckDuckGo (`ddgs`) – für aktuelle ETF-/Produktrecherche.
- Seitenabruf via httpx + BeautifulSoup – um Trefferseiten (z. B. justETF,
  extraETF, Anbieterseiten) auszulesen.
- Kursdaten über Yahoo Finance (`yfinance`) – Preise, Historie, Kennzahlen.

Fehler werden als lesbare Strings zurückgegeben, damit der Agent darauf
reagieren kann (z. B. alternative Suchbegriffe), statt abzubrechen.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

MAX_SEITEN_ZEICHEN = 8000


def web_suche(suchbegriff: str, max_treffer: int = 8) -> str:
    """DuckDuckGo-Textsuche; Rückgabe als kompakte Trefferliste."""
    try:
        from ddgs import DDGS

        treffer = list(
            DDGS(timeout=15).text(suchbegriff, region="de-de", max_results=max_treffer)
        )
    except Exception as e:  # noqa: BLE001
        return f"Websuche fehlgeschlagen: {e}"

    if not treffer:
        return "Keine Treffer gefunden. Bitte Suchbegriff variieren."

    zeilen = []
    for t in treffer:
        titel = t.get("title", "")
        url = t.get("href", "")
        snippet = (t.get("body", "") or "")[:300]
        zeilen.append(f"- {titel}\n  URL: {url}\n  {snippet}")
    return "\n".join(zeilen)


def lese_webseite(url: str) -> str:
    """Webseite abrufen und als reinen Text (gekürzt) zurückgeben."""
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return f"Abruf fehlgeschlagen ({url}): {e}"

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
    except Exception as e:  # noqa: BLE001
        return f"Konnte Seite nicht parsen ({url}): {e}"

    if not text:
        return f"Seite {url} enthielt keinen lesbaren Text."
    if len(text) > MAX_SEITEN_ZEICHEN:
        text = text[:MAX_SEITEN_ZEICHEN] + " …[gekürzt]"
    return text


def _kennzahlen_fuer_ticker(symbol: str) -> dict[str, Any]:
    import yfinance as yf

    t = yf.Ticker(symbol)
    info: dict[str, Any] = {}
    try:
        info = t.info or {}
    except Exception:  # noqa: BLE001
        info = {}

    hist = t.history(period="5y", interval="1mo", auto_adjust=True)

    daten: dict[str, Any] = {
        "symbol": symbol,
        "name": info.get("longName") or info.get("shortName"),
        "waehrung": info.get("currency"),
        "typ": info.get("quoteType"),
        "kategorie": info.get("category"),
        "fondsvolumen": info.get("totalAssets"),
        "laufende_kosten_ter": info.get("netExpenseRatio") or info.get("annualReportExpenseRatio"),
        "dividendenrendite": info.get("dividendYield") or info.get("yield"),
    }

    if hist is not None and not hist.empty:
        close = hist["Close"].dropna()
        daten["kurs_aktuell"] = round(float(close.iloc[-1]), 2)
        for jahre, label in ((1, "rendite_1j_prozent"), (3, "rendite_3j_prozent"), (5, "rendite_5j_prozent")):
            monate = jahre * 12
            if len(close) > monate:
                start = float(close.iloc[-monate - 1])
                ende = float(close.iloc[-1])
                daten[label] = round((ende / start - 1) * 100, 1)
        # Volatilität aus Monatsrenditen (annualisiert) – Risikomaß, Skript Kap. 1&2.
        renditen = close.pct_change().dropna()
        if len(renditen) >= 12:
            vol = float(renditen.std()) * (12**0.5)
            daten["volatilitaet_5j_prozent_pa"] = round(vol * 100, 1)

    return {k: v for k, v in daten.items() if v is not None}


def marktdaten(symbole: str) -> str:
    """Kurs- und Kennzahlendaten für Ticker (kommagetrennt, Yahoo-Finance-Symbole).

    Beispiele: "IWDA.AS, EUNL.DE" (ETFs), "^GSPC" (S&P 500), "EURUSD=X".
    """
    ergebnisse = []
    for symbol in [s.strip() for s in symbole.split(",") if s.strip()][:8]:
        try:
            ergebnisse.append(_kennzahlen_fuer_ticker(symbol))
        except Exception as e:  # noqa: BLE001
            ergebnisse.append({"symbol": symbol, "fehler": str(e)})
    if not ergebnisse:
        return "Keine Symbole angegeben."
    return json.dumps(ergebnisse, ensure_ascii=False, indent=1)
