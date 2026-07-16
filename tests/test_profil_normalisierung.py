"""Tests der toleranten Eingabe-Normalisierung im Nutzerprofil.

Das LLM übergibt gelegentlich freie Formulierungen statt der exakten
Literale – diese dürfen keinen Validierungsfehler auslösen.
"""

import pytest

from advisor.profile import UserProfile


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("Anfänger/Grundkenntnisse", "grundkenntnisse"),
        ("Grundkenntnisse", "grundkenntnisse"),
        ("etwas Erfahrung", "grundkenntnisse"),
        ("fortgeschritten", "fortgeschritten"),
        ("Fortgeschritten", "fortgeschritten"),
        ("sehr erfahren", "sehr_erfahren"),
        ("Experte", "sehr_erfahren"),
        ("keine", "keine"),
        ("noch nie investiert", "keine"),
    ],
)
def test_anlageerfahrung_freitext(eingabe: str, erwartet: str):
    p = UserProfile(anlageerfahrung=eingabe)  # type: ignore[arg-type]
    assert p.anlageerfahrung == erwartet


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [
        ("gelassen halten", "gelassen_halten"),
        ("Ich würde abwarten", "gelassen_halten"),
        ("nicht verkaufen", "gelassen_halten"),
        ("alles verkaufen", "alles_verkaufen"),
        ("Ich würde sofort verkaufen", "alles_verkaufen"),
        ("einen Teil verkaufen", "teilweise_verkaufen"),
        ("beunruhigt halten", "beunruhigt_halten"),
        ("würde schlecht schlafen", "beunruhigt_halten"),
        ("nachkaufen", "nachkaufen"),
        ("Ich würde nachlegen", "nachkaufen"),
    ],
)
def test_verlust_reaktion_freitext(eingabe: str, erwartet: str):
    p = UserProfile(reaktion_kursverlust_20_prozent=eingabe)  # type: ignore[arg-type]
    assert p.reaktion_kursverlust_20_prozent == erwartet


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [("ja", True), ("Ja", True), ("nein", False), ("Nein", False), ("true", True), ("false", False)],
)
def test_deutsche_booleans(eingabe: str, erwartet: bool):
    p = UserProfile(depot_vorhanden=eingabe, hat_konsumschulden=eingabe)  # type: ignore[arg-type]
    assert p.depot_vorhanden is erwartet
    assert p.hat_konsumschulden is erwartet


def test_unbrauchbarer_freitext_schlaegt_weiter_fehl():
    with pytest.raises(Exception):
        UserProfile(anlageerfahrung="banane")  # type: ignore[arg-type]
