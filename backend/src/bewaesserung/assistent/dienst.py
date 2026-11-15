"""Claude-Assistent: Saisonplan-Entwurf erstellen, prüfen, speichern, übernehmen.

Ablauf laut Plan:
1. Die App schickt Wünsche (POST /entwuerfe). Der Entwurf wird mit Status IN_ARBEIT angelegt.
2. Kontext zusammenstellen: Zonen, Kopplungen, Standort, Klima der Vorsaison (Open-Meteo-Archiv),
   Verlauf der Vorsaison, bestehende Regeln, Grenzen.
3. Claude antwortet mit JSON nach festem Schema. Prüfung wie bei manuellen Plänen (`entwurf.pruefen`).
   Bei Fehlern genau ein zweiter Versuch mit der Fehlerliste, danach FEHLERHAFT mit Fehlern.
4. OFFEN: Die App zeigt den Entwurf im Kalender. Übernehmen ganz oder je Zone; die alten Regeln dieser
   Zonen werden deaktiviert (nicht gelöscht), die neuen bekommen `quelle = CLAUDE`.

Der Assistent kennt weder Orchestrator noch Gardena-Client: Claude schaltet nie Geräte.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Callable, Protocol

from ..planung.plan import ORTSZEIT, WOCHENTAGE, GeplanterLauf, laeufe
from ..speicher.datenbank import Entwurf, Speicher
from .claude import Ablehnung, Entwerfer
from .entwurf import EntwurfRegel, Grenzen, Pruefung, pruefen, tagessummen, ueberschneidungen

log = logging.getLogger(__name__)


class Klimaquelle(Protocol):
    async def vorsaison(self, breite: float, laenge: float, jahr: int) -> list[dict]: ...


class EntwurfNichtMoeglich(Exception):
    pass


def _tage(maske: int) -> str:
    return ",".join(t for i, t in enumerate(WOCHENTAGE) if maske >> i & 1)


class Assistent:
    def __init__(self, speicher: Speicher, entwerfer: Entwerfer | None, klima: Klimaquelle | None = None,
                 grenzen: Grenzen = Grenzen(), uhr: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.speicher = speicher
        self.entwerfer = entwerfer
        self.klima = klima
        self.grenzen = grenzen
        self._uhr = uhr
        self._aufgaben: set[asyncio.Task] = set()
        for e in speicher.entwuerfe():                     # nach Neustart hängengebliebene Entwürfe
            if e.status == "IN_ARBEIT":
                speicher.entwurf_aendern(e.id, status="FEHLERHAFT", fehler=["Dienst wurde währenddessen neu gestartet"])

    @property
    def verfuegbar(self) -> bool:
        return self.entwerfer is not None

    # --- Erstellen -------------------------------------------------------

    def starten(self, wuensche: str, saison: int | None = None) -> Entwurf:
        if self.entwerfer is None:
            raise EntwurfNichtMoeglich("Kein Claude-API-Schlüssel eingerichtet")
        if any(e.status == "IN_ARBEIT" for e in self.speicher.entwuerfe()):
            raise EntwurfNichtMoeglich("Es wird schon ein Entwurf erstellt")
        saison = saison or self._standard_saison()
        e = self.speicher.entwurf_anlegen(saison, wuensche)
        aufgabe = asyncio.get_running_loop().create_task(self.erstellen(e.id))
        self._aufgaben.add(aufgabe)
        aufgabe.add_done_callback(self._aufgaben.discard)
        return e

    def _standard_saison(self) -> int:
        jetzt = self._uhr().astimezone(ORTSZEIT)
        return jetzt.year if jetzt.month <= 8 else jetzt.year + 1

    async def erstellen(self, entwurf_id: int) -> Entwurf:
        e = self.speicher.entwurf(entwurf_id)
        assert self.entwerfer is not None and e is not None
        self.speicher.entwurf_aendern(entwurf_id, modell=self.entwerfer.modell)
        try:
            kontext = await self.kontext(e.saison)
            inhalt = await self.entwerfer.entwerfen(kontext, e.wuensche)
            pruefung = self.pruefen(inhalt)
            if not pruefung.ok:
                log.info("Entwurf %d: %d Fehler, zweiter Versuch", entwurf_id, len(pruefung.fehler))
                inhalt = await self.entwerfer.entwerfen(kontext, e.wuensche, pruefung.fehler)
                pruefung = self.pruefen(inhalt)
            self.speicher.entwurf_aendern(entwurf_id, inhalt=inhalt, fehler=pruefung.fehler,
                                          status="OFFEN" if pruefung.ok else "FEHLERHAFT")
        except Ablehnung as f:
            self.speicher.entwurf_aendern(entwurf_id, status="FEHLERHAFT", fehler=[str(f)])
        except Exception as f:
            log.exception("Entwurf %d fehlgeschlagen", entwurf_id)
            self.speicher.entwurf_aendern(entwurf_id, status="FEHLERHAFT", fehler=[f"Interner Fehler: {f}"])
        return self.speicher.entwurf(entwurf_id)

    async def kontext(self, saison: int) -> str:
        teile = [f"Saison: {saison}. Heute: {self._uhr().astimezone(ORTSZEIT):%d.%m.%Y}.", "",
                 "Zonen:"]
        for z in self.speicher.zonen():
            k = self.speicher.kopplung_fuer_ventil(z.ventil_id)
            angaben = [f"Pflanzen: {z.pflanzentyp or 'unbekannt'}", f"Boden: {z.boden or 'unbekannt'}",
                       f"Sonne: {z.sonnenlage or 'unbekannt'}"]
            if z.durchfluss_l_min:
                angaben.append(f"Durchfluss {z.durchfluss_l_min:g} l/min")
            angaben.append(f"Höchstlaufzeit {k.max_laufzeit_s // 60} min" if k else "KEINE Kopplung")
            teile.append(f"- zone_id {z.id}: {z.name} ({'; '.join(angaben)})")
        teile += ["", f"Grenzen: {self.grenzen.text()} Laufzeiten in ganzen Minuten.", ""]

        standort = self.speicher.standort()
        if standort:
            teile.append(f"Standort: {standort[0]:.1f}° N, {standort[1]:.1f}° O (Deutschland).")
            if self.klima is not None:
                try:
                    monate = await self.klima.vorsaison(*standort, saison - 1)
                    teile.append(f"Wetter der Vorsaison {saison - 1} (Open-Meteo-Archiv):")
                    teile += [f"- {m['monat']}: ET₀ {m['et0_mm']} mm, Regen {m['regen_mm']} mm, "
                              f"Tageshöchstwert im Mittel {m['tmax_mittel_c']} °C" for m in monate]
                except Exception as f:
                    log.warning("Klima der Vorsaison nicht abrufbar: %s", f)
                    teile.append("Wetter der Vorsaison: nicht verfügbar.")
        else:
            teile.append("Standort: nicht eingestellt.")

        von = datetime(saison - 1, 1, 1, tzinfo=timezone.utc)
        statistik = self.speicher.statistik(von, von.replace(year=saison))
        teile += ["", f"Verlauf der Vorsaison {saison - 1}:"]
        teile += [f"- zone_id {s.zone_id}: {s.laeufe} Läufe, {s.minuten:g} min, {s.wetter_ausgesetzt}× wegen Wetter "
                  f"ausgesetzt, {s.manuell} manuelle Läufe, {s.abgebrochen} abgebrochen" for s in statistik] or ["- keine Daten"]

        regeln = [r for r in self.speicher.regeln() if r.aktiv]
        teile += ["", "Bisherige aktive Regeln:"]
        teile += [f"- zone_id {r.zone_id}: {_tage(r.wochentage)} {r.startzeit:%H:%M}, {r.dauer_min} min"
                  + (f", {r.gueltig_von} bis {r.gueltig_bis}" if r.gueltig_von or r.gueltig_bis else "")
                  for r in regeln] or ["- keine"]
        return "\n".join(teile)

    # --- Prüfen, Anzeigen, Übernehmen ------------------------------------

    def _zonen_und_kopplungen(self):
        zonen = {z.id: z.name for z in self.speicher.zonen()}
        kopplungen = {z.id: self.speicher.kopplung_fuer_ventil(z.ventil_id) for z in self.speicher.zonen()}
        return zonen, kopplungen

    def pruefen(self, inhalt: dict, nur_zonen: set[int] | None = None) -> Pruefung:
        if nur_zonen is not None and isinstance(inhalt, dict):
            inhalt = {**inhalt, "zonen": [z for z in inhalt.get("zonen") or [] if z.get("zone_id") in nur_zonen]}
        return pruefen(inhalt, *self._zonen_und_kopplungen(), self.grenzen)

    def plan(self, entwurf_id: int, von: date, bis: date) -> list[GeplanterLauf]:
        e = self._entwurf(entwurf_id)
        pruefung = self.pruefen(e.inhalt or {})
        regeln = [r.als_regel(-(i + 1)) for i, r in enumerate(pruefung.regeln)]   # negative IDs: Entwurf
        return laeufe(regeln, [], von, bis, ORTSZEIT)

    def _entwurf(self, entwurf_id: int) -> Entwurf:
        e = self.speicher.entwurf(entwurf_id)
        if e is None:
            raise KeyError(entwurf_id)
        return e

    def uebernehmen(self, entwurf_id: int, zone_ids: list[int] | None = None) -> list[int]:
        """Regeln des Entwurfs für diese Zonen (None = alle) anlegen. Gibt die neuen Regel-IDs zurück."""
        e = self._entwurf(entwurf_id)
        if e.status != "OFFEN":
            raise EntwurfNichtMoeglich(f"Entwurf ist {e.status}, übernehmen geht nur bei OFFEN")
        im_entwurf = {z.get("zone_id") for z in (e.inhalt or {}).get("zonen") or []}
        auswahl = set(zone_ids) if zone_ids is not None else im_entwurf
        if not auswahl or not auswahl <= im_entwurf:
            raise EntwurfNichtMoeglich("Zonenauswahl passt nicht zum Entwurf")
        pruefung = self.pruefen(e.inhalt, auswahl)          # erneut: Kopplungen können sich geändert haben
        fehler = list(pruefung.fehler)
        if pruefung.regeln:
            bleibende = self._bleibende_regeln(auswahl, pruefung.regeln)
            zonen, _ = self._zonen_und_kopplungen()
            fehler += ueberschneidungen(pruefung.regeln + bleibende, zonen)
            fehler += tagessummen(pruefung.regeln + bleibende, self.grenzen)
        if fehler:
            raise EntwurfNichtMoeglich("; ".join(dict.fromkeys(fehler)))
        neue = []
        with self.speicher.transaktion():
            for r in self.speicher.regeln():
                if r.zone_id in auswahl and r.aktiv:
                    self.speicher.regel_aendern(r.id, aktiv=False)
            for r in pruefung.regeln:
                neue.append(self.speicher.regel_anlegen(
                    zone_id=r.zone_id, wochentage=r.wochentage, startzeit=r.startzeit, dauer_min=r.dauer_min,
                    gueltig_von=r.gueltig_von, gueltig_bis=r.gueltig_bis, aktiv=True, quelle="CLAUDE").id)
            self.speicher.entwurf_aendern(entwurf_id, status="UEBERNOMMEN", uebernommen=sorted(auswahl))
        return neue

    def _bleibende_regeln(self, auswahl: set[int], neu: list[EntwurfRegel]) -> list[EntwurfRegel]:
        """Aktive Regeln anderer Zonen, auf den Zeitraum des Entwurfs begrenzt, für die Überschneidungsprüfung."""
        anfang, ende = min(r.gueltig_von for r in neu), max(r.gueltig_bis for r in neu)
        bleibende = []
        for r in self.speicher.regeln():
            if r.zone_id in auswahl or not r.aktiv:
                continue
            von, bis = max(r.gueltig_von or anfang, anfang), min(r.gueltig_bis or ende, ende)
            if von <= bis:
                bleibende.append(EntwurfRegel(r.zone_id, "BESTEHEND", r.wochentage, r.startzeit, r.dauer_min, von, bis))
        return bleibende

    def verwerfen(self, entwurf_id: int) -> None:
        e = self._entwurf(entwurf_id)
        if e.status == "UEBERNOMMEN":
            raise EntwurfNichtMoeglich("Übernommene Entwürfe lassen sich nicht verwerfen; Regeln einzeln ändern")
        self.speicher.entwurf_aendern(entwurf_id, status="VERWORFEN")

    async def schliessen(self) -> None:
        for a in list(self._aufgaben):
            a.cancel()
        schliessen = getattr(self.entwerfer, "schliessen", None)
        if schliessen is not None:
            await schliessen()
        http = getattr(self.klima, "http", None)
        if http is not None:
            await http.aclose()

