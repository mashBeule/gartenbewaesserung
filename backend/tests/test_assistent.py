"""Claude-Assistent: Prüfung, zweiter Versuch, Übernehmen, API – alles mit einem Fake statt der Claude API."""
from __future__ import annotations

import ast
import asyncio
from datetime import date, time
from pathlib import Path
from types import SimpleNamespace

import httpx

from bewaesserung.api.app import Kontext, app_bauen
from bewaesserung.assistent import claude as claude_modul
from bewaesserung.assistent.claude import Ablehnung, ClaudeEntwerfer
from bewaesserung.assistent.dienst import Assistent, EntwurfNichtMoeglich
from bewaesserung.assistent.entwurf import SCHEMA
from conftest import ausfuehren
from test_scheduler import Umgebung

TOKEN = "t" * 32
KOPF = {"Authorization": f"Bearer {TOKEN}"}


def regel(start="06:00", dauer=20, tage=("Mo", "Mi", "Fr"), phase="HOCHSOMMER", von="2027-06-01", bis="2027-08-31"):
    return {"phase": phase, "wochentage": list(tage), "startzeit": start, "dauer_min": dauer,
            "gueltig_von": von, "gueltig_bis": bis}


def entwurf(*zonen) -> dict:
    return {"zusammenfassung": "Test", "zonen": [
        {"zone_id": zid, "begruendung": "weil", "regeln": regeln} for zid, regeln in zonen]}


class FakeEntwerfer:
    modell = "fake"

    def __init__(self, *antworten):
        self.antworten = list(antworten)
        self.aufrufe: list[tuple[str, str, list | None]] = []
        self.sperre: asyncio.Event | None = None          # hält den Aufruf an, bis gesetzt


    async def entwerfen(self, kontext, wuensche, fehler=None):
        self.aufrufe.append((kontext, wuensche, fehler))
        if self.sperre is not None:
            await self.sperre.wait()
        antwort = self.antworten.pop(0)
        if isinstance(antwort, Exception):
            raise antwort
        return antwort


def assistent(u: Umgebung, *antworten) -> Assistent:
    return Assistent(u.db, FakeEntwerfer(*antworten), uhr=lambda: u.jetzt)


# --- Prüfung -----------------------------------------------------------------

def test_pruefung_findet_alle_fehlerarten():
    u = Umgebung()
    a = assistent(u)
    p = a.pruefen(entwurf(
        (u.z1.id, [regel(dauer=45), regel(start="13:00"), regel(start="06:10", dauer=10, phase="WINTER")]),
        (99, [regel()]),
    ))
    text = " | ".join(p.fehler)
    assert "Höchstlaufzeit" in text and "Zeitfenster" in text and "WINTER" in text and "Zone 99" in text
    p = a.pruefen(entwurf((u.z1.id, [regel()]), (u.z2.id, [regel(start="06:15")])))
    assert any("Überschneidung" in f for f in p.fehler)
    p = a.pruefen(entwurf((u.z1.id, [regel(dauer=30), regel(start="18:00", dauer=30)]),
                          (u.z2.id, [regel(start="07:00", dauer=30), regel(start="19:00", dauer=30)])),)
    assert p.ok                                   # 120 min am Tag sind erlaubt
    from bewaesserung.assistent.entwurf import Grenzen
    a.grenzen = Grenzen(max_laufzeit_tag_min=100)
    assert "erlaubt sind 100 min" in a.pruefen(entwurf((u.z1.id, [regel(dauer=30), regel(start="18:00", dauer=30)]),
                                                       (u.z2.id, [regel(start="07:00", dauer=30), regel(start="19:00", dauer=30)]))).fehler[0]


def test_zone_ohne_kopplung_bekommt_keine_regeln():
    u = Umgebung()
    u.a.gardena.ventil("ventil-3:1", "Beet")
    z3 = u.db.zone_anlegen(name="Beet", ventil_id="ventil-3:1")
    p = assistent(u).pruefen(entwurf((z3.id, [regel()])))
    assert "keine Kopplung" in p.fehler[0]


# --- Erstellen ---------------------------------------------------------------

