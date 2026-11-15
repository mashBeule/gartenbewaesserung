"""Wetter-Dienst: ruft die Quellen stündlich ab, hält die Daten im Speicher und entscheidet je Termin.

Erfüllt `planung.scheduler.Wetter`: Der Scheduler fragt direkt vor jedem geplanten Start
`faktor(zone_id, start)`. Die Entscheidung nutzt die zwischengespeicherten Wetterdaten
(höchstens eine Stunde alt) und den aktuellen Bodenfeuchte-Wert aus dem Gardena-WebSocket.

Fehlt der Standort, wird nichts abgerufen und jeder Lauf läuft unverändert (Faktor 1,0 mit Hinweis).
Fällt eine Quelle aus, bleiben die alten Daten; nach 12 h gelten sie als veraltet (Faktor 1,0).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from ..gardena.zustand import Geraetezustand
from ..speicher.datenbank import Speicher
from .entscheidung import MAX_ALTER, Entscheidung, Reihe, entscheiden
from .quellen import Wetterquelle

log = logging.getLogger(__name__)

ABRUF_INTERVALL_S = 3600.0


@dataclass(frozen=True)
class Lage:
    """Kurzfassung für App und Status."""
    standort_gesetzt: bool
    prognose_stand: datetime | None
    messung_stand: datetime | None
    veraltet: bool
    fehler: list[str]
    regen_24h_mm: float | None
    regen_prognose_12h_mm: float | None
    tiefsttemperatur_12h_c: float | None
    et0_24h_mm: float | None


class WetterDienst:
    def __init__(
        self,
        speicher: Speicher,
        zustand: Geraetezustand,
        prognosequelle: Wetterquelle,
        messquelle: Wetterquelle | None = None,
        uhr: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        schlafen: Callable[[float], Awaitable[None]] = asyncio.sleep,
        intervall_s: float = ABRUF_INTERVALL_S,
    ) -> None:
        self.speicher = speicher
        self.zustand = zustand
        self.prognosequelle = prognosequelle
        self.messquelle = messquelle
        self._uhr = uhr
        self._schlafen = schlafen
        self.intervall_s = intervall_s
        self.prognose: Reihe | None = None
        self.messung: Reihe | None = None
        self.fehler: dict[str, str] = {}
        self._geweckt: asyncio.Event | None = None

    # --- Abruf -----------------------------------------------------------

    async def aktualisieren(self) -> None:
        standort = self.speicher.standort()
        if standort is None:
            return
        jetzt = self._uhr()
        for quelle, ziel in ((self.prognosequelle, "prognose"), (self.messquelle, "messung")):
            if quelle is None:
                continue
            try:
                setattr(self, ziel, await quelle.abrufen(*standort, jetzt))
                self.fehler.pop(quelle.name, None)
            except Exception as f:                       # Netz weg, Dienst down, Format geändert
                self.fehler[quelle.name] = f"{type(f).__name__}: {f}"
                log.warning("Wetter: %s nicht abrufbar: %s", quelle.name, f)

    async def laufen(self) -> None:
        self._geweckt = asyncio.Event()
        while True:
            await self.aktualisieren()
            self._geweckt.clear()
            schlaf = asyncio.ensure_future(self._schlafen(self.intervall_s))
            weck = asyncio.ensure_future(self._geweckt.wait())
            try:
                await asyncio.wait({schlaf, weck}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                schlaf.cancel()
                weck.cancel()

    def wecken(self) -> None:
        """Sofort neu abrufen, z. B. nach Änderung des Standorts."""
        if self._geweckt is not None:
            self._geweckt.set()

    async def schliessen(self) -> None:
        for quelle in (self.prognosequelle, self.messquelle):
            http = getattr(quelle, "http", None)
            if http is not None:
                await http.aclose()

    # --- Entscheidung ----------------------------------------------------

    def _bodenfeuchte(self, sensor_id: str | None) -> float | None:
        if sensor_id is None:
            return None
        g = self.zustand.geraet(sensor_id)
        return g.bodenfeuchte_prozent if g is not None and g.online else None

    def entscheidung(self, zone_id: int, start: datetime) -> Entscheidung:
        regel = self.speicher.wetterregel_fuer(zone_id)
        if regel.aktiv and self.speicher.standort() is None and regel.sensor_id is None:
            return Entscheidung(1.0, "Kein Standort eingestellt, Wetter nicht berücksichtigt", veraltet=True)
        return entscheiden(regel, start, self._uhr(), self.prognose, self.messung,
                           bodenfeuchte_pct=self._bodenfeuchte(regel.sensor_id),
                           letzte_bewaesserung=self.speicher.letzte_bewaesserung(zone_id))

    def faktor(self, zone_id: int, start: datetime) -> tuple[float, str]:
        e = self.entscheidung(zone_id, start)
        return e.faktor, (f"Wetter: {e.text}" if e.text else "")

    def lage(self) -> Lage:
        jetzt = self._uhr()
        p, m = self.prognose, self.messung
        regen = None
        if m is not None and m.frisch(jetzt):
            regen = m.summe("niederschlag_mm", jetzt - timedelta(hours=24), jetzt)
        if regen is None and p is not None:
            regen = p.summe("niederschlag_mm", jetzt - timedelta(hours=24), jetzt)
        bald = jetzt + timedelta(hours=12)
        return Lage(
            standort_gesetzt=self.speicher.standort() is not None,
            prognose_stand=p.stand if p else None,
            messung_stand=m.stand if m else None,
            veraltet=p is None or jetzt - p.stand > MAX_ALTER,
            fehler=[f"{q}: {t}" for q, t in self.fehler.items()],
            regen_24h_mm=regen,
            regen_prognose_12h_mm=p.summe("niederschlag_mm", jetzt, bald) if p else None,
            tiefsttemperatur_12h_c=p.minimum("temperatur_c", jetzt, bald) if p else None,
            et0_24h_mm=p.summe("et0_mm", jetzt - timedelta(hours=24), jetzt) if p else None,
        )
