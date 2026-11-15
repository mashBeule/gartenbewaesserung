"""Dienst-Einstieg: Konfiguration, Kopplungsdatei, sd_notify, Wiederholung der Startprüfung."""
from __future__ import annotations

import asyncio
import json

import pytest

from bewaesserung.dienst import betreiben, startpruefung_bis_bestanden
from bewaesserung.konfiguration import Konfiguration, KonfigurationFehler, kopplungen_laden
from bewaesserung.systemd import SystemdMelder
from conftest import Aufbau, ausfuehren


def test_konfiguration_standard_ist_trockenlauf(tmp_path):
    k = Konfiguration.aus_umgebung({"BEWAESSERUNG_DATEN": str(tmp_path)})
    assert k.trockenlauf and not k.zugang_vollstaendig
    k = Konfiguration.aus_umgebung({"GARDENA_APPLICATION_KEY": "a", "GARDENA_APPLICATION_SECRET": "b",
                                    "GARDENA_DRY_RUN": "false"})
    assert k.zugang_vollstaendig and not k.trockenlauf


def test_kopplungen_laden(tmp_path):
    datei = tmp_path / "kopplungen.json"
    assert kopplungen_laden(datei) == []
    eintrag = {"id": "rasen", "ventil_id": "v:1", "steckdose_id": "s:1",
               "vorlauf_ventil_s": 20, "nachlauf_ventil_s": 30, "max_laufzeit_s": 1800}
    datei.write_text(json.dumps([eintrag]), encoding="utf-8")
    assert kopplungen_laden(datei)[0].max_laufzeit_s == 1800
    for kaputt in ("{", json.dumps([{**eintrag, "extra": 1}]), json.dumps([eintrag, eintrag]), json.dumps({})):
        datei.write_text(kaputt, encoding="utf-8")
        with pytest.raises(KonfigurationFehler):
            kopplungen_laden(datei)


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
