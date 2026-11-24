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
| `backend/src/bewaesserung/gardena/` | Gardena-Client, API-Budget, WebSocket, Gerätezustand |
| `backend/src/bewaesserung/sicherheit/` | Kopplung, Orchestrator, Interlock, Startprüfung |
| `backend/src/bewaesserung/planung/` | Regeln → Läufe (`plan.py`), Scheduler |
| `backend/src/bewaesserung/speicher/` | SQLite (Zonen, Kopplungen, Regeln, Ausnahmen, Wetterregeln, Einstellungen, run_log) |
| `backend/src/bewaesserung/wetter/` | Open-Meteo, Bright Sky, Entscheidung je Termin (`entscheidung.py`, reine Logik), Dienst mit stündlichem Abruf |
| `backend/src/bewaesserung/assistent/` | Claude-Assistent: Schema und Prüfung (`entwurf.py`, reine Logik), API-Aufruf (`claude.py`), Ablauf (`dienst.py`) – ohne Zugriff auf Geräte |
| `backend/src/bewaesserung/api/` | REST-API für die App (FastAPI, Token) |
| `backend/tests/` | pytest, Sicherheitstests laufen gegen den Gardena-Mock (`tests/gardena_mock.py`) |
| `deploy/systemd/` | `bewaesserung.service` (Type=notify, Restart=always, WatchdogSec=60), `bewaesserung-einrichtung.service` |
| `deploy/pi-gen/` | eigene Stufe `stage-bewaesserung`, vorher `vorbereiten.sh` ausführen |
| `backend/src/bewaesserung/einrichtung/` | Einrichtungsseite beim ersten Start (eigener Dienst als root, nur bis `zugang.env` existiert) |
| `.github/workflows/` | `build-image.yml` (Tests, pi-gen-Build, .img als Release-Asset bei Tag `v*`) |
| `android/` | später: Kotlin/Compose-App |

Details zum Image: `deploy/README.md`. Abweichungen vom Plan: `docs/entscheidungen.md`.

## Feste Entscheidungen

- Backend: Python, FastAPI, APScheduler, SQLite. Raspberry Pi OS Lite 64 Bit.
- **Fällt der Pi aus, wird NICHT bewässert.** Keine Ersatzpläne in der Gardena-App.
  Abgesichert ist nur, dass ein laufender Lauf von selbst endet und die Pumpe vor dem Ventil abschaltet.
- Zugangsdaten (Gardena Application Key/Secret, Claude-API-Key, Tailscale) **nie im Image
  oder Repo**; Eingabe über die Einrichtungsseite beim ersten Start.
- Claude API (Sonnet) erzeugt nur Saisonplan-Entwürfe mit festem JSON-Schema (structured outputs, keine Werkzeuge);
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
| 2, 8 | `sicherheit/kopplung.py` (`Kopplung.pruefen`, `steckdosen_laufzeit`); 8 zusätzlich beim Speichern in `api/app.py` |
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
- Der Zähler liegt in `<Datenordner>/api-budget.json` und übersteht Neustarts, damit eine
  Absturzschleife das Budget nicht leerläuft.

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
systemd-Unit, pi-gen-Stufe, GitHub-Actions-Workflow.
Aus Aufgabe 5 (Phase 2 des Plans): SQLite-Speicher, Zeitplan-Expansion mit Sommerzeit,
Scheduler mit Nachholfenster, Not-Aus, REST-API mit Token, Sicherheitsereignisse im run_log.
Wetter (Phase 4): Open-Meteo und Bright Sky stündlich, Bodensensor aus dem WebSocket, Regeln 1–5 des
Plans in `wetter/entscheidung.py`, `weather_policy` global und je Zone, Vorschau im Kalender,
Endpunkte `/wetter`, `/wetterregeln`, `/einstellungen/standort`.
Claude-Assistent (Phase 5): Entwurf mit festem Schema, Prüfung (Regel 8, Zeitfenster, Überschneidungen,
Tagessumme), ein zweiter Versuch, Kalenderansicht, Übernahme je Zone mit `quelle=CLAUDE`, Endpunkte `/entwuerfe`.
Ein Test stellt sicher, dass `assistent/` nichts importiert, was schaltet.
Einrichtungsseite (Phase 7): Gardena-Zugang prüfen, `zugang.env` schreiben (Trockenlauf bleibt an),
API-Token erzeugen, Tailscale anmelden, Standort übernehmen.

Als Nächstes (Rest von Aufgabe 5):
- Push-Meldungen (ntfy).
- Android-App (Phase 3), spricht die API unter `/openapi.json`.

In Phase 0 am echten System klären: siehe ⚠ in `docs/gardena-api.md`.

## Nicht im Umfang

Mähroboter, mehrere Nutzer, andere Hersteller, iPhone.
