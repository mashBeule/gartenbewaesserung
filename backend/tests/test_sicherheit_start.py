"""Sicherheitsregeln 6 und 7 (Pausieren) an der Startprüfung, gegen den Gardena-Mock."""
from __future__ import annotations

import asyncio

from bewaesserung.sicherheit.ereignisse import Ereignisart
from bewaesserung.sicherheit.kopplung import Kopplung
from conftest import K1, S1, V1, Aufbau, ausfuehren


def test_start_stoppt_steckdosen_zuerst_pausiert_alles_und_gibt_frei(aufbau: Aufbau):
    async def szenario():
        aufbau.gardena.ventil("ventil-2:1", "Hecke")     # ungekoppelt, wird trotzdem pausiert
        aufbau.gardena.geraete[S1].aktivitaet = "FOREVER_ON"
        aufbau.gardena.verbinden()
        bericht = await aufbau.startpruefung.ausfuehren()
        assert bericht.ok, bericht.fehler
        assert aufbau.gardena.folge()[0] == ("STOP", S1)
        assert {gid for art, gid in aufbau.gardena.folge() if art == "PAUSE"} == {V1, S1, "ventil-2:1"}
        assert all(g.pausiert for g in aufbau.gardena.geraete.values())
        assert aufbau.orchestrator.freigegeben
        assert aufbau.ereignisse.arten() == [Ereignisart.STARTPRUEFUNG]
    ausfuehren(szenario())


def test_start_stoppt_auch_ohne_verbindung_und_sperrt(aufbau: Aufbau):
    async def szenario():
        bericht = await aufbau.startpruefung.ausfuehren()
        assert aufbau.gardena.folge()[0] == ("STOP", S1)
        assert not bericht.ok
        assert "Keine Verbindung zu Gardena" in bericht.fehler
        assert not aufbau.orchestrator.freigegeben
    ausfuehren(szenario())


def test_start_wartet_auf_verbindung(aufbau: Aufbau):
    async def szenario():
        asyncio.get_running_loop().call_later(0.05, aufbau.gardena.verbinden)
        bericht = await aufbau.startpruefung.ausfuehren()
        assert bericht.ok, bericht.fehler
    ausfuehren(szenario())


def test_steckdose_bleibt_an_dann_keine_freigabe(aufbau: Aufbau):
    async def szenario():
        aufbau.gardena.geraete[S1].aktivitaet = "FOREVER_ON"
        aufbau.gardena.geraete[S1].reagiert = False
        aufbau.gardena.verbinden()
        bericht = await aufbau.startpruefung.ausfuehren()
        assert not bericht.ok
        assert any("FOREVER_ON" in f for f in bericht.fehler)
        assert not aufbau.orchestrator.freigegeben
    ausfuehren(szenario())


def test_ungueltige_kopplung_sperrt_laeufe():
    async def szenario():
        a = Aufbau(kopplungen=[Kopplung("k1", V1, S1, vorlauf_ventil_s=20, nachlauf_ventil_s=0, max_laufzeit_s=1800)])
        a.gardena.verbinden()
        bericht = await a.startpruefung.ausfuehren()
        assert any("Nachlauf" in f for f in bericht.fehler)
        assert not a.orchestrator.freigegeben
    ausfuehren(szenario())


def test_vertauschte_geraete_in_kopplung_sperren_laeufe():
    async def szenario():
        a = Aufbau(kopplungen=[Kopplung("k1", S1, V1, 20, 30, 1800)])
        a.gardena.verbinden()
        bericht = await a.startpruefung.ausfuehren()
        assert any("erwartet VENTIL" in f for f in bericht.fehler)
        assert not a.orchestrator.freigegeben
    ausfuehren(szenario())


def test_fehlendes_oder_offline_geraet_sperrt_laeufe(aufbau: Aufbau):
    async def szenario():
        aufbau.gardena.geraete[V1].online = False
        aufbau.gardena.verbinden()
        bericht = await aufbau.startpruefung.ausfuehren()
        assert any("offline" in f for f in bericht.fehler)
        aufbau.kopplungen.append(Kopplung("k2", "fehlt:1", S1, 20, 30, 1800))
        bericht = await aufbau.startpruefung.ausfuehren()
        assert any("fehlt:1 meldet sich nicht" in f for f in bericht.fehler)
    ausfuehren(szenario())


def test_pause_wird_nicht_waehrend_eines_laufs_erneuert(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit()
        aufgabe = asyncio.create_task(aufbau.orchestrator.lauf("k1", 600))
        await aufbau.zustand.warten_bis(lambda z: z.geraet(S1).aktivitaet == "TIME_LIMITED_ON", 1)
        assert not await aufbau.startpruefung.pausen_erneuern()
        await aufbau.orchestrator.abbrechen("k1")
        await aufgabe
        assert await aufbau.startpruefung.pausen_erneuern()
        assert {gid for art, gid in aufbau.gardena.folge() if art == "PAUSE"} == {V1, S1}
    ausfuehren(szenario())


def test_pause_wird_taeglich_erneuert(aufbau: Aufbau):
    async def szenario():
        aufbau.bereit(interlock=False)
        schlaf = []
        async def schlafen(s):
            schlaf.append(s)
            if len(schlaf) >= 3:
                raise asyncio.CancelledError
        aufbau.startpruefung._schlafen = schlafen
        try:
            await aufbau.startpruefung.pausen_erneuern_schleife()
        except asyncio.CancelledError:
            pass
        assert schlaf == [24 * 3600.0] * 3
        assert [art for art, _ in aufbau.gardena.folge()].count("PAUSE") == 4   # 2 Geräte × 2 Tage
    ausfuehren(szenario())
