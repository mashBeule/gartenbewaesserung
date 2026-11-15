from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bewaesserung.gardena.budget import ApiBudget, BudgetErschoepft, Prioritaet

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _fuellen(b: ApiBudget, anzahl: int, p=Prioritaet.SICHERHEIT) -> None:
    for i in range(anzahl):
        b.anfordern(p, T0 + timedelta(minutes=i))


def test_prioritaeten_lassen_reserven_uebrig():
    b = ApiBudget()
    _fuellen(b, 600)
    jetzt = T0 + timedelta(days=1)
    with pytest.raises(BudgetErschoepft):
        b.anfordern(Prioritaet.NORMAL, jetzt)
    for i in range(50):
        b.anfordern(Prioritaet.SYSTEM, jetzt + timedelta(minutes=i))
    spaeter = jetzt + timedelta(hours=1)
    with pytest.raises(BudgetErschoepft):
        b.anfordern(Prioritaet.SYSTEM, spaeter)
    b.anfordern(Prioritaet.SICHERHEIT, spaeter)


def test_woche_ist_gleitend():
    b = ApiBudget()
    _fuellen(b, 600)
    with pytest.raises(BudgetErschoepft) as f:
        b.anfordern(Prioritaet.NORMAL, T0 + timedelta(days=6))
    assert f.value.wieder_frei == T0 + timedelta(days=7)
    b.anfordern(Prioritaet.NORMAL, T0 + timedelta(days=7, seconds=1))


def test_zehn_in_zehn_sekunden():
    b = ApiBudget()
    for i in range(10):
        b.anfordern(Prioritaet.SICHERHEIT, T0 + timedelta(milliseconds=100 * i))
    with pytest.raises(BudgetErschoepft) as f:
        b.anfordern(Prioritaet.SICHERHEIT, T0 + timedelta(seconds=2))
    assert f.value.wieder_frei == T0 + timedelta(seconds=10)
    b.anfordern(Prioritaet.SICHERHEIT, T0 + timedelta(seconds=10, milliseconds=1))


def test_sperre_nach_429_gilt_fuer_alle():
    b = ApiBudget()
    b.gesperrt_melden(T0)
    with pytest.raises(BudgetErschoepft, match="Sperre"):
        b.anfordern(Prioritaet.SICHERHEIT, T0 + timedelta(hours=23))
    b.anfordern(Prioritaet.SICHERHEIT, T0 + timedelta(hours=24))
