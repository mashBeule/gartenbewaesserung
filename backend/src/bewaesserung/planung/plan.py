"""Zeitplan-Regeln und Ausnahmen → konkrete Läufe. Reine Logik, kein Netzwerk, keine Datenbank.

Eine Regel gilt pro Zone (Ventil): Wochentage als Bitmaske (Bit 0 = Montag … Bit 6 = Sonntag),
Startzeit in Ortszeit, Dauer in Minuten, optional ein Gültigkeitszeitraum. Ausnahmen ändern
einen einzelnen Termin (aus dem Kalender per Tippen oder Ziehen):

- AUSLASSEN: Termin entfällt (bleibt im Kalender sichtbar, ausgegraut).
- VERSCHIEBEN: `wert` = "HH:MM" (gleicher Tag) oder "YYYY-MM-DDTHH:MM" (anderer Tag).
- DAUER_ANPASSEN: `wert` = Minuten.

Sommerzeit: Eine Startzeit, die es nicht gibt (Umstellung im Frühjahr), rückt um die
Lücke nach hinten; eine doppelte Startzeit (Herbst) gilt beim ersten Auftreten.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

ORTSZEIT = ZoneInfo("Europe/Berlin")
WOCHENTAGE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
ALLE_TAGE = 0b1111111


class Quelle(str, Enum):
    MANUELL = "MANUELL"
    CLAUDE = "CLAUDE"


class AusnahmeAktion(str, Enum):
    AUSLASSEN = "AUSLASSEN"
    VERSCHIEBEN = "VERSCHIEBEN"
    DAUER_ANPASSEN = "DAUER_ANPASSEN"


class PlanFehler(ValueError):
    pass


@dataclass(frozen=True)
class Regel:
    id: int
    zone_id: int
    wochentage: int
    startzeit: time
    dauer_min: int
    gueltig_von: date | None = None
    gueltig_bis: date | None = None
    aktiv: bool = True
    quelle: Quelle = Quelle.MANUELL

    def fehler(self) -> list[str]:
        f = []
        if not 0 < self.wochentage <= ALLE_TAGE:
            f.append("Mindestens ein Wochentag nötig (Bitmaske 1–127)")
        if self.dauer_min <= 0:
            f.append("Dauer muss mindestens 1 Minute sein")
        if self.gueltig_von and self.gueltig_bis and self.gueltig_von > self.gueltig_bis:
            f.append("Gültig-von liegt nach Gültig-bis")
        return f

    def gilt_am(self, tag: date) -> bool:
        return (self.aktiv
                and bool(self.wochentage >> tag.weekday() & 1)
                and (self.gueltig_von is None or tag >= self.gueltig_von)
                and (self.gueltig_bis is None or tag <= self.gueltig_bis))


@dataclass(frozen=True)
class Ausnahme:
    id: int
    regel_id: int
    datum: date                  # der ursprüngliche Termin
    aktion: AusnahmeAktion
    wert: str | None = None

    def fehler(self) -> list[str]:
        try:
            if self.aktion is AusnahmeAktion.VERSCHIEBEN:
                _ziel(self.datum, self.wert)
            elif self.aktion is AusnahmeAktion.DAUER_ANPASSEN:
                if int(self.wert or "") <= 0:
                    return ["Dauer muss mindestens 1 Minute sein"]
        except (TypeError, ValueError):
            return [f"Ungültiger Wert {self.wert!r} für {self.aktion.value}"]
        return []


@dataclass(frozen=True)
class GeplanterLauf:
    schluessel: str              # eindeutig je Regel und ursprünglichem Termin
    regel_id: int
    zone_id: int
    start: datetime              # mit Zeitzone
    dauer_s: int
    ausgelassen: bool = False
    hinweis: str = ""


def ortszeit(tag: date, uhrzeit: time, tz: ZoneInfo = ORTSZEIT) -> datetime:
    """Ortszeit mit Sommerzeit-Regeln (siehe Moduldoku)."""
    naiv = datetime.combine(tag, uhrzeit)
    lokal = naiv.replace(tzinfo=tz, fold=0)
    zurueck = lokal.astimezone(timezone.utc).astimezone(tz)
    if zurueck.replace(tzinfo=None) != naiv:      # gibt es nicht → nach hinten rücken
        return zurueck
    return lokal


def _ziel(tag: date, wert: str | None) -> tuple[date, time]:
    if not wert:
        raise ValueError("leer")
    if "T" in wert:
        dt = datetime.fromisoformat(wert)
        return dt.date(), dt.time().replace(second=0, microsecond=0)
    return tag, time.fromisoformat(wert)


def schluessel(regel_id: int, tag: date) -> str:
    return f"regel-{regel_id}-{tag.isoformat()}"


def laeufe(regeln: list[Regel], ausnahmen: list[Ausnahme], von: date, bis: date,
           tz: ZoneInfo = ORTSZEIT) -> list[GeplanterLauf]:
    """Alle Läufe, deren Start (Ortszeit) zwischen `von` und `bis` (einschließlich) liegt."""
    nach_termin = {(a.regel_id, a.datum): a for a in ausnahmen}
    ergebnis = []
    # Verschiebungen können Termine bis zu einer Woche über die Grenzen tragen
    tag = von - timedelta(days=7)
    while tag <= bis + timedelta(days=7):
        for r in regeln:
            if not r.gilt_am(tag):
                continue
            start_tag, uhrzeit, dauer_min = tag, r.startzeit, r.dauer_min
            ausgelassen, hinweis = False, ""
            a = nach_termin.get((r.id, tag))
            if a is not None and not a.fehler():
                if a.aktion is AusnahmeAktion.AUSLASSEN:
                    ausgelassen, hinweis = True, "ausgelassen (Kalender)"
                elif a.aktion is AusnahmeAktion.VERSCHIEBEN:
                    start_tag, uhrzeit = _ziel(tag, a.wert)
                    hinweis = f"verschoben von {tag.isoformat()} {r.startzeit:%H:%M}"
                elif a.aktion is AusnahmeAktion.DAUER_ANPASSEN:
                    dauer_min = int(a.wert)
                    hinweis = f"Dauer angepasst ({r.dauer_min} → {dauer_min} min)"
            if not von <= start_tag <= bis:
                continue
            ergebnis.append(GeplanterLauf(
                schluessel(r.id, tag), r.id, r.zone_id, ortszeit(start_tag, uhrzeit, tz),
                dauer_min * 60, ausgelassen, hinweis))
        tag += timedelta(days=1)
    return sorted(ergebnis, key=lambda l: (l.start, l.regel_id))
