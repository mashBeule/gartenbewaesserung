"""Interlock-Wächter (Sicherheitsregeln 5 und 7): prüft jede Zustandsänderung.

- Steckdose an und keines ihrer gekoppelten Ventile meldet manuelle Bewässerung
  → Steckdose sofort stoppen, melden. Gilt auch, wenn das Ventil von außen geschlossen wurde.
- Ein Ventil meldet `SCHEDULED_WATERING` oder eine Steckdose `SCHEDULED_ON`
  (Gardena-Zeitplan läuft am Pi vorbei) → gekoppelte Steckdosen zuerst, dann das Gerät stoppen.
- WebSocket abgerissen (nicht unsere geplante Erneuerung) → gekoppelte Steckdosen,
  die zuletzt „an“ gemeldet haben, vorsorglich stoppen.

Nach einem Verbindungsaufbau kommt der vollständige Zustand in mehreren Nachrichten.
Fehlt ein gekoppeltes Ventil noch, wartet der Wächter höchstens `anlauf_s`, bevor er
die Steckdose stoppt. Ist das Ventil bekannt und zu, stoppt er sofort.

Stoppbefehle an dasselbe Gerät werden mit wachsendem Abstand wiederholt, solange es
weiter „an“ meldet, damit das API-Budget nicht leerläuft.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from ..gardena.client import Befehle
from ..gardena.modelle import Geraet, GeraeteTyp
from ..gardena.zustand import Geraetezustand
from .ereignisse import Ereignisart, Ereignisse, Sicherheitsereignis
from .kopplung import Kopplung

log = logging.getLogger(__name__)

ANLAUF_S = 10.0
WIEDERHOLUNG_S = (30.0, 60.0, 120.0, 300.0, 900.0, 1800.0)


class Interlock:
    def __init__(
        self,
        befehle: Befehle,
        zustand: Geraetezustand,
        ereignisse: Ereignisse,
        kopplungen: Callable[[], list[Kopplung]],
        anlauf_s: float = ANLAUF_S,
        wiederholung_s: tuple[float, ...] = WIEDERHOLUNG_S,
        uhr: Callable[[], float] = time.monotonic,
    ) -> None:
        self.befehle = befehle
        self.zustand = zustand
        self.ereignisse = ereignisse
        self._kopplungen = kopplungen
        self.anlauf_s = anlauf_s
        self.wiederholung_s = wiederholung_s
        self._uhr = uhr
        self._gestoppt: dict[str, tuple[float, int]] = {}   # Gerät -> (letzter Stopp, Anzahl)
        self._abriss_behandelt = False
        self._aufgabe: asyncio.Task | None = None
        self._erneut = False
        self._zeitgeber: asyncio.TimerHandle | None = None

    # --- Anbindung -------------------------------------------------------

    def starten(self) -> None:
        """Am Gerätezustand anmelden. Danach wird jede Änderung geprüft."""
        self.zustand.beobachten(self.ausloesen)
        self.ausloesen()

    def ausloesen(self) -> None:
        self._erneut = True
        if self._aufgabe is None or self._aufgabe.done():
            try:
                self._aufgabe = asyncio.get_running_loop().create_task(self._schleife())
            except RuntimeError:
                log.error("Interlock: keine laufende Ereignisschleife")

    async def abwarten(self) -> None:
        """Für Tests: warten, bis alle ausgelösten Prüfungen erledigt sind."""
        await asyncio.sleep(0)
        while self._aufgabe is not None and not self._aufgabe.done():
            await asyncio.shield(self._aufgabe)

    def anhalten(self) -> None:
        if self._zeitgeber:
            self._zeitgeber.cancel()
        if self._aufgabe:
            self._aufgabe.cancel()

    async def _schleife(self) -> None:
        while self._erneut:
            self._erneut = False
            try:
                naechste = await self.pruefen()
            except Exception:
                log.exception("Interlock: Prüfung fehlgeschlagen")
                naechste = 5.0
            if naechste is not None:
                if self._zeitgeber:
                    self._zeitgeber.cancel()
                self._zeitgeber = asyncio.get_running_loop().call_later(naechste, self.ausloesen)

    # --- Prüfung ---------------------------------------------------------

    async def pruefen(self) -> float | None:
        """Einmal alle Regeln prüfen und nötige Stopps senden. Gibt zurück, wann erneut geprüft werden soll."""
        z = self.zustand
        kopplungen = list(self._kopplungen())
        steckdosen_ids = sorted({k.steckdose_id for k in kopplungen})
        if not z.verbunden:
            return await self._abriss(steckdosen_ids)
        self._abriss_behandelt = False

        jetzt = self._uhr()
        im_anlauf = z.verbunden_seit is not None and jetzt - z.verbunden_seit < self.anlauf_s
        naechste: list[float] = []
        steckdosen: dict[str, tuple[Ereignisart, str]] = {}
        ventile: dict[str, tuple[Ereignisart, str]] = {}
        geraete = z.geraete()

        # Regel 7: Zeitplan-Läufe aller Ventile und Steckdosen
        for g in geraete:
            if not g.zeitplan_lauf:
                continue
            text = (f"{g.name} meldet {g.aktivitaet}: ein Gardena-Zeitplan läuft am Pi vorbei. "
                    "Zeitpläne in der Gardena-App löschen.")
            if g.typ is GeraeteTyp.STECKDOSE:
                steckdosen[g.id] = (Ereignisart.ZEITPLAN_LAUF, text)
            elif g.typ is GeraeteTyp.VENTIL:
                ventile[g.id] = (Ereignisart.ZEITPLAN_LAUF, text)
                for k in kopplungen:
                    s = z.geraet(k.steckdose_id)
                    if k.ventil_id == g.id and (s is None or s.steckdose_an):
                        steckdosen.setdefault(k.steckdose_id, (Ereignisart.ZEITPLAN_LAUF, text))

        # Regel 5: Steckdose an ohne offenes gekoppeltes Ventil
        for sid in steckdosen_ids:
            s = z.geraet(sid)
            if s is None or sid in steckdosen:
                continue
            if not s.steckdose_an:
                continue
            gekoppelte = [z.geraet(k.ventil_id) for k in kopplungen if k.steckdose_id == sid]
            if any(v is not None and v.aktivitaet == "MANUAL_WATERING" for v in gekoppelte):
                continue
            if im_anlauf and any(v is None for v in gekoppelte):
                naechste.append(z.verbunden_seit + self.anlauf_s - jetzt)
                continue
            steckdosen[sid] = (Ereignisart.INTERLOCK,
                               f"{s.name} ist an ({s.aktivitaet}), aber kein gekoppeltes Ventil ist offen")

        # Wiederholungszähler zurücksetzen, sobald ein Gerät aus meldet
        for g in geraete:
            if g.id not in steckdosen and g.id not in ventile and not (g.steckdose_an or g.ventil_offen):
                self._gestoppt.pop(g.id, None)

        # Steckdosen zuerst, dann Ventile
        for gid, (art, text) in steckdosen.items():
            naechste.append(await self._stopp(gid, GeraeteTyp.STECKDOSE, art, text))
        for gid, (art, text) in ventile.items():
            naechste.append(await self._stopp(gid, GeraeteTyp.VENTIL, art, text))
        return min(naechste) if naechste else None

    async def _abriss(self, steckdosen_ids: list[str]) -> float | None:
        if self.zustand.trennung_geplant or self._abriss_behandelt:
            return None
        self._abriss_behandelt = True
        for sid in steckdosen_ids:
            s: Geraet | None = self.zustand.geraet(sid)     # letzter bekannter Stand
            if s is not None and s.steckdose_an:
                await self._stopp(sid, GeraeteTyp.STECKDOSE, Ereignisart.VERBINDUNG_ABGERISSEN,
                                  f"Verbindung zu Gardena abgerissen, {s.name} vorsorglich gestoppt")
        return None

    async def _stopp(self, gid: str, typ: GeraeteTyp, art: Ereignisart, text: str) -> float:
        """Stopp senden, falls fällig. Gibt die Sekunden bis zur nächsten Wiederholung zurück."""
        jetzt = self._uhr()
        letzter, anzahl = self._gestoppt.get(gid, (None, 0))
        if letzter is not None:
            faellig = letzter + self.wiederholung_s[min(anzahl - 1, len(self.wiederholung_s) - 1)]
            if jetzt < faellig:
                return faellig - jetzt
        self._gestoppt[gid] = (jetzt, anzahl + 1)
        if anzahl:
            text = f"{text} (Stopp wird wiederholt, Versuch {anzahl + 1})"
        self.ereignisse.melden(Sicherheitsereignis(art, text, gid))
        try:
            await self.befehle.stoppen(gid, typ)
        except Exception as f:
            self.ereignisse.melden(Sicherheitsereignis(
                Ereignisart.BEFEHL_FEHLGESCHLAGEN, f"Stopp an {gid} fehlgeschlagen: {f}", gid))
        return self.wiederholung_s[min(anzahl, len(self.wiederholung_s) - 1)]
