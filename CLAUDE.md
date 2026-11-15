# Bewässerungssteuerung – Projektregeln für Claude Code

Raspberry Pi 3 als zentrale Steuerung für ein Gardena smart system (Ventile,
Smart Power Steckdose mit Pumpe, Bodensensor) über die offizielle GARDENA smart
system API (REST + WebSocket). Die Android-App ist nur Frontend über Tailscale.
Einziger Nutzer: der Projektinhaber.

Vollständiger Plan: `Erstellungsplan_Bewaesserung.md`. Übergabe: `uebergabe-claude-code.md`.
Gardena-API-Notizen: `docs/gardena-api.md`.

## Aufbau

| Ordner | Inhalt |
| --- | --- |
| `backend/` | Python-Paket `bewaesserung` (Gardena-Client, Orchestrator, Interlock, später Scheduler, REST-API, Wetter, Claude) |
| `backend/tests/` | pytest, Sicherheitstests laufen gegen den Gardena-Mock (`tests/gardena_mock.py`) |
| `deploy/systemd/` | `bewaesserung.service` (Type=notify, Restart=always, WatchdogSec=60) |
| `deploy/pi-gen/` | eigene Stufe `stage-bewaesserung`, vorher `vorbereiten.sh` ausführen |
| `deploy/firstboot/` | Einrichtungsseite für Zugangsdaten – noch leer |
| `.github/workflows/` | `build-image.yml` (Tests, pi-gen-Build, .img als Release-Asset bei Tag `v*`) |

Details zum Image: `deploy/README.md`.
| `android/` | später: Kotlin/Compose-App |

## Feste Entscheidungen

- Backend: Python, FastAPI, APScheduler, SQLite. Raspberry Pi OS Lite 64 Bit.
- **Fällt der Pi aus, wird NICHT bewässert.** Keine Ersatzpläne in der Gardena-App.
  Abgesichert ist nur, dass ein laufender Lauf von selbst endet und die Pumpe vor dem Ventil abschaltet.
- Zugangsdaten (Gardena Application Key/Secret, Claude-API-Key, Tailscale) **nie im Image
  oder Repo**; Eingabe über die Einrichtungsseite beim ersten Start.
- Claude API (Sonnet) erzeugt nur Saisonplan-Entwürfe per Tool-Aufruf mit JSON-Schema;
  Freigabe in der App; **Claude schaltet nie Geräte.**
- Wetter: Open-Meteo (Prognose, ET0), Bright Sky (DWD-Messwerte), Gardena-Bodensensor.
- Kein offener Port im Internet, Zugriff von unterwegs nur über Tailscale.

## Sicherheitsregeln (nicht verhandelbar)

1. Ventil und Steckdose nur mit `START_SECONDS_TO_OVERRIDE` + Sekundenwert schalten, nie unbegrenzt.
2. Laufzeit Steckdose = Laufzeit Ventil − Vorlauf − Nachlauf (Steckdose endet immer vor dem Ventil).
3. Ablauf: Ventil öffnen → Bestätigung per WebSocket abwarten (Timeout ~30 s) → Steckdose mit
   kürzerer Laufzeit starten → Bestätigung abwarten. Abbruch: immer Steckdose zuerst.
4. Nur gemeldeter Gerätezustand zählt, nie die API-Antwort auf einen Befehl.
5. Interlock-Wächter: Steckdose an + gekoppeltes Ventil nicht offen → sofort Steckdose stoppen,
   loggen, melden.
6. Beim Start des Dienstes: alle gekoppelten Steckdosen stoppen, alle Kopplungen prüfen,
   erst dann Scheduler starten.
7. Gardena-Zeitpläne aller Ventile und der Steckdose dauerhaft pausieren (beim Start + täglich
   erneuern). Meldet ein Gerät einen Zeitplan-Lauf (`SCHEDULED_WATERING`, `SCHEDULED_ON`)
   → sofort stoppen (Steckdose zuerst) und melden.
8. Absolute Höchstlaufzeit pro Kopplung; längere Anforderungen ablehnen (App und Claude-Entwurf).

Wo die Regeln im Code stehen:

| Regel | Ort |
| --- | --- |
| 1 | `gardena/client.py` kennt nur `starten(sekunden)`, kein `START_OVERRIDE` |
| 2, 8 | `sicherheit/kopplung.py` (`Kopplung.laufzeiten`, `steckdosen_laufzeit`) |
| 3, 4 | `sicherheit/orchestrator.py` (wartet auf `Geraetezustand`, nie auf die Befehlsantwort) |
| 5, 7 (Erkennen) | `sicherheit/interlock.py` |
| 6, 7 (Pausieren) | `sicherheit/startpruefung.py` |

Jede Änderung an diesen Dateien braucht einen Test in `backend/tests/test_sicherheit_*.py`.

## Gardena-API-Limit

Höchstens 700 REST-Aufrufe pro Woche und 10 in 10 Sekunden; bei HTTP 429 gilt das Konto als
24 Stunden gesperrt. Deshalb:

- Zustände kommen über die **WebSocket-Verbindung**, nie durch Polling.
- REST nur für Befehle, Token, Geräteliste und WebSocket-Adresse.
- Jeder REST-Aufruf geht durch `gardena/budget.py`. Sicherheits-Stopps haben Vorrang
  (Priorität `SICHERHEIT`), normale Läufe lassen eine Reserve übrig.

## Arbeitsweise

- Code-Bezeichner: Englisch für Technik, deutsche Fachbegriffe erlaubt (Ventil, Steckdose,
  Kopplung, Lauf). Texte für die App und Meldungen: Deutsch.
- Alles mit Netzwerk hängt hinter einer Schnittstelle (`Protocol`), Tests laufen mit Fakes.
- Fachlogik in `sicherheit/` hat kein Netzwerk und keine echte Uhr (Uhr und Schlafen werden übergeben).
- Einrichten: `cd backend && python -m venv .venv && .venv/Scripts/pip install -e .[dev]`
- Tests: `cd backend && .venv/Scripts/python -m pytest`
- Der Gardena-Client startet im **Trockenlauf** (`dry_run=True`): Befehle werden nur
  protokolliert. Gegen das echte Konto nur nach bewusstem `dry_run=False`.

## Stand und nächste Schritte

Fertig (Aufgaben 1–4 der Übergabe): Repo-Gerüst, Gardena-Client (Token, Standorte/Geräte,
Befehle, API-Budget, WebSocket mit Reconnect), Gerätezustand mit Wartefunktion,
Gardena-Mock, Orchestrator, Interlock-Wächter, Startprüfung mit Zeitplan-Pause und täglicher
Erneuerung, Dienst-Einstieg `python -m bewaesserung` (`dienst.py`, sd_notify-Watchdog),
systemd-Unit, pi-gen-Stufe, GitHub-Actions-Workflow. Der Image-Build ist noch nie gelaufen.

Als Nächstes:
5. Scheduler, SQLite (Datenmodell siehe Plan; ersetzt `kopplungen.json`), REST-API für die App,
   Einrichtungsseite (`deploy/firstboot`), Wetter, Claude-Assistent, Android-App.

In Phase 0 am echten System klären: siehe ⚠ in `docs/gardena-api.md`.

## Nicht im Umfang

Mähroboter, mehrere Nutzer, andere Hersteller, iPhone.
