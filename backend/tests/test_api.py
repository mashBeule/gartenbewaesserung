"""REST-API über ASGI im selben Ereignis-Loop, gegen Mock, SQLite im Speicher und gestellte Uhr."""
from __future__ import annotations

import httpx

from bewaesserung.api.app import Kontext, app_bauen
from conftest import S1, V1, ausfuehren
from test_scheduler import JETZT, Umgebung

TOKEN = "t" * 32
KOPF = {"Authorization": f"Bearer {TOKEN}"}
KOPPLUNG = {"ventil_id": V1, "steckdose_id": S1, "vorlauf_ventil_s": 20, "nachlauf_ventil_s": 30, "max_laufzeit_s": 1800}


def _client(u: Umgebung, token: str | None = TOKEN) -> httpx.AsyncClient:
    app = app_bauen(Kontext(u.db, u.a.zustand, u.a.orchestrator, u.s, token))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pi")


def _leer() -> Umgebung:
    """Umgebung ohne vorbereitete Zonen und Kopplungen."""
    u = Umgebung()
    for z in u.db.zonen():
        u.db.zone_loeschen(z.id)
    for k in u.db.kopplungen():
        u.db.kopplung_loeschen(int(k.id))
    u.a.kopplungen = u.db.kopplungen
    return u


def test_token_pflicht():
    async def szenario():
        u = _leer()
        async with _client(u) as c:
            assert (await c.get("/status")).status_code == 401
            assert (await c.get("/status", headers={"Authorization": "Bearer falsch"})).status_code == 401
            assert (await c.get("/status", headers=KOPF)).status_code == 200
        async with _client(u, token=None) as c:
            assert (await c.get("/status", headers=KOPF)).status_code == 503
    ausfuehren(szenario())


def test_zone_kopplung_regel_anlegen_und_plan_lesen():
    async def szenario():
        u = _leer()
        u.a.bereit()
        async with _client(u) as c:
            zone = (await c.post("/zonen", json={"name": "Rasen", "ventil_id": V1}, headers=KOPF)).json()
            r = await c.post("/regeln", json={"zone_id": zone["id"], "wochentage": 1, "startzeit": "06:00", "dauer_min": 10}, headers=KOPF)
            assert r.status_code == 422 and "keine Kopplung" in r.json()["detail"]
            assert (await c.post("/kopplungen", json=KOPPLUNG, headers=KOPF)).status_code == 201
            r = await c.post("/regeln", json={"zone_id": zone["id"], "wochentage": 1, "startzeit": "06:00", "dauer_min": 10}, headers=KOPF)
            assert r.status_code == 201, r.text
            plan = (await c.get("/plan", params={"von": "2026-06-01", "bis": "2026-06-14"}, headers=KOPF)).json()
            assert [l["start"][:16] for l in plan] == ["2026-06-01T06:00", "2026-06-08T06:00"]
            zonen = (await c.get("/zonen", headers=KOPF)).json()
            assert zonen[0]["kopplung_id"] is not None
    ausfuehren(szenario())


def test_regel_8_hoechstlaufzeit_beim_speichern():
    async def szenario():
        u = Umgebung()
        async with _client(u) as c:
            r = await c.post("/regeln", json={"zone_id": u.z1.id, "wochentage": 1, "startzeit": "06:00", "dauer_min": 31}, headers=KOPF)
            assert r.status_code == 422 and "Höchstlaufzeit" in r.json()["detail"]
            regel = (await c.post("/regeln", json={"zone_id": u.z1.id, "wochentage": 1, "startzeit": "06:00", "dauer_min": 25}, headers=KOPF)).json()
            # Höchstlaufzeit der Kopplung unter eine bestehende Regel senken: abgelehnt
            r = await c.put("/kopplungen/1", json={**KOPPLUNG, "max_laufzeit_s": 1200}, headers=KOPF)
            assert r.status_code == 422 and f"Regel {regel['id']}" in r.json()["detail"]
            r = await c.post("/ausnahmen", json={"regel_id": regel["id"], "datum": "2026-06-01",
                                                 "aktion": "DAUER_ANPASSEN", "wert": "45"}, headers=KOPF)
            assert r.status_code == 422
            r = await c.post("/laeufe", json={"zone_id": u.z1.id, "dauer_min": 60}, headers=KOPF)
            assert r.status_code in (409, 422)
    ausfuehren(szenario())


