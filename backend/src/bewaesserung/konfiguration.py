"""Konfiguration des Dienstes aus Umgebungsvariablen und dem Datenordner.

Zugangsdaten kommen aus `/etc/bewaesserung/zugang.env` (systemd `EnvironmentFile`,
nur für root lesbar), nie aus dem Image oder dem Repo. Fehlen sie, startet der Dienst
trotzdem, schaltet aber nichts und wartet.

Bis es SQLite gibt, stehen die Kopplungen in `<Datenordner>/kopplungen.json`:

    [{"id": "rasen", "ventil_id": "<dienst-id>", "steckdose_id": "<dienst-id>",
      "vorlauf_ventil_s": 20, "nachlauf_ventil_s": 30, "max_laufzeit_s": 1800}]
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Mapping

from .sicherheit.kopplung import Kopplung

WAHR = {"1", "true", "ja", "yes", "on"}


@dataclass(frozen=True)
class Konfiguration:
    application_key: str | None
    application_secret: str | None
    trockenlauf: bool
    standort_id: str | None
    datenordner: Path

    @classmethod
    def aus_umgebung(cls, env: Mapping[str, str] = os.environ) -> "Konfiguration":
        return cls(
            application_key=env.get("GARDENA_APPLICATION_KEY") or None,
            application_secret=env.get("GARDENA_APPLICATION_SECRET") or None,
            # Standard ist Trockenlauf; echtes Schalten nur nach bewusstem GARDENA_DRY_RUN=false
            trockenlauf=env.get("GARDENA_DRY_RUN", "true").strip().lower() in WAHR,
            standort_id=env.get("GARDENA_STANDORT_ID") or None,
            datenordner=Path(env.get("BEWAESSERUNG_DATEN", "data")),
        )

    @property
    def zugang_vollstaendig(self) -> bool:
        return bool(self.application_key and self.application_secret)

    @property
    def kopplungen_datei(self) -> Path:
        return self.datenordner / "kopplungen.json"


class KonfigurationFehler(Exception):
    pass


def kopplungen_laden(datei: Path) -> list[Kopplung]:
    """Kopplungen aus JSON lesen. Fehlende Datei = keine Kopplungen. Fehlerhafte Datei wirft."""
    if not datei.exists():
        return []
    try:
        daten = json.loads(datei.read_text(encoding="utf-8"))
    except (OSError, ValueError) as f:
        raise KonfigurationFehler(f"{datei} nicht lesbar: {f}") from f
    if not isinstance(daten, list):
        raise KonfigurationFehler(f"{datei}: erwartet eine Liste von Kopplungen")
    namen = {f.name for f in fields(Kopplung)}
    ergebnis = []
    for i, eintrag in enumerate(daten):
        if not isinstance(eintrag, dict) or set(eintrag) != namen:
            raise KonfigurationFehler(f"{datei}, Eintrag {i}: Felder müssen genau {sorted(namen)} sein")
        ergebnis.append(Kopplung(**eintrag))
    ids = [k.id for k in ergebnis]
    if len(ids) != len(set(ids)):
        raise KonfigurationFehler(f"{datei}: Kopplungs-IDs sind nicht eindeutig")
    return ergebnis
