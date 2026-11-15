"""Sicherheitsregeln 2 und 8: Laufzeitrechnung und Höchstlaufzeit."""
from __future__ import annotations

import pytest

from bewaesserung.sicherheit.kopplung import Kopplung, LaufAbgelehnt, steckdosen_laufzeit

K = Kopplung("k", "v:1", "s:1", vorlauf_ventil_s=20, nachlauf_ventil_s=30, max_laufzeit_s=1800)


@pytest.mark.parametrize("ventil_s, steckdose_s", [(600, 540), (120, 60), (1800, 1740)])
def test_steckdose_endet_vor_dem_ventil(ventil_s, steckdose_s):
    assert K.pruefen(ventil_s) == steckdose_s
    assert steckdose_s <= ventil_s - K.vorlauf_ventil_s - K.nachlauf_ventil_s


@pytest.mark.parametrize("vergangen, erwartet", [(0, 540), (21, 540), (81, 480), (95, 420), (510, 60), (571, 0)])
def test_vergangene_zeit_verkuerzt_die_steckdose(vergangen, erwartet):
    s = steckdosen_laufzeit(K, 600, vergangen)
    assert s == erwartet
    if s:
        assert vergangen + s <= 600 - K.nachlauf_ventil_s


@pytest.mark.parametrize("ventil_s, meldung", [
    (1860, "Höchstlaufzeit"),
    (0, "positive"),
    (-60, "positive"),
    (90, "Vielfaches"),
    (60, "zu kurz"),
    (600.0, "ganze Zahl"),
])
def test_ungueltige_laufzeiten_werden_abgelehnt(ventil_s, meldung):
    with pytest.raises(LaufAbgelehnt, match=meldung):
        K.pruefen(ventil_s)


@pytest.mark.parametrize("kopplung, meldung", [
    (Kopplung("k", "v", "v", 20, 30, 1800), "dasselbe"),
    (Kopplung("k", "v", "s", -1, 30, 1800), "Vorlauf"),
    (Kopplung("k", "v", "s", 20, 0, 1800), "Nachlauf"),
    (Kopplung("k", "v", "s", 20, 30, 0), "Höchstlaufzeit"),
    (Kopplung("k", "v", "s", 20, 30, 99999), "Höchstlaufzeit"),
    (Kopplung("k", "v", "s", 60, 60, 120), "keine Steckdosenlaufzeit"),
])
def test_ungueltige_kopplungen(kopplung, meldung):
    assert any(meldung in f for f in kopplung.fehler())
    with pytest.raises(LaufAbgelehnt):
        kopplung.pruefen(120)
