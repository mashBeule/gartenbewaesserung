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
| Wetter: Zeitpunkt der Entscheidung | 30 min vorher entscheiden, direkt vor dem Start prüfen | eine Entscheidung direkt vor dem Start mit den stündlich abgerufenen Daten und dem aktuellen Bodenwert; Vorschau für die nächsten 24 h im Kalender | gleiches Ergebnis, ein Rechenweg weniger |
| Wetter: Frost | „prognostizierte Tiefsttemperatur“ | Minimum von 1 h vor bis `zeitfenster_h` nach dem Start | Zeitraum war offen |
| Wetter: Regel je Zone | – | eine Zonen-Regel ersetzt die globale vollständig (kein Mischen einzelner Felder) | einfacher in App und Datenbank |
| Wetter: Skalierung | ET₀ seit letztem Lauf | Zeitraum seit Ende der letzten echten Bewässerung, höchstens 7 Tage; ohne Lauf 24 h; Referenz 4 mm/Tag; nie über die Höchstlaufzeit | 7 Tage reicht die Open-Meteo-Vergangenheit |
| Wetter: Ausfall | Daten älter als 12 h → Faktor 1,0 | so umgesetzt; ohne Bright Sky zählen für „Regen 24 h“ die Modellwerte von Open-Meteo; ein Fehler in der Wetterauswertung lässt den Termin unverändert laufen | Wetter darf Läufe kürzen, aber nie aus Versehen alle verhindern |
| Standort | Einstellungen in der App | Tabelle `einstellung`, `PUT /einstellungen/standort`; an die Wetterdienste nur auf 2 Nachkommastellen (~1 km) gerundet | kein genauer Standort nach außen |
| Claude: Antwortformat | Tool-Aufruf mit JSON-Schema | structured outputs (`output_config.format`) mit demselben festen Schema, keine Werkzeuge | Sonnet 5.5 erlaubt keinen erzwungenen Tool-Aufruf; structured outputs garantiert gültiges JSON, und ohne Werkzeuge hat Claude nachweislich keinen Zugriff auf irgendetwas |
| Claude: Modell | aktuelles Sonnet | `claude-sonnet-5-5`, über `CLAUDE_MODELL` änderbar; bei Ablehnung serverseitiges Ausweichmodell (`fallbacks: "default"`) | wie im Plan |
| Claude: Grenzen | „erlaubte Uhrzeiten“, „Gesamtwassermenge plausibel“ | Startfenster 04:00–10:00 und 18:00–22:00 (Lauf muss im Fenster enden), höchstens 240 min Laufzeit pro Tag, keine Überschneidungen, Gültigkeit höchstens 1 Jahr (`assistent/entwurf.py`, `Grenzen`) | Werte waren offen; Liter ließen sich nur mit Durchfluss rechnen, der oft fehlt |
| Claude: Übernehmen | ganz, teilweise oder gar nicht | je Zone; alte Regeln dieser Zonen werden deaktiviert, nicht gelöscht; vor dem Anlegen erneute Prüfung, auch gegen bleibende Regeln anderer Zonen | rückgängig machbar, keine Überschneidung mit dem Rest |
| Claude: Ablauf | – | Entwurf im Hintergrund (`POST /entwuerfe` → 202, Status abfragen), höchstens einer gleichzeitig | ein Aufruf kann Minuten dauern |
| Einrichtungsseite: Ort | `deploy/firstboot` | Python-Paket `bewaesserung.einrichtung` (FastAPI, gleiche venv), eigener Dienst `bewaesserung-einrichtung.service` | testbar wie das übrige Backend, keine zweite Laufzeitumgebung |
| Einrichtungsseite: Rechte und Erreichbarkeit | – | läuft als root auf Port 80 im Heimnetz, nur solange `zugang.env` fehlt (`ConditionPathExists`), beendet sich nach „Fertig“ oder 30 min | braucht root für `zugang.env`, `tailscale up` und den Neustart; kurzes Fenster statt dauerhaftem Dienst |
| Einrichtungsseite: Schutz | – | Formular-Token gegen fremde Seiten, API-Token nur für den absendenden Browser (Cookie), Schlüssel werden nie zurück angezeigt, Eingaben streng geprüft (kein Einschleusen weiterer Zeilen) | jeder im Heimnetz könnte die Seite öffnen |
| Einrichtungsseite: Trockenlauf | – | bleibt an; echtes Schalten nur von Hand per SSH | Sicherheitsregel: erst nach den Tests am Ventil |
| Standort | Einstellungen in der App | zusätzlich `BEWAESSERUNG_STANDORT` aus der Einrichtung, wird nur übernommen, solange die Datenbank keinen Standort hat | die App gibt es noch nicht |
