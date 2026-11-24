# Auslieferung als Pi-Image

| Datei | Zweck |
| --- | --- |
| `systemd/bewaesserung.service` | Dienst: `Type=notify`, `Restart=always`, `WatchdogSec=60`, startet nach Netz und Zeitsynchronisierung |
| `systemd/bewaesserung-einrichtung.service` | Einrichtungsseite im Heimnetz, läuft nur solange `zugang.env` fehlt |
| `systemd/zugang.env.beispiel` | Vorlage für `/etc/bewaesserung/zugang.env` (root, 0600) |
| `pi-gen/vorbereiten.sh` | kopiert Backend und Unit in die Stufe; vor jedem Build ausführen |
| `pi-gen/stage-bewaesserung/` | eigene pi-gen-Stufe nach `stage2` (Raspberry Pi OS Lite) |
| `../.github/workflows/build-image.yml` | Tests, Image-Build mit `usimd/pi-gen-action@v1`, Release-Asset bei Tag `v*` |

## Was im Image steckt

- Raspberry Pi OS Lite 64 Bit (trixie, pi-gen-Branch `arm64`), Hostname `bewaesserung`,
  Zeitzone Europe/Berlin, deutsche Tastatur.
- `/opt/bewaesserung/venv` mit dem Paket `bewaesserung`, Systembenutzer `bewaesserung`,
  Daten in `/var/lib/bewaesserung`: `bewaesserung.sqlite3` (Zonen, Kopplungen, Zeitpläne, Verlauf)
  und `api-budget.json` (Gardena-Aufrufzähler, übersteht Neustarts).
- Dienst `bewaesserung.service` aktiviert, dazu `systemd-time-wait-sync` (Uhrzeit vor dem Start).
- Tailscale installiert, **nicht angemeldet**. log2ram installiert.
- **Keine Zugangsdaten.** Benutzer, Passwort, WLAN und SSH setzt der Raspberry Pi Imager.

## Erster Start

1. Image mit dem Raspberry Pi Imager flashen, dabei Benutzer, WLAN und SSH einstellen.
2. Pi starten und im Heimnetz **http://bewaesserung.local** öffnen (Einrichtungsseite, `bewaesserung/einrichtung/`):
   - Gardena Application Key und Secret (Pflicht, werden bei Husqvarna geprüft),
   - optional Tailscale-Auth-Key (sonst zeigt die Seite einen Anmelde-Link),
   - optional Standort für das Wetter und Claude-API-Schlüssel.
3. Die Seite schreibt `/etc/bewaesserung/zugang.env` (root, 0600), erzeugt das API-Token für die App,
   meldet Tailscale an und startet den Dienst neu. Das Token zeigt sie nur dem Browser, der das Formular
   abgeschickt hat, und nur dieses eine Mal: notieren. Mit „Fertig“ geht die Seite aus und startet nicht wieder.
4. Die App spricht danach `http://<tailscale-ip>:8080` mit `Authorization: Bearer <Token>` an.
5. Prüfen: `systemctl status bewaesserung` zeigt den Status („Startprüfung läuft“, „Bereit …“),
   `journalctl -u bewaesserung -f` die Meldungen.

Erneut einrichten: `sudo rm /etc/bewaesserung/zugang.env && sudo systemctl start bewaesserung-einrichtung`.
Von Hand geht es weiterhin per SSH mit der Vorlage `zugang.env.beispiel`.

Ohne `zugang.env` läuft der Dienst, meldet „Warte auf Zugangsdaten“ und schaltet nichts.
Standard ist Trockenlauf (`GARDENA_DRY_RUN=true`): Befehle werden nur protokolliert.

## ⚠ Ungeprüft

Der Image-Build ist noch nie gelaufen (erst nach dem ersten Push auf GitHub). Zu prüfen:

- Läuft die Stufe unter pi-gen-action durch (pip-Installation im arm64-chroot unter QEMU)?
- Gibt es die Paketquellen für trixie: Tailscale (`pkgs.tailscale.com/stable/debian/trixie…`)
  und log2ram (`packages.azlux.fr/debian trixie`)?
- Übernimmt das Image die Einstellungen des Raspberry Pi Imagers (cloud-init)? Ohne sie ist das
  Konto `pi` gesperrt bzw. der Einrichtungsassistent startet.
- Ist die Einrichtungsseite unter `bewaesserung.local` erreichbar (avahi), und liefert `tailscale up` den
  Anmelde-Link so, wie `einrichtung/system.py` ihn sucht?
- Startet der Dienst auf dem Pi und hält der Watchdog (`systemctl show bewaesserung -p WatchdogUSec`)?
