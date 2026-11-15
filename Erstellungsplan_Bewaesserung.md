# Erstellungsplan: App-basierte Bewässerungssteuerung (Gardena smart system)

Nov 14, 2026 · @Mathias

## Ausgangslage und Ziele

Ein Raspberry Pi 3 wird zur zentralen Steuerung der Gardena-smart-Geräte; die Android-App ist nur die Bedienoberfläche. So läuft die Bewässerung auch dann zuverlässig, wenn das Handy aus ist, und alle Sicherheitsregeln greifen an einer Stelle.

Rahmenbedingungen: Gardena smart system mit Ventilen, Sensoren und Smart Power Steckdose (Power Adapter), Zugriff über die Gardena-Cloud-API. Ein Nutzer, nur Android, Zugriff unterwegs über Tailscale. Fällt der Pi aus, wird bewusst nicht bewässert. Abgesichert ist nur, dass ein gerade laufender Lauf von selbst endet und die Pumpe abschaltet.

| ID | Anforderung | Umsetzung (Kurzfassung) |
| --- | --- | --- |
| A1 | Android-basierte Steuerungs-App | Kotlin + Jetpack Compose, spricht nur mit dem Pi-Backend |
| A2 | Kalenderbasierte Planung über grafische Oberfläche | Wochen- und Monatsansicht, Termine per Tippen und Ziehen anlegen |
| A3 | Pro Ventil eigene Wochentage und Zeiten | Zeitplan-Regeln je Ventil (Wochentage, Startzeit, Dauer, Saisonzeitraum) |
| A4 | Kopplung Ventil + Smart Power Steckdose, gleichzeitige Aktivierung | Kopplungsobjekt mit Reihenfolge und Vorlaufzeiten, vom Backend orchestriert |
| A5 | Wetterdienste für smarte Anpassung | Regen-Aussetzen, Dauer-Skalierung nach Verdunstung/Temperatur, Frostsperre |
| A6 | Claude API schlägt Saison-Startplan vor | Assistent erzeugt strukturierten Plan als Entwurf, Nutzer prüft und übernimmt |
| A7 | Sicherheit: Steckdose nie ohne offenes Ventil an | Interlock im Backend + zeitbegrenzte Befehle + Watchdog (Abschnitt Sicherheitskonzept) |

## Systemarchitektur

Der Raspberry Pi ist das Gehirn: Er speichert die Pläne, führt sie aus und spricht als einziger mit der Gardena-Cloud. Die App zeigt nur an und ändert Einstellungen.

&#91;embedded content: Systemarchitektur · App, Pi-Backend, externe Dienste\]

Der Scheduler schaltet nie direkt, sondern übergibt jeden Lauf an den Kopplungs- und Interlock-Baustein; nur dieser sendet Befehle an Gardena und empfängt die Statusereignisse. Wetterdienste und Claude liefern nur Daten und Entwürfe zu.

**Hardware: Raspberry Pi 3**

- Leistung reicht: Ein Python-Dienst mit FastAPI, APScheduler und SQLite braucht nur einen Bruchteil des 1 GB Arbeitsspeichers. Raspberry Pi OS Lite (64 Bit) ohne Desktop verwenden.
- Schwachstelle ist die SD-Karte: eine robuste Karte (Endurance-Typ) nehmen, Logs in den Arbeitsspeicher legen (log2ram) und die SQLite-Datei täglich sichern. Alternative: vom USB-Stick oder einer USB-SSD booten.
- Offizielles Netzteil, Netzwerk am besten per LAN-Kabel. Optional eine kleine USV gegen kurze Stromausfälle.
- Uhrzeit per NTP, Zeitzone Europe/Berlin, damit die Sommerzeit-Umstellung korrekt läuft.
- Fernzugriff nur über Tailscale auf Pi und Handy, keine Portfreigabe im Router.

## Datenmodell

Sieben Tabellen in SQLite auf dem Pi decken alle Anforderungen ab. Zeitpläne werden als Regeln gespeichert und erst im Scheduler zu konkreten Läufen expandiert; so bleibt die Kalenderansicht und die Wetteranpassung entkoppelt.

