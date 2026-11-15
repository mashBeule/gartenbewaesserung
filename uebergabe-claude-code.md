# Übergabe an Claude Code: Bewässerungssteuerung (Gardena + Raspberry Pi 3)

Stand: 2026-11-14. Vollständiger Plan: https://claude.ai/artifact/MxNnzhehyom4mHBMtro89R

## Ziel
Raspberry Pi 3 als zentrale Steuerung für ein Gardena smart system (Ventile, Smart Power Steckdose mit Pumpe, Bodensensor) über die offizielle GARDENA smart system API (Husqvarna-Cloud, REST + WebSocket). Android-App (Kotlin/Compose) ist nur Frontend über Tailscale. Auslieferung als fertiges Pi-Image (pi-gen, Build per GitHub Actions), Anwendung startet automatisch per systemd.

## Feste Entscheidungen
- Backend: Python, FastAPI, APScheduler, SQLite. Raspberry Pi OS Lite 64 Bit.
- Fällt der Pi aus, wird NICHT bewässert. Keine Ersatzpläne in der Gardena-App.
- Zugangsdaten (Gardena Application Key/Secret, Claude-API-Key, Tailscale) nie im Image oder Repo; Eingabe über Einrichtungsseite beim ersten Start.
- Claude API (Sonnet) erzeugt nur Saisonplan-Entwürfe per Tool-Aufruf mit JSON-Schema; Freigabe in der App; Claude schaltet nie Geräte.
- Wetter: Open-Meteo (Prognose, ET0), Bright Sky (DWD-Messwerte), Gardena-Bodensensor.

## Sicherheitsregeln (nicht verhandelbar)
1. Ventil und Steckdose nur mit `START_SECONDS_TO_OVERRIDE` + Sekundenwert schalten, nie unbegrenzt.
2. Laufzeit Steckdose = Laufzeit Ventil − Vorlauf − Nachlauf (Steckdose endet immer vor dem Ventil).
3. Ablauf: Ventil öffnen → Bestätigung per WebSocket abwarten (Timeout ~30 s) → Steckdose mit kürzerer Laufzeit starten → Bestätigung abwarten. Abbruch: immer Steckdose zuerst.
4. Nur gemeldeter Gerätezustand zählt, nie die API-Antwort auf einen Befehl.
5. Interlock-Wächter: Steckdose an + gekoppeltes Ventil nicht offen → sofort Steckdose stoppen, loggen, melden.
6. Beim Start des Dienstes: alle gekoppelten Steckdosen stoppen, alle Kopplungen prüfen, erst dann Scheduler starten.
7. Gardena-Zeitpläne aller Ventile und der Steckdose dauerhaft pausieren (beim Start + täglich erneuern). Meldet ein Gerät einen Zeitplan-Lauf (`SCHEDULED_WATERING` o. ä.) → sofort stoppen und melden.
8. Absolute Höchstlaufzeit pro Kopplung; längere Anforderungen ablehnen (App und Claude-Entwurf).

## Datenmodell (SQLite)
device, zone, coupling (ventil_id, steckdose_id, vorlauf_ventil_s, nachlauf_ventil_s, max_laufzeit_s), schedule_rule (zone_id, wochentage-Bitmaske, startzeit, dauer_min, gueltig_von/bis, aktiv, quelle MANUELL|CLAUDE), schedule_exception, weather_policy, run_log.

## Vorgeschlagene Repo-Struktur
```
bewaesserung/
  backend/            Python-Paket (FastAPI-App, Gardena-Client, Orchestrator, Interlock, Scheduler, Wetter, Claude)
    tests/            pytest inkl. Sicherheitstests gegen einen Gardena-Mock
  deploy/
    systemd/          bewaesserung.service (Restart=always, WatchdogSec)
    pi-gen/           eigene Stufe stage-bewaesserung
    firstboot/        Einrichtungsseite für Zugangsdaten
  .github/workflows/  build-image.yml (pi-gen-Build, .img als Release-Asset)
  android/            später: Kotlin/Compose-App
  CLAUDE.md
```

## Erste Aufgaben für Claude Code
1. Repo-Gerüst wie oben anlegen, CLAUDE.md mit den Sicherheitsregeln.
2. Gardena-Client: Token über Authentication API (client credentials), Locations/Devices lesen, WebSocket mit Reconnect, Befehle für VALVE_CONTROL und POWER_SOCKET_CONTROL (start mit Sekunden, stop, pause/unpause).
3. Gardena-Mock für Tests, dann Orchestrator + Interlock + Start-Sicherheitsprüfung mit Tests für alle Sicherheitsregeln.
4. systemd-Dienst + pi-gen-Stufe + GitHub-Actions-Workflow.
5. Danach: Scheduler, REST-API für die App, Wetter, Claude-Assistent, Android-App.

## In Phase 0 am echten System klären
- Schrittweite/Grenzen der Laufzeiten in der API.
- Reagieren Ventil/Steckdose bei pausiertem Zeitplan noch auf `START_SECONDS_TO_OVERRIDE`?
- Welche Zustandswerte meldet die Steckdose bei Zeitplan-Lauf?
- Noch offen: Was treibt die Steckdose (Pumpe, Hauswasserwerk)? Dürfen mehrere Ventile gleichzeitig laufen? Irrigation Control oder einzelne Water Controls?
