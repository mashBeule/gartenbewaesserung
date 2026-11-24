"""Einrichtung: Formular prüfen und `zugang.env` erzeugen. Reine Logik, kein Netzwerk, kein Dateisystem.

Die Datei wird von systemd als `EnvironmentFile` gelesen. Deshalb sind nur Zeichen erlaubt, die
dort ohne Anführungszeichen und Escapes sicher sind; jede Eingabe wird streng geprüft, damit niemand
über das Formular zusätzliche Zeilen (Variablen) einschleusen kann.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Mapping

SCHLUESSEL = re.compile(r"^[A-Za-z0-9._:\-]{8,256}$")
TOKEN_ZEICHEN = 48            # Hex-Zeichen, also 24 Byte Zufall


@dataclass(frozen=True)
class Eingaben:
    gardena_key: str
    gardena_secret: str
    claude_key: str | None = None
    tailscale_authkey: str | None = None
    breite: float | None = None
    laenge: float | None = None


def _feld(form: Mapping[str, str], name: str) -> str:
    return (form.get(name) or "").strip()


def lesen(form: Mapping[str, str]) -> tuple[Eingaben | None, dict[str, str]]:
    """Formularwerte prüfen. Gibt (Eingaben, {}) oder (None, {feld: fehlertext}) zurück."""
    fehler: dict[str, str] = {}
    werte: dict[str, str | None] = {}
    for name, pflicht, bezeichnung in (("gardena_key", True, "Application Key"),
                                       ("gardena_secret", True, "Application Secret"),
                                       ("claude_key", False, "Claude-API-Schlüssel"),
                                       ("tailscale_authkey", False, "Tailscale-Auth-Key")):
        wert = _feld(form, name)
        if not wert:
            if pflicht:
                fehler[name] = f"{bezeichnung} fehlt"
            werte[name] = None
        elif not SCHLUESSEL.match(wert):
            fehler[name] = f"{bezeichnung} enthält unerwartete Zeichen (erlaubt: Buchstaben, Ziffern, . _ : -)"
        else:
            werte[name] = wert
    if werte.get("claude_key") and not werte["claude_key"].startswith("sk-ant-"):
        fehler["claude_key"] = "Claude-API-Schlüssel beginnt mit sk-ant-"
    if werte.get("tailscale_authkey") and not werte["tailscale_authkey"].startswith("tskey-"):
        fehler["tailscale_authkey"] = "Tailscale-Auth-Key beginnt mit tskey-"

    breite = laenge = None
    b, l = _feld(form, "breite").replace(",", "."), _feld(form, "laenge").replace(",", ".")
    if b or l:
        try:
            breite, laenge = float(b), float(l)
            if not (-90 <= breite <= 90 and -180 <= laenge <= 180):
                raise ValueError
        except ValueError:
            fehler["standort"] = "Standort: Breite −90…90 und Länge −180…180, z. B. 51,05 und 13,74"
            breite = laenge = None
    if fehler:
        return None, fehler
    return Eingaben(werte["gardena_key"], werte["gardena_secret"], werte["claude_key"],
                    werte["tailscale_authkey"], breite, laenge), {}


def api_token() -> str:
    return secrets.token_hex(TOKEN_ZEICHEN // 2)


def zugang_text(e: Eingaben, token: str, api_host: str = "127.0.0.1") -> str:
    """Inhalt für /etc/bewaesserung/zugang.env. Trockenlauf bleibt an (bewusst nur von Hand abschaltbar)."""
    if api_host in ("0.0.0.0", "::") or not re.match(r"^[0-9a-fA-F.:]+$", api_host):
        raise ValueError(f"Ungültige API-Adresse {api_host!r}")
    zeilen = [
        "# Geschrieben von der Einrichtungsseite. Nur root lesbar (0600). Nie ins Repo.",
        f"GARDENA_APPLICATION_KEY={e.gardena_key}",
        f"GARDENA_APPLICATION_SECRET={e.gardena_secret}",
        "# Echt schalten erst nach den Tests aus Phase 0, dann bewusst von Hand auf false setzen:",
        "GARDENA_DRY_RUN=true",
        f"BEWAESSERUNG_API_TOKEN={token}",
        f"BEWAESSERUNG_API_HOST={api_host}",
        "BEWAESSERUNG_API_PORT=8080",
    ]
    if e.claude_key:
        zeilen.append(f"ANTHROPIC_API_KEY={e.claude_key}")
    if e.breite is not None and e.laenge is not None:
        zeilen.append(f"BEWAESSERUNG_STANDORT={e.breite:.4f},{e.laenge:.4f}")
    for z in zeilen[1:]:
        if "\n" in z or "\r" in z:
            raise ValueError("Zeilenumbruch in einem Wert")
    return "\n".join(zeilen) + "\n"
