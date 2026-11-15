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

import json
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

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
        self._sichern()

    def gesperrt_melden(self, jetzt: datetime) -> None:
        """Nach HTTP 429 aufrufen. Danach 24 h keine Aufrufe, auch keine Sicherheits-Stopps."""
        self.gesperrt_bis = jetzt + SPERRE_NACH_429
        self._sichern()

    # --- Über Neustarts hinweg ------------------------------------------
    # Sonst würde jede Absturzschleife das Budget neu beginnen lassen und Gardena mit
    # Startprüfungen fluten. Die Datei wird bei jedem Aufruf atomar ersetzt.

    datei: Path | None = None

    @classmethod
    def aus_datei(cls, datei: Path, **kw) -> "ApiBudget":
        b = cls(datei=datei, **kw)
        try:
            daten = json.loads(datei.read_text(encoding="utf-8"))
            b._aufrufe.extend(sorted(datetime.fromisoformat(t) for t in daten.get("aufrufe", [])))
            if daten.get("gesperrt_bis"):
                b.gesperrt_bis = datetime.fromisoformat(daten["gesperrt_bis"])
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as f:
            # Vorsichtig: unlesbare Datei = Budget gilt als zur Hälfte verbraucht
            log.error("Budget-Datei %s unlesbar (%s), rechne vorsichtig", datei, f)
            jetzt = datetime.now(timezone.utc)
            b._aufrufe.extend([jetzt] * (b.wochenlimit // 2))
        return b

    def _sichern(self) -> None:
        if self.datei is None:
            return
        try:
            self.datei.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.datei.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "aufrufe": [t.isoformat() for t in self._aufrufe],
                "gesperrt_bis": self.gesperrt_bis.isoformat() if self.gesperrt_bis else None,
            }), encoding="utf-8")
            os.replace(tmp, self.datei)
        except OSError as f:
            log.error("Budget-Datei %s nicht schreibbar: %s", self.datei, f)
