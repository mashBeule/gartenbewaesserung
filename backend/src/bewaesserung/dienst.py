"""Der Dienst auf dem Pi: steckt Gardena-Client, WebSocket, Interlock und Startprüfung zusammen.

Start mit `python -m bewaesserung` (systemd: deploy/systemd/bewaesserung.service).

Reihenfolge (Sicherheitsregel 6):
1. Watchdog-Takt starten. Ohne Zugangsdaten: melden und warten, nichts schalten.
2. Interlock-Wächter an den Gerätezustand hängen, WebSocket-Schleife starten.
3. Startprüfung: Steckdosen stoppen, Kopplungen prüfen, Zeitpläne pausieren, freigeben.
   Schlägt sie fehl, wird sie mit wachsendem Abstand wiederholt (API-Budget!).
4. Erst danach: tägliche Erneuerung der Pause, später der Scheduler.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .gardena.budget import ApiBudget
from .gardena.client import Befehle, GardenaClient
from .gardena.websocket import WebSocketSchleife
from .gardena.zustand import Geraetezustand
from .konfiguration import Konfiguration, KonfigurationFehler, kopplungen_laden
from .sicherheit.ereignisse import EreignisListe, Ereignisse
from .sicherheit.interlock import Interlock
from .sicherheit.kopplung import Kopplung
from .sicherheit.orchestrator import Orchestrator
from .sicherheit.startpruefung import Pruefbericht, Startpruefung
from .systemd import SystemdMelder

log = logging.getLogger(__name__)

STARTPRUEFUNG_WIEDERHOLUNG_S = (300.0, 1800.0, 7200.0, 21600.0)


@dataclass
class Bausteine:
    zustand: Geraetezustand
    ereignisse: Ereignisse
    orchestrator: Orchestrator
    interlock: Interlock
    startpruefung: Startpruefung


def bausteine_bauen(befehle: Befehle, zustand: Geraetezustand, ereignisse: Ereignisse,
                    kopplungen: Callable[[], list[Kopplung]]) -> Bausteine:
    orchestrator = Orchestrator(befehle, zustand, ereignisse, kopplungen)
    return Bausteine(
        zustand=zustand,
        ereignisse=ereignisse,
        orchestrator=orchestrator,
        interlock=Interlock(befehle, zustand, ereignisse, kopplungen),
        startpruefung=Startpruefung(befehle, zustand, ereignisse, kopplungen, orchestrator),
    )


async def startpruefung_bis_bestanden(
    startpruefung: Startpruefung,
    schlafen: Callable[[float], Awaitable[None]] = asyncio.sleep,
    intervalle: tuple[float, ...] = STARTPRUEFUNG_WIEDERHOLUNG_S,
) -> Pruefbericht:
    versuch = 0
    while True:
        bericht = await startpruefung.ausfuehren()
        if bericht.ok:
            return bericht
        warten = intervalle[min(versuch, len(intervalle) - 1)]
        log.error("Startprüfung nicht bestanden, neuer Versuch in %.0f s: %s", warten, bericht.fehler)
        versuch += 1
        await schlafen(warten)


async def betreiben(konfig: Konfiguration, melder: SystemdMelder | None = None,
                    ende: asyncio.Event | None = None) -> None:
    """Läuft, bis `ende` gesetzt wird (ohne `ende`: für immer)."""
    melder = melder or SystemdMelder()
    ende = ende or asyncio.Event()
    aufgaben = [asyncio.create_task(melder.watchdog_takt(), name="watchdog")]

    if not konfig.zugang_vollstaendig:
        text = "Warte auf Zugangsdaten (/etc/bewaesserung/zugang.env), schalte nichts"
        log.warning(text)
        melder.bereit(text)
        await ende.wait()                 # nicht beenden, sonst startet systemd im Kreis neu
        for a in aufgaben:
            a.cancel()
        return

    kopplungen: list[Kopplung] = []
    kopplungen_fehler = None
    try:
        kopplungen = kopplungen_laden(konfig.kopplungen_datei)
    except KonfigurationFehler as f:
        kopplungen_fehler = str(f)
        log.error("Kopplungen nicht geladen, Läufe bleiben gesperrt: %s", f)

    budget = ApiBudget()
    client = GardenaClient(konfig.application_key, konfig.application_secret, budget,
                           dry_run=konfig.trockenlauf)
    if konfig.trockenlauf:
        log.warning("TROCKENLAUF: Befehle werden nur protokolliert (GARDENA_DRY_RUN=false zum echten Schalten)")
    b = bausteine_bauen(client, Geraetezustand(), EreignisListe(), lambda: kopplungen)
    b.interlock.starten()
    aufgaben.append(asyncio.create_task(
        WebSocketSchleife(client, b.zustand, budget, standort_id=konfig.standort_id).laufen(), name="websocket"))
    melder.bereit("Startprüfung läuft")

    bericht = await startpruefung_bis_bestanden(b.startpruefung)
    if kopplungen_fehler:
        b.orchestrator.freigegeben = False
        melder.status(f"Läufe gesperrt: {kopplungen_fehler}")
    else:
        melder.status(f"Bereit: {len(kopplungen)} Kopplungen, {len(bericht.pausiert)} Zeitpläne pausiert"
                      + (" (Trockenlauf)" if konfig.trockenlauf else ""))
    aufgaben.append(asyncio.create_task(b.startpruefung.pausen_erneuern_schleife(), name="pause"))
    # Aufgabe 5: Scheduler und REST-API für die App kommen hier dazu.
    try:
        await ende.wait()
    finally:
        for a in aufgaben:
            a.cancel()
        b.interlock.anhalten()
        await client.schliessen()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(betreiben(Konfiguration.aus_umgebung()))
