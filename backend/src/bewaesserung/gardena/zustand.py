"""Gerätezustand im Speicher, gefüllt vom Gardena-WebSocket. Kein Netzwerk.

Das ist die einzige Wahrheit über die Geräte (Sicherheitsregel 4): Der Orchestrator
wartet hier auf Bestätigungen, der Interlock-Wächter wird bei jeder Änderung aufgerufen.

Jede WebSocket-Nachricht ist ein JSON:API-Objekt und wirkt als Teil-Aktualisierung:
Felder unter `attributes` und `relationships` werden ersetzt, `null` löscht ein Feld.
DEVICE und COMMON tragen dieselbe ID, deshalb ist der Schlüssel (type, id).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

from .modelle import DIENST_ZUORDNUNG, Geraet, aus_dienst

log = logging.getLogger(__name__)

Beobachter = Callable[[], None]


class Geraetezustand:
    def __init__(self, uhr: Callable[[], float] = time.monotonic) -> None:
        self._uhr = uhr
        self._objekte: dict[tuple[str, str], dict] = {}
        self._verbunden = False
        self.verbunden_seit: float | None = None   # Uhrzeit (monoton) der letzten Verbindung
        self.trennung_geplant = False
        self._beobachter: list[Beobachter] = []
        self._wartende: set[asyncio.Event] = set()

    # --- Beobachten ------------------------------------------------------

    def beobachten(self, beobachter: Beobachter) -> None:
        """`beobachter()` wird nach jeder Änderung synchron aufgerufen (auch Verbindungswechsel)."""
        self._beobachter.append(beobachter)

    def _geaendert(self) -> None:
        for ereignis in self._wartende:
            ereignis.set()
        for beobachter in list(self._beobachter):
            try:
                beobachter()
            except Exception:
                log.exception("Beobachter des Gerätezustands fehlgeschlagen")

    def wecken(self) -> None:
        """Wartende Bedingungen neu prüfen lassen (z. B. nach einem Abbruch)."""
        for ereignis in self._wartende:
            ereignis.set()

    async def warten_bis(self, bedingung: Callable[["Geraetezustand"], bool], timeout_s: float) -> bool:
        """Wartet, bis `bedingung(self)` wahr ist. False nach Ablauf von `timeout_s`."""
        loop = asyncio.get_running_loop()
        frist = loop.time() + timeout_s
        ereignis = asyncio.Event()
        self._wartende.add(ereignis)
        try:
            while not bedingung(self):
                rest = frist - loop.time()
                if rest <= 0:
                    return False
                ereignis.clear()
                try:
                    await asyncio.wait_for(ereignis.wait(), timeout=rest)
                except asyncio.TimeoutError:
                    return bedingung(self)
            return True
        finally:
            self._wartende.discard(ereignis)

    # --- Verbindung ------------------------------------------------------

    @property
    def verbunden(self) -> bool:
        """Nur bei True ist der Stand aktuell."""
        return self._verbunden

    def verbindung_setzen(self, verbunden: bool, geplant: bool = False) -> None:
        """`geplant=True` beim Trennen heißt: wir erneuern die Verbindung selbst (kein Abriss)."""
        if verbunden != self._verbunden:
            self._verbunden = verbunden
            if verbunden:
                self.verbunden_seit = self._uhr()
            else:
                self.trennung_geplant = geplant
            self._geaendert()

    def zuruecksetzen(self) -> None:
        """Vor jeder neuen Verbindung: Gardena schickt danach den vollständigen Stand."""
        self._objekte.clear()

    # --- Nachrichten -----------------------------------------------------

    def _einarbeiten(self, objekt: dict) -> bool:
        typ, oid = objekt.get("type"), objekt.get("id")
        if not isinstance(typ, str) or not isinstance(oid, str):
            return False
        bestand = self._objekte.setdefault((typ, oid), {"id": oid, "type": typ})
        for teil in ("attributes", "relationships"):
            if teil not in objekt:
                continue
            neu = objekt[teil]
            if neu is None:
                bestand.pop(teil, None)
                continue
            ziel = bestand.setdefault(teil, {})
            for feld, inhalt in neu.items():
                if inhalt is None:
                    ziel.pop(feld, None)
                else:
                    ziel[feld] = inhalt
        return True

    def anwenden(self, *objekte: dict) -> int:
        anzahl = sum(1 for o in objekte if isinstance(o, dict) and self._einarbeiten(o))
        if anzahl:
            self._geaendert()
        return anzahl

    def nachricht_anwenden(self, text: str) -> int:
        """Eine WebSocket-Nachricht (Einzelobjekt oder Dokument mit data/included) verarbeiten."""
        try:
            inhalt = json.loads(text)
        except ValueError:
            log.warning("WebSocket: keine gültige JSON-Nachricht: %.200s", text)
            return 0
        if not isinstance(inhalt, dict):
            return 0
        if "type" in inhalt:
            objekte = [inhalt]
        else:
            daten = inhalt.get("data")
            objekte = (daten if isinstance(daten, list) else [daten]) + list(inhalt.get("included") or [])
        anzahl = self.anwenden(*objekte)
        if anzahl == 0:
            log.warning("WebSocket: Nachricht ohne verwertbares Objekt: %.200s", text)
        return anzahl

    # --- Abfragen --------------------------------------------------------

    def _geraete_id(self, dienst: dict) -> str:
        bezug = ((dienst.get("relationships") or {}).get("device") or {}).get("data") or {}
        return bezug.get("id") or dienst["id"].split(":")[0]

    def geraete(self) -> list[Geraet]:
        ergebnis = []
        for (typ, oid), dienst in sorted(self._objekte.items(), key=lambda e: e[0][1]):
            if typ not in DIENST_ZUORDNUNG:
                continue
            geraet = aus_dienst(dienst, self._objekte.get(("COMMON", self._geraete_id(dienst))))
            if geraet is not None:
                ergebnis.append(geraet)
        return ergebnis

    def geraet(self, dienst_id: str) -> Geraet | None:
        dienst = next((d for (t, oid), d in self._objekte.items() if oid == dienst_id and t in DIENST_ZUORDNUNG), None)
        if dienst is None:
            return None
        return aus_dienst(dienst, self._objekte.get(("COMMON", self._geraete_id(dienst))))
