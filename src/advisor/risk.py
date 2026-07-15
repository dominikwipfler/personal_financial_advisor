"""Risikoprofilierung: von den Dialog-Antworten zur Risikoklasse und Aktienquote.

Fachliche Grundlage (Finanzmanagement-Skript, Prof. Wirth, HKA):

- Kap. 3 (Portfoliotheorie): Bernoulli-Ansatz mit Risiko-Nutzenfunktion
  U(x) = E(x) − a · Var(x). Der Risikoaversionsparameter `a` ist individuell
  zu spezifizieren; die Maximierung der Nutzenfunktion liefert die optimale
  Aufteilung zwischen einer riskanteren und einer sichereren Anlage
  (Zwei-Anlagen-Fall mit Korrelation, Formel s. Kap. 3, "Optimales Portfolio").
- Kap. 3 (Stresstests): Schockszenarien (z. B. Aktien −14 % bis −20 %) prüfen
  die Risikotragfähigkeit. Analog erfragt der Bot die Reaktion auf einen
  20-%-Kursverlust, statt nur "hoch/mittel/niedrig" abzufragen.
- Kap. 1&2 (Zielgrößen): Rendite, Risiko, Liquidität und Zeithorizont bilden
  das Zielsystem; ein kurzer Horizont oder fehlende Liquiditätsreserve
  begrenzen die vertretbare Risikoquote unabhängig von der Risikoneigung.

Umsetzung: Zwei getrennte Scores –
- Risikobereitschaft (subjektiv: Verlustreaktion, Verlusttoleranz, Erfahrung)
- Risikotragfähigkeit (objektiv: Horizont, Notgroschen, Schulden, Alter)
Die Risikoklasse ist das Minimum beider Teilklassen (Vorsichtsprinzip: die
schwächere Dimension begrenzt), daraus wird `a` abgeleitet und die neutrale
Aktienquote über das Markowitz-/Bernoulli-Optimum berechnet.
"""

from __future__ import annotations

from dataclasses import dataclass

from advisor.profile import UserProfile

# Langfristige Kapitalmarktannahmen (nominal, p. a.) für den Zwei-Anlagen-Fall
# "riskant (globale Aktien)" vs. "defensiv (EUR-Anleihen Investment Grade)".
# Bewusst konservative, gerundete Näherungswerte; siehe LIMITATIONS.md.
MU_AKTIEN = 0.070          # erwartete Rendite globale Aktien
SIGMA_AKTIEN = 0.16        # Volatilität globale Aktien
MU_ANLEIHEN = 0.025        # erwartete Rendite EUR-Anleihen (IG, mittlere Laufzeit)
SIGMA_ANLEIHEN = 0.05      # Volatilität EUR-Anleihen
KORRELATION = 0.2          # Korrelation Aktien/Anleihen (leicht positiv angenommen)

# Risikoklasse (1–5) -> Risikoaversionsparameter a der Nutzenfunktion
# U = E(x) − a·Var(x). Kleines a = geringe Risikoaversion.
RISIKOAVERSION_JE_KLASSE: dict[int, float] = {1: 7.0, 2: 4.5, 3: 3.0, 4: 2.0, 5: 1.3}

KLASSEN_NAMEN: dict[int, str] = {
    1: "sehr defensiv",
    2: "defensiv",
    3: "ausgewogen",
    4: "wachstumsorientiert",
    5: "offensiv",
}


@dataclass
class RisikoErgebnis:
    risikoklasse: int
    klassen_name: str
    risikoaversion_a: float
    aktienquote_unbegrenzt: float   # reines Nutzenoptimum (0..1)
    aktienquote_empfohlen: float    # nach Horizont-/Tragfähigkeits-Kappung (0..1)
    begrenzungen: list[str]         # angewandte Kappungsgründe (nachvollziehbar)
    teil_score_bereitschaft: int
    teil_score_tragfaehigkeit: int


def _score_risikobereitschaft(p: UserProfile) -> int:
    """Subjektive Risikobereitschaft als Score 0–10."""
    score = 0

    reaktion_punkte = {
        "alles_verkaufen": 0,
        "teilweise_verkaufen": 1,
        "beunruhigt_halten": 2,
        "gelassen_halten": 4,
        "nachkaufen": 5,
    }
    score += reaktion_punkte.get(p.reaktion_kursverlust_20_prozent or "", 0)

    verlust = p.max_akzeptierter_verlust_prozent or 0
    if verlust >= 40:
        score += 3
    elif verlust >= 25:
        score += 2
    elif verlust >= 15:
        score += 1

    erfahrung_punkte = {"keine": 0, "grundkenntnisse": 1, "fortgeschritten": 2, "sehr_erfahren": 2}
    score += erfahrung_punkte.get(p.anlageerfahrung or "", 0)

    return min(score, 10)


