"""Tests der deterministischen Fachlogik (Risikoprofilierung + Strategie)."""

from advisor.profile import UserProfile
from advisor.risk import RISIKOAVERSION_JE_KLASSE, ermittle_risikoprofil, optimale_aktienquote
from advisor.strategy import erstelle_strategie


def _profil_offensiv() -> UserProfile:
    return UserProfile(
        anlageziel="Altersvorsorge",
        zeithorizont_jahre=30,
        alter=30,
        land_steuerkontext="Deutschland",
        anlageerfahrung="grundkenntnisse",
        vorhandene_anlagen="Tagesgeld 10k",
        depot_vorhanden=False,
        monatliche_sparrate_eur=400,
        einmalbetrag_eur=5000,
        schulden="keine",
        hat_konsumschulden=False,
        notgroschen_monatsausgaben=6,
        reaktion_kursverlust_20_prozent="gelassen_halten",
        max_akzeptierter_verlust_prozent=30,
    )


def test_aktienquote_faellt_mit_risikoaversion():
    quoten = [optimale_aktienquote(a) for a in sorted(RISIKOAVERSION_JE_KLASSE.values())]
    assert quoten == sorted(quoten, reverse=True)
    assert all(0.0 <= q <= 1.0 for q in quoten)


def test_langfristiges_profil_wird_wachstumsorientiert():
    r = ermittle_risikoprofil(_profil_offensiv())
    assert r.risikoklasse == 4
    assert r.aktienquote_empfohlen >= 0.6
    assert not r.begrenzungen


def test_vorsichtsprinzip_kappt_bei_kurzem_horizont_und_schulden():
    p = _profil_offensiv().model_copy(
        update={
            "zeithorizont_jahre": 2.0,
            "notgroschen_monatsausgaben": 0.0,
            "hat_konsumschulden": True,
            "reaktion_kursverlust_20_prozent": "alles_verkaufen",
            "max_akzeptierter_verlust_prozent": 5.0,
        }
    )
    r = ermittle_risikoprofil(p)
    assert r.risikoklasse == 1
    assert r.aktienquote_empfohlen <= 0.10
    assert len(r.begrenzungen) >= 2


def test_strategie_summen_konsistent():
    p = _profil_offensiv()
    s = erstelle_strategie(p, ermittle_risikoprofil(p))
    assert abs(sum(s["allokation_prozent"].values()) - 100) < 0.2
    assert abs(sum(s["sparplan_aufteilung_eur"].values()) - 400) < 0.01
    assert abs(sum(s["einmalbetrag_aufteilung_eur"].values()) - 5000) < 0.02
    # Keine Sparplan-Position unter der Mindestrate
    assert all(v >= 25 for v in s["sparplan_aufteilung_eur"].values())


def test_unvollstaendiges_profil_meldet_offene_angaben():
    p = UserProfile(anlageziel="Vermögensaufbau")
    offen = p.fehlende_angaben()
    assert "zeithorizont_jahre" in offen
    assert not p.ist_vollstaendig()
