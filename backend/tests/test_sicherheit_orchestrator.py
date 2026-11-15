"""Sicherheitsregeln 1–4 und 8 am Orchestrator, gegen den Gardena-Mock."""
from __future__ import annotations

import asyncio

import pytest

from bewaesserung.gardena.budget import BudgetErschoepft
from bewaesserung.sicherheit.ereignisse import Ereignisart
from bewaesserung.sicherheit.kopplung import LaufAbgelehnt
from bewaesserung.sicherheit.orchestrator import LaufStatus
from conftest import S1, V1, Aufbau, ausfuehren


async def _lauf_bis_steckdose_an(a: Aufbau, ventil_s: int = 600) -> asyncio.Task:
    aufgabe = asyncio.create_task(a.orchestrator.lauf("k1", ventil_s))
    ok = await a.zustand.warten_bis(lambda z: z.geraet(S1).aktivitaet == "TIME_LIMITED_ON", 1)
    assert ok
    return aufgabe


def test_regulaerer_lauf_ventil_zuerst_steckdose_kuerzer(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufgabe = await _lauf_bis_steckdose_an(aufbau, 600)
        # Regel 3: Ventil zuerst, dann Steckdose; Regel 1: beide mit Sekunden
        assert aufbau.gardena.folge() == [("START", V1), ("START", S1)]
        # Regel 2: 600 − 20 − 30 = 550, abgerundet auf 60 s
        assert aufbau.gardena.starts() == {V1: 600, S1: 540}
        aufbau.gardena.ablaufen(S1)          # Steckdose läuft zuerst aus
        aufbau.gardena.ablaufen(V1)
        ergebnis = await aufgabe
        assert ergebnis.status is LaufStatus.BEENDET
        assert ergebnis.steckdose_s == 540
        assert not aufbau.ereignisse.liste
    ausfuehren(szenario())


def test_ventil_bestaetigt_nicht_steckdose_wird_nie_eingeschaltet(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufbau.gardena.geraete[V1].reagiert = False   # Befehl angenommen, Gerät reagiert nicht (Regel 4)
        ergebnis = await aufbau.orchestrator.lauf("k1", 600)
        assert ergebnis.status is LaufStatus.FEHLGESCHLAGEN
        assert ("START", S1) not in aufbau.gardena.folge()
        assert aufbau.gardena.folge()[-1] == ("STOP", V1)
        assert Ereignisart.VENTIL_UNBESTAETIGT in aufbau.ereignisse.arten()
    ausfuehren(szenario())


def test_steckdose_bestaetigt_nicht_abbruch_steckdose_zuerst(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufbau.gardena.geraete[S1].reagiert = False
        ergebnis = await aufbau.orchestrator.lauf("k1", 600)
        assert ergebnis.status is LaufStatus.FEHLGESCHLAGEN
        assert aufbau.gardena.folge() == [("START", V1), ("START", S1), ("STOP", S1), ("STOP", V1)]
        assert Ereignisart.STECKDOSE_UNBESTAETIGT in aufbau.ereignisse.arten()
    ausfuehren(szenario())


def test_abbruch_stoppt_steckdose_vor_ventil(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufgabe = await _lauf_bis_steckdose_an(aufbau)
        assert await aufbau.orchestrator.abbrechen("k1", "Not-Aus")
        ergebnis = await aufgabe
        assert ergebnis.status is LaufStatus.ABGEBROCHEN
        assert aufbau.gardena.folge()[2:] == [("STOP", S1), ("STOP", V1)]
        assert aufbau.zustand.geraet(V1).aktivitaet == "CLOSED"
    ausfuehren(szenario())


def test_steckdose_unklar_ventil_bleibt_offen_bis_steckdosenende():
    async def szenario():
        a = Aufbau()
        schlaf = []
        async def schlafen(s):
            schlaf.append(s)
        a.orchestrator._schlafen = schlafen
        a.bereit(interlock=False)
        aufgabe = await _lauf_bis_steckdose_an(a)
        a.gardena.geraete[S1].reagiert = False      # Stopp kommt nicht an
        await a.orchestrator.abbrechen("k1")
        # Ventil wurde erst nach Abwarten der Steckdosenlaufzeit gestoppt
        assert schlaf and 500 < schlaf[0] <= 540
        assert a.gardena.folge()[2:] == [("STOP", S1), ("STOP", V1)]
        assert Ereignisart.STECKDOSE_ZUSTAND_UNKLAR in a.ereignisse.arten()
        aufgabe.cancel()
    ausfuehren(szenario())


def test_hoechstlaufzeit_wird_abgelehnt(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        with pytest.raises(LaufAbgelehnt, match="Höchstlaufzeit"):
            await aufbau.orchestrator.lauf("k1", 1860)
        assert aufbau.gardena.befehle == []
        assert Ereignisart.LAUF_ABGELEHNT in aufbau.ereignisse.arten()
    ausfuehren(szenario())


@pytest.mark.parametrize("vorbereitung, meldung", [
    (lambda a: setattr(a.orchestrator, "freigegeben", False), "Startprüfung"),
    (lambda a: a.gardena.trennen(), "Verbindung"),
    (lambda a: a.gardena.setzen(S1, "FOREVER_ON"), "Steckdose ist nicht aus"),
    (lambda a: a.gardena.setzen(V1, "MANUAL_WATERING"), "Ventil ist schon offen"),
])
def test_lauf_wird_ohne_sicheren_ausgangszustand_abgelehnt(vorbereitung, meldung):
    async def szenario():
        a = Aufbau()
        a.bereit(interlock=False)
        vorbereitung(a)
        with pytest.raises(LaufAbgelehnt, match=meldung):
            await a.orchestrator.lauf("k1", 600)
        assert not [b for b in a.gardena.befehle if b.art == "START"]
    ausfuehren(szenario())


def test_zweiter_lauf_auf_derselben_steckdose_wird_abgelehnt(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufgabe = await _lauf_bis_steckdose_an(aufbau)
        with pytest.raises(LaufAbgelehnt, match="belegt"):
            await aufbau.orchestrator.lauf("k1", 600)
        await aufbau.orchestrator.abbrechen("k1")
        await aufgabe
    ausfuehren(szenario())


def test_spaete_ventilbestaetigung_verkuerzt_steckdose():
    async def szenario():
        a = Aufbau()
        jetzt = [1000.0]
        a.orchestrator._uhr = lambda: jetzt[0]
        a.bereit(interlock=False)
        a.gardena.geraete[V1].reagiert = False
        aufgabe = asyncio.create_task(a.orchestrator.lauf("k1", 600))
        await asyncio.sleep(0.01)
        jetzt[0] += 95                     # Bestätigung kommt erst nach 95 s (Vorlauf 20 s)
        a.gardena.setzen(V1, "MANUAL_WATERING")
        await a.zustand.warten_bis(lambda z: z.geraet(S1).aktivitaet == "TIME_LIMITED_ON", 1)
        # 600 − 95 − 30 = 475 → 420; Steckdose endet weiter vor dem Ventil
        assert a.gardena.starts()[S1] == 420
        a.gardena.geraete[V1].reagiert = True
        await a.orchestrator.abbrechen("k1")
        await aufgabe
    ausfuehren(szenario())


def test_befehlsfehler_beim_steckdosenstart_bricht_ab(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufbau.gardena.fehler[S1] = BudgetErschoepft("Wochenbudget erschöpft")
        ergebnis = await aufbau.orchestrator.lauf("k1", 600)
        assert ergebnis.status is LaufStatus.FEHLGESCHLAGEN
        assert aufbau.gardena.folge()[-1] == ("STOP", V1)
        assert aufbau.zustand.geraet(V1).aktivitaet == "CLOSED"
    ausfuehren(szenario())
