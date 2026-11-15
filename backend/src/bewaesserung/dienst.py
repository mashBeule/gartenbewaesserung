"""Der Dienst auf dem Pi: steckt Gardena-Client, WebSocket, Sicherheit, Scheduler und API zusammen.

Start mit `python -m bewaesserung` (systemd: deploy/systemd/bewaesserung.service).

Reihenfolge (Sicherheitsregel 6):
1. Watchdog-Takt starten. Ohne Zugangsdaten: melden und warten, nichts schalten.
2. SQLite öffnen, Interlock-Wächter an den Gerätezustand hängen, WebSocket starten,
   REST-API starten (zeigt den Status schon während der Startprüfung).
3. Startprüfung: Steckdosen stoppen, Kopplungen prüfen, Zeitpläne pausieren, freigeben.
   Schlägt sie fehl, wird sie mit wachsendem Abstand wiederholt (API-Budget!).
4. Erst danach: Scheduler und tägliche Erneuerung der Pause.
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
from .konfiguration import Konfiguration
from .planung.scheduler import Scheduler
from .sicherheit.ereignisse import Ereignisse
from .sicherheit.interlock import Interlock
from .sicherheit.kopplung import Kopplung
from .sicherheit.orchestrator import Orchestrator
from .sicherheit.startpruefung import Pruefbericht, Startpruefung
from .speicher.datenbank import Speicher
from .speicher.ereignisse import SpeicherEreignisse
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


async def api_starten(konfig: Konfiguration, kontext) -> asyncio.Task | None:
    if not konfig.api_aktiv:
        log.warning("REST-API aus: BEWAESSERUNG_API_TOKEN fehlt (mind. 24 Zeichen) oder Host ist 0.0.0.0")
        return None
    import uvicorn
    from .api.app import app_bauen
    app = app_bauen(kontext)

    async def bedienen() -> None:
        # Die Tailscale-Adresse gibt es evtl. erst später: Binden scheitert dann, neuer Versuch.
        # uvicorn beendet sich bei Startfehlern mit SystemExit; das darf den Dienst nicht beenden.
        while True:
            server = uvicorn.Server(uvicorn.Config(app, host=konfig.api_host, port=konfig.api_port,
                                                   log_level="warning", access_log=False))
            server.install_signal_handlers = lambda: None
            try:
                await server.serve()
            except (SystemExit, OSError) as f:
                log.error("REST-API auf %s:%d nicht gestartet (%s), neuer Versuch in 30 s",
                          konfig.api_host, konfig.api_port, f)
            await asyncio.sleep(30)

    log.info("REST-API auf %s:%d", konfig.api_host, konfig.api_port)
    return asyncio.create_task(bedienen(), name="api")


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

    from .api.app import Kontext
    speicher = Speicher(konfig.datenbank)
    budget = ApiBudget.aus_datei(konfig.datenordner / "api-budget.json")
    client = GardenaClient(konfig.application_key, konfig.application_secret, budget,
                           dry_run=konfig.trockenlauf)
    if konfig.trockenlauf:
        log.warning("TROCKENLAUF: Befehle werden nur protokolliert (GARDENA_DRY_RUN=false zum echten Schalten)")
    b = bausteine_bauen(client, Geraetezustand(), SpeicherEreignisse(speicher), speicher.kopplungen)
    scheduler = Scheduler(speicher, b.orchestrator)
    b.interlock.starten()
    aufgaben.append(asyncio.create_task(
        WebSocketSchleife(client, b.zustand, budget, standort_id=konfig.standort_id).laufen(), name="websocket"))
    api = await api_starten(konfig, Kontext(speicher, b.zustand, b.orchestrator, scheduler,
                                            konfig.api_token, konfig.trockenlauf))
    if api is not None:
        aufgaben.append(api)
    melder.bereit("Startprüfung läuft")

    try:
        bericht = await startpruefung_bis_bestanden(b.startpruefung)
        melder.status(f"Bereit: {len(speicher.kopplungen())} Kopplungen, {len(bericht.pausiert)} Zeitpläne pausiert"
                      + (" (Trockenlauf)" if konfig.trockenlauf else ""))
        aufgaben.append(asyncio.create_task(scheduler.laufen(), name="scheduler"))
        aufgaben.append(asyncio.create_task(b.startpruefung.pausen_erneuern_schleife(), name="pause"))
        await ende.wait()
    finally:
        for a in aufgaben:
            a.cancel()
        b.interlock.anhalten()
        await client.schliessen()
        speicher.schliessen()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(betreiben(Konfiguration.aus_umgebung()))
