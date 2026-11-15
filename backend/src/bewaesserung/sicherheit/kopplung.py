"""Kopplung Ventil + Steckdose und die Laufzeitrechnung (Sicherheitsregeln 1, 2, 8). Reine Logik.

Laufzeit Steckdose = Laufzeit Ventil − Vorlauf − Nachlauf, abgerundet auf die Schrittweite
der API. Weil die Steckdose erst nach der Ventilbestätigung startet, wird beim Start zusätzlich
die seither vergangene Zeit abgezogen: Die Steckdose endet so immer mindestens `nachlauf`
Sekunden vor dem Ventil, auch wenn die Bestätigung länger dauert als der Vorlauf.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SEKUNDEN_SCHRITT = 60          # ⚠ Schrittweite der API, in Phase 0 prüfen (docs/gardena-api.md)
API_HOECHSTWERT_S = 36000      # ⚠ laut Release-Notizen für Ventile
MIN_NACHLAUF_S = 10            # deckt die Laufzeit des Steckdosenbefehls zum Gerät ab


class LaufAbgelehnt(Exception):
    """Der angeforderte Lauf verletzt eine Sicherheitsregel und wird nicht gestartet."""


def abrunden(sekunden: float) -> int:
    return int(math.floor(sekunden / SEKUNDEN_SCHRITT)) * SEKUNDEN_SCHRITT


@dataclass(frozen=True)
class Kopplung:
    id: str
    ventil_id: str                 # Gardena-Dienst-ID des VALVE
    steckdose_id: str              # Gardena-Dienst-ID des POWER_SOCKET
    vorlauf_ventil_s: int          # Ventil öffnet so viel früher als die Steckdose
    nachlauf_ventil_s: int         # Ventil bleibt so viel länger offen als die Steckdose
    max_laufzeit_s: int            # absolute Höchstlaufzeit des Ventils (Regel 8)

    def fehler(self) -> list[str]:
        """Konfigurationsfehler. Leere Liste = Kopplung ist gültig."""
        f = []
        if self.ventil_id == self.steckdose_id:
            f.append("Ventil und Steckdose sind dasselbe Gerät")
        if self.vorlauf_ventil_s < 0:
            f.append("Vorlauf darf nicht negativ sein")
        if self.nachlauf_ventil_s < MIN_NACHLAUF_S:
            f.append(f"Nachlauf muss mindestens {MIN_NACHLAUF_S} s sein")
        if not 0 < self.max_laufzeit_s <= API_HOECHSTWERT_S:
            f.append(f"Höchstlaufzeit muss zwischen 1 und {API_HOECHSTWERT_S} s liegen")
        elif abrunden(self.max_laufzeit_s - self.vorlauf_ventil_s - self.nachlauf_ventil_s) < SEKUNDEN_SCHRITT:
            f.append("Höchstlaufzeit lässt nach Vor- und Nachlauf keine Steckdosenlaufzeit übrig")
        return f

    def pruefen(self, ventil_s: int) -> int:
        """Prüft eine angeforderte Ventillaufzeit und gibt die geplante Steckdosenlaufzeit zurück."""
        if self.fehler():
            raise LaufAbgelehnt(f"Kopplung {self.id} ist ungültig: {'; '.join(self.fehler())}")
        if not isinstance(ventil_s, int) or ventil_s <= 0:
            raise LaufAbgelehnt(f"Laufzeit muss eine positive ganze Zahl sein, nicht {ventil_s!r}")
        if ventil_s > self.max_laufzeit_s:
            raise LaufAbgelehnt(f"Laufzeit {ventil_s} s über der Höchstlaufzeit {self.max_laufzeit_s} s")
        if ventil_s % SEKUNDEN_SCHRITT:
            raise LaufAbgelehnt(f"Laufzeit muss ein Vielfaches von {SEKUNDEN_SCHRITT} s sein")
        steckdose_s = steckdosen_laufzeit(self, ventil_s, vergangen_s=0)
        if steckdose_s < SEKUNDEN_SCHRITT:
            raise LaufAbgelehnt(f"Laufzeit {ventil_s} s zu kurz für Vorlauf und Nachlauf")
        return steckdose_s


def steckdosen_laufzeit(k: Kopplung, ventil_s: int, vergangen_s: float) -> int:
    """Steckdosenlaufzeit beim Start, `vergangen_s` nach dem Ventilbefehl. Kann < Schritt sein."""
    rest = min(ventil_s - k.vorlauf_ventil_s, ventil_s - math.ceil(vergangen_s)) - k.nachlauf_ventil_s
    return max(abrunden(rest), 0)
