# Entscheidungen und Abweichungen vom Plan

Hier steht, wo die Umsetzung bewusst vom Erstellungsplan oder der Übergabe abweicht.
Jede Zeile kann der Projektinhaber zurückdrehen.

| Thema | Plan | Umsetzung | Grund |
| --- | --- | --- | --- |
| Scheduler | APScheduler | eigene Takt-Schleife (30 s) in `planung/scheduler.py` | Regeln werden bei jedem Takt frisch aus SQLite gelesen, kein Job-Abgleich bei Änderungen aus der App; mit gestellter Uhr vollständig testbar. APScheduler ist nicht installiert. |
| Gleichzeitige Läufe | offen | Läufe nacheinander; manueller Lauf nur, wenn nichts läuft | sicher, solange unklar ist, ob der Wasserdruck mehrere Ventile trägt |
| Verpasste Termine | – | Nachholfenster 15 min; ältere Termine werden weder nachgeholt noch protokolliert | sonst würden neue Regeln ihre vergangenen Termine als „verpasst“ melden |
| Zone ohne Kopplung | – | keine Regeln und keine Läufe möglich | der Orchestrator kennt nur gekoppelte Läufe (Ventil + Steckdose) |
| REST-Nachladen nach WebSocket-Abriss | Zustand per REST nachladen | nicht nötig, der neue WebSocket liefert den vollen Stand | spart API-Budget |
| API-Budget | – | eingebaut, Zähler übersteht Neustarts | 700 Aufrufe/Woche, sonst 24 h Sperre |
| Laufzeiten | – | Vielfache von 60 s, Nachlauf mindestens 10 s | ⚠ Schrittweite der API in Phase 0 prüfen |
| Startprüfung | – | jeder Fehler sperrt alle Läufe | einfacher und sicherer als Sperre je Kopplung |
| API-Pfade | – | deutsche Pfade (`/zonen`, `/regeln`, `/plan`, `/notaus` …), OpenAPI unter `/openapi.json` | einheitlich mit den Fachbegriffen |
