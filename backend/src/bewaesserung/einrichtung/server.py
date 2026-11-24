"""Einrichtungsseite: läuft beim ersten Start im Heimnetz (http://bewaesserung.local), bis die Zugangsdaten stehen.

Ablauf:
1. Formular: Gardena Application Key/Secret (Pflicht), Claude-Schlüssel, Tailscale-Auth-Key, Standort.
2. Gardena-Zugang wird bei Husqvarna geprüft, dann `zugang.env` geschrieben (API nur auf localhost,
   Trockenlauf an) und der Dienst neu gestartet.
3. Tailscale wird angemeldet (Auth-Key oder Anmelde-Link auf der Seite). Steht die Tailscale-Adresse,
   bekommt die API sie als Adresse und der Dienst startet erneut.
4. Die Seite zeigt das API-Token für die App, nur dem Browser, der das Formular abgeschickt hat.
   „Fertig“ beendet die Seite; sie startet danach nicht mehr (systemd-Bedingung: zugang.env fehlt).

Schutz: Die Seite ist nur im Heimnetz erreichbar, nur bis zur Einrichtung aktiv, zeigt nie gespeicherte
Schlüssel an, und Formulare brauchen ein Zufalls-Token dieser Sitzung (gegen fremde Seiten im Browser).
"""
from __future__ import annotations

import asyncio
import hmac
import html
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .logik import Eingaben, api_token, lesen, zugang_text
from .system import EchtesSystem, System

log = logging.getLogger(__name__)
COOKIE = "einrichtung"


@dataclass
class Stand:
    phase: str = "OFFEN"                 # OFFEN, TAILSCALE, FERTIG
    login_link: str | None = None
    tailscale_ip: str | None = None
    tailscale_fehler: str | None = None
    token: str | None = None
    sitzung: str | None = None
    eingaben: Eingaben | None = None
    aufgaben: set = field(default_factory=set)


