"""Gardena-Mock für Tests: nimmt Befehle an und meldet Zustände wie der WebSocket.

- Jeder Befehl wird in `befehle` protokolliert (Reihenfolge prüfbar).
- Ein Gerät mit `reagiert=False` nimmt Befehle an, ändert aber nichts (verlorener Befehl).
- `fehler[id]` lässt Befehle an dieses Gerät mit einer Ausnahme scheitern.
- `setzen()` simuliert Änderungen von außen (Gardena-App, Taste am Gerät, Zeitplan).
- Während `trennen()` ändert sich der Gerätezustand im Mock weiter, gemeldet wird erst
  beim nächsten `verbinden()` (vollständiger Stand wie bei Gardena).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from bewaesserung.gardena.budget import Prioritaet
from bewaesserung.gardena.modelle import GeraeteTyp
from bewaesserung.gardena.zustand import Geraetezustand

DIENST_TYP = {GeraeteTyp.VENTIL: "VALVE", GeraeteTyp.STECKDOSE: "POWER_SOCKET", GeraeteTyp.SENSOR: "SENSOR"}
AN = {GeraeteTyp.VENTIL: "MANUAL_WATERING", GeraeteTyp.STECKDOSE: "TIME_LIMITED_ON"}
AUS = {GeraeteTyp.VENTIL: "CLOSED", GeraeteTyp.STECKDOSE: "OFF"}


class ApiAbgelehnt(Exception):
    """Wie HTTP 400 der echten API."""


@dataclass
class MockGeraet:
    id: str
    typ: GeraeteTyp
    name: str
    aktivitaet: str | None
    online: bool = True
    reagiert: bool = True
    pausiert: bool = False
    dauer: int | None = None

    @property
    def geraete_id(self) -> str:
        return self.id.split(":")[0]


@dataclass
class Befehl:
    art: str            # START, STOP, PAUSE
    dienst_id: str
    sekunden: int | None = None
    prioritaet: Prioritaet | None = None


@dataclass
class GardenaMock:
    zustand: Geraetezustand
    verzoegerung_s: float = 0.0
    geraete: dict[str, MockGeraet] = field(default_factory=dict)
    befehle: list[Befehl] = field(default_factory=list)
    fehler: dict[str, Exception] = field(default_factory=dict)
    verbunden: bool = False

    # --- Aufbau ----------------------------------------------------------

    def ventil(self, gid: str, name: str | None = None, aktivitaet: str | None = "CLOSED", **kw) -> MockGeraet:
        return self._neu(gid, GeraeteTyp.VENTIL, name, aktivitaet, **kw)

    def steckdose(self, gid: str, name: str | None = None, aktivitaet: str | None = "OFF", **kw) -> MockGeraet:
        return self._neu(gid, GeraeteTyp.STECKDOSE, name, aktivitaet, **kw)

    def _neu(self, gid, typ, name, aktivitaet, **kw) -> MockGeraet:
        g = MockGeraet(gid, typ, name or gid, aktivitaet, **kw)
        self.geraete[gid] = g
        if self.verbunden:
            self._melden(g)
        return g

    def _objekte(self, g: MockGeraet) -> list[dict]:
        attribute = {"activity": {"value": g.aktivitaet}} if g.aktivitaet is not None else {}
        if g.dauer is not None:
            attribute["duration"] = {"value": g.dauer}
        if g.typ is GeraeteTyp.VENTIL:
            attribute["name"] = {"value": g.name}
        return [
            {"id": g.geraete_id, "type": "COMMON", "attributes": {
                "name": {"value": g.name}, "rfLinkState": {"value": "ONLINE" if g.online else "OFFLINE"}}},
            {"id": g.id, "type": DIENST_TYP[g.typ], "attributes": attribute,
             "relationships": {"device": {"data": {"id": g.geraete_id, "type": "DEVICE"}}}},
        ]

    def _melden(self, g: MockGeraet) -> None:
        if self.verbunden:
            self.zustand.anwenden(*self._objekte(g))

    # --- Verbindung ------------------------------------------------------

    def verbinden(self, nur: list[str] | None = None) -> None:
        """Wie ein neuer WebSocket: zurücksetzen, verbinden, vollständigen Stand senden."""
        self.zustand.zuruecksetzen()
        self.verbunden = True
        self.zustand.verbindung_setzen(True)
        for g in self.geraete.values():
            if nur is None or g.id in nur:
                self._melden(g)

    def nachreichen(self, gid: str) -> None:
        self._melden(self.geraete[gid])

    def trennen(self, geplant: bool = False) -> None:
        self.verbunden = False
        self.zustand.verbindung_setzen(False, geplant=geplant)

    # --- Änderungen von außen -------------------------------------------

    def setzen(self, gid: str, aktivitaet: str | None) -> None:
        g = self.geraete[gid]
        g.aktivitaet = aktivitaet
        self._melden(g)

    def ablaufen(self, gid: str) -> None:
        """Gardena-Laufzeit des Geräts ist abgelaufen."""
        self.setzen(gid, AUS[self.geraete[gid].typ])

    # --- Befehle (Schnittstelle `Befehle`) --------------------------------

    def _ausfuehren(self, g: MockGeraet, aktivitaet: str, dauer: int | None) -> None:
        def anwenden():
            g.aktivitaet, g.dauer = aktivitaet, dauer
            self._melden(g)
        if not g.reagiert:
            return
        if self.verzoegerung_s:
            asyncio.get_running_loop().call_later(self.verzoegerung_s, anwenden)
        else:
            anwenden()

    def _geraet(self, gid: str, typ: GeraeteTyp) -> MockGeraet:
        if gid in self.fehler:
            raise self.fehler[gid]
        g = self.geraete.get(gid)
        if g is None:
            raise ApiAbgelehnt(f"404 unbekannter Dienst {gid}")
        if g.typ is not typ:
            raise ApiAbgelehnt(f"400 {gid} ist kein {typ.value}")
        return g

    async def starten(self, dienst_id: str, typ: GeraeteTyp, sekunden: int, p: Prioritaet = Prioritaet.NORMAL) -> None:
        self.befehle.append(Befehl("START", dienst_id, sekunden, p))
        g = self._geraet(dienst_id, typ)
        if not isinstance(sekunden, int) or sekunden <= 0 or sekunden % 60 or sekunden > 36000:
            raise ApiAbgelehnt(f"400 ungültige Laufzeit {sekunden}")
        self._ausfuehren(g, AN[typ], sekunden)

    async def stoppen(self, dienst_id: str, typ: GeraeteTyp, p: Prioritaet = Prioritaet.SICHERHEIT) -> None:
        self.befehle.append(Befehl("STOP", dienst_id, None, p))
        g = self._geraet(dienst_id, typ)
        self._ausfuehren(g, AUS[typ], None)

    async def zeitplan_pausieren(self, dienst_id: str, typ: GeraeteTyp) -> None:
        self.befehle.append(Befehl("PAUSE", dienst_id))
        g = self._geraet(dienst_id, typ)
        g.pausiert = True

    # --- Auswertung ------------------------------------------------------

    def folge(self) -> list[tuple[str, str]]:
        return [(b.art, b.dienst_id) for b in self.befehle]

    def starts(self) -> dict[str, int]:
        return {b.dienst_id: b.sekunden for b in self.befehle if b.art == "START"}
