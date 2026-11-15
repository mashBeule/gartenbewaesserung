"""Sicherheitsregeln 5 und 7 (Erkennen) am Interlock-Wächter, gegen den Gardena-Mock."""
from __future__ import annotations

import asyncio

from bewaesserung.sicherheit.ereignisse import Ereignisart
from conftest import K1, S1, V1, Aufbau, ausfuehren
from bewaesserung.sicherheit.kopplung import Kopplung


def test_steckdose_an_ventil_zu_wird_sofort_gestoppt(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        await aufbau.interlock.abwarten()
        aufbau.gardena.setzen(S1, "FOREVER_ON")      # z. B. in der Gardena-App eingeschaltet
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.folge() == [("STOP", S1)]
        assert aufbau.zustand.geraet(S1).aktivitaet == "OFF"
        assert aufbau.ereignisse.arten() == [Ereignisart.INTERLOCK]
    ausfuehren(szenario())


def test_ventil_waehrend_des_laufs_von_aussen_geschlossen(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufgabe = asyncio.create_task(aufbau.orchestrator.lauf("k1", 600))
        await aufbau.zustand.warten_bis(lambda z: z.geraet(S1).aktivitaet == "TIME_LIMITED_ON", 1)
        await aufbau.interlock.abwarten()
        assert ("STOP", S1) not in aufbau.gardena.folge()   # Lauf ist in Ordnung
        aufbau.gardena.setzen(V1, "CLOSED")                 # Ventil in der Gardena-App geschlossen
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.folge()[-1] == ("STOP", S1)
        assert Ereignisart.INTERLOCK in aufbau.ereignisse.arten()
        ergebnis = await aufgabe
        assert ergebnis.grund == "Ventil vorzeitig geschlossen"
    ausfuehren(szenario())


def test_zeitplan_lauf_am_ventil_stoppt_steckdose_zuerst(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        await aufbau.interlock.abwarten()
        aufbau.gardena.setzen(V1, "SCHEDULED_WATERING")
        aufbau.gardena.setzen(S1, "SCHEDULED_ON")       # beide Meldungen vor der Prüfung
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.folge() == [("STOP", S1), ("STOP", V1)]
        assert set(aufbau.ereignisse.arten()) == {Ereignisart.ZEITPLAN_LAUF}
        assert "Gardena-App" in aufbau.ereignisse.liste[0].text
    ausfuehren(szenario())


def test_zeitplan_lauf_nur_am_ventil_stoppt_ventil(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        await aufbau.interlock.abwarten()
        aufbau.gardena.setzen(V1, "SCHEDULED_WATERING")
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.folge() == [("STOP", V1)]
    ausfuehren(szenario())


def test_zeitplan_lauf_an_ungekoppeltem_ventil_wird_gestoppt(aufbau: Aufbau):
    async def szenario():
        aufbau.gardena.ventil("ventil-2:1", "Hecke")
        aufbau.bereit()
        await aufbau.interlock.abwarten()
        aufbau.gardena.setzen("ventil-2:1", "SCHEDULED_WATERING")
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.folge() == [("STOP", "ventil-2:1")]
    ausfuehren(szenario())


def test_steckdose_mit_zeitplan_wird_gestoppt_auch_bei_offenem_ventil(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufbau.gardena.geraete[V1].aktivitaet = "MANUAL_WATERING"
        aufbau.gardena.setzen(S1, "SCHEDULED_ON")
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.folge() == [("STOP", S1)]
        assert aufbau.ereignisse.arten() == [Ereignisart.ZEITPLAN_LAUF]
    ausfuehren(szenario())


def test_unbekannter_steckdosenzustand_gilt_als_an(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufbau.gardena.setzen(S1, "IRGENDWAS_NEUES")
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.folge() == [("STOP", S1)]
    ausfuehren(szenario())


def test_abriss_stoppt_laufende_steckdose_vorsorglich(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufgabe = asyncio.create_task(aufbau.orchestrator.lauf("k1", 600))
        await aufbau.zustand.warten_bis(lambda z: z.geraet(S1).aktivitaet == "TIME_LIMITED_ON", 1)
        aufbau.gardena.trennen(geplant=False)
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.folge()[-1] == ("STOP", S1)
        assert Ereignisart.VERBINDUNG_ABGERISSEN in aufbau.ereignisse.arten()
        aufgabe.cancel()
    ausfuehren(szenario())


def test_geplante_erneuerung_stoppt_nichts(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufgabe = asyncio.create_task(aufbau.orchestrator.lauf("k1", 600))
        await aufbau.zustand.warten_bis(lambda z: z.geraet(S1).aktivitaet == "TIME_LIMITED_ON", 1)
        aufbau.gardena.trennen(geplant=True)
        await aufbau.interlock.abwarten()
        aufbau.gardena.verbinden()
        await aufbau.interlock.abwarten()
        assert ("STOP", S1) not in aufbau.gardena.folge()
        aufgabe.cancel()
    ausfuehren(szenario())


def test_anlauf_wartet_auf_spaet_gemeldetes_ventil(aufbau: Aufbau):
    async def szenario():
        aufbau.gardena.geraete[V1].aktivitaet = "MANUAL_WATERING"
        aufbau.gardena.geraete[S1].aktivitaet = "TIME_LIMITED_ON"
        aufbau.interlock.starten()
        aufbau.gardena.verbinden(nur=[S1])            # Steckdose kommt vor dem Ventil
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.befehle == []
        aufbau.gardena.nachreichen(V1)
        await asyncio.sleep(0.3)                      # Anlaufzeit 0,2 s verstreicht
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.befehle == []
    ausfuehren(szenario())


def test_anlauf_stoppt_wenn_ventil_ausbleibt(aufbau: Aufbau):
    async def szenario():
        aufbau.gardena.geraete[S1].aktivitaet = "TIME_LIMITED_ON"
        aufbau.interlock.starten()
        aufbau.gardena.verbinden(nur=[S1])
        await asyncio.sleep(0.3)
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.folge() == [("STOP", S1)]
    ausfuehren(szenario())


def test_stopp_wird_mit_abstand_wiederholt_wenn_steckdose_an_bleibt(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufbau.gardena.geraete[S1].reagiert = False
        aufbau.gardena.setzen(S1, "FOREVER_ON")
        await aufbau.interlock.abwarten()
        aufbau.gardena.setzen(S1, "FOREVER_ON")         # weitere Meldungen lösen keinen neuen Stopp aus
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.folge() == [("STOP", S1)]
        await asyncio.sleep(0.4)                       # erste Wiederholung nach 0,3 s
        await aufbau.interlock.abwarten()
        assert aufbau.gardena.folge() == [("STOP", S1), ("STOP", S1)]
    ausfuehren(szenario())


def test_steckdose_mit_zwei_ventilen_laeuft_wenn_eines_offen_ist():
    async def szenario():
        a = Aufbau(kopplungen=[K1, Kopplung("k2", "ventil-2:1", S1, 20, 30, 1800)])
        a.gardena.ventil("ventil-2:1", "Hecke", aktivitaet="MANUAL_WATERING")
        a.gardena.geraete[S1].aktivitaet = "TIME_LIMITED_ON"
        a.bereit()
        await a.interlock.abwarten()
        assert a.gardena.befehle == []
        a.gardena.setzen("ventil-2:1", "CLOSED")
        await a.interlock.abwarten()
        assert a.gardena.folge() == [("STOP", S1)]
    ausfuehren(szenario())
