"""SQLite-Speicher. Synchron und klein; läuft im Thread der Ereignisschleife.

Tabellen laut Plan: zone, coupling, schedule_rule, schedule_exception, run_log.
(`device` ist der Gerätezustand aus dem WebSocket und liegt nur im Speicher;
`weather_policy` kommt mit der Wetter-Anbindung.) Das Schema wird über
`PRAGMA user_version` versioniert, damit spätere Änderungen als Migration laufen.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

from ..planung.plan import Ausnahme, AusnahmeAktion, Quelle, Regel
from ..sicherheit.kopplung import Kopplung

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE zone (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    ventil_id TEXT NOT NULL UNIQUE,
    pflanzentyp TEXT, boden TEXT, sonnenlage TEXT, durchfluss_l_min REAL
);
CREATE TABLE coupling (
    id INTEGER PRIMARY KEY,
    ventil_id TEXT NOT NULL UNIQUE,
    steckdose_id TEXT NOT NULL,
    vorlauf_ventil_s INTEGER NOT NULL,
    nachlauf_ventil_s INTEGER NOT NULL,
    max_laufzeit_s INTEGER NOT NULL
);
CREATE TABLE schedule_rule (
    id INTEGER PRIMARY KEY,
    zone_id INTEGER NOT NULL REFERENCES zone(id) ON DELETE CASCADE,
    wochentage INTEGER NOT NULL,
    startzeit TEXT NOT NULL,
    dauer_min INTEGER NOT NULL,
    gueltig_von TEXT, gueltig_bis TEXT,
    aktiv INTEGER NOT NULL DEFAULT 1,
    quelle TEXT NOT NULL DEFAULT 'MANUELL'
);
CREATE TABLE schedule_exception (
    id INTEGER PRIMARY KEY,
    rule_id INTEGER NOT NULL REFERENCES schedule_rule(id) ON DELETE CASCADE,
    datum TEXT NOT NULL,
    aktion TEXT NOT NULL,
    wert TEXT,
    UNIQUE (rule_id, datum)
);
CREATE TABLE run_log (
    id INTEGER PRIMARY KEY,
    zeitpunkt TEXT NOT NULL,
    zone_id INTEGER,
    plan_schluessel TEXT UNIQUE,
    geplant_s INTEGER,
    tatsaechlich_s REAL,
    grund TEXT NOT NULL,
    status TEXT,
    text TEXT,
    sicherheitsereignis TEXT
);
CREATE INDEX run_log_zeit ON run_log (zeitpunkt);
"""


@dataclass(frozen=True)
class Zone:
    id: int
    name: str
    ventil_id: str
    pflanzentyp: str | None = None
    boden: str | None = None
    sonnenlage: str | None = None
    durchfluss_l_min: float | None = None


@dataclass(frozen=True)
class Protokolleintrag:
    id: int
    zeitpunkt: datetime
    zone_id: int | None
    plan_schluessel: str | None
    geplant_s: int | None
    tatsaechlich_s: float | None
    grund: str
    status: str | None
    text: str | None
    sicherheitsereignis: str | None


