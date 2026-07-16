"""Tests der Umschichtungs-Engine (Kauf-/Verkaufsempfehlungen mit Gebühren)."""

from advisor.rebalancing import Position, erstelle_umschichtungsplan

ZIEL = {
    "aktien_welt_industrielaender": 47.7,
    "aktien_schwellenlaender": 20.4,
    "anleihen_eur_investment_grade": 24.2,
    "geldmarkt_tagesgeld": 2.7,
    "gold": 5.0,
}


def test_neues_geld_zuerst_keine_verkaeufe_wenn_kapital_reicht():
    """Leichtes Übergewicht wird mit neuem Kapital verwässert statt verkauft."""
    positionen = [
        Position(name="MSCI World ETF", wert_eur=10000, kategorie="aktien_welt_industrielaender"),
        Position(name="Tagesgeld", wert_eur=8000, kategorie="geldmarkt_tagesgeld"),
    ]
    plan = erstelle_umschichtungsplan(positionen, ZIEL, neues_kapital_eur=10000)

    basis = 28000
    assert plan["basis_vermoegen_eur"] == basis
    # World ist mit 10k/28k = 35,7 % unter dem Ziel (47,7 %) -> kein Verkauf
    kategorien_verkauft = [v["kategorie"] for v in plan["verkaeufe"]]
    assert "aktien_welt_industrielaender" not in kategorien_verkauft
    # Tagesgeld ist massiv übergewichtet (28,6 % statt 2,7 %) -> Verkauf/Umbuchung
    assert "geldmarkt_tagesgeld" in kategorien_verkauft
    # Käufe füllen die Untergewichte
    gekauft = {k["kategorie"] for k in plan["kaeufe"]}
    assert "aktien_schwellenlaender" in gekauft
    assert "anleihen_eur_investment_grade" in gekauft


def test_kleines_uebergewicht_wird_gehalten_statt_verkauft():
    positionen = [
        Position(name="World", wert_eur=5000, kategorie="aktien_welt_industrielaender"),
        Position(name="EM", wert_eur=2200, kategorie="aktien_schwellenlaender"),
        Position(name="Anleihen", wert_eur=2400, kategorie="anleihen_eur_investment_grade"),
        Position(name="Geldmarkt", wert_eur=300, kategorie="geldmarkt_tagesgeld"),
        Position(name="Gold", wert_eur=600, kategorie="gold"),
    ]
    # Nahezu Ziel-Allokation, kleine Abweichungen, kein neues Kapital.
    plan = erstelle_umschichtungsplan(positionen, ZIEL, neues_kapital_eur=0)
    assert plan["verkaeufe"] == []
    assert plan["kaeufe"] == []
    assert any("Schwelle" in h for h in plan["hinweise"])


def test_gebuehren_mit_mindestgebuehr():
    positionen = [Position(name="Tagesgeld", wert_eur=20000, kategorie="geldmarkt_tagesgeld")]
    plan = erstelle_umschichtungsplan(
        positionen, ZIEL, neues_kapital_eur=0, gebuehr_prozent=0.25, gebuehr_min_eur=4.9
    )
    for trade in plan["verkaeufe"] + plan["kaeufe"]:
        erwartet = max(4.9, trade["betrag_eur"] * 0.25 / 100)
        assert abs(trade["gebuehr_eur"] - round(erwartet, 2)) < 0.01
    assert plan["gebuehren_summe_eur"] > 0


def test_sonstiges_wird_nie_automatisch_verkauft():
    positionen = [
        Position(name="Tesla-Aktien", wert_eur=15000, kategorie="sonstiges"),
        Position(name="Tagesgeld", wert_eur=5000, kategorie="geldmarkt_tagesgeld"),
    ]
    plan = erstelle_umschichtungsplan(positionen, ZIEL, neues_kapital_eur=0)
    assert all(v["kategorie"] != "sonstiges" for v in plan["verkaeufe"])
    assert plan["nicht_zugeordnete_positionen"][0]["name"] == "Tesla-Aktien"
    assert any("Tesla" in h for h in plan["hinweise"])


