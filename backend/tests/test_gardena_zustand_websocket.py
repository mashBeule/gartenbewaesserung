"""Gerätezustand (Teil-Aktualisierungen, Warten) und WebSocket-Schleife mit Fakes."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from bewaesserung.gardena.budget import ApiBudget, BudgetErschoepft
from bewaesserung.gardena.modelle import GeraeteTyp
from bewaesserung.gardena.websocket import VerbindungBeendet, WebSocketSchleife, wartezeit
from bewaesserung.gardena.zustand import Geraetezustand
from conftest import ausfuehren

VENTIL = {"id": "g1:1", "type": "VALVE", "attributes": {
    "activity": {"value": "CLOSED"}, "name": {"value": "Rasen"}, "duration": {"value": 0}},
    "relationships": {"device": {"data": {"id": "g1", "type": "DEVICE"}}}}
COMMON = {"id": "g1", "type": "COMMON", "attributes": {"rfLinkState": {"value": "ONLINE"}, "batteryLevel": {"value": 80}}}


def test_teil_aktualisierung_und_null_loescht():
    z = Geraetezustand()
    z.nachricht_anwenden(json.dumps({"data": VENTIL, "included": [COMMON]}))
    v = z.geraet("g1:1")
    assert (v.typ, v.name, v.aktivitaet, v.batterie_prozent, v.online) == (GeraeteTyp.VENTIL, "Rasen", "CLOSED", 80, True)
    z.nachricht_anwenden(json.dumps({"id": "g1:1", "type": "VALVE", "attributes": {"activity": {"value": "MANUAL_WATERING"}}}))
    assert z.geraet("g1:1").ventil_offen and z.geraet("g1:1").name == "Rasen"
    z.nachricht_anwenden(json.dumps({"id": "g1:1", "type": "VALVE", "attributes": {"activity": None}}))
    assert z.geraet("g1:1").aktivitaet is None and not z.geraet("g1:1").ventil_offen
    z.nachricht_anwenden(json.dumps({"id": "g1", "type": "COMMON", "attributes": {"rfLinkState": {"value": "OFFLINE"}}}))
    assert not z.geraet("g1:1").online
    assert z.nachricht_anwenden("kein json") == 0


def test_steckdose_vorsichtig_ausgelegt():
    z = Geraetezustand()
    z.anwenden({"id": "s:1", "type": "POWER_SOCKET", "attributes": {}})
    assert z.geraet("s:1").steckdose_an                # unbekannt zählt als an
    z.anwenden({"id": "s:1", "type": "POWER_SOCKET", "attributes": {"activity": {"value": "OFF"}}})
    assert not z.geraet("s:1").steckdose_an


def test_warten_bis():
    async def szenario():
        z = Geraetezustand()
        asyncio.get_running_loop().call_later(0.02, lambda: z.anwenden(VENTIL))
        assert await z.warten_bis(lambda z: z.geraet("g1:1") is not None, 1)
        assert not await z.warten_bis(lambda z: z.geraet("fehlt") is not None, 0.05)
    ausfuehren(szenario())


class FakeQuelle:
    def __init__(self, fehler: Exception | None = None):
        self.fehler = fehler
        self.adressen = 0

    async def standorte(self):
        return [{"id": "ort-1"}]

    async def websocket_adresse(self, standort_id):
        if self.fehler:
            raise self.fehler
        self.adressen += 1
        return f"wss://ws/{self.adressen}"


class FakeVerbindung:
    def __init__(self, nachrichten, ende: Exception | None = VerbindungBeendet("1001")):
        self.nachrichten = list(nachrichten)
        self.ende = ende
        self.geschlossen = False

    async def empfangen(self):
        if self.nachrichten:
            return self.nachrichten.pop(0)
        if self.ende:
            raise self.ende
        await asyncio.sleep(3600)

    async def schliessen(self):
        self.geschlossen = True


def test_abriss_wird_als_ungeplant_gemeldet_und_zustand_uebernommen():
    async def szenario():
        z = Geraetezustand()
        verlauf = []
        z.beobachten(lambda: verlauf.append((z.verbunden, z.trennung_geplant)))
        verbindung = FakeVerbindung([json.dumps(VENTIL)])
        async def verbinder(url):
            return verbindung
        s = WebSocketSchleife(FakeQuelle(), z, ApiBudget(), verbinder=verbinder)
        warten = await s.einmal()
        assert z.geraet("g1:1") is not None and verbindung.geschlossen
        assert verlauf[0] == (True, False) and verlauf[-1] == (False, False)
        assert warten == wartezeit(1) and s.fehlversuche == 1
    ausfuehren(szenario())


def test_geplante_erneuerung_nach_hoechstdauer():
    async def szenario():
        z = Geraetezustand()
        async def verbinder(url):
            return FakeVerbindung([], ende=None)
        s = WebSocketSchleife(FakeQuelle(), z, ApiBudget(), verbinder=verbinder,
                              max_verbindungsdauer=timedelta(milliseconds=50))
        assert await s.einmal() == timedelta(0)
        assert not z.verbunden and z.trennung_geplant
    ausfuehren(szenario())


def test_budget_sperre_wartet_bis_ende():
    async def szenario():
        b = ApiBudget()
        jetzt = datetime.now(timezone.utc)
        b.gesperrt_melden(jetzt)
        s = WebSocketSchleife(FakeQuelle(BudgetErschoepft("Sperre")), Geraetezustand(), b)
        warten = await s.einmal()
        assert timedelta(hours=23) < warten <= timedelta(hours=24)
    ausfuehren(szenario())


def test_wartezeit_waechst_und_ist_begrenzt():
    assert [wartezeit(n).total_seconds() for n in range(5)] == [0, 30, 60, 120, 240]
    assert wartezeit(20) == timedelta(minutes=30)
