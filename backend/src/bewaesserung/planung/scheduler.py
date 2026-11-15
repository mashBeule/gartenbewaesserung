"""Scheduler: gibt fällige Läufe nacheinander an den Orchestrator. Schaltet nie selbst.

Statt APScheduler eine einfache Takt-Schleife (alle 30 s): Sie liest Regeln und Ausnahmen
jedes Mal frisch aus SQLite, damit Änderungen aus der App sofort gelten, und ist mit einer
übergebenen Uhr vollständig testbar.

- Fällig ist ein Termin, sobald seine Startzeit erreicht ist. Jeder Termin wird genau einmal
  behandelt (run_log mit eindeutigem `plan_schluessel`), auch über Neustarts hinweg.
- Läufe laufen nacheinander (ob mehrere Ventile gleichzeitig dürfen, ist noch offen).
- Ein Termin, der länger als `nachholfenster` nicht starten konnte (Pi aus, Startprüfung
  nicht bestanden, vorheriger Lauf), wird als VERPASST protokolliert und nicht nachgeholt.
- Ein Wetter-Baustein kann die Dauer skalieren oder den Lauf aussetzen (Schnittstelle `Wetter`,
  umgesetzt in `wetter/dienst.py`). Hochskaliert wird höchstens bis zur Höchstlaufzeit (Regel 8);
  wirft die Wetterentscheidung einen Fehler, läuft der Termin unverändert.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from ..sicherheit.kopplung import SEKUNDEN_SCHRITT, LaufAbgelehnt, abrunden
from ..sicherheit.orchestrator import Orchestrator
from ..speicher.datenbank import Speicher
from .plan import ORTSZEIT, GeplanterLauf, laeufe

log = logging.getLogger(__name__)

TAKT_S = 30.0
NACHHOLFENSTER = timedelta(minutes=15)


class Wetter(Protocol):
    def faktor(self, zone_id: int, start: datetime) -> tuple[float, str]:
        """Faktor für die Dauer (0 = aussetzen) und Begründung fürs run_log."""
        ...


class LaufNichtMoeglich(Exception):
    pass


class Scheduler:
    def __init__(
        self,
        speicher: Speicher,
        orchestrator: Orchestrator,
        tz: ZoneInfo = ORTSZEIT,
        uhr: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        schlafen: Callable[[float], Awaitable[None]] = asyncio.sleep,
        takt_s: float = TAKT_S,
        nachholfenster: timedelta = NACHHOLFENSTER,
        wetter: Wetter | None = None,
    ) -> None:
        self.speicher = speicher
        self.orchestrator = orchestrator
        self.tz = tz
        self._uhr = uhr
        self._schlafen = schlafen
        self.takt_s = takt_s
        self.nachholfenster = nachholfenster
        self.wetter = wetter
        self.warteschlange: list[GeplanterLauf] = []
        self._aktuell: asyncio.Task | None = None

    # --- Takt ------------------------------------------------------------

    async def laufen(self) -> None:
        while True:
            try:
                await self.takt()
            except Exception:
                log.exception("Scheduler: Takt fehlgeschlagen")
            await self._schlafen(self.takt_s)

    def jetzt(self) -> datetime:
        return self._uhr()

    def plan(self, von, bis) -> list[GeplanterLauf]:
        return laeufe(self.speicher.regeln(), self.speicher.ausnahmen(), von, bis, self.tz)

    def naechste(self, anzahl: int = 5) -> list[GeplanterLauf]:
        jetzt = self._uhr()
        heute = jetzt.astimezone(self.tz).date()
        return [l for l in self.plan(heute, heute + timedelta(days=14))
                if l.start > jetzt and not l.ausgelassen][:anzahl]

    async def takt(self) -> None:
        jetzt = self._uhr()
        heute = jetzt.astimezone(self.tz).date()
        in_schlange = {l.schluessel for l in self.warteschlange}
        # Nur Termine im Nachholfenster ansehen. Ältere (Pi war aus, Regel erst danach angelegt)
        # werden nicht nachgeholt und auch nicht protokolliert: das wäre bei neuen Regeln irreführend.
        frueheste = jetzt - self.nachholfenster
        for l in self.plan(heute - timedelta(days=1), heute):
            if not frueheste <= l.start <= jetzt:
                continue
            if l.schluessel in in_schlange or self.speicher.termin_erledigt(l.schluessel):
                continue
            if l.ausgelassen:
                self._abschliessen(l, "AUSGELASSEN", l.hinweis)
            else:
                self.warteschlange.append(l)
        self._naechsten_starten()

    def _abschliessen(self, l: GeplanterLauf, status: str, text: str) -> None:
        self.speicher.protokollieren("PLAN", zone_id=l.zone_id, plan_schluessel=l.schluessel,
                                     geplant_s=l.dauer_s, tatsaechlich_s=0, status=status, text=text)
        log.info("Termin %s: %s (%s)", l.schluessel, status, text)

    @property
    def beschaeftigt(self) -> bool:
        try:
            ich = asyncio.current_task()
        except RuntimeError:
            ich = None
        laufend = self._aktuell is not None and not self._aktuell.done() and self._aktuell is not ich
        return laufend or bool(self.orchestrator.aktive_laeufe)

    def _naechsten_starten(self) -> None:
        if self.beschaeftigt:
            return
        jetzt = self._uhr()
        while self.warteschlange:
            l = self.warteschlange[0]
            if jetzt - l.start > self.nachholfenster:
                self.warteschlange.pop(0)
                self._abschliessen(l, "VERPASST", "konnte nicht rechtzeitig starten")
                continue
            if not self.orchestrator.freigegeben:
                return
            self.warteschlange.pop(0)
            self._aktuell = asyncio.get_running_loop().create_task(self._geplant(l))
            return

    # --- Ausführen -------------------------------------------------------

    async def _geplant(self, l: GeplanterLauf) -> None:
        try:
            dauer_s, text = l.dauer_s, l.hinweis
            if self.wetter is not None:
                try:
                    faktor, begruendung = self.wetter.faktor(l.zone_id, l.start)
                except Exception as f:                # Wetter darf einen Lauf nie verhindern
                    log.exception("Wetterentscheidung fehlgeschlagen")
                    faktor, begruendung = 1.0, f"Wetter nicht auswertbar ({f}), Dauer unverändert"
                text = "; ".join(t for t in (text, begruendung) if t)
                if faktor <= 0:
                    self._abschliessen(l, "UEBERSPRUNGEN_WETTER", text)
                    return
                dauer_s = max(SEKUNDEN_SCHRITT, abrunden(dauer_s * faktor))
                try:
                    hoechst = self._kopplung(l.zone_id).max_laufzeit_s
                except LaufNichtMoeglich:
                    hoechst = None                    # meldet _ausfuehren
                if hoechst is not None and dauer_s > hoechst:
                    dauer_s = abrunden(hoechst)       # Skalierung nie über Regel 8 hinaus
                    text += "; auf Höchstlaufzeit begrenzt"
            await self._ausfuehren(l.zone_id, dauer_s, "PLAN", l.schluessel, text)
        finally:
            self._naechsten_starten()

    async def _ausfuehren(self, zone_id: int, dauer_s: int, grund: str, schluessel: str | None, text: str) -> None:
        eintrag = self.speicher.protokollieren(grund, zone_id=zone_id, plan_schluessel=schluessel,
                                               geplant_s=dauer_s, status="LAEUFT", text=text or None)
        if eintrag is None:
            return                                   # Termin schon behandelt
        try:
            kopplung = self._kopplung(zone_id)
            ergebnis = await self.orchestrator.lauf(kopplung.id, dauer_s)
        except (LaufAbgelehnt, LaufNichtMoeglich) as f:
            self.speicher.protokoll_aendern(eintrag, status="ABGELEHNT", tatsaechlich_s=0, text=str(f))
            log.warning("Lauf Zone %s abgelehnt: %s", zone_id, f)
            return
        except asyncio.CancelledError:
            self.speicher.protokoll_aendern(eintrag, status="ABGEBROCHEN", text="Dienst beendet")
            raise
        except Exception as f:
            log.exception("Lauf Zone %s fehlgeschlagen", zone_id)
            self.speicher.protokoll_aendern(eintrag, status="FEHLGESCHLAGEN", text=str(f))
            return
        self.speicher.protokoll_aendern(
            eintrag, status=ergebnis.status.value, tatsaechlich_s=round(ergebnis.tatsaechlich_s, 1),
            text="; ".join(t for t in (text, ergebnis.grund) if t) or None)

    def _kopplung(self, zone_id: int):
        zone = self.speicher.zone(zone_id)
        if zone is None:
            raise LaufNichtMoeglich(f"Zone {zone_id} gibt es nicht")
        kopplung = self.speicher.kopplung_fuer_ventil(zone.ventil_id)
        if kopplung is None:
            raise LaufNichtMoeglich(f"Zone {zone.name} hat keine Kopplung mit einer Steckdose")
        return kopplung

    # --- Von der App -----------------------------------------------------

    def manuell(self, zone_id: int, dauer_s: int) -> asyncio.Task:
        """Manuellen Lauf starten. Wirft LaufNichtMoeglich, wenn gerade etwas läuft."""
        if self.beschaeftigt:
            raise LaufNichtMoeglich("Es läuft schon ein Lauf; bitte warten oder Not-Aus")
        if not self.orchestrator.freigegeben:
            raise LaufNichtMoeglich("Läufe sind gesperrt (Startprüfung nicht bestanden)")
        kopplung = self._kopplung(zone_id)
        kopplung.pruefen(dauer_s)                    # Regel 8, wirft LaufAbgelehnt
        self._aktuell = asyncio.get_running_loop().create_task(
            self._ausfuehren(zone_id, dauer_s, "MANUELL", None, ""))
        return self._aktuell

    async def not_aus(self) -> None:
        for l in self.warteschlange:
            self._abschliessen(l, "VERPASST", "Not-Aus")
        self.warteschlange.clear()
        await self.orchestrator.not_aus()
