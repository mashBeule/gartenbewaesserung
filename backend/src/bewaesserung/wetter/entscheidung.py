"""Wetterentscheidung für einen Termin. Reine Logik: kein Netzwerk, keine Datenbank, keine echte Uhr.

Regeln laut Plan, die erste zutreffende gewinnt (Startwerte, je Zone einstellbar):

1. Bodenfeuchte ≥ Schwelle → aussetzen (Gardena-Sensor, höchste Priorität, gilt auch ohne Wetterdaten).
2. Gemessener Regen der letzten 24 h ≥ `regen_schwelle_mm` → aussetzen
   (Bright Sky; fehlt die Messung, die Modellwerte von Open-Meteo).
3. Prognose für die nächsten `zeitfenster_h` ≥ `prognose_schwelle_mm` bei mindestens
   `prognose_wahrscheinlichkeit_pct` → aussetzen.
4. Tiefsttemperatur von 1 h vor bis `zeitfenster_h` nach dem Start < `frost_grenze_c` → aussetzen.
5. Sonst Dauer skalieren: (ET₀ − Regen) seit der letzten Bewässerung geteilt durch
   Referenz-ET₀ je Tag × Tage, begrenzt auf `faktor_min` … `faktor_max`.

Ausfall: Sind die Prognosedaten älter als `MAX_ALTER` oder fehlen sie, gilt Faktor 1,0 mit Hinweis.

Stundenwerte: `Stunde.zeit` ist das Ende der Stunde (UTC). Niederschlag und ET₀ sind die Summe
der Stunde davor (so liefern es Open-Meteo und Bright Sky), die Temperatur gilt zum Zeitpunkt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

MAX_ALTER = timedelta(hours=12)
MAX_SKALIERUNG = timedelta(days=7)        # weiter reichen die Vergangenheitsdaten nicht
ABDECKUNG = 0.8                           # Anteil der Stunden mit Wert, damit eine Summe zählt


@dataclass(frozen=True)
class Stunde:
    zeit: datetime
    niederschlag_mm: float | None = None
    wahrscheinlichkeit_pct: float | None = None
    temperatur_c: float | None = None
    et0_mm: float | None = None


@dataclass(frozen=True)
class Reihe:
    """Stundenwerte einer Quelle samt Abrufzeitpunkt."""
    quelle: str
    stand: datetime
    stunden: tuple[Stunde, ...] = field(default=())

    def _werte(self, feld: str, von: datetime, bis: datetime) -> list[float]:
        return [getattr(s, feld) for s in self.stunden
                if von < s.zeit <= bis and getattr(s, feld) is not None]

    def summe(self, feld: str, von: datetime, bis: datetime) -> float | None:
        """Summe über (von, bis]; None, wenn weniger als 80 % der Stunden einen Wert haben."""
        werte = self._werte(feld, von, bis)
        erwartet = max(1, round((bis - von).total_seconds() / 3600))
        return sum(werte) if len(werte) >= ABDECKUNG * erwartet else None

    def minimum(self, feld: str, von: datetime, bis: datetime) -> float | None:
        werte = self._werte(feld, von, bis)
        return min(werte) if werte else None

    def maximum(self, feld: str, von: datetime, bis: datetime) -> float | None:
        werte = self._werte(feld, von, bis)
        return max(werte) if werte else None

    def frisch(self, jetzt: datetime, max_alter: timedelta = MAX_ALTER) -> bool:
        return jetzt - self.stand <= max_alter


@dataclass(frozen=True)
class WetterRegel:
    """Eine Zeile aus `weather_policy`. `zone_id=None` ist die globale Regel."""
    zone_id: int | None = None
    aktiv: bool = True
    regen_schwelle_mm: float = 5.0
    prognose_schwelle_mm: float = 4.0
    prognose_wahrscheinlichkeit_pct: int = 70
    zeitfenster_h: int = 12
    frost_grenze_c: float = 2.0
    bodenfeuchte_schwelle_pct: float | None = None
    sensor_id: str | None = None
    skalierung_aktiv: bool = True
    referenz_et0_mm_tag: float = 4.0
    faktor_min: float = 0.3
    faktor_max: float = 1.5

    def fehler(self) -> list[str]:
        f = []
        if self.regen_schwelle_mm <= 0 or self.prognose_schwelle_mm <= 0:
            f.append("Regenschwellen müssen größer als 0 mm sein")
        if not 0 <= self.prognose_wahrscheinlichkeit_pct <= 100:
            f.append("Wahrscheinlichkeit muss zwischen 0 und 100 % liegen")
        if not 1 <= self.zeitfenster_h <= 48:
            f.append("Zeitfenster muss 1 bis 48 Stunden sein")
        if self.referenz_et0_mm_tag <= 0:
            f.append("Referenz-ET₀ muss größer als 0 sein")
        if not 0 < self.faktor_min <= 1 <= self.faktor_max <= 2:
            f.append("Faktorgrenzen: 0 < Minimum ≤ 1 ≤ Maximum ≤ 2")
        if (self.bodenfeuchte_schwelle_pct is None) != (self.sensor_id is None):
            f.append("Bodenfeuchte-Schwelle und Sensor nur zusammen angeben")
        if self.bodenfeuchte_schwelle_pct is not None and not 0 < self.bodenfeuchte_schwelle_pct <= 100:
            f.append("Bodenfeuchte-Schwelle muss zwischen 0 und 100 % liegen")
        return f


@dataclass(frozen=True)
class Entscheidung:
    faktor: float                 # 0 = aussetzen
    text: str
    veraltet: bool = False

    @property
    def aussetzen(self) -> bool:
        return self.faktor <= 0


def zahl(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def entscheiden(
    regel: WetterRegel,
    start: datetime,
    jetzt: datetime,
    prognose: Reihe | None,
    messung: Reihe | None,
    bodenfeuchte_pct: float | None = None,
    letzte_bewaesserung: datetime | None = None,
) -> Entscheidung:
    if not regel.aktiv:
        return Entscheidung(1.0, "")
    hinweise: list[str] = []

    # 1. Bodenfeuchte
    if regel.sensor_id is not None and regel.bodenfeuchte_schwelle_pct is not None:
        if bodenfeuchte_pct is None:
            hinweise.append("Bodensensor ohne Wert")
        elif bodenfeuchte_pct >= regel.bodenfeuchte_schwelle_pct:
            return Entscheidung(0.0, f"Boden feucht: {zahl(bodenfeuchte_pct)} % ≥ {zahl(regel.bodenfeuchte_schwelle_pct)} %")

    prognose_ok = prognose is not None and prognose.frisch(jetzt)
    messung_ok = messung is not None and messung.frisch(jetzt)

    def regen(von: datetime, bis: datetime) -> tuple[float | None, str]:
        if messung_ok:
            wert = messung.summe("niederschlag_mm", von, bis)
            if wert is not None:
                return wert, "gemessen"
        if prognose_ok:
            return prognose.summe("niederschlag_mm", von, bis), "Modell"
        return None, ""

    # 2. Regen der letzten 24 h
    gefallen, art = regen(start - timedelta(hours=24), min(start, jetzt))
    if gefallen is not None and gefallen >= regel.regen_schwelle_mm:
        return Entscheidung(0.0, f"Regen letzte 24 h: {zahl(gefallen)} mm ({art})")

    if not prognose_ok:
        hinweise.append("Wetterdaten veraltet, Dauer unverändert")
        return Entscheidung(1.0, "; ".join(hinweise), veraltet=True)

    # 3. Regenprognose
    fenster = start + timedelta(hours=regel.zeitfenster_h)
    erwartet = prognose.summe("niederschlag_mm", start, fenster)
    wahrscheinlich = prognose.maximum("wahrscheinlichkeit_pct", start, fenster)
    if (erwartet is not None and erwartet >= regel.prognose_schwelle_mm
            and wahrscheinlich is not None and wahrscheinlich >= regel.prognose_wahrscheinlichkeit_pct):
        return Entscheidung(0.0, f"Regen erwartet: {zahl(erwartet)} mm in {regel.zeitfenster_h} h "
                                 f"({wahrscheinlich:.0f} %)")

    # 4. Frost
    tiefst = prognose.minimum("temperatur_c", start - timedelta(hours=1), fenster)
    if tiefst is not None and tiefst < regel.frost_grenze_c:
        return Entscheidung(0.0, f"Frostgefahr: Tiefstwert {zahl(tiefst)} °C")

    # 5. Skalieren
    if not regel.skalierung_aktiv:
        return Entscheidung(1.0, "; ".join(hinweise))
    seit = letzte_bewaesserung if letzte_bewaesserung is not None else start - timedelta(hours=24)
    seit = max(seit, start - MAX_SKALIERUNG)
    bis = min(start, jetzt)
    if bis - seit < timedelta(hours=1):
        return Entscheidung(regel.faktor_min, "; ".join(hinweise + ["vor weniger als 1 h bewässert"]))
    et0 = prognose.summe("et0_mm", seit, bis)
    if et0 is None:
        hinweise.append("ET₀ fehlt, Dauer unverändert")
        return Entscheidung(1.0, "; ".join(hinweise))
    gefallen, art = regen(seit, bis)
    gefallen = gefallen or 0.0
    tage = (bis - seit).total_seconds() / 86400
    referenz = regel.referenz_et0_mm_tag * tage
    faktor = min(regel.faktor_max, max(regel.faktor_min, (et0 - gefallen) / referenz))
    hinweise.insert(0, f"Faktor {faktor:.2f}: ET₀ {zahl(et0)} mm − Regen {zahl(gefallen)} mm "
                       f"in {zahl(tage)} Tagen, Referenz {zahl(referenz)} mm")
    return Entscheidung(round(faktor, 2), "; ".join(hinweise))
