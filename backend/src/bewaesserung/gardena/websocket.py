"""WebSocket-Schleife: hält den Gerätezustand aktuell, ohne Polling, mit Reconnect.

Ablauf (docs/gardena-api.md):
1. Zustand zurücksetzen.
2. Adresse per `POST /websocket` holen (kostet einen REST-Aufruf), sofort verbinden.
3. Jede Nachricht als Teil-Aktualisierung anwenden. Gardena schickt beim Verbinden
   den vollständigen Stand; ein REST-Nachladen nach einem Abriss ist deshalb nicht nötig.
4. Ping alle 150 s. Nach MAX_VERBINDUNGSDAUER selbst neu verbinden (geplante Trennung),
   bevor Gardena nach ~2 h trennt.

Nach Fehlern wartet die Schleife mit wachsendem Abstand, bei Budget-Sperre bis zu deren Ende.
Ein Abriss wird dem Gerätezustand als ungeplante Trennung gemeldet; der Interlock-Wächter
stoppt dann vorsorglich laufende gekoppelte Steckdosen.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Protocol

from .budget import ApiBudget, BudgetErschoepft
from .client import GardenaGesperrt
from .zustand import Geraetezustand

log = logging.getLogger(__name__)

PING_INTERVALL_S = 150
MAX_VERBINDUNGSDAUER = timedelta(minutes=110)
STABIL_NACH = timedelta(minutes=10)
ERSTE_WARTEZEIT = timedelta(seconds=30)
LAENGSTE_WARTEZEIT = timedelta(minutes=30)
WARTEZEIT_BUDGET = timedelta(minutes=15)


class VerbindungBeendet(Exception):
    """Die Gegenseite hat den WebSocket geschlossen oder er ist abgerissen."""


class Verbindung(Protocol):
    async def empfangen(self) -> str: ...        # wirft VerbindungBeendet
    async def schliessen(self) -> None: ...


Verbinder = Callable[[str], Awaitable[Verbindung]]


class AdressQuelle(Protocol):
    async def standorte(self) -> list[dict]: ...
    async def websocket_adresse(self, standort_id: str) -> str: ...


def wartezeit(fehlversuche: int) -> timedelta:
    """30 s, 1 min, 2 min, … höchstens 30 min. Ohne Fehlversuch sofort."""
    if fehlversuche <= 0:
        return timedelta(0)
    return min(ERSTE_WARTEZEIT * 2 ** (fehlversuche - 1), LAENGSTE_WARTEZEIT)


class _WebsocketsVerbindung:
    def __init__(self, ws) -> None:
        self._ws = ws

    async def empfangen(self) -> str:
        import websockets
        try:
            nachricht = await self._ws.recv()
        except websockets.ConnectionClosed as f:
            raise VerbindungBeendet(str(f)) from f
        return nachricht if isinstance(nachricht, str) else nachricht.decode("utf-8", "replace")

    async def schliessen(self) -> None:
        await self._ws.close()


async def websockets_verbinden(url: str) -> Verbindung:
    import websockets
    ws = await websockets.connect(url, ping_interval=PING_INTERVALL_S, ping_timeout=30)
    return _WebsocketsVerbindung(ws)


class WebSocketSchleife:
    def __init__(
        self,
        quelle: AdressQuelle,
        zustand: Geraetezustand,
        budget: ApiBudget,
        standort_id: str | None = None,
        verbinder: Verbinder = websockets_verbinden,
        schlafen: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uhr: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        max_verbindungsdauer: timedelta = MAX_VERBINDUNGSDAUER,
    ) -> None:
        self.quelle = quelle
        self.zustand = zustand
        self.budget = budget
        self.standort_id = standort_id
        self._verbinder = verbinder
        self._schlafen = schlafen
        self._uhr = uhr
        self._max_dauer = max_verbindungsdauer
        self.fehlversuche = 0

    async def laufen(self) -> None:
        """Endlos verbinden und neu verbinden, bis die Aufgabe abgebrochen wird."""
        while True:
            warten = await self.einmal()
            if warten > timedelta(0):
                log.info("WebSocket: nächster Versuch in %s", warten)
                await self._schlafen(warten.total_seconds())

    async def einmal(self) -> timedelta:
        """Eine Verbindung aufbauen und halten. Gibt die Wartezeit bis zum nächsten Versuch zurück."""
        self.zustand.zuruecksetzen()
        try:
            if self.standort_id is None:
                self.standort_id = await self._standort_ermitteln()
            url = await self.quelle.websocket_adresse(self.standort_id)
            verbindung = await self._verbinder(url)
        except (BudgetErschoepft, GardenaGesperrt) as f:
            log.warning("WebSocket: kein REST-Aufruf möglich: %s", f)
            return self._warten_wegen_budget()
        except Exception:
            log.exception("WebSocket: Verbindungsaufbau fehlgeschlagen")
            return self._fehlversuch()

        beginn = self._uhr()
        self.zustand.verbindung_setzen(True)
        geplant = False
        try:
            geplant = await self._empfangen(verbindung, beginn + self._max_dauer)
        finally:
            self.zustand.verbindung_setzen(False, geplant=geplant)
            with contextlib.suppress(Exception):
                await verbindung.schliessen()

        if geplant or self._uhr() - beginn >= STABIL_NACH:
            self.fehlversuche = 0
            return timedelta(0)
        return self._fehlversuch()

    async def _standort_ermitteln(self) -> str:
        standorte = await self.quelle.standorte()
        if not standorte:
            raise RuntimeError("Gardena meldet keinen Standort")
        if len(standorte) > 1:
            log.warning("Gardena meldet %d Standorte, verwende den ersten", len(standorte))
        return standorte[0]["id"]

    async def _empfangen(self, verbindung: Verbindung, frist: datetime) -> bool:
        """Nachrichten anwenden. True = Frist erreicht (geplante Erneuerung), False = Abriss."""
        while True:
            rest = (frist - self._uhr()).total_seconds()
            if rest <= 0:
                return True
            try:
                text = await asyncio.wait_for(verbindung.empfangen(), timeout=rest)
            except asyncio.TimeoutError:
                return True
            except VerbindungBeendet as f:
                log.warning("WebSocket beendet: %s", f)
                return False
            except Exception:
                log.exception("WebSocket: Fehler beim Empfangen")
                return False
            self.zustand.nachricht_anwenden(text)

    def _fehlversuch(self) -> timedelta:
        self.fehlversuche += 1
        return wartezeit(self.fehlversuche)

    def _warten_wegen_budget(self) -> timedelta:
        jetzt = self._uhr()
        if self.budget.gesperrt_bis and self.budget.gesperrt_bis > jetzt:
            return self.budget.gesperrt_bis - jetzt
        return max(self._fehlversuch(), WARTEZEIT_BUDGET)
