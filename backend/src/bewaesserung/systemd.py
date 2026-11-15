"""Minimale sd_notify-Anbindung (READY, STATUS, WATCHDOG) ohne Zusatzpaket.

Außerhalb von systemd (kein NOTIFY_SOCKET) tut sie nichts. Der Watchdog-Takt läuft als
Aufgabe in derselben Ereignisschleife wie die Steuerung: hängt die Schleife, bleibt der
Takt aus und systemd startet den Dienst neu (WatchdogSec in bewaesserung.service).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

log = logging.getLogger(__name__)


class SystemdMelder:
    def __init__(self, env=os.environ) -> None:
        adresse = env.get("NOTIFY_SOCKET")
        if adresse and adresse.startswith("@"):
            adresse = "\0" + adresse[1:]
        self._adresse = adresse
        usec = env.get("WATCHDOG_USEC")
        self.watchdog_intervall_s = int(usec) / 2_000_000 if usec and usec.isdigit() else None

    @property
    def aktiv(self) -> bool:
        return bool(self._adresse) and hasattr(socket, "AF_UNIX")

    def senden(self, nachricht: str) -> None:
        if not self.aktiv:
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(self._adresse)
                s.sendall(nachricht.encode())
        except OSError as f:
            log.warning("sd_notify fehlgeschlagen: %s", f)

    def bereit(self, status: str) -> None:
        self.senden(f"READY=1\nSTATUS={status}")

    def status(self, status: str) -> None:
        self.senden(f"STATUS={status}")

    async def watchdog_takt(self) -> None:
        if self.watchdog_intervall_s is None:
            return
        while True:
            self.senden("WATCHDOG=1")
            await asyncio.sleep(self.watchdog_intervall_s)
