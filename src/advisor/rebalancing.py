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
    einstandswert_eur: float | None = Field(
        default=None,
        description=(
            "Ursprünglicher Kaufwert der Position in EUR (falls bekannt) – "
            "ermöglicht die Schätzung von steuerpflichtigem Gewinn/Verlust bei Verkauf"
        ),
        ge=0,
    )


# Deutsche Kapitalertragsbesteuerung (vereinfachte Schätzwerte, s. LIMITATIONS):
# Abgeltungsteuer 25 % + Solidaritätszuschlag 5,5 % darauf = 26,375 %
# (ohne Kirchensteuer, ohne individuelle Günstigerprüfung).
ABGELTUNGSTEUER_SATZ = 0.26375
# Teilfreistellung für Aktienfonds/-ETFs (§ 20 InvStG): 30 % der Erträge steuerfrei.
TEILFREISTELLUNG: dict[str, float] = {
    "aktien_welt_industrielaender": 0.30,
    "aktien_schwellenlaender": 0.30,
    "anleihen_eur_investment_grade": 0.0,
    "geldmarkt_tagesgeld": 0.0,
    "gold": 0.0,
}


def _gebuehr(betrag: float, prozent: float, minimum: float) -> float:
    return round(max(minimum, betrag * prozent / 100), 2)


def ist_deutscher_steuerkontext(land: str | None) -> bool:
    """Prüft, ob der angegebene Steuerkontext (grob) Deutschland entspricht.

    Die Steuerschätzung (Abgeltungsteuer/Teilfreistellung) gilt nur für den
    deutschen Rechtsrahmen; bei anderen/unklaren Angaben wird sie nicht
    berechnet, um keine falschen ausländischen Steuerbeträge vorzugaukeln.
    """
    if not land:
        return False
    s = land.strip().lower()
    return any(k in s for k in ("deutschland", "germany", "bundesrepublik")) or s in ("de", "d")


