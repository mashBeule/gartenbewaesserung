"""Regeln + Ausnahmen → Läufe, inklusive Sommerzeit."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from bewaesserung.planung.plan import ORTSZEIT, Ausnahme, AusnahmeAktion, Regel, laeufe, ortszeit

MO_MI_FR = 0b0010101
R = Regel(1, zone_id=7, wochentage=MO_MI_FR, startzeit=time(6, 0), dauer_min=15)
WOCHE = (date(2026, 6, 1), date(2026, 6, 7))     # Montag bis Sonntag


def test_wochentage_und_dauer():
    ls = laeufe([R], [], *WOCHE)
    assert [l.start.date().weekday() for l in ls] == [0, 2, 4]
    assert all(l.dauer_s == 900 and l.zone_id == 7 for l in ls)
    assert ls[0].start == datetime(2026, 6, 1, 6, 0, tzinfo=ORTSZEIT)
    assert len({l.schluessel for l in ls}) == 3


def test_gueltigkeit_und_inaktiv():
    r = Regel(1, 7, MO_MI_FR, time(6), 15, gueltig_von=date(2026, 6, 2), gueltig_bis=date(2026, 6, 4))
    assert [l.start.day for l in laeufe([r], [], *WOCHE)] == [3]
    assert laeufe([Regel(1, 7, MO_MI_FR, time(6), 15, aktiv=False)], [], *WOCHE) == []


def test_ausnahmen():
    ausnahmen = [
        Ausnahme(1, 1, date(2026, 6, 1), AusnahmeAktion.AUSLASSEN),
        Ausnahme(2, 1, date(2026, 6, 3), AusnahmeAktion.VERSCHIEBEN, "2026-06-04T20:30"),
        Ausnahme(3, 1, date(2026, 6, 5), AusnahmeAktion.DAUER_ANPASSEN, "5"),
    ]
    mo, mi, fr = laeufe([R], ausnahmen, *WOCHE)
    assert mo.ausgelassen and "ausgelassen" in mo.hinweis
    assert mi.start == datetime(2026, 6, 4, 20, 30, tzinfo=ORTSZEIT) and mi.schluessel.endswith("2026-06-03")
    assert fr.dauer_s == 300


def test_verschiebung_ueber_die_bereichsgrenze():
    a = Ausnahme(1, 1, date(2026, 5, 29), AusnahmeAktion.VERSCHIEBEN, "2026-06-01T07:00")   # Fr → Mo
    starts = [l.start for l in laeufe([R], [a], *WOCHE)]
    assert datetime(2026, 6, 1, 7, 0, tzinfo=ORTSZEIT) in starts


def test_ungueltige_ausnahme_wird_ignoriert():
    a = Ausnahme(1, 1, date(2026, 6, 1), AusnahmeAktion.VERSCHIEBEN, "morgen")
    assert a.fehler()
    assert laeufe([R], [a], *WOCHE)[0].start.hour == 6


def test_sommerzeit_luecke_und_doppelte_stunde():
    # 29.03.2026: 02:00 → 03:00, 02:30 gibt es nicht
    start = ortszeit(date(2026, 3, 29), time(2, 30))
    assert start.astimezone(timezone.utc) == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)
    assert start.hour == 3
    # 25.10.2026: 02:30 gibt es zweimal, gilt beim ersten Mal (noch Sommerzeit)
    start = ortszeit(date(2026, 10, 25), time(2, 30))
    assert start.utcoffset() == timedelta(hours=2)


def test_regel_fehler():
    assert Regel(1, 1, 0, time(6), 15).fehler()
    assert Regel(1, 1, 1, time(6), 0).fehler()
    assert Regel(1, 1, 1, time(6), 5, date(2026, 6, 2), date(2026, 6, 1)).fehler()
    assert not R.fehler()