def test_kaufsumme_uebersteigt_nie_verfuegbares_kapital():
    positionen = [Position(name="World", wert_eur=1000, kategorie="aktien_welt_industrielaender")]
    plan = erstelle_umschichtungsplan(positionen, ZIEL, neues_kapital_eur=2000)
    kaufsumme = sum(k["betrag_eur"] for k in plan["kaeufe"])
    verkauf_erloes = sum(v["betrag_eur"] for v in plan["verkaeufe"])
    assert kaufsumme <= 2000 + verkauf_erloes + 0.01


def test_steuerschaetzung_mit_teilfreistellung_bei_aktien_gewinn():
    """Aktien-ETF mit 50 % Gewinn wird teilverkauft: 30 % teilfreigestellt."""
    positionen = [
        Position(
            name="World", wert_eur=30000, einstandswert_eur=20000,
            kategorie="aktien_welt_industrielaender",
        ),
    ]
    plan = erstelle_umschichtungsplan(positionen, ZIEL, neues_kapital_eur=0)
    verkauf = next(v for v in plan["verkaeufe"] if v["kategorie"] == "aktien_welt_industrielaender")
    # Gewinnquote 1/3 des Verkaufsbetrags
    erwarteter_gewinn = verkauf["betrag_eur"] * (30000 - 20000) / 30000
    assert abs(verkauf["geschaetzter_gewinn_eur"] - round(erwarteter_gewinn, 2)) < 0.05
    erwartete_steuer = erwarteter_gewinn * 0.70 * 0.26375
    assert abs(verkauf["geschaetzte_steuer_eur"] - round(erwartete_steuer, 2)) < 0.05
    assert plan["geschaetzte_steuer_summe_eur"] > 0


def test_verlustverkauf_erzeugt_verlustverrechnungs_hinweis():
    positionen = [
        Position(name="Gold-ETC", wert_eur=5000, einstandswert_eur=7000, kategorie="gold"),
        Position(name="Tagesgeld", wert_eur=1000, kategorie="geldmarkt_tagesgeld"),
    ]
    ziel_ohne_gold = {
        "aktien_welt_industrielaender": 70.0,
        "anleihen_eur_investment_grade": 30.0,
    }
    plan = erstelle_umschichtungsplan(positionen, ziel_ohne_gold, neues_kapital_eur=0)
    gold_verkauf = next(v for v in plan["verkaeufe"] if v["kategorie"] == "gold")
    assert gold_verkauf["geschaetzter_gewinn_eur"] < 0
    assert gold_verkauf["geschaetzte_steuer_eur"] == 0.0
    assert "Verlustverrechnung" in gold_verkauf["steuer_hinweis"]


def test_verlustposition_wird_als_steuertipp_genannt():
    """Gewinn-Verkauf + andere Position im Minus -> Verlustverrechnungs-Tipp."""
    positionen = [
        Position(
            name="World", wert_eur=30000, einstandswert_eur=15000,
            kategorie="aktien_welt_industrielaender",
        ),
        Position(
            name="EM-ETF", wert_eur=2000, einstandswert_eur=3000,
            kategorie="aktien_schwellenlaender",
        ),
    ]
    plan = erstelle_umschichtungsplan(positionen, ZIEL, neues_kapital_eur=0)
    assert plan["geschaetzte_steuer_summe_eur"] > 0
    assert any("Verlustverrechnung" in h and "EM-ETF" in h for h in plan["hinweise"])


def test_unbekannter_einstand_gibt_hinweis_statt_schaetzung():
    positionen = [Position(name="Tagesgeld", wert_eur=20000, kategorie="geldmarkt_tagesgeld")]
    plan = erstelle_umschichtungsplan(positionen, ZIEL, neues_kapital_eur=0)
    verkauf = plan["verkaeufe"][0]
    assert "geschaetzter_gewinn_eur" not in verkauf
    assert "unbekannt" in verkauf["steuer_hinweis"]
