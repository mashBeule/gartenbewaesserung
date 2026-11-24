"""Was die Einrichtung am System tut: Gardena-Zugang prüfen, Datei schreiben, Tailscale, Dienst neu starten.

Alles hinter der Schnittstelle `System`, damit die Seite mit einem Fake getestet werden kann.
Die echte Umsetzung läuft als root (nur solange der Pi nicht eingerichtet ist, siehe
deploy/systemd/bewaesserung-einrichtung.service).

⚠ Ungeprüft auf dem Pi: Ausgabeformat von `tailscale up` (Anmelde-Link) und das Verhalten,
wenn Tailscale schon angemeldet ist.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Protocol

import httpx

from ..gardena.client import AUTH_URL

TAILSCALE_LINK = re.compile(r"https://login\.tailscale\.com/\S+")
TAILSCALE_WARTEN_S = 15 * 60


class System(Protocol):
    async def gardena_pruefen(self, key: str, secret: str) -> str | None:
        """None = Zugang gültig, sonst Fehlertext für die Seite."""
        ...

    def zugang_schreiben(self, text: str) -> None: ...

    def zugang_vorhanden(self) -> bool: ...

    async def tailscale_verbinden(self, authkey: str | None, link_melden: Callable[[str], None]) -> str | None:
        """Tailscale anmelden und die IPv4-Adresse liefern; None, wenn es nicht klappt."""
        ...

    async def dienst_neustarten(self) -> None: ...


async def _befehl(*args: str, timeout: float = 30) -> tuple[int, str]:
    prozess = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        ausgabe, _ = await asyncio.wait_for(prozess.communicate(), timeout)
    except asyncio.TimeoutError:
        prozess.kill()
        return -1, "Zeitüberschreitung"
    return prozess.returncode or 0, ausgabe.decode(errors="replace")


class EchtesSystem:
    def __init__(self, zugang: Path = Path("/etc/bewaesserung/zugang.env"), dienst: str = "bewaesserung.service") -> None:
        self.zugang = zugang
        self.dienst = dienst

    async def gardena_pruefen(self, key: str, secret: str) -> str | None:
        # Nur die Husqvarna-Anmeldung, kein Aufruf der Gardena-API (deren Budget bleibt unberührt).
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                r = await http.post(AUTH_URL, data={"grant_type": "client_credentials",
                                                    "client_id": key, "client_secret": secret})
        except httpx.HTTPError:
            return "Keine Verbindung zur Husqvarna-Anmeldung. Ist der Pi im Internet?"
        if r.status_code == 200:
            return None
        if r.status_code in (400, 401, 403):
            return "Husqvarna lehnt Application Key oder Secret ab. Bitte im Entwicklerportal prüfen."
        if r.status_code == 429:
            return "Husqvarna meldet zu viele Anfragen. Bitte später erneut versuchen."
        return f"Husqvarna-Anmeldung antwortet mit Fehler {r.status_code}."

    def zugang_vorhanden(self) -> bool:
        return self.zugang.exists()

    def zugang_schreiben(self, text: str) -> None:
        self.zugang.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.zugang.parent, prefix=".zugang-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.zugang)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    async def tailscale_verbinden(self, authkey: str | None, link_melden: Callable[[str], None]) -> str | None:
        args = ["tailscale", "up", "--hostname=bewaesserung"]
        if authkey:
            args.append(f"--auth-key={authkey}")
        prozess = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

        async def lesen() -> None:
            assert prozess.stdout is not None
            async for zeile in prozess.stdout:
                treffer = TAILSCALE_LINK.search(zeile.decode(errors="replace"))
                if treffer:
                    link_melden(treffer.group(0))
        try:
            await asyncio.wait_for(asyncio.gather(lesen(), prozess.wait()), TAILSCALE_WARTEN_S)
        except asyncio.TimeoutError:
            prozess.kill()
            return None
        code, ausgabe = await _befehl("tailscale", "ip", "-4")
        ip = ausgabe.strip().splitlines()[0] if code == 0 and ausgabe.strip() else None
        return ip if ip and re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) else None

    async def dienst_neustarten(self) -> None:
        await _befehl("systemctl", "restart", self.dienst)
