"""Startprüfung (Sicherheitsregeln 6 und 7) und Erneuern der Zeitplan-Pause.

Beim Start des Dienstes, in dieser Reihenfolge:
1. Läufe sperren.
2. Alle gekoppelten Steckdosen per Befehl stoppen, ohne auf den Zustand zu warten.
3. Auf die WebSocket-Verbindung und die Meldung aller gekoppelten Geräte warten.
4. Prüfen: Steckdosen melden „aus“, Kopplungen gültig, Geräte vorhanden, vom richtigen Typ, online.
5. Gardena-Zeitpläne aller Ventile und Steckdosen pausieren.
6. Nur wenn alles in Ordnung ist: Läufe freigeben. Erst danach darf der Scheduler starten.

Die Pause wird täglich erneuert (`pausen_erneuern_schleife`), aber nie während eines Laufs,
weil ⚠ unklar ist, ob PAUSE eine laufende Bewässerung abbricht.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..gardena.client import Befehle
from ..gardena.modelle import GeraeteTyp
from ..gardena.zustand import Geraetezustand
from .ereignisse import Ereignisart, Ereignisse, Sicherheitsereignis
from .kopplung import Kopplung
from .orchestrator import Orchestrator

log = logging.getLogger(__name__)

VERBINDUNG_TIMEOUT_S = 120.0
BESTAETIGUNG_TIMEOUT_S = 30.0
PAUSE_INTERVALL_S = 24 * 3600.0
PAUSE_NEUER_VERSUCH_S = 600.0


@dataclass
class Pruefbericht:
    fehler: list[str] = field(default_factory=list)
    gestoppt: list[str] = field(default_factory=list)
    pausiert: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fehler


class Startpruefung:
    def __init__(
        self,
        befehle: Befehle,
        zustand: Geraetezustand,
        ereignisse: Ereignisse,
        kopplungen: Callable[[], list[Kopplung]],
        orchestrator: Orchestrator,
        verbindung_timeout_s: float = VERBINDUNG_TIMEOUT_S,
        bestaetigung_timeout_s: float = BESTAETIGUNG_TIMEOUT_S,
        schlafen: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.befehle = befehle
        self.zustand = zustand
        self.ereignisse = ereignisse
        self._kopplungen = kopplungen
        self.orchestrator = orchestrator
        self.verbindung_timeout_s = verbindung_timeout_s
        self.bestaetigung_timeout_s = bestaetigung_timeout_s
        self._schlafen = schlafen

    async def ausfuehren(self) -> Pruefbericht:
        self.orchestrator.freigegeben = False
        bericht = Pruefbericht()
        kopplungen = list(self._kopplungen())
        steckdosen = sorted({k.steckdose_id for k in kopplungen})
        z = self.zustand

        # 2. Steckdosen stoppen, egal was sie melden
        for sid in steckdosen:
            try:
                await self.befehle.stoppen(sid, GeraeteTyp.STECKDOSE)
                bericht.gestoppt.append(sid)
            except Exception as f:
                bericht.fehler.append(f"Steckdose {sid} konnte nicht gestoppt werden: {f}")

        # 3. Auf vollständigen Zustand warten
        benoetigt = {k.ventil_id for k in kopplungen} | set(steckdosen)
        if not await z.warten_bis(lambda z: z.verbunden and all(z.geraet(g) for g in benoetigt),
                                  self.verbindung_timeout_s):
            if not z.verbunden:
                bericht.fehler.append("Keine Verbindung zu Gardena")
            for g in sorted(benoetigt):
                if z.geraet(g) is None:
                    bericht.fehler.append(f"Gerät {g} meldet sich nicht")

        # 4a. Steckdosen müssen „aus“ melden
        def alle_aus(z: Geraetezustand) -> bool:
            return z.verbunden and all((s := z.geraet(sid)) is not None and s.aktivitaet == "OFF"
                                       for sid in steckdosen)
        if z.verbunden and not await z.warten_bis(alle_aus, self.bestaetigung_timeout_s):
            for sid in steckdosen:
                s = z.geraet(sid)
                if s is not None and s.aktivitaet != "OFF":
                    bericht.fehler.append(f"Steckdose {sid} meldet nach dem Stopp {s.aktivitaet}")

        # 4b. Kopplungen prüfen
        for k in kopplungen:
            bericht.fehler += [f"Kopplung {k.id}: {f}" for f in k.fehler()]
            for gid, typ in ((k.ventil_id, GeraeteTyp.VENTIL), (k.steckdose_id, GeraeteTyp.STECKDOSE)):
                g = z.geraet(gid)
                if g is None:
                    continue
                if g.typ is not typ:
                    bericht.fehler.append(f"Kopplung {k.id}: {gid} ist {g.typ.value}, erwartet {typ.value}")
                elif not g.online:
                    bericht.fehler.append(f"Kopplung {k.id}: {g.name} ist offline")
        ventile = [k.ventil_id for k in kopplungen]
        doppelt = sorted({v for v in ventile if ventile.count(v) > 1})
        if doppelt:
            bericht.fehler.append(f"Ventil in mehreren Kopplungen: {', '.join(doppelt)}")

        # 5. Zeitpläne pausieren
        bericht.pausiert, pause_fehler = await self.zeitplaene_pausieren()
        bericht.fehler += pause_fehler

        # 6. Freigabe
        self.orchestrator.freigegeben = bericht.ok
        if bericht.ok:
            text = f"Startprüfung bestanden: {len(kopplungen)} Kopplungen, {len(bericht.pausiert)} Zeitpläne pausiert"
        else:
            text = "Startprüfung nicht bestanden, Läufe gesperrt: " + "; ".join(bericht.fehler)
        self.ereignisse.melden(Sicherheitsereignis(Ereignisart.STARTPRUEFUNG, text))
        return bericht

    async def zeitplaene_pausieren(self) -> tuple[list[str], list[str]]:
        """Zeitpläne aller bekannten Ventile und Steckdosen pausieren. Gibt (pausiert, Fehler) zurück."""
        pausiert, fehler = [], []
        for g in self.zustand.geraete():
            if g.typ not in (GeraeteTyp.VENTIL, GeraeteTyp.STECKDOSE):
                continue
            try:
                await self.befehle.zeitplan_pausieren(g.id, g.typ)
                pausiert.append(g.id)
            except Exception as f:
                fehler.append(f"Zeitplan von {g.name} nicht pausiert: {f}")
        return pausiert, fehler

    async def pausen_erneuern(self) -> bool:
        """Einmal erneuern. False, wenn gerade ein Lauf aktiv oder Gardena nicht verbunden ist."""
        if self.orchestrator.aktive_laeufe or not self.zustand.verbunden:
            return False
        _, fehler = await self.zeitplaene_pausieren()
        for f in fehler:
            self.ereignisse.melden(Sicherheitsereignis(Ereignisart.BEFEHL_FEHLGESCHLAGEN, f))
        return True

    async def pausen_erneuern_schleife(self, intervall_s: float = PAUSE_INTERVALL_S,
                                       neuer_versuch_s: float = PAUSE_NEUER_VERSUCH_S) -> None:
        """Täglich erneuern (Regel 7). Läuft bis zum Abbruch der Aufgabe."""
        while True:
            await self._schlafen(intervall_s)
            while not await self.pausen_erneuern():
                await self._schlafen(neuer_versuch_s)
