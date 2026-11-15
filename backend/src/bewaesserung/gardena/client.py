"""Gardena-REST-Client: Token, Standorte/Geräte, WebSocket-Adresse, Befehle.

Endpunkte siehe docs/gardena-api.md. Jeder Aufruf geht durch das `ApiBudget`.
Standard ist Trockenlauf: Befehle werden nur protokolliert, nicht gesendet.

Sicherheitsregel 1: Es gibt nur einen Startbefehl, `START_SECONDS_TO_OVERRIDE` mit
Sekundenwert. Unbegrenzte Befehle (`START_OVERRIDE`) bietet der Client nicht an.
Die Antwort auf einen Befehl sagt nichts über den Gerätezustand (Regel 4);
deshalb geben die Befehlsmethoden nichts zurück.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .budget import ApiBudget, BudgetErschoepft, Prioritaet
from .modelle import DIENST_ZUORDNUNG, GeraeteTyp

log = logging.getLogger(__name__)

AUTH_URL = "https://api.authentication.husqvarnagroup.dev/v1/oauth2/token"
API_URL = "https://api.smart.gardena.dev/v2"

BEFEHLS_TYP = {typ: befehl for typ, befehl in DIENST_ZUORDNUNG.values() if befehl}
LAENGSTES_WARTEN_KURZFENSTER_S = 11.0


class GardenaGesperrt(Exception):
    """HTTP 429 – 24 h nichts mehr senden."""


class GardenaFehler(Exception):
    """Anderer HTTP-Fehler oder Netzwerkfehler."""


class Befehle(Protocol):
    """Was Orchestrator, Interlock und Startprüfung vom Client brauchen. Tests nutzen den Mock."""

    async def starten(self, dienst_id: str, typ: GeraeteTyp, sekunden: int, p: Prioritaet = Prioritaet.NORMAL) -> None: ...
    async def stoppen(self, dienst_id: str, typ: GeraeteTyp, p: Prioritaet = Prioritaet.SICHERHEIT) -> None: ...
    async def zeitplan_pausieren(self, dienst_id: str, typ: GeraeteTyp) -> None: ...


def _befehls_typ(typ: GeraeteTyp) -> str:
    try:
        return BEFEHLS_TYP[typ]
    except KeyError:
        raise ValueError(f"{typ.value} kann keine Befehle empfangen") from None


class GardenaClient:
    def __init__(
        self,
        application_key: str,
        application_secret: str,
        budget: ApiBudget,
        dry_run: bool = True,
        http=None,                                   # httpx.AsyncClient, Tests übergeben einen mit MockTransport
        uhr: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        schlafen=asyncio.sleep,
    ) -> None:
        self.application_key = application_key
        self.application_secret = application_secret
        self.budget = budget
        self.dry_run = dry_run
        self._http = http
        self._uhr = uhr
        self._schlafen = schlafen
        self._token: str | None = None
        self._token_ablauf: datetime | None = None

    # --- Grundlagen -------------------------------------------------------

    def _client(self):
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=20)
        return self._http

    async def _budget(self, p: Prioritaet) -> None:
        """Budget anfordern. Sicherheits-Stopps warten das 10-Sekunden-Fenster einmal ab."""
        try:
            self.budget.anfordern(p, self._uhr())
        except BudgetErschoepft as f:
            if p is not Prioritaet.SICHERHEIT or f.wieder_frei is None or self.budget.gesperrt_bis:
                raise
            warten = (f.wieder_frei - self._uhr()).total_seconds()
            if warten > LAENGSTES_WARTEN_KURZFENSTER_S:
                raise
            await self._schlafen(max(warten, 0))
            self.budget.anfordern(p, self._uhr())

    def _pruefe(self, antwort) -> None:
        if antwort.status_code == 429:
            self.budget.gesperrt_melden(self._uhr())
            log.error("Gardena meldet 429 – 24 h keine Aufrufe")
            raise GardenaGesperrt("Gardena meldet 429 – 24 h Sperre")
        if antwort.status_code >= 400:
            raise GardenaFehler(f"HTTP {antwort.status_code}: {antwort.text[:200]}")

    async def _token_holen(self, p: Prioritaet) -> str:
        jetzt = self._uhr()
        if self._token and self._token_ablauf and jetzt < self._token_ablauf:
            return self._token
        await self._budget(Prioritaet.SICHERHEIT if p is Prioritaet.SICHERHEIT else Prioritaet.SYSTEM)
        r = await self._client().post(AUTH_URL, data={
            "grant_type": "client_credentials",
            "client_id": self.application_key,
            "client_secret": self.application_secret,
        })
        self._pruefe(r)
        daten = r.json()
        self._token = daten["access_token"]
        self._token_ablauf = jetzt + timedelta(seconds=int(daten.get("expires_in", 3600)) - 60)
        return self._token

    async def _aufruf(self, methode: str, pfad: str, p: Prioritaet, json: dict | None = None) -> dict:
        for versuch in (1, 2):
            token = await self._token_holen(p)
            await self._budget(p)
            r = await self._client().request(methode, f"{API_URL}{pfad}", json=json, headers={
                "Authorization": f"Bearer {token}",
                "X-Api-Key": self.application_key,
                "Content-Type": "application/vnd.api+json",
            })
            if r.status_code == 401 and versuch == 1:
                log.warning("Gardena: Token abgelehnt, hole ein neues")
                self._token = None
                continue
            self._pruefe(r)
            return r.json() if r.content else {}
        raise AssertionError("unerreichbar")

    # --- Lesen -----------------------------------------------------------

    async def standorte(self) -> list[dict]:
        return (await self._aufruf("GET", "/locations", Prioritaet.SYSTEM)).get("data", [])

    async def standort(self, standort_id: str) -> dict:
        """JSON:API-Dokument mit allen Geräten und Diensten in `included`.

        Für den Gerätezustand `Geraetezustand.anwenden(*dokument["included"])` benutzen.
        """
        return await self._aufruf("GET", f"/locations/{standort_id}", Prioritaet.SYSTEM)

    async def websocket_adresse(self, standort_id: str) -> str:
        antwort = await self._aufruf("POST", "/websocket", Prioritaet.SYSTEM, json={"data": {
            "type": "WEBSOCKET",
            "id": str(uuid.uuid4()),
            "attributes": {"locationId": standort_id},
        }})
        return antwort["data"]["attributes"]["url"]

    # --- Befehle ---------------------------------------------------------

    async def _befehl(self, dienst_id: str, typ: GeraeteTyp, attribute: dict, p: Prioritaet) -> None:
        nutzlast = {"data": {"id": str(uuid.uuid4()), "type": _befehls_typ(typ), "attributes": attribute}}
        if self.dry_run:
            log.info("TROCKENLAUF %s %s %s", dienst_id, nutzlast["data"]["type"], attribute)
            return
        log.info("Befehl %s an %s", attribute, dienst_id)
        await self._aufruf("PUT", f"/command/{dienst_id}", p, json=nutzlast)

    async def starten(self, dienst_id: str, typ: GeraeteTyp, sekunden: int, p: Prioritaet = Prioritaet.NORMAL) -> None:
        if not isinstance(sekunden, int) or sekunden <= 0:
            raise ValueError(f"Laufzeit muss eine positive ganze Zahl sein, nicht {sekunden!r}")
        await self._befehl(dienst_id, typ, {"command": "START_SECONDS_TO_OVERRIDE", "seconds": sekunden}, p)

    async def stoppen(self, dienst_id: str, typ: GeraeteTyp, p: Prioritaet = Prioritaet.SICHERHEIT) -> None:
        await self._befehl(dienst_id, typ, {"command": "STOP_UNTIL_NEXT_TASK"}, p)

    async def zeitplan_pausieren(self, dienst_id: str, typ: GeraeteTyp) -> None:
        """Gardena-Zeitplan unbegrenzt pausieren. ⚠ Wirkung in Phase 0 prüfen (docs/gardena-api.md)."""
        await self._befehl(dienst_id, typ, {"command": "PAUSE"}, Prioritaet.SYSTEM)

    async def zeitplan_fortsetzen(self, dienst_id: str, typ: GeraeteTyp) -> None:
        await self._befehl(dienst_id, typ, {"command": "UNPAUSE"}, Prioritaet.NORMAL)

    async def schliessen(self) -> None:
        if self._http is not None:
            await self._http.aclose()