SEITE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bewässerung einrichten</title>
<style>
:root {{ --bg:#f3f6f2; --karte:#fff; --tinte:#1d2a22; --leise:#5b6b60; --gruen:#2f7d4f; --rot:#b3261e; --rand:#d6ded8; color-scheme: light dark; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141a16; --karte:#1e2620; --tinte:#e6eee8; --leise:#9fb0a4; --gruen:#5cc28a; --rot:#ff8a80; --rand:#334037; }} }}
body {{ margin:0; background:var(--bg); color:var(--tinte); font:16px/1.5 system-ui, sans-serif;
  padding-block:24px; padding-left:max(16px, env(safe-area-inset-left, 0px)); padding-right:max(16px, env(safe-area-inset-right, 0px)); }}
main {{ max-width:620px; margin:0 auto; display:grid; gap:18px; }}
h1 {{ margin:0; font-size:28px; line-height:1.2; }}
h2 {{ margin:0 0 4px; font-size:18px; }}
p {{ margin:0; }}
.leise {{ color:var(--leise); font-size:14px; }}
section {{ background:var(--karte); border:1px solid var(--rand); border-radius:12px; padding:16px; display:grid; gap:10px; }}
label {{ display:grid; gap:4px; font-weight:600; }}
input {{ font:inherit; padding:10px 12px; border:1px solid var(--rand); border-radius:8px; background:var(--bg); color:var(--tinte); min-width:0; }}
.zwei {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; }}
.fehler {{ color:var(--rot); font-weight:600; font-size:14px; }}
button {{ font:inherit; font-weight:700; padding:12px 20px; border:0; border-radius:10px; background:var(--gruen); color:#fff; cursor:pointer; justify-self:start; }}
button:focus-visible, input:focus-visible, a:focus-visible {{ outline:3px solid var(--gruen); outline-offset:2px; }}
code {{ font-family:ui-monospace, Consolas, monospace; background:var(--bg); padding:2px 6px; border-radius:6px; word-break:break-all; }}
.token {{ font-size:15px; display:block; padding:10px; }}
a {{ color:var(--gruen); }}
.status {{ font-weight:700; }}
</style></head><body><main>{inhalt}</main></body></html>"""


def _seite(inhalt: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(SEITE.format(inhalt=inhalt), status_code=status,
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})


def _wert(form: dict, name: str) -> str:
    return html.escape(form.get(name, ""), quote=True)


def formular(csrf: str, form: dict | None = None, fehler: dict | None = None, gesamtfehler: str | None = None) -> HTMLResponse:
    form, fehler = form or {}, fehler or {}

    def f(name: str) -> str:
        return f'<span class="fehler">{html.escape(fehler[name])}</span>' if name in fehler else ""

    return _seite(f"""
<h1>Bewässerung einrichten</h1>
<p class="leise">Diese Seite gibt es nur, bis der Pi eingerichtet ist. Die Schlüssel werden nur auf dem Pi gespeichert
(<code>/etc/bewaesserung/zugang.env</code>, nur für root lesbar) und hier nie wieder angezeigt.</p>
{f'<p class="fehler">{html.escape(gesamtfehler)}</p>' if gesamtfehler else ''}
<form method="post" action="/einrichten" style="display:grid; gap:18px">
<input type="hidden" name="csrf" value="{csrf}">
<section>
  <h2>Gardena smart system</h2>
  <p class="leise">Aus dem Husqvarna-Entwicklerportal (developer.husqvarnagroup.cloud), Anwendung mit
  Authentication API und GARDENA smart system API.</p>
  <label for="gardena_key">Application Key <input id="gardena_key" name="gardena_key" autocomplete="off" spellcheck="false" value="{_wert(form, 'gardena_key')}" required></label>{f('gardena_key')}
  <label for="gardena_secret">Application Secret <input id="gardena_secret" name="gardena_secret" type="password" autocomplete="off" required></label>{f('gardena_secret')}
</section>
<section>
  <h2>Zugriff von unterwegs (Tailscale)</h2>
  <p class="leise">Optional ein Auth-Key aus der Tailscale-Verwaltung. Ohne Key zeigt die nächste Seite einen Anmelde-Link.</p>
  <label for="tailscale_authkey">Tailscale-Auth-Key <input id="tailscale_authkey" name="tailscale_authkey" type="password" autocomplete="off" placeholder="tskey-…"></label>{f('tailscale_authkey')}
</section>
<section>
  <h2>Standort für das Wetter</h2>
  <p class="leise">Optional, in Grad. Den Wetterdiensten wird er nur auf etwa 1 km gerundet übermittelt.</p>
  <div class="zwei">
    <label for="breite">Breite <input id="breite" name="breite" inputmode="decimal" placeholder="51,05" value="{_wert(form, 'breite')}"></label>
    <label for="laenge">Länge <input id="laenge" name="laenge" inputmode="decimal" placeholder="13,74" value="{_wert(form, 'laenge')}"></label>
  </div>{f('standort')}
</section>
<section>
  <h2>Claude-Assistent</h2>
  <p class="leise">Optional, für Saisonplan-Entwürfe. Schlüssel von platform.claude.com.</p>
  <label for="claude_key">Claude-API-Schlüssel <input id="claude_key" name="claude_key" type="password" autocomplete="off" placeholder="sk-ant-…"></label>{f('claude_key')}
</section>
<p class="leise">Der Pi startet im Trockenlauf: Er schaltet nichts, sondern protokolliert nur. Echtes Schalten wird
erst nach den Tests am Ventil von Hand freigegeben.</p>
<button type="submit">Prüfen und speichern</button>
</form>""", 422 if (fehler or gesamtfehler) else 200)


def app_bauen(system: System, beenden: Callable[[], None] = lambda: None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    stand = Stand()
    csrf = secrets.token_urlsafe(24)

    def csrf_ok(form: dict) -> bool:
        return hmac.compare_digest(form.get("csrf", "").encode(), csrf.encode())

    def eigene_sitzung(request: Request) -> bool:
        wert = request.cookies.get(COOKIE, "")
        return bool(stand.sitzung) and hmac.compare_digest(wert.encode(), stand.sitzung.encode())

    async def formular_lesen(request: Request) -> dict:
        roh = (await request.body())[:20000].decode(errors="replace")
        return {k: v[0] for k, v in parse_qs(roh, keep_blank_values=True).items()}

    @app.get("/", response_class=HTMLResponse)
    async def start(request: Request):
        if stand.phase != "OFFEN":
            return RedirectResponse("/fertig", status_code=303)
        if system.zugang_vorhanden():
            return _seite("<h1>Schon eingerichtet</h1><p>Die Zugangsdaten sind gespeichert. Zum erneuten Einrichten "
                          "per SSH <code>sudo rm /etc/bewaesserung/zugang.env</code> und "
                          "<code>sudo systemctl start bewaesserung-einrichtung</code>.</p>")
        return formular(csrf)

    @app.post("/einrichten")
    async def einrichten(request: Request):
        form = await formular_lesen(request)
        if not csrf_ok(form):
            return _seite("<h1>Sitzung abgelaufen</h1><p><a href=\"/\">Seite neu laden</a> und erneut absenden.</p>", 403)
        if stand.phase != "OFFEN" or system.zugang_vorhanden():
            return _seite("<h1>Schon eingerichtet</h1>", 409)
        eingaben, fehler = lesen(form)
        if eingaben is None:
            return formular(csrf, form, fehler)
        problem = await system.gardena_pruefen(eingaben.gardena_key, eingaben.gardena_secret)
        if problem:
            return formular(csrf, form, gesamtfehler=problem)
        stand.token = api_token()
        stand.eingaben = eingaben
        system.zugang_schreiben(zugang_text(eingaben, stand.token))
        await system.dienst_neustarten()
        stand.phase = "TAILSCALE"
        stand.sitzung = secrets.token_urlsafe(24)
        aufgabe = asyncio.get_running_loop().create_task(tailscale(eingaben))
        stand.aufgaben.add(aufgabe)
        antwort = RedirectResponse("/fertig", status_code=303)
        antwort.set_cookie(COOKIE, stand.sitzung, httponly=True, samesite="strict")
        log.info("Einrichtung: Zugangsdaten gespeichert, Tailscale wird angemeldet")
        return antwort

    async def tailscale(eingaben: Eingaben) -> None:
        def link(url: str) -> None:
            stand.login_link = url
        try:
            ip = await system.tailscale_verbinden(eingaben.tailscale_authkey, link)
        except Exception as f:                       # Tailscale fehlt o. Ä.: API bleibt auf localhost
            log.exception("Tailscale-Anmeldung fehlgeschlagen")
            ip, stand.tailscale_fehler = None, str(f)
        if ip:
            stand.tailscale_ip = ip
            system.zugang_schreiben(zugang_text(eingaben, stand.token, ip))
            await system.dienst_neustarten()
        elif not stand.tailscale_fehler:
            stand.tailscale_fehler = "Tailscale-Anmeldung hat nicht geklappt"
        stand.phase = "FERTIG"
        asyncio.get_running_loop().call_later(30 * 60, beenden)     # spätestens nach 30 min aus

    @app.get("/stand")
    async def stand_lesen():
        return JSONResponse({"phase": stand.phase, "login_link": stand.login_link, "tailscale_ip": stand.tailscale_ip,
                             "tailscale_fehler": stand.tailscale_fehler}, headers={"Cache-Control": "no-store"})

    @app.get("/fertig", response_class=HTMLResponse)
    async def fertig(request: Request):
        if stand.phase == "OFFEN":
            return RedirectResponse("/", status_code=303)
        token = (f'<p>Dieses Token braucht die App. Es wird nur jetzt angezeigt; bitte sicher notieren.</p>'
                 f'<code class="token" id="token">{stand.token}</code>' if eigene_sitzung(request)
                 else '<p class="leise">Das API-Token sieht nur der Browser, der die Einrichtung abgeschickt hat.</p>')
        return _seite(f"""
<h1>Gespeichert</h1>
<section><h2>Gardena</h2><p>Zugang geprüft und gespeichert. Der Dienst läuft im Trockenlauf.</p></section>
<section><h2>Tailscale</h2><p class="status" id="ts">Wird angemeldet …</p><p id="ts-link"></p></section>
<section><h2>Für die App</h2>{token}<p id="adresse" class="leise"></p></section>
<form method="post" action="/abschliessen"><input type="hidden" name="csrf" value="{csrf}"><button type="submit">Fertig, Seite schließen</button></form>
<script>
async function pruefen() {{
  try {{
    const s = await (await fetch('/stand', {{cache: 'no-store'}})).json();
    const ts = document.getElementById('ts'), link = document.getElementById('ts-link'), adr = document.getElementById('adresse');
    if (s.tailscale_ip) {{ ts.textContent = 'Verbunden: ' + s.tailscale_ip; link.textContent = '';
      adr.textContent = 'Adresse für die App: http://' + s.tailscale_ip + ':8080'; return; }}
    if (s.tailscale_fehler && s.phase === 'FERTIG') {{ ts.textContent = 'Nicht verbunden: ' + s.tailscale_fehler +
      '. Die API ist vorerst nur auf dem Pi selbst erreichbar; später per SSH: sudo tailscale up'; return; }}
    if (s.login_link) {{ link.innerHTML = ''; const a = document.createElement('a'); a.href = s.login_link; a.target = '_blank';
      a.rel = 'noopener'; a.textContent = 'Bei Tailscale anmelden'; link.append('Bitte hier anmelden: ', a);
      ts.textContent = 'Wartet auf Anmeldung …'; }}
  }} catch (e) {{}}
  setTimeout(pruefen, 2000);
}}
pruefen();
</script>""")

    @app.post("/abschliessen")
    async def abschliessen(request: Request):
        form = await formular_lesen(request)
        if not csrf_ok(form) or stand.phase == "OFFEN":
            return _seite("<h1>Nicht möglich</h1>", 403)
        if stand.phase != "FERTIG":
            return _seite("<h1>Bitte kurz warten</h1><p>Tailscale wird noch angemeldet. "
                          "<a href=\"/fertig\">Zurück</a></p>", 409)
        asyncio.get_running_loop().call_later(1.0, beenden)
        return _seite("<h1>Fertig</h1><p>Die Einrichtungsseite ist jetzt aus. Der Pi läuft weiter und startet "
                      "die Seite nicht mehr.</p>")

    app.state.stand = stand
    app.state.csrf = csrf
    return app


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    zugang = Path(os.environ.get("EINRICHTUNG_ZUGANG", "/etc/bewaesserung/zugang.env"))
    port = int(os.environ.get("EINRICHTUNG_PORT", "80"))
    server: uvicorn.Server | None = None

    def beenden() -> None:
        if server is not None:
            server.should_exit = True

    app = app_bauen(EchtesSystem(zugang), beenden)
    # Heimnetz: alle Adressen, damit http://bewaesserung.local erreichbar ist. Läuft nur bis zur Einrichtung.
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning"))
    server.run()
