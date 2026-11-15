"""Gemeinsamer Testaufbau: ein Ventil, eine Steckdose, eine Kopplung, alles gegen den Gardena-Mock.

Die Tests sind synchron und starten ihre Szenarien mit `asyncio.run`, damit kein
zusätzliches Plugin nötig ist. Zeitgrenzen sind auf Sekundenbruchteile verkürzt.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from bewaesserung.gardena.zustand import Geraetezustand
from bewaesserung.sicherheit.ereignisse import EreignisListe
from bewaesserung.sicherheit.interlock import Interlock
from bewaesserung.sicherheit.kopplung import Kopplung
from bewaesserung.sicherheit.orchestrator import Orchestrator
from bewaesserung.sicherheit.startpruefung import Startpruefung
from gardena_mock import GardenaMock

V1, S1 = "ventil-1:1", "steckdose-1:1"
K1 = Kopplung("k1", V1, S1, vorlauf_ventil_s=20, nachlauf_ventil_s=30, max_laufzeit_s=1800)


@dataclass
class Aufbau:
    kopplungen: list[Kopplung] = field(default_factory=lambda: [K1])
    timeout_s: float = 0.2

    def __post_init__(self) -> None:
        self.zustand = Geraetezustand()
        self.gardena = GardenaMock(self.zustand)
        self.ereignisse = EreignisListe()
        self.orchestrator = Orchestrator(
            self.gardena, self.zustand, self.ereignisse, lambda: self.kopplungen,
            bestaetigung_timeout_s=self.timeout_s, sicherheitsabstand_s=0.0, ende_toleranz_s=0.5)
        self.interlock = Interlock(
            self.gardena, self.zustand, self.ereignisse, lambda: self.kopplungen,
            anlauf_s=0.2, wiederholung_s=(0.3, 0.6))
        self.startpruefung = Startpruefung(
            self.gardena, self.zustand, self.ereignisse, lambda: self.kopplungen, self.orchestrator,
            verbindung_timeout_s=0.3, bestaetigung_timeout_s=self.timeout_s)
        self.gardena.ventil(V1, "Rasen")
        self.gardena.steckdose(S1, "Pumpe")

    def bereit(self, interlock: bool = True) -> None:
        """Verbunden und freigegeben, Interlock aktiv. Nur innerhalb einer laufenden Ereignisschleife."""
        self.gardena.verbinden()
        self.orchestrator.freigegeben = True
        if interlock:
            self.interlock.starten()


def ausfuehren(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


@pytest.fixture
def aufbau() -> Aufbau:
    return Aufbau()
