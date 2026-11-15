"""Sicherheitsereignisse: werden geloggt, später ins run_log geschrieben und als Push gemeldet."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

log = logging.getLogger(__name__)


class Ereignisart(str, Enum):
    INTERLOCK = "INTERLOCK"                         # Steckdose an, Ventil nicht offen (Regel 5)
    ZEITPLAN_LAUF = "ZEITPLAN_LAUF"                 # Gardena-Zeitplan hat etwas gestartet (Regel 7)
    VERBINDUNG_ABGERISSEN = "VERBINDUNG_ABGERISSEN" # WebSocket weg, Steckdose vorsorglich gestoppt
    VENTIL_UNBESTAETIGT = "VENTIL_UNBESTAETIGT"     # Ventil meldet keine Bewässerung (Regel 3)
    STECKDOSE_UNBESTAETIGT = "STECKDOSE_UNBESTAETIGT"
    STECKDOSE_ZUSTAND_UNKLAR = "STECKDOSE_ZUSTAND_UNKLAR"  # Stopp nicht bestätigt, Ventil bleibt offen
    STOPP_UNBESTAETIGT = "STOPP_UNBESTAETIGT"
    BEFEHL_FEHLGESCHLAGEN = "BEFEHL_FEHLGESCHLAGEN"
    LAUF_ABGELEHNT = "LAUF_ABGELEHNT"               # z. B. über Höchstlaufzeit (Regel 8)
    LAUF_ABGEBROCHEN = "LAUF_ABGEBROCHEN"
    STARTPRUEFUNG = "STARTPRUEFUNG"                 # Ergebnis der Prüfung beim Start (Regel 6)


@dataclass(frozen=True)
class Sicherheitsereignis:
    art: Ereignisart
    text: str
    geraet_id: str | None = None
    kopplung_id: str | None = None
    zeitpunkt: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Ereignisse(Protocol):
    def melden(self, ereignis: Sicherheitsereignis) -> None: ...


class EreignisListe:
    """Hält Ereignisse im Speicher und loggt sie. Ersatz für run_log + Push, bis es die gibt."""

    def __init__(self) -> None:
        self.liste: list[Sicherheitsereignis] = []

    def melden(self, ereignis: Sicherheitsereignis) -> None:
        self.liste.append(ereignis)
        log.warning("Sicherheitsereignis %s: %s", ereignis.art.value, ereignis.text)

    def arten(self) -> list[Ereignisart]:
        return [e.art for e in self.liste]
