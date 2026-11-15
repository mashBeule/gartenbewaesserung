# Auslieferung als Pi-Image

| Datei | Zweck |
| --- | --- |
| `systemd/bewaesserung.service` | Dienst: `Type=notify`, `Restart=always`, `WatchdogSec=60`, startet nach Netz und Zeitsynchronisierung |
| `systemd/zugang.env.beispiel` | Vorlage für `/etc/bewaesserung/zugang.env` (root, 0600) |
| `pi-gen/vorbereiten.sh` | kopiert Backend und Unit in die Stufe; vor jedem Build ausführen |
| `pi-gen/stage-bewaesserung/` | eigene pi-gen-Stufe nach `stage2` (Raspberry Pi OS Lite) |
| `firstboot/` | Einrichtungsseite für Zugangsdaten – **noch offen** |
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

## Erster Start (bis die Einrichtungsseite fertig ist)

1. Image mit dem Raspberry Pi Imager flashen, dabei Benutzer, WLAN und SSH einstellen.
2. Per SSH anmelden, Zugangsdaten eintragen:
   `sudo cp /etc/bewaesserung/zugang.env.beispiel /etc/bewaesserung/zugang.env`,
   Werte eintragen, `sudo chmod 600 /etc/bewaesserung/zugang.env`.
   Für die App zusätzlich `BEWAESSERUNG_API_TOKEN` (z. B. `openssl rand -hex 24`) und
   `BEWAESSERUNG_API_HOST` = Tailscale-Adresse des Pi (`tailscale ip -4`).
3. `sudo tailscale up`, dann `sudo systemctl restart bewaesserung`.
   Die API ist danach nur über Tailscale erreichbar: `http://<tailscale-ip>:8080/status`
   mit `Authorization: Bearer <Token>`; Beschreibung unter `/openapi.json`.
4. Standort für das Wetter setzen (bis die App das kann):
   `curl -X PUT -H "Authorization: Bearer <Token>" -H "Content-Type: application/json" -d '{"breite": 51.1, "laenge": 13.6}' http://<tailscale-ip>:8080/einstellungen/standort`.
   Ohne Standort laufen alle Termine ohne Wetteranpassung.
5. Prüfen: `systemctl status bewaesserung` zeigt den Status („Startprüfung läuft“, „Bereit …“),
   `journalctl -u bewaesserung -f` die Meldungen.

Ohne `zugang.env` läuft der Dienst, meldet „Warte auf Zugangsdaten“ und schaltet nichts.
Standard ist Trockenlauf (`GARDENA_DRY_RUN=true`): Befehle werden nur protokolliert.

## ⚠ Ungeprüft

Der Image-Build ist noch nie gelaufen (erst nach dem ersten Push auf GitHub). Zu prüfen:

- Läuft die Stufe unter pi-gen-action durch (pip-Installation im arm64-chroot unter QEMU)?
- Gibt es die Paketquellen für trixie: Tailscale (`pkgs.tailscale.com/stable/debian/trixie…`)
  und log2ram (`packages.azlux.fr/debian trixie`)?
- Übernimmt das Image die Einstellungen des Raspberry Pi Imagers (cloud-init)? Ohne sie ist das
  Konto `pi` gesperrt bzw. der Einrichtungsassistent startet.
- Startet der Dienst auf dem Pi und hält der Watchdog (`systemctl show bewaesserung -p WatchdogUSec`)?
