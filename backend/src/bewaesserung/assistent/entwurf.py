"""Saisonplan-Entwurf von Claude: festes JSON-Schema und Prüfung. Reine Logik, kein Netzwerk.

Ein Entwurf wird erst durch die Freigabe in der App zu Regeln. Vorher prüft `pruefen` ihn
genauso streng wie einen manuell angelegten Plan, und noch etwas strenger:

- nur bekannte Zonen, jede höchstens einmal, jede mit Kopplung
- keine Dauer über der Höchstlaufzeit der Kopplung (Sicherheitsregel 8)
- Startzeiten nur in den erlaubten Fenstern
- keine Überschneidung zweier Läufe: Läufe laufen nacheinander, ein Termin, der länger als
  15 min warten müsste, würde verpasst
- Gesamtlaufzeit pro Tag plausibel (Summe aller Läufe)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time, timedelta

from ..planung.plan import WOCHENTAGE, Quelle, Regel
from ..sicherheit.kopplung import Kopplung, LaufAbgelehnt

PHASEN = ("FRUEHJAHR", "HOCHSOMMER", "HERBST")

# Schema für structured outputs: alle Felder Pflicht, keine Zusatzfelder.
SCHEMA = {
    "type": "object",
    "properties": {
        "zusammenfassung": {"type": "string", "description": "2–4 Sätze zum Gesamtplan, Deutsch"},
        "zonen": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "zone_id": {"type": "integer"},
                    "begruendung": {"type": "string", "description": "kurze Begründung für diese Zone, Deutsch"},
                    "regeln": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "phase": {"type": "string", "enum": list(PHASEN)},
                                "wochentage": {"type": "array", "items": {"type": "string", "enum": list(WOCHENTAGE)}},
                                "startzeit": {"type": "string", "description": "HH:MM, Ortszeit"},
                                "dauer_min": {"type": "integer"},
                                "gueltig_von": {"type": "string", "description": "YYYY-MM-DD"},
                                "gueltig_bis": {"type": "string", "description": "YYYY-MM-DD"},
                            },
                            "required": ["phase", "wochentage", "startzeit", "dauer_min", "gueltig_von", "gueltig_bis"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["zone_id", "begruendung", "regeln"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["zusammenfassung", "zonen"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Grenzen:
    startfenster: tuple[tuple[time, time], ...] = ((time(4, 0), time(10, 0)), (time(18, 0), time(22, 0)))
    max_laufzeit_tag_min: int = 240          # Summe aller Läufe an einem Tag
    gleichzeitig: int = 1                    # Läufe laufen nacheinander (docs/entscheidungen.md)

    def text(self) -> str:
        fenster = ", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in self.startfenster)
        return (f"Startzeiten nur in {fenster} Uhr. Höchstens {self.gleichzeitig} Lauf gleichzeitig; "
                f"Läufe dürfen sich nicht überschneiden. Summe aller Laufzeiten pro Tag höchstens "
                f"{self.max_laufzeit_tag_min} min.")


@dataclass(frozen=True)
class EntwurfRegel:
    zone_id: int
    phase: str
    wochentage: int                          # Bitmaske wie Regel
    startzeit: time
    dauer_min: int
    gueltig_von: date
    gueltig_bis: date

    def als_regel(self, id_: int = 0) -> Regel:
        return Regel(id_, self.zone_id, self.wochentage, self.startzeit, self.dauer_min,
                     self.gueltig_von, self.gueltig_bis, True, Quelle.CLAUDE)


@dataclass
class Pruefung:
    regeln: list[EntwurfRegel] = field(default_factory=list)
    fehler: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fehler


def _maske(tage: list[str]) -> int:
    return sum(1 << WOCHENTAGE.index(t) for t in set(tage) if t in WOCHENTAGE)


def _minuten(t: time) -> int:
    return t.hour * 60 + t.minute


def pruefen(inhalt: dict, zonen: dict[int, str], kopplungen: dict[int, Kopplung | None],
            grenzen: Grenzen = Grenzen()) -> Pruefung:
    """`zonen`: id → Name; `kopplungen`: zone_id → Kopplung (None = keine)."""
    p = Pruefung()
    if not isinstance(inhalt, dict) or not isinstance(inhalt.get("zonen"), list):
        p.fehler.append("Antwort hat nicht das vorgegebene Schema")
        return p
    gesehen: set[int] = set()
    for z in inhalt["zonen"]:
        zid = z.get("zone_id")
        if zid not in zonen:
            p.fehler.append(f"Zone {zid} gibt es nicht")
            continue
        if zid in gesehen:
            p.fehler.append(f"Zone {zid} kommt doppelt vor")
            continue
        gesehen.add(zid)
        kopplung = kopplungen.get(zid)
        if kopplung is None:
            p.fehler.append(f"Zone {zid} ({zonen[zid]}) hat keine Kopplung und darf keine Regeln bekommen")
            continue
        for i, r in enumerate(z.get("regeln") or [], 1):
            wo = f"Zone {zid} ({zonen[zid]}), Regel {i}"
            try:
                startzeit = time.fromisoformat(r["startzeit"])
                von, bis = date.fromisoformat(r["gueltig_von"]), date.fromisoformat(r["gueltig_bis"])
                dauer = int(r["dauer_min"])
            except (KeyError, TypeError, ValueError) as f:
                p.fehler.append(f"{wo}: ungültiger Wert ({f})")
                continue
            regel = EntwurfRegel(zid, r.get("phase", ""), _maske(r.get("wochentage") or []), startzeit, dauer, von, bis)
            fehler = regel.als_regel().fehler()
            if r.get("phase") not in PHASEN:
                fehler.append(f"unbekannte Phase {r.get('phase')!r}")
            try:
                kopplung.pruefen(dauer * 60)                 # Regel 8
            except LaufAbgelehnt as f:
                fehler.append(str(f))
            if not any(a <= startzeit and _minuten(startzeit) + dauer <= _minuten(b) for a, b in grenzen.startfenster):
                fehler.append(f"Lauf {startzeit:%H:%M} + {dauer} min liegt nicht in einem erlaubten Zeitfenster")
            if (bis - von).days > 366:
                fehler.append("Gültigkeit länger als ein Jahr")
            p.fehler.extend(f"{wo}: {t}" for t in fehler)
            if not fehler:
                p.regeln.append(regel)
    p.fehler.extend(ueberschneidungen(p.regeln, zonen))
    p.fehler.extend(tagessummen(p.regeln, grenzen))
    return p


def _gemeinsame_tage(a: EntwurfRegel, b: EntwurfRegel) -> bool:
    return bool(a.wochentage & b.wochentage) and a.gueltig_von <= b.gueltig_bis and b.gueltig_von <= a.gueltig_bis


def ueberschneidungen(regeln: list[EntwurfRegel], zonen: dict[int, str]) -> list[str]:
    fehler = []
    for i, a in enumerate(regeln):
        for b in regeln[i + 1:]:
            if not _gemeinsame_tage(a, b):
                continue
            a0, b0 = _minuten(a.startzeit), _minuten(b.startzeit)
            if a0 < b0 + b.dauer_min and b0 < a0 + a.dauer_min:
                fehler.append(f"Überschneidung: {zonen.get(a.zone_id)} {a.startzeit:%H:%M} ({a.dauer_min} min) "
                              f"und {zonen.get(b.zone_id)} {b.startzeit:%H:%M} ({b.dauer_min} min)")
    return fehler


def tagessummen(regeln: list[EntwurfRegel], grenzen: Grenzen) -> list[str]:
    if not regeln:
        return []
    tag, ende = min(r.gueltig_von for r in regeln), max(r.gueltig_bis for r in regeln)
    while tag <= ende:
        summe = sum(r.dauer_min for r in regeln
                    if r.gueltig_von <= tag <= r.gueltig_bis and r.wochentage >> tag.weekday() & 1)
        if summe > grenzen.max_laufzeit_tag_min:
            return [f"Am {tag:%d.%m.%Y} zusammen {summe} min Laufzeit, erlaubt sind {grenzen.max_laufzeit_tag_min} min"]
        tag += timedelta(days=1)
    return []

