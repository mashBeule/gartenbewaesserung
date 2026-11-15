"""Aufruf der Claude API für einen Saisonplan-Entwurf.

Claude bekommt nur Text (Kontext, Wünsche) und antwortet mit JSON nach `entwurf.SCHEMA`
(structured outputs, `output_config.format`). Claude hat keine Werkzeuge und damit keinen
Zugriff auf Geräte, Datenbank oder Netz des Pi.

Modell laut Plan: ein aktuelles Sonnet (`claude-sonnet-5-5`), über CLAUDE_MODELL änderbar.
Lehnt das Modell aus Sicherheitsgründen ab, springt serverseitig ein Ausweichmodell ein
(`fallbacks: "default"`); lehnt auch das ab, meldet `Ablehnung` das der App.

⚠ Ungeprüft gegen die echte API (kein Schlüssel im Repo, Aufrufe kosten Geld).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import anthropic

from .entwurf import SCHEMA

STANDARD_MODELL = "claude-sonnet-5-5"
MAX_TOKENS = 16000

SYSTEM = """Du planst die Bewässerung eines Hausgartens in Deutschland für eine Saison.
Gesteuert werden Gardena-Ventile; zu jedem Ventil läuft eine Pumpe, deshalb laufen alle Zonen nacheinander.
Du erstellst nur einen Entwurf: Der Gartenbesitzer prüft ihn in einer App und übernimmt ihn ganz, teilweise
oder gar nicht. Du schaltest nichts.

Teile die Saison in die Phasen FRUEHJAHR, HOCHSOMMER und HERBST und gib je Zone und Phase eine oder wenige Regeln an
(Wochentage, Startzeit, Dauer in Minuten, Gültigkeitszeitraum). Die Wetteranpassung (Regen, Frost, Verdunstung)
macht das System selbst vor jedem Lauf; plane deshalb für durchschnittliches Wetter der jeweiligen Phase.
Halte dich genau an die Grenzen im Kontext. Lieber seltener und gründlich gießen als täglich kurz.
Zonen ohne Kopplung bekommen keine Regeln. Antworte auf Deutsch, Begründungen kurz und konkret."""


class Ablehnung(Exception):
    """Das Modell hat den Entwurf abgelehnt oder keine verwertbare Antwort geliefert."""


class Entwerfer(Protocol):
    modell: str

    async def entwerfen(self, kontext: str, wuensche: str, fehler: list[str] | None = None) -> dict: ...


def nachricht(kontext: str, wuensche: str, fehler: list[str] | None) -> str:
    teile = [f"<kontext>\n{kontext}\n</kontext>",
             f"<wuensche>\n{wuensche.strip() or 'keine besonderen Wünsche'}\n</wuensche>"]
    if fehler:
        liste = "\n".join(f"- {f}" for f in fehler)
        teile.append(f"Dein vorheriger Entwurf wurde abgelehnt. Behebe diese Fehler:\n{liste}")
    teile.append("Erstelle den Saisonplan-Entwurf.")
    return "\n\n".join(teile)


@dataclass
class ClaudeEntwerfer:
    api_key: str
    modell: str = STANDARD_MODELL

    def __post_init__(self) -> None:
        self.client = anthropic.AsyncAnthropic(api_key=self.api_key)

    async def entwerfen(self, kontext: str, wuensche: str, fehler: list[str] | None = None) -> dict:
        try:
            antwort = await self.client.beta.messages.create(
                model=self.modell,
                max_tokens=MAX_TOKENS,
                system=SYSTEM,
                messages=[{"role": "user", "content": nachricht(kontext, wuensche, fehler)}],
                output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as f:
            raise Ablehnung("Claude-API-Schlüssel ungültig") from f
        except anthropic.RateLimitError as f:
            raise Ablehnung("Claude API ausgelastet, bitte später erneut versuchen") from f
        except anthropic.APIStatusError as f:
            raise Ablehnung(f"Claude API meldet Fehler {f.status_code}: {f.message}") from f
        except anthropic.APIConnectionError as f:
            raise Ablehnung("Claude API nicht erreichbar") from f
        if antwort.stop_reason == "refusal":
            raise Ablehnung("Claude hat den Entwurf abgelehnt")
        if antwort.stop_reason == "max_tokens":
            raise Ablehnung("Antwort war zu lang und wurde abgeschnitten")
        text = next((b.text for b in antwort.content if b.type == "text"), None)
        if text is None:
            raise Ablehnung("Antwort ohne Text")
        try:
            return json.loads(text)
        except ValueError as f:
            raise Ablehnung("Antwort ist kein gültiges JSON") from f

    async def schliessen(self) -> None:
        await self.client.close()
