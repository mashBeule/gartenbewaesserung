"""REST-API für die Android-App.

Die API lauscht nur auf localhost bzw. der Tailscale-Adresse (BEWAESSERUNG_API_HOST) und
verlangt bei jedem Aufruf `Authorization: Bearer <BEWAESSERUNG_API_TOKEN>`. Ohne Token
antwortet sie mit 503. Die OpenAPI-Beschreibung für die App liegt unter /openapi.json.

Die App schaltet nie direkt: Manuelle Läufe und Not-Aus gehen über Scheduler und Orchestrator,
alle Sicherheitsregeln gelten genauso. Längere Laufzeiten als die Höchstlaufzeit der Kopplung
lehnt die API schon beim Speichern einer Regel ab (Regel 8).

Wetter: `/wetter` (aktuelle Lage), `/wetterregeln` (global und je Zone), `/einstellungen/standort`.
Im Kalender (`/plan`) tragen Termine der nächsten 24 h die voraussichtliche Wetterentscheidung,
vergangene Termine ihren Status aus dem Verlauf.

Claude-Assistent: `/entwuerfe` erstellt Saisonplan-Entwürfe im Hintergrund (Status abfragen),
`/entwuerfe/{id}/plan` zeigt sie im Kalender, `/entwuerfe/{id}/uebernehmen` legt Regeln an.
Claude schaltet nie: Entwürfe werden nur durch die Freigabe hier zu Regeln.
"""
from __future__ import annotations

import hmac
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..assistent.dienst import Assistent, EntwurfNichtMoeglich
from ..gardena.modelle import GeraeteTyp
from ..gardena.zustand import Geraetezustand
from ..planung.plan import Ausnahme, AusnahmeAktion, Quelle, Regel
from ..planung.scheduler import LaufNichtMoeglich, Scheduler
from ..sicherheit.kopplung import Kopplung, LaufAbgelehnt
from ..sicherheit.orchestrator import Orchestrator
from ..speicher.datenbank import Speicher
from ..wetter.dienst import WetterDienst
from ..wetter.entscheidung import WetterRegel

MAX_PLAN_TAGE = 62
WETTER_VORSCHAU = timedelta(hours=24)


@dataclass
class Kontext:
    speicher: Speicher
    zustand: Geraetezustand
    orchestrator: Orchestrator
    scheduler: Scheduler
    token: str | None
    trockenlauf: bool = True
    wetter: WetterDienst | None = None
    assistent: Assistent | None = None


# --- Modelle -------------------------------------------------------------

