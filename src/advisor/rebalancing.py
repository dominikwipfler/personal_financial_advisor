"""Umschichtungsplan: vom Ist-Depot zur Ziel-Allokation, mit Gebühren.

Erzeugt konkrete Kauf- und Verkaufsempfehlungen unter Berücksichtigung von
Ordergebühren. Prinzipien:

- "Neues Geld zuerst": Einmalbetrag und Sparrate füllen Untergewichte auf,
  bevor verkauft wird – das minimiert Gebühren und vermeidet unnötige
  Steuerrealisierung (Portfoliorevision, Skript Kap. 3).
- Verkäufe nur bei deutlichem Übergewicht (Schwelle in Prozentpunkten) und
  oberhalb eines Mindest-Handelsbetrags – kleine Abweichungen werden über
  den laufenden Sparplan ausgeglichen statt über gebührenpflichtige Orders.
- Positionen der Kategorie "sonstiges" (Einzelaktien, aktive Fonds, Krypto …)
  werden nie automatisch zum Verkauf gesetzt, sondern als "prüfen" markiert –
  die Entscheidung bespricht der Agent mit dem Nutzer.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

PositionsKategorie = Literal[
    "aktien_welt_industrielaender",
    "aktien_schwellenlaender",
    "anleihen_eur_investment_grade",
    "geldmarkt_tagesgeld",
    "gold",
    "sonstiges",
]


class Position(BaseModel):
    """Eine bestehende Depot-/Vermögensposition des Nutzers."""

    name: str = Field(description="Bezeichnung, z. B. 'iShares Core MSCI World' oder 'Tagesgeld ING'")
    wert_eur: float = Field(description="Aktueller Wert in EUR", ge=0)
    kategorie: PositionsKategorie = Field(
        description=(
            "Zuordnung zum Allokations-Baustein; 'sonstiges' für alles, was nicht "
            "in die Ziel-Bausteine passt (Einzelaktien, aktive Fonds, Krypto, …)"
        )
    )


def _gebuehr(betrag: float, prozent: float, minimum: float) -> float:
    return round(max(minimum, betrag * prozent / 100), 2)


def erstelle_umschichtungsplan(
    positionen: list[Position],
    ziel_allokation_prozent: dict[str, float],
    neues_kapital_eur: float,
    gebuehr_prozent: float = 0.25,
    gebuehr_min_eur: float = 1.0,
    schwelle_prozentpunkte: float = 5.0,
    min_handelsbetrag_eur: float = 200.0,
) -> dict[str, Any]:
    """Kauf-/Verkaufsliste vom Ist-Depot zur Ziel-Allokation berechnen."""
    ist: dict[str, float] = {}
    sonstige: list[Position] = []
    for pos in positionen:
        if pos.kategorie == "sonstiges":
            sonstige.append(pos)
        else:
            ist[pos.kategorie] = ist.get(pos.kategorie, 0.0) + pos.wert_eur

    basis = sum(ist.values()) + max(neues_kapital_eur, 0.0)
    if basis <= 0:
        return {"fehler": "Kein investierbares Vermögen übergeben (Positionen + neues Kapital = 0)."}

    ziel_eur = {k: basis * p / 100 for k, p in ziel_allokation_prozent.items()}

    # Differenz je Kategorie; Kategorien mit Bestand, aber ohne Ziel (z. B. Gold
    # nicht in der Ziel-Allokation) gelten als komplettes Übergewicht.
    kategorien = set(ziel_eur) | set(ist)
    diff = {k: ziel_eur.get(k, 0.0) - ist.get(k, 0.0) for k in kategorien}

    # --- Verkäufe: nur deutliche Übergewichte ---
    verkaeufe: list[dict[str, Any]] = []
    halten_hinweise: list[str] = []
    verkaufs_erloes = 0.0
    for k, d in sorted(diff.items(), key=lambda kv: kv[1]):
        if d >= 0:
            continue
        uebergewicht = -d
        ueber_pp = uebergewicht / basis * 100
        if ueber_pp >= schwelle_prozentpunkte and uebergewicht >= min_handelsbetrag_eur:
            geb = _gebuehr(uebergewicht, gebuehr_prozent, gebuehr_min_eur)
            verkaeufe.append(
                {
                    "kategorie": k,
                    "betrag_eur": round(uebergewicht, 2),
                    "gebuehr_eur": geb,
                    "begruendung": f"Übergewicht von {ueber_pp:.1f} Prozentpunkten gegenüber der Ziel-Allokation",
                }
            )
            verkaufs_erloes += uebergewicht
        else:
            halten_hinweise.append(
                f"{k}: Übergewicht von {ueber_pp:.1f} Prozentpunkten ({uebergewicht:.0f} €) liegt unter der "
                f"Handels-Schwelle – halten und über künftige Sparraten ausgleichen (spart Gebühren und Steuern)."
            )

    # --- Käufe: neues Kapital + Verkaufserlöse füllen Untergewichte ---
    verfuegbar = max(neues_kapital_eur, 0.0) + verkaufs_erloes
    untergewichte = {k: d for k, d in diff.items() if d > 0}
    summe_bedarf = sum(untergewichte.values())
    kaeufe: list[dict[str, Any]] = []
    if verfuegbar > 0 and summe_bedarf > 0:
        faktor = min(1.0, verfuegbar / summe_bedarf)
        grob = {k: d * faktor for k, d in untergewichte.items()}
        # Kleinstbeträge dem größten Kauf zuschlagen (gebühren-effizient).
        zu_klein = sum(b for b in grob.values() if b < min_handelsbetrag_eur)
        grob = {k: b for k, b in grob.items() if b >= min_handelsbetrag_eur}
        if grob:
            groesster = max(grob, key=lambda k: grob[k])
            grob[groesster] += zu_klein
        elif zu_klein > 0:
            halten_hinweise.append(
                f"Kaufbedarf von {zu_klein:.0f} € liegt unter dem Mindest-Handelsbetrag – "
                "besser über die monatliche Sparrate aufbauen."
            )
        for k, betrag in sorted(grob.items(), key=lambda kv: -kv[1]):
            geb = _gebuehr(betrag, gebuehr_prozent, gebuehr_min_eur)
            kaeufe.append(
                {
                    "kategorie": k,
                    "betrag_eur": round(betrag, 2),
                    "gebuehr_eur": geb,
                    "begruendung": f"Untergewicht von {diff[k] / basis * 100:.1f} Prozentpunkten auffüllen",
                }
            )

    gebuehren_summe = round(sum(t["gebuehr_eur"] for t in verkaeufe + kaeufe), 2)
    handelsvolumen = round(sum(t["betrag_eur"] for t in verkaeufe + kaeufe), 2)

    hinweise = [
        "Reihenfolge: erst neues Kapital einsetzen, dann (falls nötig) verkaufen – "
        "das hält Gebühren und Steuerlast minimal.",
    ]
    if verkaeufe:
        hinweise.append(
            "Verkäufe können Abgeltungsteuer auf realisierte Gewinne auslösen (in Deutschland "
            "25 % zzgl. Solidaritätszuschlag, Sparer-Pauschbetrag/Freistellungsauftrag prüfen) – "
            "allgemeine Information, keine Steuerberatung."
        )
    hinweise.extend(halten_hinweise)
    if sonstige:
        hinweise.append(
            "Positionen außerhalb der Ziel-Allokation (Kategorie 'sonstiges') wurden NICHT zum "
            "Verkauf gesetzt – mit dem Nutzer besprechen, ob sie behalten oder schrittweise "
            "umgeschichtet werden sollen: "
            + ", ".join(f"{p.name} ({p.wert_eur:.0f} €)" for p in sonstige)
        )

    return {
        "basis_vermoegen_eur": round(basis, 2),
        "ist_verteilung_eur": {k: round(v, 2) for k, v in ist.items()},
        "ziel_verteilung_eur": {k: round(v, 2) for k, v in ziel_eur.items()},
        "verkaeufe": verkaeufe,
        "kaeufe": kaeufe,
        "handelsvolumen_eur": handelsvolumen,
        "gebuehren_summe_eur": gebuehren_summe,
        "gebuehren_modell": f"{gebuehr_prozent} % pro Order, mindestens {gebuehr_min_eur} €",
        "nicht_zugeordnete_positionen": [p.model_dump() for p in sonstige],
        "hinweise": hinweise,
    }