| Tabelle | Wichtige Felder | Zweck |
| --- | --- | --- |
| device | gardena\_id, typ (VALVE, POWER\_SOCKET, SENSOR), name, online, letzter\_status | Spiegel der Gardena-Geräte, per WebSocket aktuell gehalten |
| zone | name, ventil\_id, pflanzentyp, boden, sonnenlage, durchfluss\_l\_min | Fachliche Sicht auf ein Ventil (Beet, Rasen, Hecke) |
| coupling | ventil\_id, steckdose\_id, vorlauf\_ventil\_s, nachlauf\_ventil\_s, max\_laufzeit\_s | Kopplung Ventil + Steckdose (A4), Parameter für das Interlock |
| schedule\_rule | zone\_id, wochentage (Bitmaske), startzeit, dauer\_min, gueltig\_von, gueltig\_bis, aktiv, quelle (MANUELL, CLAUDE) | Regel pro Ventil (A3), Herkunft des Plans (A6) |
| schedule\_exception | rule\_id, datum, aktion (AUSLASSEN, VERSCHIEBEN, DAUER\_ANPASSEN), wert | Einzelne Änderungen aus dem Kalender per Ziehen |
| weather\_policy | zone\_id oder global, regen\_schwelle\_mm, zeitfenster\_h, frost\_grenze\_c, skalierung\_aktiv | Wetterregeln (A5) |
| run\_log | zeitpunkt, zone\_id, geplant\_s, tatsaechlich\_s, grund (PLAN, MANUELL, ÜBERSPRUNGEN\_REGEN, …), sicherheitsereignis | Verlauf, Statistik, Nachweis jeder Abschaltung |

Ein geplanter Lauf ist immer: Regel + Ausnahmen + Wetterfaktor = konkrete Dauer in Sekunden. Diese Rechnung läuft täglich und kurz vor jedem Start erneut.

## Sicherheitskonzept Ventil + Steckdose

Die Steckdose bekommt immer eine kürzere Laufzeit als das Ventil, und zwar direkt im Gardena-Befehl. Dadurch schaltet die Steckdose auch dann rechtzeitig ab, wenn der Pi abstürzt oder das Netz weg ist. Alle weiteren Maßnahmen sind zusätzliche Schichten darüber.

**Grundregeln**

- Nur zeitbegrenzte Befehle: Ventil und Steckdose ausschließlich mit `START_SECONDS_TO_OVERRIDE` (mit Sekundenwert) starten, nie mit einem unbegrenzten Einschaltbefehl.
- Laufzeit Steckdose = Laufzeit Ventil − Vorlauf − Nachlauf. Das Ventil läuft immer länger als die Steckdose.
- Beim Abbruch immer zuerst die Steckdose aus, dann das Ventil zu.
- Ist der Zustand der Steckdose unklar, bleibt das Ventil offen, bis die Steckdosen-Laufzeit sicher abgelaufen ist.
- Absolute Höchstlaufzeit pro Kopplung (`max_laufzeit_s`); längere Läufe lehnt das Backend ab, auch aus der App oder von Claude.

**Ablauf eines gekoppelten Laufs**

1. Ventil öffnen mit Dauer T.
2. Auf Bestätigung per WebSocket warten (Ventil meldet aktive Bewässerung), Timeout z. B. 30 s. Ohne Bestätigung: Ventil stoppen, Lauf abbrechen, Push-Meldung.
3. Steckdose einschalten mit Dauer T − Vorlauf − Nachlauf.
4. Auf Bestätigung der Steckdose warten. Ohne Bestätigung: Steckdose erneut stoppen, Meldung.
5. Reguläres Ende: Steckdose läuft zuerst aus, das Ventil schließt nach der Nachlaufzeit.

**Überwachung (Interlock-Wächter im Backend)**

