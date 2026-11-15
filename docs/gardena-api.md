# Gardena smart system API – Notizen

Übernommen aus dem Vorgängerprojekt `gartensteuerung` (dort am 2026-11-08 gegen die
offizielle Doku, API V2, Spezifikation 2.3.6 abgeglichen). In diesem Projekt nicht erneut
gegen die Doku geprüft. **Was ⚠ trägt, ist nicht belegt und wird in Phase 0 am echten Gerät geklärt.**

Quellen:

- [Developer Portal: GARDENA smart system API](https://developer.husqvarnagroup.cloud/apis/gardena-smart-system-api)
- [OpenAPI-Datei V2](https://docs.developer.husqvarnagroup.cloud/gardena-smart-system-api/iapi-v2.yml)

## Zugang

Im Husqvarna-Entwicklerportal eine Anwendung anlegen und mit „Authentication API“ und
„GARDENA smart system API“ verbinden. Application Key und Secret gibt man später auf der
Einrichtungsseite des Pi ein, nie im Repo.

## Limits

- Im Mittel ein Aufruf je 15 Minuten, **höchstens 700 pro Woche**, **höchstens 10 in 10 Sekunden**.
- Darüber: HTTP 429. ⚠ Die 24-Stunden-Sperre danach stammt aus der openHAB-Doku, nicht von
  Gardena; wir nehmen sie vorsichtshalber an.
- Umsetzung: `backend/src/bewaesserung/gardena/budget.py` (gleitende Fenster, Zähler nur im Speicher).

Grober Verbrauch: WebSocket-Adresse alle ~110 min ≈ 13/Tag, Pause-Erneuerung 1 je Gerät und Tag,
je gekoppeltem Lauf 2 Starts. Mit 4 Ventilen, 1 Steckdose und 2 Läufen pro Tag ≈ 23/Tag ≈ 160/Woche.

## Endpunkte

| Zweck | Aufruf | Erfolg |
| --- | --- | --- |
| Token | `POST https://api.authentication.husqvarnagroup.dev/v1/oauth2/token` (client_credentials) | 200 |
| Standorte | `GET https://api.smart.gardena.dev/v2/locations` | 200 |
| Geräte eines Standorts | `GET …/v2/locations/{locationId}` (Dienste in `included`) | 200 |
| WebSocket-Adresse | `POST …/v2/websocket` | 201 |
| Befehl | `PUT …/v2/command/{serviceId}` | 202 |

Kopfzeilen: `Authorization: Bearer <token>`, `X-Api-Key: <Application Key>`,
`Content-Type: application/vnd.api+json`. Die `serviceId` ist nicht die Geräte-ID und kann
ein Suffix tragen (`<uuid>:1`).

## Befehle

Körper: `{"data": {"id": "…", "type": "VALVE_CONTROL", "attributes": {"command": "…", "seconds": 60}}}`

| Dienst | Befehl | Wir benutzen |
| --- | --- | --- |
| `VALVE_CONTROL` | `START_SECONDS_TO_OVERRIDE` (`seconds`) | ja, einziger Startbefehl |
| `VALVE_CONTROL` | `STOP_UNTIL_NEXT_TASK` | ja |
| `VALVE_CONTROL` | `PAUSE`, `UNPAUSE` | ja (Zeitpläne pausieren) |
| `POWER_SOCKET_CONTROL` | wie oben, zusätzlich `START_OVERRIDE` | `START_OVERRIDE` **nie** (unbegrenzt, Sicherheitsregel 1) |

- `seconds` ist eine Ganzzahl, positives Vielfaches von 60; Ventile laut Release-Notizen bis 36000 s.
  ⚠ Schrittweite und Grenzen am echten Gerät prüfen (`SEKUNDEN_SCHRITT` in `sicherheit/kopplung.py`).
- Eine Antwort 202 heißt nur, dass das Gateway den Befehl angenommen hat. Ausgeführt ist er erst,
  wenn der WebSocket die Zustandsänderung meldet.
- Zeitpläne kommen in der API nicht vor; sie lassen sich nur pausieren.

### ⚠ PAUSE

Die Spezifikation beschreibt `PAUSE` als „until specified time“, definiert aber keinen Parameter
dafür. py-smart-gardena2 sendet `PAUSE` ohne Dauer; laut openHAB-Community wirkt das als
unbegrenzte Pause. Wir senden `PAUSE` ohne Dauer. In Phase 0 prüfen:

- Gilt die Pause wirklich unbegrenzt (Anzeige in der Gardena-App)?
- Reagieren Ventil und Steckdose bei pausiertem Zeitplan noch auf `START_SECONDS_TO_OVERRIDE`?
- Bricht `PAUSE` eine laufende Bewässerung ab? (Darum pausiert die Startprüfung erst,
  nachdem alles gestoppt ist, und die tägliche Erneuerung darf nicht während eines Laufs kommen.)

## WebSocket

- Adresse gilt nur Sekunden (`validity`), also sofort verbinden.
- Gardena schließt nach ~10 min Leerlauf (Ping alle 150 s) und spätestens nach ~2 h;
  wir verbinden nach 110 min selbst neu.
- Beim Verbinden kommt der komplette Zustand, danach Teil-Aktualisierungen
  (`null` löscht ein Feld). Vor jedem Neuverbinden den Zustand zurücksetzen.

## Dienste und Zustände

- `VALVE`: `activity` = `CLOSED`, `MANUAL_WATERING`, `SCHEDULED_WATERING`; `state`, `duration`, `name`.
- `POWER_SOCKET`: `activity` = `OFF`, `FOREVER_ON`, `TIME_LIMITED_ON`, `SCHEDULED_ON`; `state`, `duration`.
  ⚠ Ob die Steckdose bei einem Zeitplan-Lauf wirklich `SCHEDULED_ON` meldet, in Phase 0 prüfen.
- `SENSOR`: `soilHumidity`, `soilTemperature`, `ambientTemperature`, `lightIntensity`.
- `COMMON`: `name`, `batteryLevel`, `rfLinkState`.
- Werte liegen als `{"value": …, "timestamp": …}` vor.

Für die Sicherheitslogik gilt vorsichtig: Ein Ventil ist nur bei `MANUAL_WATERING` oder
`SCHEDULED_WATERING` offen; eine Steckdose ist bei jedem Wert außer `OFF` an (auch bei
unbekannten Werten oder fehlendem Zustand).