def erstelle_umschichtungsplan(
    positionen: list[Position],
    ziel_allokation_prozent: dict[str, float],
    neues_kapital_eur: float,
    gebuehr_prozent: float = 0.25,
    gebuehr_min_eur: float = 1.0,
    schwelle_prozentpunkte: float = 5.0,
    min_handelsbetrag_eur: float = 200.0,
    steuerschaetzung_de: bool = True,
) -> dict[str, Any]:
    """Kauf-/Verkaufsliste vom Ist-Depot zur Ziel-Allokation berechnen."""
    ist: dict[str, float] = {}
    einstand: dict[str, float] = {}
    einstand_vollstaendig: dict[str, bool] = {}
    sonstige: list[Position] = []
    for pos in positionen:
        if pos.kategorie == "sonstiges":
            sonstige.append(pos)
            continue
        k = pos.kategorie
        ist[k] = ist.get(k, 0.0) + pos.wert_eur
        einstand_vollstaendig.setdefault(k, True)
        if pos.einstandswert_eur is None:
            einstand_vollstaendig[k] = False
        else:
            einstand[k] = einstand.get(k, 0.0) + pos.einstandswert_eur

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
            trade: dict[str, Any] = {
                "kategorie": k,
                "betrag_eur": round(uebergewicht, 2),
                "gebuehr_eur": geb,
                "begruendung": f"Übergewicht von {ueber_pp:.1f} Prozentpunkten gegenüber der Ziel-Allokation",
            }
            # Steuer-Schätzung: anteiliger Gewinn/Verlust des Verkaufs, sofern
            # der Einstandswert aller Positionen der Kategorie bekannt ist.
            # Gilt nur für den deutschen Rechtsrahmen (Abgeltungsteuer/
            # Teilfreistellung) – bei anderem/unklarem Steuerkontext würden
            # sonst falsche ausländische Steuerbeträge suggeriert.
            if not steuerschaetzung_de:
                trade["steuer_hinweis"] = (
                    "Keine Steuerschätzung: Der angegebene Steuerkontext ist nicht Deutschland "
                    "(oder unbekannt). Abgeltungsteuer und Teilfreistellung gelten nur für in "
                    "Deutschland steuerpflichtige Personen – bitte lokale Regeln separat prüfen."
                )
            elif einstand_vollstaendig.get(k) and ist.get(k, 0) > 0:
                gewinn_quote = (ist[k] - einstand[k]) / ist[k]
                gewinn = uebergewicht * gewinn_quote
                trade["geschaetzter_gewinn_eur"] = round(gewinn, 2)
                tf = TEILFREISTELLUNG.get(k, 0.0)
                if gewinn > 0:
                    steuer = gewinn * (1 - tf) * ABGELTUNGSTEUER_SATZ
                    trade["geschaetzte_steuer_eur"] = round(steuer, 2)
                    if tf > 0:
                        trade["steuer_hinweis"] = (
                            f"Teilfreistellung {tf:.0%} für Aktienfonds berücksichtigt; "
                            "Sparer-Pauschbetrag mindert die Steuer zusätzlich."
                        )
                else:
                    trade["geschaetzte_steuer_eur"] = 0.0
                    trade["steuer_hinweis"] = (
                        "Realisierter Verlust – landet im Verlustverrechnungstopf und kann "
                        "mit steuerpflichtigen Gewinnen (auch künftiger Jahre) verrechnet werden."
                    )
            else:
                trade["steuer_hinweis"] = (
                    "Einstandswert unbekannt – Gewinn/Steuer nicht schätzbar; beim Nutzer erfragen."
                )
            verkaeufe.append(trade)
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
    steuer_summe = round(sum(t.get("geschaetzte_steuer_eur", 0.0) for t in verkaeufe), 2)

    hinweise = [
        "Reihenfolge: erst neues Kapital einsetzen, dann (falls nötig) verkaufen – "
        "das hält Gebühren und Steuerlast minimal.",
    ]
    if verkaeufe and steuerschaetzung_de:
        hinweise.append(
            "Steuern (Deutschland, vereinfacht): Realisierte Gewinne unterliegen der "
            "Abgeltungsteuer (25 % + Soli ≈ 26,4 %); bei Aktienfonds sind 30 % der Erträge "
            "teilfreigestellt. Der Sparer-Pauschbetrag (1.000 € p. P. und Jahr, "
            "Freistellungsauftrag stellen!) bleibt steuerfrei. Gewinne und Verluste desselben "
            "Jahres werden automatisch verrechnet; nicht genutzte Verluste trägt der Broker "
            "ins Folgejahr vor. Alles allgemeine Information, keine Steuerberatung."
        )
    elif verkaeufe:
        hinweise.append(
            "Steuern: Die eingebaute Schätzung deckt nur den deutschen Rechtsrahmen ab. Beim "
            "angegebenen Steuerkontext bitte die Besteuerung von Kapitalerträgen separat prüfen "
            "(z. B. steuerlichen Berater vor Ort fragen) – keine Steuerberatung."
        )
    # Steueroptimierung: Verlustpositionen identifizieren, deren Realisierung
    # Gewinne aus den Verkäufen ausgleichen könnte (Verlustverrechnung).
    verlust_texte = [
        f"{p.name} ({p.wert_eur - e:+.0f} €)"
        for p in positionen
        if (e := p.einstandswert_eur) is not None and p.wert_eur < e
    ]
    if steuer_summe > 0 and verlust_texte:
        hinweise.append(
            "Steuer-Tipp (Verlustverrechnung): Folgende Positionen stehen im Minus und "
            f"könnten Gewinne aus den Verkäufen steuerlich ausgleichen: {', '.join(verlust_texte)}. "
            "Mit dem Nutzer besprechen, ob eine Realisierung fachlich sinnvoll ist – "
            "Steuern allein sind kein Verkaufsgrund."
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
        "geschaetzte_steuer_summe_eur": steuer_summe,
        "gebuehren_modell": f"{gebuehr_prozent} % pro Order, mindestens {gebuehr_min_eur} €",
        "nicht_zugeordnete_positionen": [p.model_dump() for p in sonstige],
        "hinweise": hinweise,
    }