def _score_risikotragfaehigkeit(p: UserProfile) -> int:
    """Objektive Risikotragfähigkeit als Score 0–10."""
    score = 0

    horizont = p.zeithorizont_jahre or 0
    if horizont >= 15:
        score += 4
    elif horizont >= 10:
        score += 3
    elif horizont >= 5:
        score += 2
    elif horizont >= 3:
        score += 1

    notgroschen = p.notgroschen_monatsausgaben or 0
    if notgroschen >= 6:
        score += 3
    elif notgroschen >= 3:
        score += 2
    elif notgroschen >= 1:
        score += 1

    if p.hat_konsumschulden is False:
        score += 2

    alter = p.alter or 45
    if alter < 40:
        score += 1

    return min(score, 10)


def _score_zu_klasse(score: int) -> int:
    """Score 0–10 -> Teilklasse 1–5."""
    if score <= 1:
        return 1
    if score <= 3:
        return 2
    if score <= 6:
        return 3
    if score <= 8:
        return 4
    return 5


def optimale_aktienquote(a: float) -> float:
    """Nutzenoptimale Aktienquote z* im Zwei-Anlagen-Fall (Skript Kap. 3).

    Maximiert U = E(x) − a·Var(x) für das Portfolio
    z·Aktien + (1−z)·Anleihen mit Korrelation rho:

        z* = [(mu1 − mu2) − 2a(rho·s1·s2 − s2²)] / [2a(s1² − 2rho·s1·s2 + s2²)]

    (Ableitung der Nutzenfunktion nach z, Nullsetzen; entspricht der Formel
    auf den Folien "Optimales Portfolio", Kap. 3.)
    """
    mu1, s1 = MU_AKTIEN, SIGMA_AKTIEN
    mu2, s2 = MU_ANLEIHEN, SIGMA_ANLEIHEN
    rho = KORRELATION

    zaehler = (mu1 - mu2) - 2 * a * (rho * s1 * s2 - s2**2)
    nenner = 2 * a * (s1**2 - 2 * rho * s1 * s2 + s2**2)
    z = zaehler / nenner
    return max(0.0, min(1.0, z))


def ermittle_risikoprofil(p: UserProfile) -> RisikoErgebnis:
    """Vollständige Risikoprofilierung aus dem Nutzerprofil."""
    score_b = _score_risikobereitschaft(p)
    score_t = _score_risikotragfaehigkeit(p)

    # Vorsichtsprinzip: die schwächere der beiden Dimensionen begrenzt.
    klasse = min(_score_zu_klasse(score_b), _score_zu_klasse(score_t))
    a = RISIKOAVERSION_JE_KLASSE[klasse]

    z_opt = optimale_aktienquote(a)
    z = z_opt
    begrenzungen: list[str] = []

    # Kappungen nach Zielgrößen-Logik (Kap. 1&2: Liquidität und Zeithorizont).
    horizont = p.zeithorizont_jahre or 0
    if horizont < 3:
        z = min(z, 0.10)
        begrenzungen.append(
            "Zeithorizont unter 3 Jahren: Aktienquote auf 10 % begrenzt, da "
            "Kursschwankungen kurzfristig nicht ausgesessen werden können."
        )
    elif horizont < 5:
        z = min(z, 0.30)
        begrenzungen.append("Zeithorizont unter 5 Jahren: Aktienquote auf 30 % begrenzt.")
    elif horizont < 10:
        z = min(z, 0.70)
        begrenzungen.append("Zeithorizont unter 10 Jahren: Aktienquote auf 70 % begrenzt.")

    if (p.notgroschen_monatsausgaben or 0) < 3:
        z = min(z, 0.50)
        begrenzungen.append(
            "Notgroschen unter 3 Monatsausgaben: Aktienquote auf 50 % begrenzt; "
            "zuerst Liquiditätsreserve aufbauen (Liquiditätsziel, Skript Kap. 1&2)."
        )

    if p.hat_konsumschulden:
        z = min(z, 0.30)
        begrenzungen.append(
            "Bestehende Konsumschulden: Tilgung ist eine risikofreie 'Rendite' in Höhe "
            "des Kreditzinses und hat Vorrang; Aktienquote auf 30 % begrenzt."
        )

    return RisikoErgebnis(
        risikoklasse=klasse,
        klassen_name=KLASSEN_NAMEN[klasse],
        risikoaversion_a=a,
        aktienquote_unbegrenzt=round(z_opt, 4),
        aktienquote_empfohlen=round(z, 4),
        begrenzungen=begrenzungen,
        teil_score_bereitschaft=score_b,
        teil_score_tragfaehigkeit=score_t,
    )