class ZoneEin(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    ventil_id: str = Field(min_length=1)
    pflanzentyp: str | None = None
    boden: str | None = None
    sonnenlage: str | None = None
    durchfluss_l_min: float | None = Field(default=None, ge=0)


class ZoneAus(ZoneEin):
    id: int
    kopplung_id: int | None


class KopplungEin(BaseModel):
    ventil_id: str = Field(min_length=1)
    steckdose_id: str = Field(min_length=1)
    vorlauf_ventil_s: int = Field(ge=0)
    nachlauf_ventil_s: int = Field(ge=0)
    max_laufzeit_s: int = Field(gt=0)


class KopplungAus(KopplungEin):
    id: int


class RegelEin(BaseModel):
    zone_id: int
    wochentage: int = Field(ge=1, le=127, description="Bit 0 = Montag … Bit 6 = Sonntag")
    startzeit: time
    dauer_min: int = Field(ge=1)
    gueltig_von: date | None = None
    gueltig_bis: date | None = None
    aktiv: bool = True
    quelle: Quelle = Quelle.MANUELL


class RegelAus(RegelEin):
    id: int


class AusnahmeEin(BaseModel):
    regel_id: int
    datum: date
    aktion: AusnahmeAktion
    wert: str | None = Field(default=None, description="VERSCHIEBEN: HH:MM oder YYYY-MM-DDTHH:MM; DAUER_ANPASSEN: Minuten")


class AusnahmeAus(AusnahmeEin):
    id: int


class LaufAus(BaseModel):
    schluessel: str
    regel_id: int
    zone_id: int
    start: datetime
    dauer_s: int
    ausgelassen: bool
    hinweis: str
    wetter_faktor: float | None = Field(default=None, description="Vorschau für die nächsten 24 h; 0 = fällt aus")
    wetter_hinweis: str | None = None
    status: str | None = Field(default=None, description="Status aus dem Verlauf, wenn der Termin schon behandelt ist")
    tatsaechlich_s: float | None = None


class WetterLageAus(BaseModel):
    standort_gesetzt: bool
    prognose_stand: datetime | None
    messung_stand: datetime | None
    veraltet: bool
    fehler: list[str]
    regen_24h_mm: float | None
    regen_prognose_12h_mm: float | None
    tiefsttemperatur_12h_c: float | None
    et0_24h_mm: float | None


class WetterRegelEin(BaseModel):
    zone_id: int | None = Field(default=None, description="None = globale Regel")
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


class EntwurfEin(BaseModel):
    wuensche: str = Field(default="", max_length=2000, description="z. B. „Rasen neu angelegt“, „ab Juli drei Wochen Urlaub“")
    saison: int | None = Field(default=None, ge=2024, le=2100, description="Standard: laufendes Jahr bis August, sonst das nächste")


class EntwurfAus(BaseModel):
    id: int
    erstellt: datetime
    status: str = Field(description="IN_ARBEIT, OFFEN, FEHLERHAFT, UEBERNOMMEN, VERWORFEN")
    saison: int
    wuensche: str
    modell: str | None
    inhalt: dict | None
    fehler: list[str]
    uebernommen: list[int]


class UebernehmenEin(BaseModel):
    zone_ids: list[int] | None = Field(default=None, description="None = alle Zonen des Entwurfs")


class UebernommenAus(BaseModel):
    regel_ids: list[int]


class StandortEin(BaseModel):
    breite: float = Field(ge=-90, le=90)
    laenge: float = Field(ge=-180, le=180)


class GeraetAus(BaseModel):
    id: str
    name: str
    typ: GeraeteTyp
    aktivitaet: str | None
    online: bool
    rest_sekunden: int | None
    batterie_prozent: int | None
    bodenfeuchte_prozent: float | None
    temperatur_c: float | None


class StatusAus(BaseModel):
    verbunden: bool
    freigegeben: bool
    trockenlauf: bool
    aktive_laeufe: list[str]
    warteschlange: int
    naechste_laeufe: list[LaufAus]
    wetter: WetterLageAus | None = None


class ProtokollAus(BaseModel):
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


class ManuellEin(BaseModel):
    zone_id: int
    dauer_min: int = Field(ge=1)


class Meldung(BaseModel):
    meldung: str


# --- App -----------------------------------------------------------------

def app_bauen(k: Kontext) -> FastAPI:
    app = FastAPI(title="Bewässerung", version="0.1.0",
                  description="Steuerung des Gardena smart system auf dem Raspberry Pi")

    def pruefe_token(authorization: str | None = Header(default=None)) -> None:
        if not k.token:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API-Token ist nicht eingerichtet")
        erwartet = f"Bearer {k.token}"
        if authorization is None or not hmac.compare_digest(authorization.encode(), erwartet.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token fehlt oder ist falsch",
                                headers={"WWW-Authenticate": "Bearer"})

    geschuetzt = [Depends(pruefe_token)]

    def ungueltig(text: str) -> HTTPException:
        return HTTPException(422, text)

    def nicht_gefunden(was: str) -> HTTPException:
        return HTTPException(status.HTTP_404_NOT_FOUND, f"{was} nicht gefunden")

    def geraetetyp_pruefen(geraet_id: str, typ: GeraeteTyp) -> None:
        g = k.zustand.geraet(geraet_id)
        if g is not None and g.typ is not typ:
            raise ungueltig(f"{geraet_id} ist {g.typ.value}, erwartet {typ.value}")

    def kopplung_der_zone(zone_id: int) -> Kopplung | None:
        zone = k.speicher.zone(zone_id)
        return k.speicher.kopplung_fuer_ventil(zone.ventil_id) if zone else None

    def zone_aus(z) -> ZoneAus:
        kopplung = k.speicher.kopplung_fuer_ventil(z.ventil_id)
        return ZoneAus(**asdict(z), kopplung_id=int(kopplung.id) if kopplung else None)

    def regel_pruefen(r: RegelEin) -> None:
        if k.speicher.zone(r.zone_id) is None:
            raise ungueltig(f"Zone {r.zone_id} gibt es nicht")
        fehler = Regel(0, **r.model_dump()).fehler()
        if fehler:
            raise ungueltig("; ".join(fehler))
        kopplung = kopplung_der_zone(r.zone_id)
        if kopplung is None:
            raise ungueltig("Zone hat keine Kopplung mit einer Steckdose; erst Kopplung anlegen")
        try:
            kopplung.pruefen(r.dauer_min * 60)       # Regel 8
        except LaufAbgelehnt as f:
            raise ungueltig(str(f))

    def laeufe_aus(liste) -> list[LaufAus]:
        jetzt = k.scheduler.jetzt()
        erledigt = k.speicher.protokoll_zu_terminen([l.schluessel for l in liste])
        aus = []
        for l in liste:
            zusatz = {}
            if (e := erledigt.get(l.schluessel)) is not None:
                zusatz = {"status": e.status, "tatsaechlich_s": e.tatsaechlich_s}
            elif k.wetter is not None and not l.ausgelassen and jetzt <= l.start <= jetzt + WETTER_VORSCHAU:
                try:
                    w = k.wetter.entscheidung(l.zone_id, l.start)
                    zusatz = {"wetter_faktor": w.faktor, "wetter_hinweis": w.text or None}
                except Exception as f:
                    zusatz = {"wetter_hinweis": f"Wetter nicht auswertbar: {f}"}
            aus.append(LaufAus(**asdict(l), **zusatz))
        return aus

    def wetter_lage() -> WetterLageAus | None:
        return WetterLageAus(**asdict(k.wetter.lage())) if k.wetter is not None else None

    def schreiben(aktion):
        try:
            return aktion()
        except sqlite3.IntegrityError as f:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Widerspricht vorhandenen Daten: {f}")

    # --- Status ----------------------------------------------------------

    @app.get("/status", response_model=StatusAus, dependencies=geschuetzt)
    async def status_lesen():
        return StatusAus(
            verbunden=k.zustand.verbunden, freigegeben=k.orchestrator.freigegeben, trockenlauf=k.trockenlauf,
            aktive_laeufe=k.orchestrator.aktive_laeufe, warteschlange=len(k.scheduler.warteschlange),
            naechste_laeufe=laeufe_aus(k.scheduler.naechste()), wetter=wetter_lage())

    @app.get("/geraete", response_model=list[GeraetAus], dependencies=geschuetzt)
    async def geraete():
        return [GeraetAus(**{f: getattr(g, f) for f in GeraetAus.model_fields}) for g in k.zustand.geraete()]

    # --- Zonen -----------------------------------------------------------

    @app.get("/zonen", response_model=list[ZoneAus], dependencies=geschuetzt)
    async def zonen():
        return [zone_aus(z) for z in k.speicher.zonen()]

    @app.post("/zonen", response_model=ZoneAus, status_code=201, dependencies=geschuetzt)
    async def zone_anlegen(z: ZoneEin):
        geraetetyp_pruefen(z.ventil_id, GeraeteTyp.VENTIL)
        return zone_aus(schreiben(lambda: k.speicher.zone_anlegen(**z.model_dump())))

    @app.put("/zonen/{zone_id}", response_model=ZoneAus, dependencies=geschuetzt)
    async def zone_aendern(zone_id: int, z: ZoneEin):
        geraetetyp_pruefen(z.ventil_id, GeraeteTyp.VENTIL)
        neu = schreiben(lambda: k.speicher.zone_aendern(zone_id, **z.model_dump()))
        if neu is None:
            raise nicht_gefunden("Zone")
        return zone_aus(neu)

    @app.delete("/zonen/{zone_id}", status_code=204, dependencies=geschuetzt)
    async def zone_loeschen(zone_id: int):
        if not k.speicher.zone_loeschen(zone_id):
            raise nicht_gefunden("Zone")

    # --- Kopplungen ------------------------------------------------------

    def kopplung_pruefen(id_: int, kp: KopplungEin) -> None:
        fehler = Kopplung(str(id_), **kp.model_dump()).fehler()
        if fehler:
            raise ungueltig("; ".join(fehler))
        geraetetyp_pruefen(kp.ventil_id, GeraeteTyp.VENTIL)
        geraetetyp_pruefen(kp.steckdose_id, GeraeteTyp.STECKDOSE)
        if str(id_) in k.orchestrator.aktive_laeufe:
            raise HTTPException(status.HTTP_409_CONFLICT, "Kopplung ist gerade im Lauf")
        neu = Kopplung(str(id_), **kp.model_dump())
        for z in k.speicher.zonen():
            if z.ventil_id != kp.ventil_id:
                continue
            for r in k.speicher.regeln(z.id):
                try:
                    neu.pruefen(r.dauer_min * 60)
                except LaufAbgelehnt as f:
                    raise ungueltig(f"Regel {r.id} der Zone {z.name} passt nicht mehr: {f}")

    @app.get("/kopplungen", response_model=list[KopplungAus], dependencies=geschuetzt)
    async def kopplungen():
        return [KopplungAus(**{**asdict(kp), "id": int(kp.id)}) for kp in k.speicher.kopplungen()]

    @app.post("/kopplungen", response_model=KopplungAus, status_code=201, dependencies=geschuetzt)
    async def kopplung_anlegen(kp: KopplungEin):
        kopplung_pruefen(0, kp)
        neu = schreiben(lambda: k.speicher.kopplung_anlegen(**kp.model_dump()))
        return KopplungAus(**{**asdict(neu), "id": int(neu.id)})

    @app.put("/kopplungen/{kopplung_id}", response_model=KopplungAus, dependencies=geschuetzt)
    async def kopplung_aendern(kopplung_id: int, kp: KopplungEin):
        kopplung_pruefen(kopplung_id, kp)
        neu = schreiben(lambda: k.speicher.kopplung_aendern(kopplung_id, **kp.model_dump()))
        if neu is None:
            raise nicht_gefunden("Kopplung")
        return KopplungAus(**{**asdict(neu), "id": int(neu.id)})

    @app.delete("/kopplungen/{kopplung_id}", status_code=204, dependencies=geschuetzt)
    async def kopplung_loeschen(kopplung_id: int):
        if str(kopplung_id) in k.orchestrator.aktive_laeufe:
            raise HTTPException(status.HTTP_409_CONFLICT, "Kopplung ist gerade im Lauf")
        if not k.speicher.kopplung_loeschen(kopplung_id):
            raise nicht_gefunden("Kopplung")

    # --- Regeln ----------------------------------------------------------

    @app.get("/regeln", response_model=list[RegelAus], dependencies=geschuetzt)
    async def regeln(zone_id: int | None = None):
        return [RegelAus(**asdict(r)) for r in k.speicher.regeln(zone_id)]

    @app.post("/regeln", response_model=RegelAus, status_code=201, dependencies=geschuetzt)
    async def regel_anlegen(r: RegelEin):
        regel_pruefen(r)
        return RegelAus(**asdict(schreiben(lambda: k.speicher.regel_anlegen(**r.model_dump()))))

    @app.put("/regeln/{regel_id}", response_model=RegelAus, dependencies=geschuetzt)
    async def regel_aendern(regel_id: int, r: RegelEin):
        regel_pruefen(r)
        neu = schreiben(lambda: k.speicher.regel_aendern(regel_id, **r.model_dump()))
        if neu is None:
            raise nicht_gefunden("Regel")
        return RegelAus(**asdict(neu))

    @app.delete("/regeln/{regel_id}", status_code=204, dependencies=geschuetzt)
    async def regel_loeschen(regel_id: int):
        if not k.speicher.regel_loeschen(regel_id):
            raise nicht_gefunden("Regel")

    # --- Ausnahmen -------------------------------------------------------

    @app.get("/ausnahmen", response_model=list[AusnahmeAus], dependencies=geschuetzt)
    async def ausnahmen():
        return [AusnahmeAus(**asdict(a)) for a in k.speicher.ausnahmen()]

    @app.post("/ausnahmen", response_model=AusnahmeAus, status_code=201, dependencies=geschuetzt)
    async def ausnahme_setzen(a: AusnahmeEin):
        regel = k.speicher.regel(a.regel_id)
        if regel is None:
            raise ungueltig(f"Regel {a.regel_id} gibt es nicht")
        fehler = Ausnahme(0, **a.model_dump()).fehler()
        if fehler:
            raise ungueltig("; ".join(fehler))
        if a.aktion is AusnahmeAktion.DAUER_ANPASSEN:
            kopplung = kopplung_der_zone(regel.zone_id)
            try:
                if kopplung is None:
                    raise LaufAbgelehnt("Zone hat keine Kopplung")
                kopplung.pruefen(int(a.wert) * 60)
            except LaufAbgelehnt as f:
                raise ungueltig(str(f))
        return AusnahmeAus(**asdict(k.speicher.ausnahme_setzen(a.regel_id, a.datum, a.aktion, a.wert)))

    @app.delete("/ausnahmen/{ausnahme_id}", status_code=204, dependencies=geschuetzt)
    async def ausnahme_loeschen(ausnahme_id: int):
        if not k.speicher.ausnahme_loeschen(ausnahme_id):
            raise nicht_gefunden("Ausnahme")

    # --- Kalender, Verlauf -----------------------------------------------

    @app.get("/plan", response_model=list[LaufAus], dependencies=geschuetzt)
    async def plan(von: date, bis: date):
        if bis < von or (bis - von) > timedelta(days=MAX_PLAN_TAGE):
            raise ungueltig(f"Zeitraum muss 0 bis {MAX_PLAN_TAGE} Tage umfassen")
        return laeufe_aus(k.scheduler.plan(von, bis))

    @app.get("/verlauf", response_model=list[ProtokollAus], dependencies=geschuetzt)
    async def verlauf(limit: int = Query(100, ge=1, le=1000), nur_sicherheit: bool = False):
        return [ProtokollAus(**asdict(e)) for e in k.speicher.protokoll(limit, nur_sicherheit)]

    # --- Wetter ----------------------------------------------------------

    @app.get("/wetter", response_model=WetterLageAus, dependencies=geschuetzt)
    async def wetter():
        if k.wetter is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Wetter-Dienst läuft nicht")
        return wetter_lage()

    @app.get("/einstellungen/standort", response_model=StandortEin, dependencies=geschuetzt)
    async def standort():
        s = k.speicher.standort()
        if s is None:
            raise nicht_gefunden("Standort")
        return StandortEin(breite=s[0], laenge=s[1])

    @app.put("/einstellungen/standort", response_model=StandortEin, dependencies=geschuetzt)
    async def standort_setzen(s: StandortEin):
        k.speicher.standort_setzen(s.breite, s.laenge)
        if k.wetter is not None:
            k.wetter.wecken()
        return s

    @app.get("/wetterregeln", response_model=list[WetterRegelEin], dependencies=geschuetzt)
    async def wetterregeln():
        """Gespeicherte Regeln; ohne globale Zeile gelten die Startwerte (zone_id = null)."""
        regeln = k.speicher.wetterregeln()
        if not any(r.zone_id is None for r in regeln):
            regeln.insert(0, WetterRegel())
        return [WetterRegelEin(**asdict(r)) for r in regeln]

    @app.put("/wetterregeln", response_model=WetterRegelEin, dependencies=geschuetzt)
    async def wetterregel_setzen(w: WetterRegelEin):
        regel = WetterRegel(**w.model_dump())
        fehler = regel.fehler()
        if fehler:
            raise ungueltig("; ".join(fehler))
        if regel.zone_id is not None and k.speicher.zone(regel.zone_id) is None:
            raise ungueltig(f"Zone {regel.zone_id} gibt es nicht")
        if regel.sensor_id is not None:
            geraetetyp_pruefen(regel.sensor_id, GeraeteTyp.SENSOR)
        return WetterRegelEin(**asdict(k.speicher.wetterregel_setzen(regel)))

    @app.delete("/wetterregeln/zone/{zone_id}", status_code=204, dependencies=geschuetzt)
    async def wetterregel_loeschen(zone_id: int):
        """Zone fällt auf die globale Regel zurück."""
        if not k.speicher.wetterregel_loeschen(zone_id):
            raise nicht_gefunden("Wetterregel")

    # --- Claude-Assistent ------------------------------------------------

    def assistent() -> Assistent:
        if k.assistent is None or not k.assistent.verfuegbar:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "Claude-Assistent nicht eingerichtet (ANTHROPIC_API_KEY fehlt)")
        return k.assistent

    def entwurf_holen(entwurf_id: int):
        e = k.speicher.entwurf(entwurf_id)
        if e is None:
            raise nicht_gefunden("Entwurf")
        return e

    @app.get("/entwuerfe", response_model=list[EntwurfAus], dependencies=geschuetzt)
    async def entwuerfe():
        return [EntwurfAus(**asdict(e)) for e in k.speicher.entwuerfe()]

    @app.post("/entwuerfe", response_model=EntwurfAus, status_code=202, dependencies=geschuetzt)
    async def entwurf_starten(e: EntwurfEin):
        try:
            return EntwurfAus(**asdict(assistent().starten(e.wuensche, e.saison)))
        except EntwurfNichtMoeglich as f:
            raise HTTPException(status.HTTP_409_CONFLICT, str(f))

    @app.get("/entwuerfe/{entwurf_id}", response_model=EntwurfAus, dependencies=geschuetzt)
    async def entwurf(entwurf_id: int):
        return EntwurfAus(**asdict(entwurf_holen(entwurf_id)))

    @app.get("/entwuerfe/{entwurf_id}/plan", response_model=list[LaufAus], dependencies=geschuetzt)
    async def entwurf_plan(entwurf_id: int, von: date, bis: date):
        """Termine des Entwurfs für den Kalender (regel_id negativ = Regel aus dem Entwurf)."""
        entwurf_holen(entwurf_id)
        if bis < von or (bis - von) > timedelta(days=MAX_PLAN_TAGE):
            raise ungueltig(f"Zeitraum muss 0 bis {MAX_PLAN_TAGE} Tage umfassen")
        if k.assistent is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Claude-Assistent läuft nicht")
        return [LaufAus(**asdict(l)) for l in k.assistent.plan(entwurf_id, von, bis)]

    @app.post("/entwuerfe/{entwurf_id}/uebernehmen", response_model=UebernommenAus, dependencies=geschuetzt)
    async def entwurf_uebernehmen(entwurf_id: int, u: UebernehmenEin):
        entwurf_holen(entwurf_id)
        if k.assistent is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Claude-Assistent läuft nicht")
        try:
            return UebernommenAus(regel_ids=k.assistent.uebernehmen(entwurf_id, u.zone_ids))
        except EntwurfNichtMoeglich as f:
            raise HTTPException(status.HTTP_409_CONFLICT, str(f))

    @app.post("/entwuerfe/{entwurf_id}/verwerfen", response_model=EntwurfAus, dependencies=geschuetzt)
    async def entwurf_verwerfen(entwurf_id: int):
        entwurf_holen(entwurf_id)
        if k.assistent is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Claude-Assistent läuft nicht")
        try:
            k.assistent.verwerfen(entwurf_id)
        except EntwurfNichtMoeglich as f:
            raise HTTPException(status.HTTP_409_CONFLICT, str(f))
        return EntwurfAus(**asdict(entwurf_holen(entwurf_id)))

    # --- Schalten --------------------------------------------------------

    @app.post("/laeufe", response_model=Meldung, status_code=202, dependencies=geschuetzt)
    async def manuell(m: ManuellEin):
        try:
            k.scheduler.manuell(m.zone_id, m.dauer_min * 60)
        except LaufNichtMoeglich as f:
            raise HTTPException(status.HTTP_409_CONFLICT, str(f))
        except LaufAbgelehnt as f:
            raise ungueltig(str(f))
        return Meldung(meldung="Lauf gestartet")

    @app.post("/notaus", response_model=Meldung, dependencies=geschuetzt)
    async def notaus():
        await k.scheduler.not_aus()
        return Meldung(meldung="Alle Läufe abgebrochen, Steckdosen vor Ventilen gestoppt")

    return app
