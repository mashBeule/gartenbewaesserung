"""Dienst-Einstieg: Konfiguration, Budget-Datei, sd_notify, Wiederholung der Startprüfung."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from bewaesserung.dienst import betreiben, startpruefung_bis_bestanden
from bewaesserung.gardena.budget import ApiBudget, Prioritaet
from bewaesserung.konfiguration import Konfiguration
from bewaesserung.systemd import SystemdMelder
from conftest import Aufbau, ausfuehren


def test_konfiguration_standard_ist_trockenlauf_und_api_aus(tmp_path):
    k = Konfiguration.aus_umgebung({"BEWAESSERUNG_DATEN": str(tmp_path)})
    assert k.trockenlauf and not k.zugang_vollstaendig and not k.api_aktiv
    assert k.api_host == "127.0.0.1"
    k = Konfiguration.aus_umgebung({"GARDENA_APPLICATION_KEY": "a", "GARDENA_APPLICATION_SECRET": "b",
                                    "GARDENA_DRY_RUN": "false", "BEWAESSERUNG_API_TOKEN": "x" * 24})
    assert k.zugang_vollstaendig and not k.trockenlauf and k.api_aktiv


def test_api_nie_auf_allen_adressen_oder_mit_kurzem_token():
    assert not Konfiguration.aus_umgebung({"BEWAESSERUNG_API_TOKEN": "x" * 24, "BEWAESSERUNG_API_HOST": "0.0.0.0"}).api_aktiv
    assert not Konfiguration.aus_umgebung({"BEWAESSERUNG_API_TOKEN": "kurz"}).api_aktiv


def test_budget_ueberlebt_neustart(tmp_path):
    datei = tmp_path / "api-budget.json"
    jetzt = datetime.now(timezone.utc)
    b = ApiBudget.aus_datei(datei)
    for i in range(5):
        b.anfordern(Prioritaet.SYSTEM, jetzt + timedelta(seconds=i))
    b.gesperrt_melden(jetzt)
    neu = ApiBudget.aus_datei(datei)
    assert neu.verbraucht(jetzt + timedelta(seconds=10)) == 5
    assert neu.gesperrt_bis == jetzt + timedelta(hours=24)


def test_kaputte_budget_datei_rechnet_vorsichtig(tmp_path):
    datei = tmp_path / "api-budget.json"
    datei.write_text("{kaputt", encoding="utf-8")
    b = ApiBudget.aus_datei(datei)
    assert b.verbraucht(datetime.now(timezone.utc)) == 350


def test_systemd_melder_ohne_socket_tut_nichts():
    m = SystemdMelder(env={})
    assert not m.aktiv and m.watchdog_intervall_s is None
    m.bereit("x")
    assert SystemdMelder(env={"WATCHDOG_USEC": "60000000"}).watchdog_intervall_s == 30


def test_ohne_zugangsdaten_wird_nichts_geschaltet(tmp_path):
    class Melder(SystemdMelder):
        def __init__(self):
            super().__init__(env={})
            self.meldungen = []
        def bereit(self, status):
            self.meldungen.append(status)
    async def szenario():
        m = Melder()
        ende = asyncio.Event()
        lauf = asyncio.create_task(betreiben(Konfiguration.aus_umgebung({"BEWAESSERUNG_DATEN": str(tmp_path)}), m, ende))
        await asyncio.sleep(0.05)
        assert not lauf.done()                       # wartet, statt sich zu beenden
        assert "Zugangsdaten" in m.meldungen[0]
        ende.set()
        await lauf
        assert not (tmp_path / "bewaesserung.sqlite3").exists()
    ausfuehren(szenario())


def test_startpruefung_wird_mit_abstand_wiederholt(aufbau: Aufbau):
    async def szenario():
        geschlafen = []
        async def schlafen(s):
            geschlafen.append(s)
            if len(geschlafen) == 2:
                aufbau.gardena.verbinden()
        bericht = await startpruefung_bis_bestanden(aufbau.startpruefung, schlafen, (300.0, 1800.0))
        assert bericht.ok and aufbau.orchestrator.freigegeben
        assert geschlafen == [300.0, 1800.0]
    ausfuehren(szenario())
