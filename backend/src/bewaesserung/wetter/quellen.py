"""Wetterquellen: Open-Meteo (Prognose, ET₀, Modell-Vergangenheit) und Bright Sky (DWD-Messwerte).

Beide sind kostenlos und ohne Schlüssel. Abgerufen wird stündlich (`wetter/dienst.py`),
nie im Takt des Schedulers. Die Koordinaten werden auf zwei Nachkommastellen (~1 km) gerundet
übertragen; genauer braucht es kein Wettermodell.

⚠ Ungeprüft gegen die echten Dienste: Feldnamen sind aus der Doku übernommen
(https://open-meteo.com/en/docs, https://brightsky.dev/docs). Die Parser sind tolerant:
fehlende oder leere Werte werden zu None, kaputte Antworten zu einem Fehler beim Abruf.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from .entscheidung import Reihe, Stunde

TIMEOUT_S = 20.0
VERGANGENHEIT_TAGE = 7


class Wetterquelle(Protocol):
    name: str

    async def abrufen(self, breite: float, laenge: float, jetzt: datetime) -> Reihe: ...


def _zahl(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def open_meteo_lesen(daten: dict, stand: datetime) -> Reihe:
    stunden = daten["hourly"]
    zeiten = stunden["time"]

    def spalte(name: str) -> list:
        werte = stunden.get(name) or []
        return list(werte) + [None] * (len(zeiten) - len(werte))

    regen, wahrsch = spalte("precipitation"), spalte("precipitation_probability")
    temp, et0 = spalte("temperature_2m"), spalte("et0_fao_evapotranspiration")
    return Reihe("Open-Meteo", stand, tuple(
        Stunde(datetime.fromtimestamp(int(t), timezone.utc), _zahl(regen[i]), _zahl(wahrsch[i]),
               _zahl(temp[i]), _zahl(et0[i]))
        for i, t in enumerate(zeiten)))


def bright_sky_lesen(daten: dict, stand: datetime) -> Reihe:
    """Nur gemessene Werte: Einträge aus Quellen mit `observation_type` "forecast" fallen weg."""
    prognose_quellen = {q.get("id") for q in daten.get("sources") or [] if q.get("observation_type") == "forecast"}
    stunden = []
    for w in daten.get("weather") or []:
        if w.get("source_id") in prognose_quellen or not w.get("timestamp"):
            continue
        zeit = datetime.fromisoformat(w["timestamp"]).astimezone(timezone.utc)
        if zeit > stand:
            continue
        stunden.append(Stunde(zeit, niederschlag_mm=_zahl(w.get("precipitation")),
                              temperatur_c=_zahl(w.get("temperature"))))
    return Reihe("Bright Sky", stand, tuple(sorted(stunden, key=lambda s: s.zeit)))


class OpenMeteo:
    name = "Open-Meteo"
    URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self.http = http or httpx.AsyncClient(timeout=TIMEOUT_S)

    async def abrufen(self, breite: float, laenge: float, jetzt: datetime) -> Reihe:
        antwort = await self.http.get(self.URL, params={
            "latitude": round(breite, 2), "longitude": round(laenge, 2),
            "hourly": "precipitation,precipitation_probability,temperature_2m,et0_fao_evapotranspiration",
            "past_days": VERGANGENHEIT_TAGE, "forecast_days": 2,
            "timeformat": "unixtime", "timezone": "GMT"})
        antwort.raise_for_status()
        return open_meteo_lesen(antwort.json(), jetzt)


class BrightSky:
    name = "Bright Sky"
    URL = "https://api.brightsky.dev/weather"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self.http = http or httpx.AsyncClient(timeout=TIMEOUT_S)

    async def abrufen(self, breite: float, laenge: float, jetzt: datetime) -> Reihe:
        von = (jetzt - timedelta(days=VERGANGENHEIT_TAGE)).replace(minute=0, second=0, microsecond=0)
        antwort = await self.http.get(self.URL, params={
            "lat": round(breite, 2), "lon": round(laenge, 2),
            "date": von.isoformat(), "last_date": jetzt.isoformat(), "tz": "UTC"})
        antwort.raise_for_status()
        return bright_sky_lesen(antwort.json(), jetzt)


# --- Klima der Vorsaison (für den Claude-Assistenten) -----------------------------

MONATE = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")


def klima_lesen(daten: dict) -> list[dict]:
    """Tageswerte des Open-Meteo-Archivs zu Monatswerten: ET₀- und Regensumme, mittleres Tagesmaximum."""
    tage = daten["daily"]
    monate: dict[str, dict] = {}
    for i, tag in enumerate(tage["time"]):
        m = monate.setdefault(tag[:7], {"et0": [], "regen": [], "tmax": []})
        for schluessel, spalte in (("et0", "et0_fao_evapotranspiration"), ("regen", "precipitation_sum"),
                                   ("tmax", "temperature_2m_max")):
            wert = _zahl((tage.get(spalte) or [None] * (i + 1))[i])
            if wert is not None:
                m[schluessel].append(wert)
    return [{"monat": f"{MONATE[int(k[5:7]) - 1]} {k[:4]}",
             "et0_mm": round(sum(v["et0"]), 1), "regen_mm": round(sum(v["regen"]), 1),
             "tmax_mittel_c": round(sum(v["tmax"]) / len(v["tmax"]), 1) if v["tmax"] else None}
            for k, v in sorted(monate.items())]


class Klima:
    """Open-Meteo-Archiv: Monatswerte April bis Oktober eines vergangenen Jahres."""
    URL = "https://archive-api.open-meteo.com/v1/archive"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self.http = http or httpx.AsyncClient(timeout=TIMEOUT_S)

    async def vorsaison(self, breite: float, laenge: float, jahr: int) -> list[dict]:
        antwort = await self.http.get(self.URL, params={
            "latitude": round(breite, 2), "longitude": round(laenge, 2),
            "start_date": f"{jahr}-04-01", "end_date": f"{jahr}-10-31",
            "daily": "et0_fao_evapotranspiration,precipitation_sum,temperature_2m_max",
            "timezone": "Europe/Berlin"})
        antwort.raise_for_status()
        return klima_lesen(antwort.json())