def test_ungueltige_kopplung_und_vertauschte_geraete():
    async def szenario():
        u = _leer()
        u.a.bereit()
        async with _client(u) as c:
            r = await c.post("/kopplungen", json={**KOPPLUNG, "nachlauf_ventil_s": 0}, headers=KOPF)
            assert r.status_code == 422 and "Nachlauf" in r.json()["detail"]
            r = await c.post("/kopplungen", json={**KOPPLUNG, "ventil_id": S1, "steckdose_id": V1}, headers=KOPF)
            assert r.status_code == 422 and "erwartet VENTIL" in r.json()["detail"]
            assert (await c.post("/kopplungen", json=KOPPLUNG, headers=KOPF)).status_code == 201
            assert (await c.post("/kopplungen", json=KOPPLUNG, headers=KOPF)).status_code == 409   # Ventil doppelt
    ausfuehren(szenario())


def test_ausnahme_ersetzt_termin():
    async def szenario():
        u = Umgebung()
        regel = u.regel(u.z1)
        async with _client(u) as c:
            for aktion, wert in (("AUSLASSEN", None), ("VERSCHIEBEN", "07:30")):
                r = await c.post("/ausnahmen", json={"regel_id": regel.id, "datum": "2026-06-02", "aktion": aktion, "wert": wert}, headers=KOPF)
                assert r.status_code == 201, r.text
            assert len((await c.get("/ausnahmen", headers=KOPF)).json()) == 1
            plan = (await c.get("/plan", params={"von": "2026-06-02", "bis": "2026-06-02"}, headers=KOPF)).json()
            assert plan[0]["start"][11:16] == "07:30" and not plan[0]["ausgelassen"]
            r = await c.post("/ausnahmen", json={"regel_id": regel.id, "datum": "2026-06-03", "aktion": "VERSCHIEBEN", "wert": "bald"}, headers=KOPF)
            assert r.status_code == 422
    ausfuehren(szenario())


def test_manueller_lauf_status_und_not_aus():
    async def szenario():
        u = Umgebung()
        u.a.bereit()
        async with _client(u) as c:
            r = await c.post("/laeufe", json={"zone_id": u.z1.id, "dauer_min": 5}, headers=KOPF)
            assert r.status_code == 202, r.text
            await u.warten_auf_steckdose()
            st = (await c.get("/status", headers=KOPF)).json()
            assert st["verbunden"] and st["freigegeben"] and st["aktive_laeufe"] == ["1"] and st["trockenlauf"]
            assert (await c.post("/laeufe", json={"zone_id": u.z2.id, "dauer_min": 5}, headers=KOPF)).status_code == 409
            assert (await c.post("/notaus", headers=KOPF)).status_code == 200
            await u.s._aktuell
            assert u.a.gardena.folge()[2:4] == [("STOP", S1), ("STOP", V1)]
            verlauf = (await c.get("/verlauf", headers=KOPF)).json()
            assert any(e["grund"] == "MANUELL" and e["status"] == "ABGEBROCHEN" for e in verlauf)
            geraete = (await c.get("/geraete", headers=KOPF)).json()
            assert {g["id"] for g in geraete} == {V1, S1, "ventil-2:1"}
    ausfuehren(szenario())


def test_sicherheitsereignisse_landen_im_verlauf():
    async def szenario():
        from bewaesserung.speicher.ereignisse import SpeicherEreignisse
        u = Umgebung()
        u.a.interlock.ereignisse = SpeicherEreignisse(u.db)
        u.a.bereit()
        u.a.gardena.setzen(S1, "FOREVER_ON")
        await u.a.interlock.abwarten()
        async with _client(u) as c:
            e = (await c.get("/verlauf", params={"nur_sicherheit": True}, headers=KOPF)).json()
            assert [x["sicherheitsereignis"] for x in e] == ["INTERLOCK"]
    ausfuehren(szenario())


def test_plan_zeitraum_begrenzt():
    async def szenario():
        async with _client(Umgebung()) as c:
            r = await c.get("/plan", params={"von": "2026-06-01", "bis": "2026-12-01"}, headers=KOPF)
            assert r.status_code == 422
    ausfuehren(szenario())


assert JETZT  # gleiche Uhr wie im Scheduler-Test
