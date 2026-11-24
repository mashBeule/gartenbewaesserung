"""Einrichtungsseite: Formularprüfung, zugang.env, Ablauf mit Tailscale – gegen ein Fake-System."""
from __future__ import annotations

import asyncio
import re

import httpx

from bewaesserung.einrichtung.logik import lesen, zugang_text
from bewaesserung.einrichtung.server import app_bauen
from bewaesserung.konfiguration import Konfiguration
from conftest import ausfuehren

GUT = {"gardena_key": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "gardena_secret": "geheim-12345678",
       "claude_key": "sk-ant-api03-abcdefgh", "tailscale_authkey": "", "breite": "51,05", "laenge": "13,74"}


# --- Logik -------------------------------------------------------------------

def test_lesen_prueft_felder_und_einschleusen():
    e, f = lesen(GUT)
    assert f == {} and e.breite == 51.05 and e.tailscale_authkey is None
    _, f = lesen({**GUT, "gardena_secret": "abc\nGARDENA_DRY_RUN=false"})
    assert "gardena_secret" in f
    _, f = lesen({**GUT, "gardena_key": ""})
    assert f["gardena_key"] == "Application Key fehlt"
    _, f = lesen({**GUT, "claude_key": "falsch-12345678", "tailscale_authkey": "abcdefghij", "breite": "99"})
    assert set(f) == {"claude_key", "tailscale_authkey", "standort"}


def test_zugang_text_bleibt_im_trockenlauf_und_lokal():
    e, _ = lesen(GUT)
    text = zugang_text(e, "t" * 48)
    assert "GARDENA_DRY_RUN=true" in text and "BEWAESSERUNG_API_HOST=127.0.0.1" in text
    assert "ANTHROPIC_API_KEY=sk-ant-api03-abcdefgh" in text and "BEWAESSERUNG_STANDORT=51.0500,13.7400" in text
    assert "100.64.1.2" in zugang_text(e, "t" * 48, "100.64.1.2")
    for schlecht in ("0.0.0.0", "evil\nX=1"):
        try:
            zugang_text(e, "t" * 48, schlecht)
            raise AssertionError(schlecht)
        except ValueError:
            pass
    # was die Einrichtung schreibt, versteht die Konfiguration
    env = dict(z.split("=", 1) for z in text.splitlines() if z and not z.startswith("#"))
    k = Konfiguration.aus_umgebung(env)
    assert k.zugang_vollstaendig and k.trockenlauf and k.api_aktiv and k.standort_start == (51.05, 13.74)
    assert k.claude_api_key == "sk-ant-api03-abcdefgh"


# --- Seite -------------------------------------------------------------------

class FakeSystem:
    def __init__(self, gardena_fehler=None, ip="100.64.1.2", link="https://login.tailscale.com/a/abc123"):
        self.gardena_fehler, self.ip, self.link = gardena_fehler, ip, link
        self.dateien: list[str] = []
        self.neustarts = 0
        self.tailscale_frei = asyncio.Event()
        self.authkey = "unset"

    async def gardena_pruefen(self, key, secret):
        return self.gardena_fehler

    def zugang_vorhanden(self):
        return bool(self.dateien)

    def zugang_schreiben(self, text):
        self.dateien.append(text)

    async def tailscale_verbinden(self, authkey, link_melden):
        self.authkey = authkey
        link_melden(self.link)
        await self.tailscale_frei.wait()
        return self.ip

    async def dienst_neustarten(self):
        self.neustarts += 1


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bewaesserung.local")


def test_ablauf_mit_tailscale_link_und_token_nur_fuer_absender():
    async def szenario():
        system, beendet = FakeSystem(), []
        app = app_bauen(system, lambda: beendet.append(True))
        csrf = app.state.csrf
        async with client(app) as c, client(app) as fremd:
            seite = (await c.get("/")).text
            assert 'name="csrf"' in seite and "Application Key" in seite
            assert (await c.post("/einrichten", data={**GUT, "csrf": "falsch"})).status_code == 403
            r = await c.post("/einrichten", data={**GUT, "csrf": csrf})
            assert r.status_code == 303 and r.headers["location"] == "/fertig"
            assert len(system.dateien) == 1 and "API_HOST=127.0.0.1" in system.dateien[0] and system.neustarts == 1
            await asyncio.sleep(0.01)
            stand = (await c.get("/stand")).json()
            assert stand["phase"] == "TAILSCALE" and stand["login_link"].startswith("https://login.tailscale.com/")
            token = app.state.stand.token
            assert re.fullmatch(r"[0-9a-f]{48}", token)
            assert token in (await c.get("/fertig")).text
            assert token not in (await fremd.get("/fertig")).text         # anderer Browser sieht es nicht
            assert (await c.post("/abschliessen", data={"csrf": csrf})).status_code == 409   # Tailscale läuft noch
            system.tailscale_frei.set()
            await asyncio.sleep(0.01)
            assert (await c.get("/stand")).json()["tailscale_ip"] == "100.64.1.2"
            assert "API_HOST=100.64.1.2" in system.dateien[-1] and system.neustarts == 2
            assert (await c.post("/einrichten", data={**GUT, "csrf": csrf})).status_code == 409  # nur einmal
            assert (await c.post("/abschliessen", data={"csrf": csrf})).status_code == 200
            await asyncio.sleep(1.1)
            assert beendet == [True]
    ausfuehren(szenario())


def test_falscher_gardena_zugang_speichert_nichts_und_zeigt_kein_secret():
    async def szenario():
        system = FakeSystem(gardena_fehler="Husqvarna lehnt Application Key oder Secret ab.")
        app = app_bauen(system)
        async with client(app) as c:
            r = await c.post("/einrichten", data={**GUT, "csrf": app.state.csrf})
            assert r.status_code == 422 and "lehnt" in r.text
            assert GUT["gardena_secret"] not in r.text and GUT["gardena_key"] in r.text
            assert system.dateien == [] and system.neustarts == 0
            r = await c.post("/einrichten", data={**GUT, "gardena_key": "", "csrf": app.state.csrf})
            assert "Application Key fehlt" in r.text
    ausfuehren(szenario())


def test_ohne_tailscale_bleibt_api_lokal():
    async def szenario():
        system = FakeSystem(ip=None)
        system.tailscale_frei.set()
        app = app_bauen(system)
        async with client(app) as c:
            await c.post("/einrichten", data={**GUT, "tailscale_authkey": "tskey-auth-abcdef123", "csrf": app.state.csrf})
            await asyncio.sleep(0.01)
            stand = (await c.get("/stand")).json()
            assert stand["phase"] == "FERTIG" and stand["tailscale_fehler"] and stand["tailscale_ip"] is None
            assert system.authkey == "tskey-auth-abcdef123"
            assert len(system.dateien) == 1 and "API_HOST=127.0.0.1" in system.dateien[0]
    ausfuehren(szenario())


def test_schon_eingerichtet():
    async def szenario():
        system = FakeSystem()
        system.dateien.append("alt")
        app = app_bauen(system)
        async with client(app) as c:
            assert "Schon eingerichtet" in (await c.get("/")).text
            assert (await c.post("/einrichten", data={**GUT, "csrf": app.state.csrf})).status_code == 409
    ausfuehren(szenario())

