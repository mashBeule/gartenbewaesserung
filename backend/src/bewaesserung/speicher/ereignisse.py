"""Sicherheitsereignisse ins run_log schreiben (und weiter an eine Push-Meldung, sobald es die gibt)."""
from __future__ import annotations

import logging

from ..sicherheit.ereignisse import Ereignisse, Sicherheitsereignis
from .datenbank import Speicher

log = logging.getLogger(__name__)


class SpeicherEreignisse:
    def __init__(self, speicher: Speicher, weiter: Ereignisse | None = None) -> None:
        self.speicher = speicher
        self.weiter = weiter

    def melden(self, ereignis: Sicherheitsereignis) -> None:
        log.warning("Sicherheitsereignis %s: %s", ereignis.art.value, ereignis.text)
        zone_id = None
        if ereignis.geraet_id:
            zone_id = next((z.id for z in self.speicher.zonen() if z.ventil_id == ereignis.geraet_id), None)
        try:
            self.speicher.protokollieren("SICHERHEIT", zone_id=zone_id, text=ereignis.text,
                                         sicherheitsereignis=ereignis.art.value, zeitpunkt=ereignis.zeitpunkt)
        except Exception:
            log.exception("Sicherheitsereignis konnte nicht gespeichert werden")
        if self.weiter is not None:
            self.weiter.melden(ereignis)
