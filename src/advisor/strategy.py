"""Strategie-Engine: von Risikoprofil und Kapital zur konkreten Asset-Allokation.

Fachliche Grundlage (Finanzmanagement-Skript, Prof. Wirth, HKA):

- Kap. 3, "Vorgehensweise der Asset Allokation": dreistufiger Prozess –
  (1) strategische Allokation über Anlageklassen, (2) taktische Allokation
  innerhalb der Klassen (Regionen, Branchen, Laufzeiten), (3) Titelauswahl.
  Die Stufen 1 und 2 berechnet dieses Modul deterministisch; Stufe 3
  (konkrete Produkte) übernimmt der Agent auf Basis aktueller Web-Recherche.
- Kap. 3, Diversifikation: unsystematische (titelspezifische) Risiken werden
  durch breite Streuung eliminiert; deshalb bilden marktbreite Fonds/ETFs
  (Grundsatz der Risikostreuung, Kap. 1&2 Investmentfonds) die Bausteine,
  keine Einzeltitel.
- Kap. 3, "Beschränkungen der Asset Allokation": kein statisches Buy-and-Hold –
  regelmäßige Überprüfung und Rebalancing gehören zur Empfehlung.
"""

from __future__ import annotations

from typing import Any

from advisor.profile import UserProfile
from advisor.risk import RisikoErgebnis

# Mindestrate je Sparplan-Position (üblicher Minimalbetrag bei Brokern).
MIN_SPARPLAN_RATE_EUR = 25.0


def _strategische_allokation(risiko: RisikoErgebnis, p: UserProfile) -> dict[str, float]:
    """Stufe 1+2: Anteile je Baustein (Summe 1.0).

    Aufteilung innerhalb der Aktienquote: ca. 70/30 Industrieländer/
    Schwellenländer (taktische Diversifikation nach Regionen, Kap. 3).
    Ab Risikoklasse 3 und ausreichender Quote wird ein kleiner Gold-Anteil
    als schwach korrelierter Diversifikationsbaustein beigemischt.
    """
    aktien = risiko.aktienquote_empfohlen

    gold = 0.05 if (risiko.risikoklasse >= 3 and aktien >= 0.4) else 0.0

    rest = 1.0 - aktien - gold
    # Defensiver Block: je kürzer der Horizont, desto mehr Geldmarkt/Tagesgeld
    # statt Anleihen (Liquiditätsziel, Kap. 1&2).
    horizont = p.zeithorizont_jahre or 0
    if horizont < 3:
        geldmarkt_anteil_im_rest = 0.8
    elif horizont < 5:
        geldmarkt_anteil_im_rest = 0.5
    elif horizont < 10:
        geldmarkt_anteil_im_rest = 0.25
    else:
        geldmarkt_anteil_im_rest = 0.1

    geldmarkt = rest * geldmarkt_anteil_im_rest
    anleihen = rest - geldmarkt

    allokation = {
        "aktien_welt_industrielaender": round(aktien * 0.70, 4),
        "aktien_schwellenlaender": round(aktien * 0.30, 4),
        "anleihen_eur_investment_grade": round(anleihen, 4),
        "geldmarkt_tagesgeld": round(geldmarkt, 4),
    }
    if gold > 0:
        allokation["gold"] = gold

    # Rundungsdifferenzen auf den größten Baustein schieben.
    diff = round(1.0 - sum(allokation.values()), 4)
    if abs(diff) >= 0.0001:
        groesster = max(allokation, key=lambda k: allokation[k])
        allokation[groesster] = round(allokation[groesster] + diff, 4)

    return allokation


def _sparplan_aufteilung(allokation: dict[str, float], rate: float) -> dict[str, float]:
    """Monatliche Sparrate auf die Bausteine verteilen.

    Positionen unter der Mindestrate werden dem größten Aktienbaustein
    zugeschlagen (praktikabel umsetzbar; Feinjustierung übers Rebalancing).
    """
    if rate <= 0:
        return {}

    grob = {k: rate * v for k, v in allokation.items()}
    ergebnis: dict[str, float] = {}
    zu_klein = 0.0
    for k, betrag in grob.items():
        if betrag < MIN_SPARPLAN_RATE_EUR:
            zu_klein += betrag
        else:
            ergebnis[k] = betrag

    if not ergebnis:
        # Rate insgesamt klein: alles in einen einzigen breiten Baustein.
        haupt = max(allokation, key=lambda k: allokation[k])
        return {haupt: round(rate, 2)}

    groesster = max(ergebnis, key=lambda k: ergebnis[k])
    ergebnis[groesster] += zu_klein

    ergebnis = {k: round(v, 2) for k, v in ergebnis.items()}
    # Rundungsdifferenz ausgleichen.
    diff = round(rate - sum(ergebnis.values()), 2)
    if diff:
        ergebnis[groesster] = round(ergebnis[groesster] + diff, 2)
    return ergebnis


def erstelle_strategie(p: UserProfile, risiko: RisikoErgebnis) -> dict[str, Any]:
    """Komplette Strategie-Basis als strukturierte Daten für den Agenten."""
    allokation = _strategische_allokation(risiko, p)

    rate = p.monatliche_sparrate_eur or 0.0
    einmalbetrag = p.einmalbetrag_eur or 0.0

    hinweise: list[str] = list(risiko.begrenzungen)

    if (p.notgroschen_monatsausgaben or 0) < 3:
        hinweise.append(
            "Priorität vor dem Investieren: Notgroschen von 3–6 Monatsausgaben auf einem "
            "Tagesgeldkonto aufbauen (Liquiditätsreserve, Skript Kap. 1&2/7)."
        )
    if p.hat_konsumschulden:
        hinweise.append(
            "Priorität vor dem Investieren: Konsum-/Ratenkredite tilgen – die ersparten "
            "Kreditzinsen sind eine sichere, steuerfreie Rendite."
        )
    if einmalbetrag >= 10000:
        hinweise.append(
            "Einmalbetrag: gestaffelter Einstieg über z. B. 6–12 Monate reduziert das "
            "Timing-Risiko (Cost-Averaging), erwartungswertig ist Sofortanlage leicht überlegen – "
            "beides sauber erklären und dem Nutzer die Wahl lassen."
        )
    hinweise.append(
        "Kein statisches Buy-and-Hold: Allokation jährlich überprüfen und per Rebalancing "
        "auf die Zielquoten zurückführen (Portfoliorevision, Skript Kap. 3)."
    )

    return {
        "risikoklasse": risiko.risikoklasse,
        "risikoklasse_name": risiko.klassen_name,
        "risikoaversion_a": risiko.risikoaversion_a,
        "aktienquote_nutzenoptimum": risiko.aktienquote_unbegrenzt,
        "aktienquote_final": risiko.aktienquote_empfohlen,
        "allokation_prozent": {k: round(v * 100, 1) for k, v in allokation.items()},
        "sparplan_aufteilung_eur": _sparplan_aufteilung(allokation, rate),
        "einmalbetrag_aufteilung_eur": {
            k: round(einmalbetrag * v, 2) for k, v in allokation.items()
        }
        if einmalbetrag > 0
        else {},
        "hinweise": hinweise,
        "teil_scores": {
            "risikobereitschaft_0_10": risiko.teil_score_bereitschaft,
            "risikotragfaehigkeit_0_10": risiko.teil_score_tragfaehigkeit,
        },
    }
