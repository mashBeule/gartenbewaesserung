"""API-Budget für die Gardena-REST-API. Reine Logik, kein Netzwerk.

Gardena erlaubt höchstens 700 Aufrufe pro Woche und 10 in 10 Sekunden; bei HTTP 429
nehmen wir eine Sperre von 24 Stunden an (docs/gardena-api.md). Beide Grenzen zählen
in gleitenden Fenstern. Jeder REST-Aufruf ruft vorher `anfordern()` auf.

Prioritäten:
- SICHERHEIT (Stopps): darf das ganze Wochenbudget nutzen.
- SYSTEM (Token, Standorte, WebSocket-Adresse): lässt die Sicherheitsreserve übrig.
- NORMAL (Starts, Pausen): lässt zusätzlich eine Reserve für SYSTEM übrig.
Die 10-in-10-Sekunden-Grenze gilt für alle, sonst droht 429.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

SPERRE_NACH_429 = timedelta(hours=24)
WOCHE = timedelta(days=7)
KURZFENSTER = timedelta(seconds=10)


class Prioritaet(str, Enum):
    NORMAL = "NORMAL"
    SYSTEM = "SYSTEM"
    SICHERHEIT = "SICHERHEIT"


class BudgetErschoepft(Exception):
    def __init__(self, grund: str, wieder_frei: datetime | None = None):
        super().__init__(grund)
        self.grund = grund
        self.wieder_frei = wieder_frei


@dataclass
class ApiBudget:
    wochenlimit: int = 700
    kurzlimit: int = 10
    reserve_sicherheit: int = 50
    reserve_system: int = 50
    gesperrt_bis: datetime | None = None
    _aufrufe: deque[datetime] = field(default_factory=deque, repr=False)

    def verbraucht(self, jetzt: datetime) -> int:
        while self._aufrufe and self._aufrufe[0] <= jetzt - WOCHE:
            self._aufrufe.popleft()
        return len(self._aufrufe)

    def grenze_fuer(self, p: Prioritaet) -> int:
        if p is Prioritaet.SICHERHEIT:
            return self.wochenlimit
        if p is Prioritaet.SYSTEM:
            return self.wochenlimit - self.reserve_sicherheit
        return self.wochenlimit - self.reserve_sicherheit - self.reserve_system

    def pruefen(self, p: Prioritaet, jetzt: datetime) -> None:
        """Wirft BudgetErschoepft, wenn der Aufruf jetzt nicht erlaubt ist."""
        if self.gesperrt_bis and jetzt < self.gesperrt_bis:
            raise BudgetErschoepft(f"Gardena-Sperre bis {self.gesperrt_bis:%d.%m. %H:%M}", self.gesperrt_bis)
        verbraucht = self.verbraucht(jetzt)
        if verbraucht >= self.grenze_fuer(p):
            frei = self._aufrufe[verbraucht - self.grenze_fuer(p)] + WOCHE if self._aufrufe else None
            raise BudgetErschoepft(f"Wochenbudget für {p.value} erschöpft ({verbraucht})", frei)
        kurz = [t for t in self._aufrufe if t > jetzt - KURZFENSTER]
        if len(kurz) >= self.kurzlimit:
            raise BudgetErschoepft(
                f"Höchstens {self.kurzlimit} Aufrufe in 10 Sekunden", kurz[-self.kurzlimit] + KURZFENSTER)

    def anfordern(self, p: Prioritaet, jetzt: datetime) -> None:
        self.pruefen(p, jetzt)
        self._aufrufe.append(jetzt)

    def gesperrt_melden(self, jetzt: datetime) -> None:
        """Nach HTTP 429 aufrufen. Danach 24 h keine Aufrufe, auch keine Sicherheits-Stopps."""
        self.gesperrt_bis = jetzt + SPERRE_NACH_429
