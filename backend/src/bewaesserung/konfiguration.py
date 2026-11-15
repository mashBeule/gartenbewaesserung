"""Konfiguration des Dienstes aus Umgebungsvariablen.

Zugangsdaten kommen aus `/etc/bewaesserung/zugang.env` (systemd `EnvironmentFile`,
nur für root lesbar), nie aus dem Image oder dem Repo. Fehlen die Gardena-Zugangsdaten,
startet der Dienst trotzdem, schaltet aber nichts und wartet. Fehlt das API-Token, ist
die REST-API aus.

Zonen, Kopplungen und Zeitpläne liegen in `<Datenordner>/bewaesserung.sqlite3`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

WAHR = {"1", "true", "ja", "yes", "on"}
MIN_TOKEN_LAENGE = 24


@dataclass(frozen=True)
class Konfiguration:
    application_key: str | None
    application_secret: str | None
    trockenlauf: bool
    standort_id: str | None
    datenordner: Path
    api_token: str | None
    api_host: str
    api_port: int

    @classmethod
    def aus_umgebung(cls, env: Mapping[str, str] = os.environ) -> "Konfiguration":
        return cls(
            application_key=env.get("GARDENA_APPLICATION_KEY") or None,
            application_secret=env.get("GARDENA_APPLICATION_SECRET") or None,
            # Standard ist Trockenlauf; echtes Schalten nur nach bewusstem GARDENA_DRY_RUN=false
            trockenlauf=env.get("GARDENA_DRY_RUN", "true").strip().lower() in WAHR,
            standort_id=env.get("GARDENA_STANDORT_ID") or None,
            datenordner=Path(env.get("BEWAESSERUNG_DATEN", "data")),
            api_token=env.get("BEWAESSERUNG_API_TOKEN") or None,
            # Nie 0.0.0.0: nur localhost oder die Tailscale-Adresse des Pi (100.x.y.z)
            api_host=env.get("BEWAESSERUNG_API_HOST", "127.0.0.1"),
            api_port=int(env.get("BEWAESSERUNG_API_PORT", "8080")),
        )

    @property
    def zugang_vollstaendig(self) -> bool:
        return bool(self.application_key and self.application_secret)

    @property
    def api_aktiv(self) -> bool:
        return bool(self.api_token) and len(self.api_token) >= MIN_TOKEN_LAENGE and self.api_host not in ("0.0.0.0", "::")

    @property
    def datenbank(self) -> Path:
        return self.datenordner / "bewaesserung.sqlite3"
