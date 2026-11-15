"""Orchestrator: führt einen gekoppelten Lauf aus (Sicherheitsregeln 2, 3, 4, 8).

Ablauf:
1. Prüfen (freigegeben, verbunden, Höchstlaufzeit, Geräte frei und bereit).
2. Ventil mit Dauer T öffnen, auf Meldung `MANUAL_WATERING` warten. Ohne Bestätigung:
   Ventil stoppen, abbrechen, melden.
3. Steckdose mit T − Vorlauf − Nachlauf (abzüglich der seit dem Ventilbefehl vergangenen
   Zeit) starten, auf Meldung „an“ warten. Ohne Bestätigung: abbrechen.
4. Warten, bis das Ventil wieder geschlossen meldet. Die Steckdose läuft zuerst aus.

Abbruch: immer Steckdose zuerst. Ist ihr Zustand danach unklar, bleibt das Ventil offen,
bis die Steckdosenlaufzeit sicher abgelaufen ist.

Gezählt wird nur, was der Gerätezustand (WebSocket) meldet, nie die Antwort auf einen Befehl.
Der Scheduler und die App dürfen nur über `lauf()` schalten.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from ..gardena.client import Befehle
from ..gardena.modelle import GeraeteTyp
from ..gardena.zustand import Geraetezustand
from .ereignisse import Ereignisart, Ereignisse, Sicherheitsereignis
from .kopplung import SEKUNDEN_SCHRITT, Kopplung, LaufAbgelehnt, steckdosen_laufzeit

log = logging.getLogger(__name__)

BESTAETIGUNG_TIMEOUT_S = 30.0
SICHERHEITSABSTAND_S = 30.0     # nach geplantem Steckdosenende, bevor das Ventil zugemacht wird
ENDE_TOLERANZ_S = 120.0         # so lange nach T darf das Ventil noch „offen“ melden


class LaufStatus(str, Enum):
    BEENDET = "BEENDET"
    ABGEBROCHEN = "ABGEBROCHEN"
    FEHLGESCHLAGEN = "FEHLGESCHLAGEN"


@dataclass
class LaufErgebnis:
    kopplung_id: str
    status: LaufStatus
    geplant_s: int
    steckdose_s: int = 0
    tatsaechlich_s: float = 0.0
    grund: str = ""


@dataclass
class _Lauf:
    kopplung: Kopplung
    ventil_s: int
    sperre: asyncio.Lock = field(default_factory=asyncio.Lock)
    abbruch_grund: str | None = None
    ventil_gestartet: bool = False
    steckdose_gestartet: bool = False
    steckdose_ende: float | None = None      # geplantes Ende laut unserer Uhr


def _ventil_bewaessert(z: Geraetezustand, ventil_id: str) -> bool:
    v = z.geraet(ventil_id)
    return z.verbunden and v is not None and v.aktivitaet == "MANUAL_WATERING"


def _ventil_zu(z: Geraetezustand, ventil_id: str) -> bool:
    v = z.geraet(ventil_id)
    return z.verbunden and v is not None and not v.ventil_offen


def _steckdose_an(z: Geraetezustand, steckdose_id: str) -> bool:
    s = z.geraet(steckdose_id)
    return z.verbunden and s is not None and s.aktivitaet not in (None, "OFF")


def _steckdose_aus(z: Geraetezustand, steckdose_id: str) -> bool:
    s = z.geraet(steckdose_id)
    return z.verbunden and s is not None and s.aktivitaet == "OFF"


class Orchestrator:
    def __init__(
        self,
        befehle: Befehle,
        zustand: Geraetezustand,
        ereignisse: Ereignisse,
        kopplungen: Callable[[], list[Kopplung]],
        bestaetigung_timeout_s: float = BESTAETIGUNG_TIMEOUT_S,
        sicherheitsabstand_s: float = SICHERHEITSABSTAND_S,
        ende_toleranz_s: float = ENDE_TOLERANZ_S,
        uhr: Callable[[], float] = time.monotonic,
        schlafen: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.befehle = befehle
        self.zustand = zustand
        self.ereignisse = ereignisse
        self._kopplungen = kopplungen
        self.timeout_s = bestaetigung_timeout_s
        self.sicherheitsabstand_s = sicherheitsabstand_s
        self.ende_toleranz_s = ende_toleranz_s
        self._uhr = uhr
        self._schlafen = schlafen
        self.freigegeben = False          # setzt nur die Startprüfung (Regel 6)
        self._aktiv: dict[str, _Lauf] = {}

    # --- Hilfen ----------------------------------------------------------

    def _melden(self, art: Ereignisart, text: str, k: Kopplung | None = None, geraet_id: str | None = None) -> None:
        self.ereignisse.melden(Sicherheitsereignis(art, text, geraet_id, k.id if k else None))

    def kopplung(self, kopplung_id: str) -> Kopplung:
        for k in self._kopplungen():
            if k.id == kopplung_id:
                return k
        raise LaufAbgelehnt(f"Kopplung {kopplung_id} unbekannt")

    @property
    def aktive_laeufe(self) -> list[str]:
        return list(self._aktiv)

    async def _stoppen(self, k: Kopplung | None, geraet_id: str, typ: GeraeteTyp) -> bool:
        try:
            await self.befehle.stoppen(geraet_id, typ)
            return True
        except Exception as f:
            self._melden(Ereignisart.BEFEHL_FEHLGESCHLAGEN, f"Stopp an {geraet_id} fehlgeschlagen: {f}", k, geraet_id)
            return False

    # --- Lauf ------------------------------------------------------------

    def _vorpruefen(self, k: Kopplung, ventil_s: int) -> int:
        if not self.freigegeben:
            raise LaufAbgelehnt("Startprüfung nicht bestanden, Läufe sind gesperrt")
        steckdose_s = k.pruefen(ventil_s)                       # Regeln 2 und 8
        if not self.zustand.verbunden:
            raise LaufAbgelehnt("Keine Verbindung zu Gardena, Gerätezustand unbekannt")
        for lauf in self._aktiv.values():
            belegt = {lauf.kopplung.ventil_id, lauf.kopplung.steckdose_id}
            if belegt & {k.ventil_id, k.steckdose_id}:
                raise LaufAbgelehnt(f"Gerät ist schon im Lauf {lauf.kopplung.id} belegt")
        v, s = self.zustand.geraet(k.ventil_id), self.zustand.geraet(k.steckdose_id)
        if v is None or s is None:
            raise LaufAbgelehnt("Ventil oder Steckdose meldet keinen Zustand")
        if not v.online or not s.online:
            raise LaufAbgelehnt("Ventil oder Steckdose ist offline")
        if v.ventil_offen:
            raise LaufAbgelehnt("Ventil ist schon offen")
        if s.aktivitaet != "OFF":
            raise LaufAbgelehnt("Steckdose ist nicht aus")
        return steckdose_s

    async def lauf(self, kopplung_id: str, ventil_s: int) -> LaufErgebnis:
        """Einen gekoppelten Lauf ausführen und bis zum Ende begleiten.

        Wirft LaufAbgelehnt, wenn der Lauf gar nicht erst beginnt.
        """
        k = self.kopplung(kopplung_id)
        try:
            self._vorpruefen(k, ventil_s)
        except LaufAbgelehnt as f:
            self._melden(Ereignisart.LAUF_ABGELEHNT, str(f), k)
            raise
        lauf = _Lauf(k, ventil_s)
        self._aktiv[k.id] = lauf
        try:
            return await self._ausfuehren(lauf)
        finally:
            del self._aktiv[k.id]

    async def _ausfuehren(self, lauf: _Lauf) -> LaufErgebnis:
        k, z, T = lauf.kopplung, self.zustand, lauf.ventil_s
        ergebnis = LaufErgebnis(k.id, LaufStatus.FEHLGESCHLAGEN, T)

        # 1. Ventil öffnen
        t0 = self._uhr()
        try:
            lauf.ventil_gestartet = True
            await self.befehle.starten(k.ventil_id, GeraeteTyp.VENTIL, T)
        except Exception as f:
            self._melden(Ereignisart.BEFEHL_FEHLGESCHLAGEN, f"Ventilstart fehlgeschlagen: {f}", k, k.ventil_id)
            await self._abbruch_sequenz(lauf)
            ergebnis.grund = f"Ventilstart fehlgeschlagen: {f}"
            return ergebnis

        # 2. Bestätigung des Ventils abwarten
        bestaetigt = await z.warten_bis(
            lambda z: _ventil_bewaessert(z, k.ventil_id) or lauf.abbruch_grund is not None, self.timeout_s)
        if lauf.abbruch_grund:               # abbrechen() führt die Abbruchsequenz selbst aus
            ergebnis.status, ergebnis.grund = LaufStatus.ABGEBROCHEN, lauf.abbruch_grund
            return ergebnis
        if not bestaetigt:
            self._melden(Ereignisart.VENTIL_UNBESTAETIGT,
                         f"Ventil {k.ventil_id} meldet nach {self.timeout_s:.0f} s keine Bewässerung", k, k.ventil_id)
            await self._abbruch_sequenz(lauf)
            ergebnis.grund = "Ventil nicht bestätigt"
            return ergebnis
        offen_seit = self._uhr()

        # 3. Steckdose mit kürzerer Laufzeit starten
        async with lauf.sperre:
            steckdose_s = steckdosen_laufzeit(k, T, self._uhr() - t0)
            grund = lauf.abbruch_grund
            if grund is None and steckdose_s < SEKUNDEN_SCHRITT:
                grund = "Ventilbestätigung kam zu spät, keine Steckdosenlaufzeit übrig"
            if grund is None and not _ventil_bewaessert(z, k.ventil_id):
                grund = "Ventil vor dem Steckdosenstart wieder geschlossen"
            if grund is None:
                try:
                    lauf.steckdose_gestartet = True
                    lauf.steckdose_ende = self._uhr() + steckdose_s
                    await self.befehle.starten(k.steckdose_id, GeraeteTyp.STECKDOSE, steckdose_s)
                except Exception as f:
                    grund = f"Steckdosenstart fehlgeschlagen: {f}"
                    self._melden(Ereignisart.BEFEHL_FEHLGESCHLAGEN, grund, k, k.steckdose_id)
        if grund is not None:
            if lauf.abbruch_grund is None:
                self._melden(Ereignisart.LAUF_ABGEBROCHEN, grund, k)
                await self._abbruch_sequenz(lauf)
            ergebnis.grund = grund
            ergebnis.status = LaufStatus.ABGEBROCHEN if lauf.abbruch_grund else LaufStatus.FEHLGESCHLAGEN
            ergebnis.tatsaechlich_s = self._uhr() - offen_seit
            return ergebnis
        ergebnis.steckdose_s = steckdose_s

        # 4. Bestätigung der Steckdose abwarten
        if not await z.warten_bis(lambda z: _steckdose_an(z, k.steckdose_id) or lauf.abbruch_grund, self.timeout_s):
            self._melden(Ereignisart.STECKDOSE_UNBESTAETIGT,
                         f"Steckdose {k.steckdose_id} meldet nach {self.timeout_s:.0f} s nicht „an“", k, k.steckdose_id)
            await self._abbruch_sequenz(lauf)
            ergebnis.grund = "Steckdose nicht bestätigt"
            ergebnis.tatsaechlich_s = self._uhr() - offen_seit
            return ergebnis

        # 5. Ende abwarten: Steckdose läuft zuerst aus, dann schließt das Ventil
        frist = T - (self._uhr() - t0) + self.ende_toleranz_s
        if not await z.warten_bis(lambda z: _ventil_zu(z, k.ventil_id), frist):
            self._melden(Ereignisart.STOPP_UNBESTAETIGT,
                         f"Ventil {k.ventil_id} meldet nach Laufzeitende nicht „geschlossen“", k, k.ventil_id)
            await self._abbruch_sequenz(lauf)
        ergebnis.tatsaechlich_s = self._uhr() - offen_seit
        if lauf.abbruch_grund:
            ergebnis.status, ergebnis.grund = LaufStatus.ABGEBROCHEN, lauf.abbruch_grund
        else:
            ergebnis.status = LaufStatus.BEENDET
            if ergebnis.tatsaechlich_s < T - k.vorlauf_ventil_s - self.ende_toleranz_s:
                ergebnis.grund = "Ventil vorzeitig geschlossen"
        return ergebnis

    # --- Abbruch ---------------------------------------------------------

    async def abbrechen(self, kopplung_id: str, grund: str = "Abbruch angefordert") -> bool:
        """Laufenden Lauf abbrechen (Steckdose zuerst). False, wenn kein Lauf aktiv ist."""
        lauf = self._aktiv.get(kopplung_id)
        if lauf is None or lauf.abbruch_grund:
            return False
        lauf.abbruch_grund = grund
        self._melden(Ereignisart.LAUF_ABGEBROCHEN, grund, lauf.kopplung)
        self.zustand.wecken()            # Lauf prüft seine Wartebedingungen neu
        async with lauf.sperre:          # wartet, falls gerade die Steckdose gestartet wird
            await self._abbruch_sequenz(lauf)
        return True

    async def not_aus(self, grund: str = "Not-Aus") -> None:
        """Alle Läufe abbrechen, danach alles, was noch läuft, stoppen: Steckdosen vor Ventilen."""
        await asyncio.gather(*(self.abbrechen(kid, grund) for kid in list(self._aktiv)))
        geraete = self.zustand.geraete()
        for g in geraete:
            if g.typ is GeraeteTyp.STECKDOSE and g.aktivitaet != "OFF":
                await self._stoppen(None, g.id, GeraeteTyp.STECKDOSE)
        for g in geraete:
            if g.ventil_offen:
                await self._stoppen(None, g.id, GeraeteTyp.VENTIL)

    async def _abbruch_sequenz(self, lauf: _Lauf) -> None:
        """Steckdose aus, Bestätigung abwarten, erst dann Ventil zu."""
        k, z = lauf.kopplung, self.zustand
        if lauf.steckdose_gestartet or not _steckdose_aus(z, k.steckdose_id):
            await self._stoppen(k, k.steckdose_id, GeraeteTyp.STECKDOSE)
            if not await z.warten_bis(lambda z: _steckdose_aus(z, k.steckdose_id), self.timeout_s):
                if lauf.steckdose_ende is None:
                    self._melden(Ereignisart.STECKDOSE_ZUSTAND_UNKLAR,
                                 "Steckdose meldet nicht „aus“; Ventil bleibt offen und endet per Gardena-Laufzeit",
                                 k, k.steckdose_id)
                    return
                warten = max(lauf.steckdose_ende + self.sicherheitsabstand_s - self._uhr(), 0)
                self._melden(Ereignisart.STECKDOSE_ZUSTAND_UNKLAR,
                             f"Steckdose meldet nicht „aus“; Ventil bleibt noch {warten:.0f} s offen",
                             k, k.steckdose_id)
                await self._schlafen(warten)
        if _ventil_zu(z, k.ventil_id) and not lauf.ventil_gestartet:
            return
        # Auch bei „geschlossen“ stoppen, wenn wir gestartet haben: der Startbefehl kann noch unterwegs sein.
        await self._stoppen(k, k.ventil_id, GeraeteTyp.VENTIL)
        if not await z.warten_bis(lambda z: _ventil_zu(z, k.ventil_id), self.timeout_s):
            self._melden(Ereignisart.STOPP_UNBESTAETIGT,
                         "Ventil meldet nicht „geschlossen“; es endet per Gardena-Laufzeit", k, k.ventil_id)
