"""Wetter: Entscheidungsregeln, Parser der Quellen, Dienst, Scheduler und API mit simulierten Wetterdaten."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from bewaesserung.api.app import Kontext, app_bauen
from bewaesserung.wetter.dienst import WetterDienst
from bewaesserung.wetter.entscheidung import Reihe, Stunde, WetterRegel, entscheiden
from bewaesserung.wetter.quellen import bright_sky_lesen, open_meteo_lesen
from conftest import V1, ausfuehren
from test_scheduler import JETZT, Umgebung

UTC = timezone.utc
START = JETZT.astimezone(UTC).replace(second=0)          # 2026-06-01 04:00 UTC
REGEL = WetterRegel()


def reihe(quelle="Open-Meteo", stand=START, von=-7 * 24, bis=36, **werte) -> Reihe:
    """Stundenreihe um START; `werte` sind Funktionen Stunde-Offset → Wert."""
    stunden = []
    for h in range(von, bis + 1):
        stunden.append(Stunde(START + timedelta(hours=h), **{f: fn(h) for f, fn in werte.items()}))
    return Reihe(quelle, stand, tuple(stunden))


def normal(**ueber) -> Reihe:
    """Trocken, 15 °C, ET₀ 4 mm/Tag – ergibt Faktor 1,0."""
    werte = dict(niederschlag_mm=lambda h: 0.0, wahrscheinlichkeit_pct=lambda h: 10,
                 temperatur_c=lambda h: 15.0, et0_mm=lambda h: 4 / 24)
    werte.update(ueber)
    return reihe(**werte)


def regen_gemessen(mm_je_stunde: float, stunden: int) -> Reihe:
    return reihe("Bright Sky", niederschlag_mm=lambda h: mm_je_stunde if -stunden < h <= 0 else 0.0, bis=0)


# --- Entscheidungsregeln -----------------------------------------------------

def test_normaler_tag_faktor_eins():
    e = entscheiden(REGEL, START, START, normal(), regen_gemessen(0, 0))
    assert e.faktor == 1.0 and not e.aussetzen and "Faktor 1.00" in e.text


def test_gemessener_regen_setzt_aus():
    e = entscheiden(REGEL, START, START, normal(), regen_gemessen(1.0, 6))
    assert e.aussetzen and "6,0 mm (gemessen)" in e.text


def test_ohne_messung_zaehlt_das_modell():
    p = normal(niederschlag_mm=lambda h: 1.0 if -6 < h <= 0 else 0.0)
    e = entscheiden(REGEL, START, START, p, None)
    assert e.aussetzen and "(Modell)" in e.text


def test_regenprognose_braucht_menge_und_wahrscheinlichkeit():
    nass = dict(niederschlag_mm=lambda h: 0.5 if 0 < h <= 12 else 0.0)
    e = entscheiden(REGEL, START, START, normal(**nass, wahrscheinlichkeit_pct=lambda h: 80), None)
    assert e.aussetzen and "Regen erwartet: 6,0 mm" in e.text
    e = entscheiden(REGEL, START, START, normal(**nass, wahrscheinlichkeit_pct=lambda h: 50), None)
    assert not e.aussetzen


def test_frost_setzt_aus():
    e = entscheiden(REGEL, START, START, normal(temperatur_c=lambda h: 1.0 if h == 3 else 10.0), None)
    assert e.aussetzen and "Frost" in e.text


def test_bodenfeuchte_hat_vorrang_auch_ohne_wetterdaten():
    regel = WetterRegel(bodenfeuchte_schwelle_pct=40, sensor_id="sensor-1:1")
    assert entscheiden(regel, START, START, None, None, bodenfeuchte_pct=45).aussetzen
    e = entscheiden(regel, START, START, None, None, bodenfeuchte_pct=None)
    assert e.faktor == 1.0 and "Bodensensor ohne Wert" in e.text and e.veraltet


def test_skalierung_heiss_trocken_und_begrenzt():
    heiss = normal(et0_mm=lambda h: 6 / 24)
    assert entscheiden(REGEL, START, START, heiss, None).faktor == 1.5
    kuehl = normal(et0_mm=lambda h: 2 / 24)
    assert entscheiden(REGEL, START, START, kuehl, None).faktor == 0.5
    # zwei Tage seit der letzten Bewässerung, 2 mm Regen: (8 − 2) / 8
    p = normal(niederschlag_mm=lambda h: 1.0 if h in (-30, -40) else 0.0)
    e = entscheiden(REGEL, START, START, p, None, letzte_bewaesserung=START - timedelta(days=2))
    assert e.faktor == 0.75


def test_veraltete_daten_laufen_unveraendert():
    alt = normal()
    jetzt = START + timedelta(hours=13)
    e = entscheiden(REGEL, jetzt, jetzt, alt, None)
    assert e.faktor == 1.0 and e.veraltet and "veraltet" in e.text


def test_fehlende_et0_aendert_nichts():
    e = entscheiden(REGEL, START, START, normal(et0_mm=lambda h: None), None)
    assert e.faktor == 1.0 and "ET₀ fehlt" in e.text


def test_regel_aus_und_ungueltige_regel():
    assert entscheiden(WetterRegel(aktiv=False), START, START, None, None).faktor == 1.0
    assert WetterRegel(faktor_min=0).fehler()
    assert WetterRegel(sensor_id="s").fehler()


# --- Parser --------------------------------------------------------------------

def test_open_meteo_lesen():
    t0 = int(START.timestamp())
    daten = {"hourly": {"time": [t0, t0 + 3600], "precipitation": [0.2, None],
                        "precipitation_probability": [30, 40], "temperature_2m": [12.5, 13],
                        "et0_fao_evapotranspiration": [0.1, 0.12]}}
    r = open_meteo_lesen(daten, START)
    assert r.stunden[0] == Stunde(START, 0.2, 30.0, 12.5, 0.1)
    assert r.stunden[1].niederschlag_mm is None and r.stunden[1].zeit == START + timedelta(hours=1)


def test_bright_sky_nur_messwerte():
    daten = {
        "weather": [
            {"timestamp": "2026-06-01T05:00:00+02:00", "source_id": 1, "precipitation": 1.2, "temperature": 11},
            {"timestamp": "2026-06-01T06:00:00+02:00", "source_id": 2, "precipitation": 3.0, "temperature": 12},
        ],
        "sources": [{"id": 1, "observation_type": "synop"}, {"id": 2, "observation_type": "forecast"}],
    }
    r = bright_sky_lesen(daten, START)
    assert [(s.zeit, s.niederschlag_mm) for s in r.stunden] == [(START - timedelta(hours=1), 1.2)]


# --- Dienst, Scheduler, API ------------------------------------------------------

class FesteQuelle:
    def __init__(self, name, reihe=None, fehler=None):
        self.name, self.reihe, self.fehler, self.aufrufe = name, reihe, fehler, []

    async def abrufen(self, breite, laenge, jetzt):
        self.aufrufe.append((breite, laenge))
        if self.fehler:
            raise self.fehler
        return self.reihe


def mit_wetter(u: Umgebung, prognose: Reihe, messung: Reihe | None = None) -> WetterDienst:
    u.db.standort_setzen(51.1, 13.6)
    w = WetterDienst(u.db, u.a.zustand, FesteQuelle("Open-Meteo", prognose),
                     FesteQuelle("Bright Sky", messung), uhr=lambda: u.jetzt)
    u.s.wetter = w
    return w


def test_simulierter_regentag_setzt_lauf_aus():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        w = mit_wetter(u, normal(), regen_gemessen(2.0, 4))
        await w.aktualisieren()
        u.regel(u.z1)
        await u.s.takt()
        await u.s._aktuell
        assert u.a.gardena.befehle == []
        eintrag = u.db.protokoll()[0]
        assert eintrag.status == "UEBERSPRUNGEN_WETTER" and "Regen letzte 24 h: 8,0 mm" in eintrag.text
    ausfuehren(szenario())


def test_ohne_standort_und_bei_quellenfehler_laeuft_der_plan():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        w = WetterDienst(u.db, u.a.zustand, FesteQuelle("Open-Meteo", fehler=httpx.ConnectError("weg")),
                         uhr=lambda: u.jetzt)
        u.s.wetter = w
        await w.aktualisieren()
        assert w.prognosequelle.aufrufe == []                 # ohne Standort kein Abruf
        u.db.standort_setzen(51.1, 13.6)
        await w.aktualisieren()
        assert "Open-Meteo" in w.fehler and w.lage().veraltet
        u.regel(u.z1)
        await u.s.takt()
        await u.warten_auf_steckdose()
        assert u.a.gardena.starts()[V1] == 600
        await u.lauf_beenden()
        assert "Wetterdaten veraltet" in u.db.protokoll()[0].text
    ausfuehren(szenario())


def test_hochskalieren_endet_an_der_hoechstlaufzeit():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        w = mit_wetter(u, normal(et0_mm=lambda h: 8 / 24))
        await w.aktualisieren()
        u.regel(u.z1, dauer_min=25)                          # × 1,5 = 37,5 min > 30 min
        await u.s.takt()
        await u.warten_auf_steckdose()
        assert u.a.gardena.starts()[V1] == 1800
        await u.lauf_beenden()
        assert "Höchstlaufzeit" in u.db.protokoll()[0].text
    ausfuehren(szenario())


def test_bodensensor_aus_dem_websocket():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        u.a.zustand.anwenden({"id": "sensor-1:1", "type": "SENSOR",
                              "attributes": {"soilHumidity": {"value": 55}}})
        w = mit_wetter(u, normal())
        await w.aktualisieren()
        u.db.wetterregel_setzen(WetterRegel(zone_id=u.z1.id, bodenfeuchte_schwelle_pct=50, sensor_id="sensor-1:1"))
        e = w.entscheidung(u.z1.id, START)
        assert e.aussetzen and "Boden feucht" in e.text
        assert w.entscheidung(u.z2.id, START).faktor == 1.0  # z2 nutzt die globale Regel
    ausfuehren(szenario())


TOKEN = "t" * 32
KOPF = {"Authorization": f"Bearer {TOKEN}"}


def test_api_wetterregeln_standort_und_kalender():
    async def szenario():
        u = Umgebung(JETZT - timedelta(hours=1))
        w = mit_wetter(u, normal(niederschlag_mm=lambda h: 1.0 if 0 < h <= 12 else 0.0,
                                 wahrscheinlichkeit_pct=lambda h: 90))
        u.jetzt = START - timedelta(hours=1)
        await w.aktualisieren()
        u.regel(u.z1)
        app = app_bauen(Kontext(u.db, u.a.zustand, u.a.orchestrator, u.s, TOKEN, wetter=w))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pi") as c:
            regeln = (await c.get("/wetterregeln", headers=KOPF)).json()
            assert regeln[0]["zone_id"] is None and regeln[0]["regen_schwelle_mm"] == 5.0
            r = await c.put("/wetterregeln", json={"zone_id": u.z1.id, "faktor_min": 0}, headers=KOPF)
            assert r.status_code == 422
            r = await c.put("/wetterregeln", json={"zone_id": u.z1.id, "prognose_schwelle_mm": 20}, headers=KOPF)
            assert r.status_code == 200
            plan = (await c.get("/plan", params={"von": "2026-06-01", "bis": "2026-06-01"}, headers=KOPF)).json()
            assert plan[0]["wetter_faktor"] == 1.0                     # 12 mm < 20 mm Zonen-Schwelle
            assert (await c.delete(f"/wetterregeln/zone/{u.z1.id}", headers=KOPF)).status_code == 204
            plan = (await c.get("/plan", params={"von": "2026-06-01", "bis": "2026-06-01"}, headers=KOPF)).json()
            assert plan[0]["wetter_faktor"] == 0.0 and "Regen erwartet" in plan[0]["wetter_hinweis"]
            r = await c.put("/einstellungen/standort", json={"breite": 51.05, "laenge": 13.74}, headers=KOPF)
            assert r.status_code == 200
            assert (await c.get("/einstellungen/standort", headers=KOPF)).json() == {"breite": 51.05, "laenge": 13.74}
            assert (await c.put("/einstellungen/standort", json={"breite": 99, "laenge": 0}, headers=KOPF)).status_code == 422
            lage = (await c.get("/wetter", headers=KOPF)).json()
            assert lage["standort_gesetzt"] and not lage["veraltet"] and lage["regen_prognose_12h_mm"] == 11.0
            assert (await c.get("/status", headers=KOPF)).json()["wetter"]["standort_gesetzt"]
    ausfuehren(szenario())


def test_migration_von_schema_1(tmp_path):
    import sqlite3
    from bewaesserung.speicher.datenbank import SCHEMA, Speicher
    pfad = tmp_path / "alt.sqlite3"
    db = sqlite3.connect(pfad)
    db.executescript(SCHEMA + "PRAGMA user_version = 1;")
    db.execute("INSERT INTO zone (name, ventil_id) VALUES ('Rasen', 'v')")
    db.commit()
    db.close()
    s = Speicher(pfad)
    assert [z.name for z in s.zonen()] == ["Rasen"]
    assert s.wetterregel_fuer(1) == WetterRegel()
    s.wetterregel_setzen(WetterRegel(regen_schwelle_mm=3))
    s.wetterregel_setzen(WetterRegel(regen_schwelle_mm=4))            # ersetzt die globale Zeile
    assert [r.regen_schwelle_mm for r in s.wetterregeln()] == [4]
    s.schliessen()