def test_zweiter_versuch_mit_fehlerliste():
    async def szenario():
        u = Umgebung()
        u.db.standort_setzen(51.05, 13.74)
        a = assistent(u, entwurf((u.z1.id, [regel(dauer=45)])), entwurf((u.z1.id, [regel(dauer=25)])))
        e = await a.erstellen(u.db.entwurf_anlegen(2027, "Rasen neu").id)
        assert e.status == "OFFEN" and e.fehler == [] and e.modell == "fake"
        (kontext, wuensche, fehler1), (_, _, fehler2) = a.entwerfer.aufrufe
        assert fehler1 is None and "Höchstlaufzeit" in fehler2[0] and wuensche == "Rasen neu"
        assert "zone_id 1: Rasen" in kontext and "Höchstlaufzeit 30 min" in kontext and "51.0° N" in kontext
    ausfuehren(szenario())


def test_nach_zwei_fehlversuchen_fehlerhaft_und_ablehnung():
    async def szenario():
        u = Umgebung()
        a = assistent(u, entwurf((99, [])), entwurf((98, [])), Ablehnung("Claude hat den Entwurf abgelehnt"))
        e = await a.erstellen(u.db.entwurf_anlegen(2027, "").id)
        assert e.status == "FEHLERHAFT" and "Zone 98" in e.fehler[0]
        e = await a.erstellen(u.db.entwurf_anlegen(2027, "").id)
        assert e.status == "FEHLERHAFT" and e.fehler == ["Claude hat den Entwurf abgelehnt"]
    ausfuehren(szenario())


def test_haengender_entwurf_wird_nach_neustart_fehlerhaft():
    u = Umgebung()
    e = u.db.entwurf_anlegen(2027, "")
    assistent(u)
    assert u.db.entwurf(e.id).status == "FEHLERHAFT"


# --- Übernehmen --------------------------------------------------------------

def test_uebernehmen_je_zone_deaktiviert_alte_regeln():
    async def szenario():
        u = Umgebung()
        alt1, alt2 = u.regel(u.z1), u.regel(u.z2, start=time(7, 0))
        a = assistent(u, entwurf((u.z1.id, [regel(start="05:00")]), (u.z2.id, [regel(start="05:30")])))
        e = await a.erstellen(u.db.entwurf_anlegen(2027, "").id)
        termine = a.plan(e.id, date(2027, 6, 1), date(2027, 6, 7))
        assert all(l.regel_id < 0 for l in termine)
        assert [l.start.strftime("%d. %H:%M") for l in termine] == [
            "02. 05:00", "02. 05:30", "04. 05:00", "04. 05:30", "07. 05:00", "07. 05:30"]
        ids = a.uebernehmen(e.id, [u.z1.id])
        assert not u.db.regel(alt1.id).aktiv and u.db.regel(alt2.id).aktiv
        neu = u.db.regel(ids[0])
        assert neu.quelle.value == "CLAUDE" and neu.startzeit == time(5, 0) and neu.wochentage == 0b10101
        assert u.db.entwurf(e.id).status == "UEBERNOMMEN" and u.db.entwurf(e.id).uebernommen == [u.z1.id]
        try:
            a.uebernehmen(e.id)
            raise AssertionError("zweimal übernommen")
        except EntwurfNichtMoeglich:
            pass
    ausfuehren(szenario())


def test_uebernehmen_prueft_gegen_bleibende_regeln_und_neue_grenzen():
    async def szenario():
        u = Umgebung()
        u.regel(u.z2, start=time(6, 10))                    # bleibt: Hecke täglich 06:10
        a = assistent(u, entwurf((u.z1.id, [regel(start="06:00")])))
        e = await a.erstellen(u.db.entwurf_anlegen(2027, "").id)
        assert e.status == "OFFEN"
        try:
            a.uebernehmen(e.id, [u.z1.id])
            raise AssertionError("Überschneidung nicht erkannt")
        except EntwurfNichtMoeglich as f:
            assert "Überschneidung" in str(f)
        u.db.kopplung_aendern(1, max_laufzeit_s=600)        # Höchstlaufzeit inzwischen gesenkt
        try:
            a.uebernehmen(e.id, [u.z1.id])
            raise AssertionError("Regel 8 nicht erneut geprüft")
        except EntwurfNichtMoeglich as f:
            assert "Höchstlaufzeit" in str(f)
        assert all(r.quelle.value == "MANUELL" for r in u.db.regeln())
    ausfuehren(szenario())


