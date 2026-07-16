"""Nutzerprofil und Session-State.

Das Profil bildet die im Dialog erfragten Angaben strukturiert ab
(Anlegerprofilierung analog zum Portfolio-Management-Prozess aus dem
Finanzmanagement-Skript, Kap. 1&2: Ertrags- und Risikoziele als Ausgangspunkt
der Asset-Allokation). Der Agent befüllt die Felder schrittweise über Tools;
das Profil lebt im Session-State des Servers, sodass bereits beantwortete
Fragen nicht erneut gestellt werden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

# Verständlich formulierte Antwortoptionen statt bloßem "hoch/mittel/niedrig":
# Reaktion auf einen hypothetischen Kursverlust von 20 % (Stresstest-Gedanke,
# Skript Kap. 3: Schockszenarien auf dem Aktienmarkt).
ReaktionKursverlust = Literal[
    "alles_verkaufen",       # würde verkaufen, um weitere Verluste zu vermeiden
    "teilweise_verkaufen",   # würde einen Teil verkaufen
    "beunruhigt_halten",     # würde halten, aber schlecht schlafen
    "gelassen_halten",       # würde halten und abwarten
    "nachkaufen",            # würde die niedrigen Kurse zum Nachkaufen nutzen
]

Anlageerfahrung = Literal["keine", "grundkenntnisse", "fortgeschritten", "sehr_erfahren"]


class UserProfile(BaseModel):
    """Alle beratungsrelevanten Angaben des Nutzers (werden im Dialog erfragt)."""

    # 1. Ziel und Horizont
    anlageziel: str | None = Field(
        default=None, description="z. B. Altersvorsorge, Vermögensaufbau, größere Anschaffung"
    )
    zeithorizont_jahre: float | None = Field(
        default=None, description="Geplanter Anlagehorizont in Jahren"
    )

    # 2. Vorhandene Anlagen
    vorhandene_anlagen: str | None = Field(
        default=None,
        description="Bestehende Assets in Kurzform, z. B. 'ETF-Depot 10k, Tagesgeld 5k' oder 'keine'",
    )
    depot_vorhanden: bool | None = Field(
        default=None, description="Hat der Nutzer bereits ein Wertpapierdepot?"
    )

    # 3. Kapital
    monatliche_sparrate_eur: float | None = Field(
        default=None, description="Monatlich verfügbarer Sparbetrag in EUR"
    )
    einmalbetrag_eur: float | None = Field(
        default=None, description="Einmalig anzulegender Betrag in EUR (0, wenn keiner)"
    )

    # 4. Risikobereitschaft (subjektiv)
    reaktion_kursverlust_20_prozent: ReaktionKursverlust | None = Field(
        default=None,
        description="Reaktion auf einen hypothetischen Kursverlust von 20 % im Depot",
    )
    max_akzeptierter_verlust_prozent: float | None = Field(
        default=None,
        description="Zwischenzeitlicher Wertverlust in %, der emotional noch tragbar wäre",
    )

    # 5. Risikotragfähigkeit (objektiv)
    schulden: str | None = Field(
        default=None,
        description="Bestehende Schulden in Kurzform, z. B. 'Konsumkredit 5k', 'Immobilienkredit', 'keine'",
    )
    hat_konsumschulden: bool | None = Field(
        default=None, description="Bestehen Konsum-/Ratenkredite oder Dispo-Schulden?"
    )
    notgroschen_monatsausgaben: float | None = Field(
        default=None,
        description="Liquiditätsreserve in Monatsausgaben (z. B. 3 = drei Netto-Monatsausgaben)",
    )

    # 6. Rahmendaten
    alter: int | None = Field(default=None, description="Alter in Jahren")
    land_steuerkontext: str | None = Field(
        default=None, description="Wohnsitzland bzw. Steuerkontext, z. B. 'Deutschland'"
    )
    anlageerfahrung: Anlageerfahrung | None = Field(
        default=None, description="Erfahrung mit Wertpapieren/Kapitalanlagen"
    )

    # Ergebnis der Risikoprofilierung (wird berechnet, nicht erfragt)
    risikoklasse: int | None = Field(
        default=None, description="Ermittelte Risikoklasse 1 (sehr defensiv) bis 5 (sehr offensiv)"
    )

    def fehlende_angaben(self) -> list[str]:
        """Noch nicht erfragte Pflichtangaben, in sinnvoller Frage-Reihenfolge."""
        return [name for name in PFLICHTANGABEN if getattr(self, name) is None]

    def ist_vollstaendig(self) -> bool:
        return not self.fehlende_angaben()

    def fortschritt_zeile(self) -> str:
        """Fortschrittsanzeige für die Profilierung, z. B. '6/13 · ▓▓▓▓▓▓░░░░░░░'."""
        gesamt = len(PFLICHTANGABEN)
        erfasst = gesamt - len(self.fehlende_angaben())
        balken = "▓" * erfasst + "░" * (gesamt - erfasst)
        return f"📋 Profil-Fortschritt: {erfasst}/{gesamt} Angaben · {balken}"


# Pflichtangaben in sinnvoller Frage-Reihenfolge; Basis für Dialogsteuerung
# und Fortschrittsanzeige.
PFLICHTANGABEN: list[str] = [
    "anlageziel",
    "zeithorizont_jahre",
    "alter",
    "land_steuerkontext",
    "anlageerfahrung",
    "vorhandene_anlagen",
    "depot_vorhanden",
    "monatliche_sparrate_eur",
    "einmalbetrag_eur",
    "schulden",
    "notgroschen_monatsausgaben",
    "reaktion_kursverlust_20_prozent",
    "max_akzeptierter_verlust_prozent",
]


@dataclass
class AdvisorDeps:
    """Dependencies des Agenten: hält das Nutzerprofil als Session-State.

    Hinweis: `agent.to_web(deps=...)` verwendet dasselbe Deps-Objekt für alle
    Requests des Server-Prozesses. Für den vorgesehenen Einsatz (lokale
    Einzelnutzer-App) ist das ausreichend; siehe LIMITATIONS.md.
    """

    profile: UserProfile = field(default_factory=UserProfile)

    def reset(self) -> None:
        self.profile = UserProfile()