- Jedes Statusereignis wird geprüft: Steckdose an und gekoppeltes Ventil nicht offen → sofort Steckdose stoppen, Eintrag im run\_log, Push-Meldung. Das gilt auch, wenn das Ventil von außen geschlossen wurde (Gardena-App, Taste am Gerät).
- Bricht die WebSocket-Verbindung ab, wird der Zustand per REST nachgeladen; bei laufender Steckdose wird vorsorglich gestoppt.
- Beim Start des Backends (z. B. nach Stromausfall) gehen alle gekoppelten Steckdosen per Befehl aus.
- Ein systemd-Watchdog startet den Dienst neu, wenn er hängt.

Die Gardena-API bestätigt einen Befehl, ohne zu garantieren, dass er beim Gerät ankommt ([openHAB-Doku](https://www.openhab.org/addons/bindings/gardena/)). Deshalb zählt für die Logik nur der gemeldete Gerätezustand, nie die Antwort auf den Befehl.

**Ausfall des Pi während eines Laufs**: Beide Geräte haben ihre Laufzeit schon beim Start erhalten und zählen selbst herunter. Die Steckdose (Pumpe) schaltet nach T − Vorlauf − Nachlauf ab, das Ventil kurz danach nach T. Danach bleibt alles aus, bis der Pi wieder läuft. Nach dem Neustart prüft er zuerst alle Kopplungen und setzt erst dann den Plan fort.

**Schutz vor parallelen Gardena-Zeitplänen**: Zeitpläne aus der Gardena-App laufen am Pi vorbei, und eine so gestartete Pumpe wäre nicht durch das Interlock geschützt. Die API kann diese Pläne weder auslesen noch löschen ([Symcon-Doku](https://www.symcon.de/en/service/documentation/module-reference/devices/gardena-smart-system/smart-irrigation-control/)), daher drei Stufen:

1. **Löschen** (einmalig, von Hand): vor der Inbetriebnahme alle Gardena-Zeitpläne für Ventile und Steckdose in der Gardena-App löschen.
2. **Dauerhaft pausieren** (Pi): Der Pi setzt für jedes Ventil und die Steckdose den Zeitplan auf unbegrenzt pausiert ([openHAB-Community](https://community.openhab.org/t/gardena-binding-pause-schedule-not-working-correct/124491)). Er wiederholt das beim Start und täglich, falls die Pause in der Gardena-App aufgehoben wurde.
3. **Erkennen und stoppen** (Pi): Meldet ein Ventil `SCHEDULED_WATERING` oder die Steckdose einen zeitplanbasierten Zustand, stoppt der Pi sofort (Steckdose zuerst), schreibt ins run\_log und schickt eine Meldung mit dem Hinweis auf einen vergessenen Gardena-Plan.

In Phase 0 prüfen: Starten Ventil und Steckdose bei pausiertem Zeitplan weiterhin auf einen zeitbegrenzten Befehl des Pi? Und welche Zustandswerte meldet die Steckdose bei einem Zeitplan-Lauf?

## Wetterintegration

Open-Meteo liefert die Prognose inklusive Verdunstung (ET₀), Bright Sky die tatsächlich gemessenen DWD-Werte der nächsten Station, der eigene Gardena-Sensor die Bodenfeuchte vor Ort. Beide Dienste sind kostenlos und ohne API-Schlüssel nutzbar.

| Quelle | Liefert | Einsatz |
| --- | --- | --- |
| [Open-Meteo](https://open-meteo.com/en/docs) | Niederschlag, Regenwahrscheinlichkeit, Tmin/Tmax, ET₀ nach FAO-56 (stündlich und täglich) | Prognose: Aussetzen, Dauer skalieren, Frostsperre |
| [Bright Sky](https://brightsky.dev) (DWD-Daten als JSON) | Gemessener Niederschlag der letzten Stunden | Was ist wirklich gefallen? |
| Gardena smart Sensor | Bodenfeuchte, Bodentemperatur | Lokale Bestätigung, höchste Priorität |

**Entscheidungsregeln** (Startwerte, in der App je Zone einstellbar)

1. Bodenfeuchte über Schwelle → Lauf aussetzen.
2. Gemessener Regen der letzten 24 h ≥ 5 mm → aussetzen.
3. Prognose nächste 12 h ≥ 4 mm bei mindestens 70 % Wahrscheinlichkeit → aussetzen.
4. Frost: prognostizierte Tiefsttemperatur unter 2 °C → aussetzen.
5. Sonst Dauer skalieren: Faktor = (ET₀ seit letztem Lauf − Regen) / Referenz-ET₀, begrenzt auf 0,3 bis 1,5.

**Zeitpunkte**: Wetterdaten stündlich abrufen und zwischenspeichern. Die Entscheidung fällt 30 min vor dem Start und wird direkt vor dem Start mit aktuellem Messwert noch einmal geprüft.

**Ausfall**: Sind die Wetterdaten älter als 12 h, läuft der Plan unverändert (Faktor 1,0) und die App zeigt einen Hinweis. Jede Wetterentscheidung landet mit Begründung im run\_log und ist im Kalender als ausgegrauter oder gekürzter Termin sichtbar.

## Claude API: Saisonplan vorschlagen

Claude erstellt nur einen Entwurf; aktiv wird ein Plan erst, wenn du ihn in der App bestätigst. Claude bekommt keinen Zugriff auf Ventile oder Steckdosen, und jeder Vorschlag läuft durch dieselbe Prüfung wie ein manuell angelegter Plan.

**Ablauf**

1. In der App „Neue Saison planen“ antippen, optional Wünsche eintragen (z. B. „Rasen neu angelegt“, „ab Juli drei Wochen Urlaub“).
2. Das Backend stellt den Kontext zusammen und ruft die Claude API auf. Der API-Schlüssel liegt nur auf dem Pi, nie in der App.
3. Claude antwortet per Tool-Aufruf mit festem JSON-Schema: Regeln je Zone, Saisonphasen (Frühjahr, Hochsommer, Herbst) und eine kurze Begründung je Zone.
4. Das Backend validiert den Vorschlag (siehe unten) und speichert ihn als Entwurf mit `quelle = CLAUDE`.
5. Die App zeigt den Entwurf im Kalender neben dem alten Plan; du übernimmst ihn ganz, teilweise oder gar nicht.

**Kontext für den Prompt**

- Zonen mit Pflanzentyp, Boden, Sonnenlage, Durchfluss
- Standort (Koordinaten) und langjähriges Klimamittel bzw. ET₀-Summen der Vorsaison aus Open-Meteo
- Statistik der Vorsaison aus dem run\_log: Laufzeiten, Wetteraussetzer, manuelle Korrekturen
- Grenzen: erlaubte Uhrzeiten, Höchstlaufzeit je Kopplung, wie viele Ventile gleichzeitig laufen dürfen

**Validierung vor dem Speichern**

- JSON-Schema eingehalten, nur bekannte Zonen-IDs
- Keine Laufzeit über `max_laufzeit_s`, keine überlappenden Läufe gegen die Gleichzeitigkeitsgrenze
- Startzeiten im erlaubten Fenster, Gesamtwassermenge pro Tag plausibel
- Bei Fehlern: ein automatischer zweiter Versuch mit Fehlerliste, danach Meldung in der App

Modell: ein aktuelles Sonnet-Modell (z. B. `claude-sonnet-5-5`) reicht. Es fallen wenige Aufrufe pro Saison an, die Kosten sind entsprechend gering. Optional später: monatlicher „Plan-Check“, bei dem Claude Anpassungen auf Basis der bisherigen Saison vorschlägt.

## Android-App

Die App ist eine native Kotlin-App mit Jetpack Compose und spricht ausschließlich mit dem Pi-Backend über Tailscale. Sie hält keine eigene Logik für Zeitpläne oder Sicherheit; fällt sie aus, läuft alles weiter.

**Technik**

- Kotlin, Jetpack Compose, Material 3, MVVM mit ViewModels und Kotlin Flows
- Netzwerk: Retrofit oder Ktor-Client für REST, WebSocket für Live-Status
- Lokaler Cache: Room, damit Kalender und letzte Zustände auch offline sichtbar sind
- Kalender: Monats- und Wochenraster mit einer Compose-Kalenderbibliothek (z. B. kizitonwose/Calendar), Tagesansicht als eigene Zeitleiste mit Ziehen zum Verschieben
- Push-Meldungen bei Sicherheitsereignissen: ntfy auf dem Pi (läuft über Tailscale, ohne Google-Dienste) oder Firebase Cloud Messaging
- Zugang: Tailscale plus API-Token im verschlüsselten Android-Speicher

| Bildschirm | Inhalt |
| --- | --- |
| Übersicht | Status aller Ventile und Steckdosen live, nächster Lauf, Wetterentscheidung des Tages, Not-Aus-Taste |
| Kalender | Woche/Monat, farbig je Zone; ausgesetzte Läufe ausgegraut mit Grund; Antippen zum Bearbeiten, Ziehen zum Verschieben |
| Zone bearbeiten | Wochentage als Chips, Startzeit, Dauer, Gültigkeitszeitraum, Wetterregeln, Kopplung |
| Kopplungen | Ventil + Steckdose zuordnen, Vor-/Nachlauf, Höchstlaufzeit, Testlauf mit 60 s |
| Saisonassistent | Wünsche eingeben, Claude-Entwurf als Vergleich alt/neu, übernehmen oder verwerfen |
| Verlauf | run\_log mit Filter, Wasserverbrauch je Zone, alle Sicherheitsereignisse |
| Einstellungen | Standort, Wetterquellen, Schwellenwerte, Server-Adresse, Benachrichtigungen |

## Phasenplan

Sicherheit kommt vor der Oberfläche: Die Kopplungslogik wird vollständig getestet, bevor die App Geräte schalten darf. Bei grob 13 bis 17 Wochenenden ist das System bis zum Saisonstart im Frühjahr 2027 einsatzbereit; der Aufwand ist eine Schätzung.

| Phase | Inhalt | Fertig, wenn | Aufwand (Schätzung) |
| --- | --- | --- | --- |
| 0 Grundlagen | Konto im Husqvarna-Entwicklerportal, App mit Authentication API + GARDENA smart system API verbinden; Pi mit Python-Umgebung, Tailscale | Geräteliste per Skript abrufbar, WebSocket-Ereignisse sichtbar, Ventil per Skript 60 s geöffnet | 1 WE |
| 1 Backend-Kern + Sicherheit | Gardena-Client (Token, REST, WebSocket mit Reconnect), Gerätespiegel, Kopplungsablauf, Interlock-Wächter, systemd-Dienst | Alle Sicherheitstests (nächster Abschnitt) bestanden | 2–3 WE |
| 2 Zeitpläne | Regeln, Ausnahmen, Scheduler, REST-API für die App | Zwei Zonen laufen eine Woche nach Plan ohne Eingriff | 2 WE |
| 3 App-MVP | Übersicht, Zone bearbeiten, Kopplungen, Kalender-Wochenansicht, Push | Kompletter Plan nur über die App anlegbar und änderbar | 3 WE |
| 4 Wetter | Open-Meteo, Bright Sky, Sensor, Regeln, Anzeige im Kalender | Simulierte Regentage setzen Läufe korrekt aus | 1–2 WE |
| 5 Claude-Assistent | Prompt, Tool-Schema, Validierung, Entwurfsansicht in der App | Vorschlag für alle Zonen besteht die Validierung | 1–2 WE |
| 6 Probebetrieb | 2–4 Wochen unter Beobachtung, Schwellen justieren | Keine Fehlalarme mehr, alle Gardena-Zeitpläne gelöscht | Kalenderzeit |
| 7 Pi-Image | pi-gen-Stufe, systemd-Dienst, GitHub-Actions-Build, Ersteinrichtung für Zugangsdaten | Frisch geflashter Pi startet die Anwendung ohne Handgriff, nach Eingabe der Zugangsdaten läuft der Plan | 1–2 WE |

Backend-Empfehlung: Python mit FastAPI, APScheduler und SQLite. Das läuft schlank auf dem Pi, und für die Gardena-API gibt es Python-Bibliotheken als Vorlage (z. B. [py-smart-gardena](https://github.com/py-smart-gardena/hass-gardena-smart-system)).

## Auslieferung als Pi-Image

Am Ende steht ein fertiges Image: mit dem Raspberry Pi Imager auf die SD-Karte flashen, Pi einschalten, die Anwendung startet von selbst. Das Grundgerüst für Dienst und Image entsteht schon in Phase 1, damit jeder Testlauf auf einem echten Image läuft.

- **Image-Bau mit pi-gen**, dem offiziellen Werkzeug, mit dem auch Raspberry Pi OS gebaut wird. Eine eigene Stufe auf Basis von Raspberry Pi OS Lite (64 Bit) installiert Python, die Anwendung, Tailscale und log2ram.
- **Autostart per systemd-Dienst**: startet nach dem Booten, sobald das Netzwerk steht, und nach einem Absturz automatisch neu (mit Watchdog). Beim Start läuft zuerst die Sicherheitsprüfung aller Kopplungen.
- **Automatischer Build über GitHub Actions**: Bei jedem neuen Stand im Repository baut GitHub das Image und stellt die `.img`-Datei als Download bereit. Ein eigener Linux-Rechner ist nicht nötig.
- **Keine Zugangsdaten im Image**: WLAN, Benutzer und SSH über die Einstellungen des Raspberry Pi Imagers. Gardena-Schlüssel, Claude-Schlüssel und Tailscale-Anmeldung über eine Einrichtungsseite, die der Pi beim ersten Start im Heimnetz anzeigt. Bis die Daten eingetragen sind, schaltet die Anwendung nichts.
- **Updates**: neues Image flashen oder per Update-Befehl aus der App die neue Version aus GitHub nachladen; die Datenbank bleibt dabei erhalten.

## Tests, Risiken, offene Fragen

**Sicherheitstests vor Phase 2** (mit Steckdose ohne Pumpe, z. B. Lampe als Anzeige)

- [ ] Pi während eines Laufs vom Strom trennen: Steckdose geht vor dem Ventil aus
- [ ] Netzwerk des Pi trennen: Steckdose läuft per Gardena-Timeout aus
- [ ] Ventil während des Laufs in der Gardena-App schließen: Steckdose geht sofort aus
- [ ] Ventil bestätigt nicht: Steckdose wird nie eingeschaltet
- [ ] Steckdose manuell einschalten, Ventil zu: Wächter schaltet ab und meldet
- [ ] Backend-Neustart: alle gekoppelten Steckdosen gehen aus
- [ ] Laufzeit über Höchstwert anfordern (App und Claude-Entwurf): wird abgelehnt
- [ ] Zeitplan in der Gardena-App anlegen: Pi hält ihn pausiert, er startet nicht
- [ ] Pause in der Gardena-App aufheben und Plan starten lassen: Pi erkennt den Zeitplan-Lauf, stoppt Steckdose und Ventil, meldet es

**Risiken**

| Risiko | Gegenmaßnahme |
| --- | --- |
| Gardena-Cloud oder Gateway offline | Zeitbegrenzte Befehle sichern ab; Push-Meldung |
| API-Ratenlimits ([laut Gardena vorhanden](https://developer.husqvarnagroup.cloud/apis/gardena-smart-system-api?tab=readme#rate-limits)) | Status über WebSocket statt Abfragen, REST nur für Befehle und beim Reconnect |
| API-Schlüssel wird ungültig | Token-Erneuerung automatisieren, bei Fehlern Push-Meldung |
| Wetterdienst liefert nichts | Faktor 1,0, Hinweis in der App |
| Claude-Vorschlag unpassend | Nur Entwurf, harte Validierung, Freigabe durch dich |

**Offene Fragen**

- Was treibt die Steckdose genau (Pumpe, Brunnen, Hauswasserwerk)? Davon hängen Vor- und Nachlaufzeiten ab.
- Dürfen mehrere Ventile gleichzeitig laufen, oder reicht der Wasserdruck nur für eines?
- Welches Gerät steuert die Ventile: smart Irrigation Control (bis 6 Ventile) oder einzelne smart Water Controls?
- In welcher Schrittweite akzeptiert die API Laufzeiten? In Phase 0 mit kurzen Testläufen prüfen.