class Speicher:
    def __init__(self, pfad: Path | str = ":memory:") -> None:
        if pfad != ":memory:":
            Path(pfad).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(pfad), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self._migrieren()

    def _migrieren(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            with self.db:
                self.db.executescript("BEGIN;" + SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
        elif version > SCHEMA_VERSION:
            raise RuntimeError(f"Datenbank hat Schema {version}, Programm kennt nur {SCHEMA_VERSION}")

    def schliessen(self) -> None:
        self.db.close()

    def _einfuegen(self, tabelle: str, werte: dict) -> int:
        spalten = ", ".join(werte)
        platzhalter = ", ".join("?" for _ in werte)
        return self.db.execute(f"INSERT INTO {tabelle} ({spalten}) VALUES ({platzhalter})",
                               tuple(werte.values())).lastrowid

    def _aendern(self, tabelle: str, id_: int, werte: dict) -> bool:
        zuweisung = ", ".join(f"{s} = ?" for s in werte)
        return self.db.execute(f"UPDATE {tabelle} SET {zuweisung} WHERE id = ?",
                               (*werte.values(), id_)).rowcount == 1

    def _loeschen(self, tabelle: str, id_: int) -> bool:
        return self.db.execute(f"DELETE FROM {tabelle} WHERE id = ?", (id_,)).rowcount == 1

    # --- Zonen -----------------------------------------------------------

    def zonen(self) -> list[Zone]:
        return [Zone(**dict(r)) for r in self.db.execute("SELECT * FROM zone ORDER BY id")]

    def zone(self, id_: int) -> Zone | None:
        r = self.db.execute("SELECT * FROM zone WHERE id = ?", (id_,)).fetchone()
        return Zone(**dict(r)) if r else None

    def zone_anlegen(self, **werte) -> Zone:
        return self.zone(self._einfuegen("zone", werte))

    def zone_aendern(self, id_: int, **werte) -> Zone | None:
        return self.zone(id_) if self._aendern("zone", id_, werte) else None

    def zone_loeschen(self, id_: int) -> bool:
        return self._loeschen("zone", id_)

    # --- Kopplungen ------------------------------------------------------

    @staticmethod
    def _kopplung(r: sqlite3.Row) -> Kopplung:
        d = dict(r)
        d["id"] = str(d["id"])
        return Kopplung(**d)

    def kopplungen(self) -> list[Kopplung]:
        return [self._kopplung(r) for r in self.db.execute("SELECT * FROM coupling ORDER BY id")]

    def kopplung(self, id_: int) -> Kopplung | None:
        r = self.db.execute("SELECT * FROM coupling WHERE id = ?", (id_,)).fetchone()
        return self._kopplung(r) if r else None

    def kopplung_fuer_ventil(self, ventil_id: str) -> Kopplung | None:
        r = self.db.execute("SELECT * FROM coupling WHERE ventil_id = ?", (ventil_id,)).fetchone()
        return self._kopplung(r) if r else None

    def kopplung_anlegen(self, **werte) -> Kopplung:
        return self.kopplung(self._einfuegen("coupling", werte))

    def kopplung_aendern(self, id_: int, **werte) -> Kopplung | None:
        return self.kopplung(id_) if self._aendern("coupling", id_, werte) else None

    def kopplung_loeschen(self, id_: int) -> bool:
        return self._loeschen("coupling", id_)

    # --- Regeln ----------------------------------------------------------

    @staticmethod
    def _regel(r: sqlite3.Row) -> Regel:
        return Regel(
            id=r["id"], zone_id=r["zone_id"], wochentage=r["wochentage"],
            startzeit=time.fromisoformat(r["startzeit"]), dauer_min=r["dauer_min"],
            gueltig_von=date.fromisoformat(r["gueltig_von"]) if r["gueltig_von"] else None,
            gueltig_bis=date.fromisoformat(r["gueltig_bis"]) if r["gueltig_bis"] else None,
            aktiv=bool(r["aktiv"]), quelle=Quelle(r["quelle"]))

    @staticmethod
    def _regel_werte(werte: dict) -> dict:
        d = dict(werte)
        for feld in ("startzeit", "gueltig_von", "gueltig_bis"):
            if d.get(feld) is not None and not isinstance(d[feld], str):
                d[feld] = d[feld].isoformat(timespec="minutes") if feld == "startzeit" else d[feld].isoformat()
        if "quelle" in d:
            d["quelle"] = Quelle(d["quelle"]).value
        if "aktiv" in d:
            d["aktiv"] = int(bool(d["aktiv"]))
        return d

    def regeln(self, zone_id: int | None = None) -> list[Regel]:
        if zone_id is None:
            zeilen = self.db.execute("SELECT * FROM schedule_rule ORDER BY id")
        else:
            zeilen = self.db.execute("SELECT * FROM schedule_rule WHERE zone_id = ? ORDER BY id", (zone_id,))
        return [self._regel(r) for r in zeilen]

    def regel(self, id_: int) -> Regel | None:
        r = self.db.execute("SELECT * FROM schedule_rule WHERE id = ?", (id_,)).fetchone()
        return self._regel(r) if r else None

    def regel_anlegen(self, **werte) -> Regel:
        return self.regel(self._einfuegen("schedule_rule", self._regel_werte(werte)))

    def regel_aendern(self, id_: int, **werte) -> Regel | None:
        return self.regel(id_) if self._aendern("schedule_rule", id_, self._regel_werte(werte)) else None

    def regel_loeschen(self, id_: int) -> bool:
        return self._loeschen("schedule_rule", id_)

    # --- Ausnahmen -------------------------------------------------------

    @staticmethod
    def _ausnahme(r: sqlite3.Row) -> Ausnahme:
        return Ausnahme(r["id"], r["rule_id"], date.fromisoformat(r["datum"]), AusnahmeAktion(r["aktion"]), r["wert"])

    def ausnahmen(self) -> list[Ausnahme]:
        return [self._ausnahme(r) for r in self.db.execute("SELECT * FROM schedule_exception ORDER BY datum, id")]

    def ausnahme(self, id_: int) -> Ausnahme | None:
        r = self.db.execute("SELECT * FROM schedule_exception WHERE id = ?", (id_,)).fetchone()
        return self._ausnahme(r) if r else None

    def ausnahme_setzen(self, regel_id: int, datum: date, aktion: AusnahmeAktion, wert: str | None) -> Ausnahme:
        """Legt die Ausnahme für diesen Termin an oder ersetzt sie (eine je Regel und Tag)."""
        self.db.execute(
            "INSERT INTO schedule_exception (rule_id, datum, aktion, wert) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (rule_id, datum) DO UPDATE SET aktion = excluded.aktion, wert = excluded.wert",
            (regel_id, datum.isoformat(), AusnahmeAktion(aktion).value, wert))
        r = self.db.execute("SELECT * FROM schedule_exception WHERE rule_id = ? AND datum = ?",
                            (regel_id, datum.isoformat())).fetchone()
        return self._ausnahme(r)

    def ausnahme_loeschen(self, id_: int) -> bool:
        return self._loeschen("schedule_exception", id_)

    # --- run_log ---------------------------------------------------------

    def protokollieren(self, grund: str, *, zone_id: int | None = None, plan_schluessel: str | None = None,
                       geplant_s: int | None = None, tatsaechlich_s: float | None = None,
                       status: str | None = None, text: str | None = None,
                       sicherheitsereignis: str | None = None, zeitpunkt: datetime | None = None) -> int | None:
        """Eintrag schreiben. Mit `plan_schluessel` höchstens einmal je Termin; sonst None."""
        try:
            return self._einfuegen("run_log", {
                "zeitpunkt": (zeitpunkt or datetime.now(timezone.utc)).isoformat(),
                "zone_id": zone_id, "plan_schluessel": plan_schluessel, "geplant_s": geplant_s,
                "tatsaechlich_s": tatsaechlich_s, "grund": grund, "status": status, "text": text,
                "sicherheitsereignis": sicherheitsereignis})
        except sqlite3.IntegrityError:
            return None

    def protokoll_aendern(self, id_: int, **werte) -> None:
        self._aendern("run_log", id_, werte)

    def termin_erledigt(self, plan_schluessel: str) -> bool:
        return self.db.execute("SELECT 1 FROM run_log WHERE plan_schluessel = ?", (plan_schluessel,)).fetchone() is not None

    def protokoll(self, limit: int = 100, nur_sicherheit: bool = False) -> list[Protokolleintrag]:
        bedingung = "WHERE sicherheitsereignis IS NOT NULL" if nur_sicherheit else ""
        zeilen = self.db.execute(f"SELECT * FROM run_log {bedingung} ORDER BY zeitpunkt DESC, id DESC LIMIT ?", (limit,))
        return [Protokolleintrag(**{**dict(r), "zeitpunkt": datetime.fromisoformat(r["zeitpunkt"])}) for r in zeilen]
