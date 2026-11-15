"""Vereinfachtes Abbild der Gardena-Dienste.

Gardena liefert JSON:API-Objekte: ein DEVICE hat mehrere Dienste (COMMON, VALVE,
POWER_SOCKET, SENSOR …) mit eigener Dienst-ID. Wir bilden pro schaltbarem oder
messendem Dienst ein flaches `Geraet`.

Die Auslegung der Aktivitäten ist bewusst vorsichtig (docs/gardena-api.md):
Ventil offen nur bei bekannten Bewässerungswerten, Steckdose an bei allem außer OFF.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GeraeteTyp(str, Enum):
    VENTIL = "VENTIL"
    STECKDOSE = "STECKDOSE"
    SENSOR = "SENSOR"


DIENST_ZUORDNUNG: dict[str, tuple[GeraeteTyp, str | None]] = {
    "VALVE": (GeraeteTyp.VENTIL, "VALVE_CONTROL"),
    "POWER_SOCKET": (GeraeteTyp.STECKDOSE, "POWER_SOCKET_CONTROL"),
    "SENSOR": (GeraeteTyp.SENSOR, None),
}

VENTIL_OFFEN = {"MANUAL_WATERING", "SCHEDULED_WATERING"}
STECKDOSE_AUS = "OFF"
ZEITPLAN_AKTIVITAETEN = {"SCHEDULED_WATERING", "SCHEDULED_ON"}


@dataclass(frozen=True)
class Geraet:
    id: str                       # Gardena-Dienst-ID, z. B. "<geraet-uuid>:1"
    name: str
    typ: GeraeteTyp
    befehls_typ: str | None
    aktivitaet: str | None = None  # Rohwert von `activity`, None = unbekannt
    online: bool = True
    rest_sekunden: int | None = None
    batterie_prozent: int | None = None
    bodenfeuchte_prozent: float | None = None
    temperatur_c: float | None = None

    @property
    def ventil_offen(self) -> bool:
        return self.typ is GeraeteTyp.VENTIL and self.aktivitaet in VENTIL_OFFEN

    @property
    def steckdose_an(self) -> bool:
        """Vorsichtig: unbekannter oder fehlender Zustand zählt als an."""
        return self.typ is GeraeteTyp.STECKDOSE and self.aktivitaet != STECKDOSE_AUS

    @property
    def zeitplan_lauf(self) -> bool:
        return self.aktivitaet in ZEITPLAN_AKTIVITAETEN


def wert(objekt: dict | None, feld: str):
    """`value` eines Gardena-Attributs ({"value": …, "timestamp": …}) oder None."""
    attribut = ((objekt or {}).get("attributes") or {}).get(feld)
    return attribut.get("value") if isinstance(attribut, dict) else None


def aus_dienst(dienst: dict, common: dict | None = None) -> Geraet | None:
    """Ein Gardena-Dienstobjekt (JSON:API) übersetzen. `common` ist der COMMON-Dienst des Geräts."""
    zuordnung = DIENST_ZUORDNUNG.get(dienst.get("type", ""))
    if zuordnung is None:
        return None
    typ, befehls_typ = zuordnung
    temperatur = wert(dienst, "soilTemperature")
    if temperatur is None:
        temperatur = wert(dienst, "ambientTemperature")
    return Geraet(
        id=dienst["id"],
        name=wert(dienst, "name") or wert(common, "name") or dienst["id"],
        typ=typ,
        befehls_typ=befehls_typ,
        aktivitaet=wert(dienst, "activity"),
        online=wert(common, "rfLinkState") != "OFFLINE" and wert(dienst, "state") != "UNAVAILABLE",
        rest_sekunden=wert(dienst, "duration"),
        batterie_prozent=wert(common, "batteryLevel"),
        bodenfeuchte_prozent=wert(dienst, "soilHumidity"),
        temperatur_c=temperatur,
    )
