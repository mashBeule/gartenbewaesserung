"""Scheduler + SQLite + Orchestrator gegen den Gardena-Mock, mit gestellter Uhr."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta

import pytest

from bewaesserung.planung.plan import ORTSZEIT
from bewaesserung.planung.scheduler import LaufNichtMoeglich, Scheduler
from bewaesserung.sicherheit.kopplung import LaufAbgelehnt
from bewaesserung.speicher.datenbank import Speicher
from conftest import S1, V1, Aufbau, ausfuehren

V2 = "ventil-2:1"
JETZT = datetime(2026, 6, 1, 6, 0, 10, tzinfo=ORTSZEIT)       # Montag, 10 s nach 06:00


class Umgebung:
    def __init__(self, jetzt: datetime = JETZT):
        self.a = Aufbau()
        self.a.gardena.ventil(V2, "Hecke")
        self.db = Speicher()
        self.z1 = self.db.zone_anlegen(name="Rasen", ventil_id=V1)
        self.z2 = self.db.zone_anlegen(name="Hecke", ventil_id=V2)
        self.db.kopplung_anlegen(ventil_id=V1, steckdose_id=S1, vorlauf_ventil_s=20, nachlauf_ventil_s=30, max_laufzeit_s=1800)
        self.db.kopplung_anlegen(ventil_id=V2, steckdose_id=S1, vorlauf_ventil_s=20, nachlauf_ventil_s=30, max_laufzeit_s=1800)
        self.a.kopplungen = self.db.kopplungen()
        self.jetzt = jetzt
        self.s = Scheduler(self.db, self.a.orchestrator, uhr=lambda: self.jetzt)

    def regel(self, zone, start=time(6, 0), dauer_min=10, **kw):
        return self.db.regel_anlegen(zone_id=zone.id, wochentage=0b1111111, startzeit=start, dauer_min=dauer_min, **kw)

    def status(self) -> list[tuple[str | None, str | None]]:
        return [(e.plan_schluessel, e.status) for e in reversed(self.db.protokoll())]

    async def warten_auf_steckdose(self):
        assert await self.a.zustand.warten_bis(lambda z: z.geraet(S1).aktivitaet == "TIME_LIMITED_ON", 1)

    async def lauf_beenden(self, ventil=V1):
        self.a.gardena.ablaufen(S1)
        self.a.gardena.ablaufen(ventil)
        await self.s._aktuell


def test_faelliger_termin_laeuft_genau_einmal():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        u.regel(u.z1)
        await u.s.takt()
        await u.warten_auf_steckdose()
        assert u.a.gardena.starts() == {V1: 600, S1: 540}
        assert u.status() == [("regel-1-2026-06-01", "LAEUFT")]
        await u.lauf_beenden()
        assert u.status() == [("regel-1-2026-06-01", "BEENDET")]
        await u.s.takt()                              # gleicher Termin nicht nochmal
        assert len(u.a.gardena.befehle) == 2
    ausfuehren(szenario())


def test_alter_termin_wird_nicht_nachgeholt():
    async def szenario():
        u = Umgebung(JETZT + timedelta(minutes=20))
        u.a.bereit()
        u.regel(u.z1)
        await u.s.takt()
        assert u.a.gardena.befehle == []
        assert u.status() == []          # auch kein „verpasst“ für Termine vor Anlage der Regel
    ausfuehren(szenario())


def test_ausgelassener_termin_schaltet_nichts():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        r = u.regel(u.z1)
        u.db.ausnahme_setzen(r.id, JETZT.date(), "AUSLASSEN", None)
        await u.s.takt()
        assert u.a.gardena.befehle == []
        assert u.status() == [("regel-1-2026-06-01", "AUSGELASSEN")]
    ausfuehren(szenario())


def test_gesperrt_wartet_und_verpasst_nach_fenster():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        u.a.orchestrator.freigegeben = False
        u.regel(u.z1)
        await u.s.takt()
        assert u.a.gardena.befehle == [] and len(u.s.warteschlange) == 1
        u.jetzt += timedelta(minutes=16)
        await u.s.takt()
        assert u.status() == [("regel-1-2026-06-01", "VERPASST")]
        assert u.a.gardena.befehle == []
    ausfuehren(szenario())


def test_gesperrt_startet_nach_freigabe_im_fenster():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        u.a.orchestrator.freigegeben = False
        u.regel(u.z1)
        await u.s.takt()
        u.a.orchestrator.freigegeben = True
        u.jetzt += timedelta(minutes=5)
        await u.s.takt()
        await u.warten_auf_steckdose()
        await u.lauf_beenden()
        assert u.status() == [("regel-1-2026-06-01", "BEENDET")]
    ausfuehren(szenario())


def test_gleichzeitige_termine_laufen_nacheinander():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        u.regel(u.z1)
        u.regel(u.z2)
        await u.s.takt()
        await u.warten_auf_steckdose()
        assert [b.dienst_id for b in u.a.gardena.befehle if b.art == "START"] == [V1, S1]
        u.a.gardena.ablaufen(S1)
        u.a.gardena.ablaufen(V1)
        await asyncio.sleep(0.05)                     # zweiter Lauf startet ohne neuen Takt
        await u.warten_auf_steckdose()
        assert [b.dienst_id for b in u.a.gardena.befehle if b.art == "START"] == [V1, S1, V2, S1]
        await u.lauf_beenden(V2)
    ausfuehren(szenario())


def test_manueller_lauf_regel_8_und_belegt():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        with pytest.raises(LaufAbgelehnt, match="Höchstlaufzeit"):
            u.s.manuell(u.z1.id, 3600)
        u.s.manuell(u.z1.id, 300)
        await u.warten_auf_steckdose()
        with pytest.raises(LaufNichtMoeglich, match="läuft schon"):
            u.s.manuell(u.z2.id, 300)
        await u.lauf_beenden()
        assert [(e.grund, e.status) for e in u.db.protokoll()] == [("MANUELL", "BEENDET")]
    ausfuehren(szenario())


def test_zone_ohne_kopplung_wird_abgelehnt():
    async def szenario():
        u = Umgebung()
        u.a.gardena.ventil("ventil-3:1", "Beet")
        z3 = u.db.zone_anlegen(name="Beet", ventil_id="ventil-3:1")
        u.a.bereit()
        u.regel(z3)
        await u.s.takt()
        await u.s._aktuell
        assert u.status() == [("regel-1-2026-06-01", "ABGELEHNT")]
        assert u.a.gardena.befehle == []
    ausfuehren(szenario())


def test_not_aus_stoppt_steckdose_vor_ventil_und_leert_warteschlange():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        u.regel(u.z1)
        u.regel(u.z2)
        await u.s.takt()
        await u.warten_auf_steckdose()
        await u.s.not_aus()
        await u.s._aktuell
        assert u.a.gardena.folge()[2:4] == [("STOP", S1), ("STOP", V1)]
        assert dict(u.status()) == {"regel-1-2026-06-01": "ABGEBROCHEN", "regel-2-2026-06-01": "VERPASST"}
        await asyncio.sleep(0.05)
        assert ("START", V2) not in u.a.gardena.folge()
    ausfuehren(szenario())


class FesterFaktor:
    def __init__(self, faktor, text):
        self.wert = (faktor, text)

    def faktor(self, zone_id, start):
        return self.wert


def test_wetter_skaliert_oder_setzt_aus():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        u.regel(u.z1, dauer_min=20)
        u.s.wetter = FesterFaktor(0.5, "ET0 niedrig")
        await u.s.takt()
        await u.warten_auf_steckdose()
        assert u.a.gardena.starts()[V1] == 600
        await u.lauf_beenden()
        u.regel(u.z2, start=time(6, 1))
        u.s.wetter = FesterFaktor(0.0, "Regen 8 mm")
        u.jetzt += timedelta(minutes=1)
        await u.s.takt()
        await u.s._aktuell
        assert ("regel-2-2026-06-01", "UEBERSPRUNGEN_WETTER") in u.status()
    ausfuehren(szenario())
