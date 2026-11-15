"""Gardena-REST-Client gegen httpx.MockTransport (kein echtes Netzwerk)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bewaesserung.gardena.budget import ApiBudget, BudgetErschoepft, Prioritaet
from bewaesserung.gardena.client import AUTH_URL, API_URL, GardenaClient, GardenaGesperrt
from bewaesserung.gardena.modelle import GeraeteTyp
from conftest import ausfuehren


class Server:
    def __init__(self, antworten: dict[str, list[httpx.Response]] | None = None):
        self.anfragen: list[httpx.Request] = []
        self.antworten = antworten or {}

    def __call__(self, anfrage: httpx.Request) -> httpx.Response:
        self.anfragen.append(anfrage)
        if str(anfrage.url) == AUTH_URL:
            return httpx.Response(200, json={"access_token": f"token-{len(self.anfragen)}", "expires_in": 86399})
        weg = anfrage.url.path.removeprefix("/v2")
        if self.antworten.get(weg):
            return self.antworten[weg].pop(0)
        if weg == "/locations":
            return httpx.Response(200, json={"data": [{"id": "ort-1", "type": "LOCATION"}]})
        if weg == "/websocket":
            return httpx.Response(201, json={"data": {"attributes": {"url": "wss://beispiel/ws"}}})
        return httpx.Response(202)


def _client(server: Server, dry_run=False, **kw) -> GardenaClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return GardenaClient("schluessel", "geheim", ApiBudget(), dry_run=dry_run, http=http, **kw)


def test_start_sendet_seconds_override_mit_kopfzeilen():
    async def szenario():
        server = Server()
        c = _client(server)
        await c.starten("ventil-1:1", GeraeteTyp.VENTIL, 600)
        token, befehl = server.anfragen
        assert token.method == "POST" and b"grant_type=client_credentials" in token.content
        assert befehl.method == "PUT"
        assert str(befehl.url) == f"{API_URL}/command/ventil-1:1"
        assert befehl.headers["X-Api-Key"] == "schluessel"
        assert befehl.headers["Authorization"] == "Bearer token-1"
        assert befehl.headers["Content-Type"] == "application/vnd.api+json"
        daten = json.loads(befehl.content)["data"]
        assert daten["type"] == "VALVE_CONTROL"
        assert daten["attributes"] == {"command": "START_SECONDS_TO_OVERRIDE", "seconds": 600}
    ausfuehren(szenario())


def test_stopp_und_pause_fuer_steckdose():
    async def szenario():
        server = Server()
        c = _client(server)
        await c.stoppen("steckdose-1:1", GeraeteTyp.STECKDOSE)
        await c.zeitplan_pausieren("steckdose-1:1", GeraeteTyp.STECKDOSE)
        stopp, pause = (json.loads(a.content)["data"] for a in server.anfragen[1:])
        assert stopp["type"] == pause["type"] == "POWER_SOCKET_CONTROL"
        assert stopp["attributes"] == {"command": "STOP_UNTIL_NEXT_TASK"}
        assert pause["attributes"] == {"command": "PAUSE"}
    ausfuehren(szenario())


def test_regel_1_kein_unbegrenzter_start():
    assert not any("override" in n.lower() and n != "starten" for n in dir(GardenaClient))
    async def szenario():
        c = _client(Server())
        for falsch in (0, -60, None, 60.5):
            with pytest.raises(ValueError):
                await c.starten("v:1", GeraeteTyp.VENTIL, falsch)
        with pytest.raises(ValueError):
            await c.starten("sensor:1", GeraeteTyp.SENSOR, 60)
    ausfuehren(szenario())


def test_trockenlauf_sendet_nichts():
    async def szenario():
        server = Server()
        c = _client(server, dry_run=True)
        await c.starten("v:1", GeraeteTyp.VENTIL, 60)
        await c.stoppen("v:1", GeraeteTyp.VENTIL)
        assert server.anfragen == []
    ausfuehren(szenario())


def test_429_sperrt_24_stunden():
    async def szenario():
        server = Server({"/command/v:1": [httpx.Response(429)]})
        c = _client(server)
        with pytest.raises(GardenaGesperrt):
            await c.stoppen("v:1", GeraeteTyp.VENTIL)
        assert c.budget.gesperrt_bis is not None
        anzahl = len(server.anfragen)
        with pytest.raises(BudgetErschoepft):
            await c.stoppen("v:1", GeraeteTyp.VENTIL)
        assert len(server.anfragen) == anzahl
    ausfuehren(szenario())


def test_401_holt_neues_token_einmal():
    async def szenario():
        server = Server({"/command/v:1": [httpx.Response(401)]})
        c = _client(server)
        await c.stoppen("v:1", GeraeteTyp.VENTIL)
        assert [a.url.path for a in server.anfragen] == [
            "/v1/oauth2/token", "/v2/command/v:1", "/v1/oauth2/token", "/v2/command/v:1"]
    ausfuehren(szenario())


def test_token_wird_wiederverwendet_und_budget_gezaehlt():
    async def szenario():
        server = Server()
        c = _client(server)
        assert await c.standorte() == [{"id": "ort-1", "type": "LOCATION"}]
        assert await c.websocket_adresse("ort-1") == "wss://beispiel/ws"
        assert sum(1 for a in server.anfragen if str(a.url) == AUTH_URL) == 1
        assert c.budget.verbraucht(datetime.now(timezone.utc)) == 3
        ws = json.loads(server.anfragen[-1].content)["data"]
        assert ws["type"] == "WEBSOCKET" and ws["attributes"] == {"locationId": "ort-1"}
    ausfuehren(szenario())


def test_sicherheitsstopp_wartet_kurzfenster_ab():
    async def szenario():
        jetzt = [datetime(2026, 11, 14, tzinfo=timezone.utc)]
        geschlafen = []
        async def schlafen(s):
            geschlafen.append(s)
            jetzt[0] += timedelta(seconds=s)
        server = Server()
        c = _client(server, uhr=lambda: jetzt[0], schlafen=schlafen)
        for _ in range(10):
            c.budget.anfordern(Prioritaet.SICHERHEIT, jetzt[0])
        await c.stoppen("s:1", GeraeteTyp.STECKDOSE)
        assert geschlafen and geschlafen[0] == pytest.approx(10)
        with pytest.raises(BudgetErschoepft):
            for _ in range(10):
                c.budget.anfordern(Prioritaet.NORMAL, jetzt[0])
            await c.starten("v:1", GeraeteTyp.VENTIL, 60)
    ausfuehren(szenario())