# --- API ---------------------------------------------------------------------

def test_api_ablauf():
    async def szenario():
        u = Umgebung()
        a = assistent(u, entwurf((u.z1.id, [regel(start="05:00")])))
        a.entwerfer.sperre = asyncio.Event()
        app = app_bauen(Kontext(u.db, u.a.zustand, u.a.orchestrator, u.s, TOKEN, assistent=a))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pi") as c:
            r = await c.post("/entwuerfe", json={"wuensche": "Urlaub im Juli", "saison": 2027}, headers=KOPF)
            assert r.status_code == 202 and r.json()["status"] == "IN_ARBEIT"
            eid = r.json()["id"]
            assert (await c.post("/entwuerfe", json={}, headers=KOPF)).status_code == 409   # läuft schon
            a.entwerfer.sperre.set()
            for _ in range(50):
                if (await c.get(f"/entwuerfe/{eid}", headers=KOPF)).json()["status"] != "IN_ARBEIT":
                    break
                await asyncio.sleep(0.01)
            assert (await c.get(f"/entwuerfe/{eid}", headers=KOPF)).json()["status"] == "OFFEN"
            plan = (await c.get(f"/entwuerfe/{eid}/plan", params={"von": "2027-06-01", "bis": "2027-06-07"}, headers=KOPF)).json()
            assert len(plan) == 3 and all(l["regel_id"] < 0 for l in plan)
            r = await c.post(f"/entwuerfe/{eid}/uebernehmen", json={"zone_ids": [u.z2.id]}, headers=KOPF)
            assert r.status_code == 409
            r = await c.post(f"/entwuerfe/{eid}/uebernehmen", json={}, headers=KOPF)
            assert r.status_code == 200 and len(r.json()["regel_ids"]) == 1
            assert (await c.post(f"/entwuerfe/{eid}/verwerfen", headers=KOPF)).status_code == 409
        app = app_bauen(Kontext(u.db, u.a.zustand, u.a.orchestrator, u.s, TOKEN, assistent=Assistent(u.db, None)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pi") as c:
            assert (await c.post("/entwuerfe", json={}, headers=KOPF)).status_code == 503
    ausfuehren(szenario())


# --- Claude-Aufruf und Abgrenzung --------------------------------------------

def test_claude_anfrage_ohne_werkzeuge_mit_schema_und_ausweichmodell():
    async def szenario():
        gesendet = {}

        async def create(**kw):
            gesendet.update(kw)
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text='{"zusammenfassung": "x", "zonen": []}')])

        e = ClaudeEntwerfer("sk-test")
        e.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
        assert await e.entwerfen("K", "W", ["F1"]) == {"zusammenfassung": "x", "zonen": []}
        assert gesendet["model"] == "claude-sonnet-5-5" and "tools" not in gesendet
        assert gesendet["output_config"]["format"]["schema"] is SCHEMA and gesendet["fallbacks"] == "default"
        assert "- F1" in gesendet["messages"][0]["content"]

        async def ablehnen(**kw):
            return SimpleNamespace(stop_reason="refusal", content=[])
        e.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=ablehnen)))
        try:
            await e.entwerfen("K", "W")
            raise AssertionError("Ablehnung nicht gemeldet")
        except Ablehnung:
            pass
    ausfuehren(szenario())


def test_assistent_importiert_nichts_was_schaltet():
    ordner = Path(claude_modul.__file__).parent
    verboten = ("orchestrator", "gardena.client", "interlock", "startpruefung", "scheduler")
    for datei in ordner.glob("*.py"):
        for knoten in ast.walk(ast.parse(datei.read_text(encoding="utf-8"))):
            if isinstance(knoten, ast.ImportFrom) and knoten.module:
                assert not any(v in knoten.module for v in verboten), f"{datei.name} importiert {knoten.module}"
