"""Modellwahl pro Chat/Kontakt — so einfach wie möglich.

Eine Wahl ist ein kleines Dict, das das Gateway versteht (``models.resolve_pick``):
  {"tier": "small|medium|heavy|code|osint"}    → die Rolle nehmen (Anbieter/Modell aus den Einstellungen)
  {"provider": "openrouter", "model": "x/y"}   → ein konkretes Modell
  {} / None                                    → Standard des Aufrufs

Hier steht nur die reine Logik (ohne I/O): Optionsliste für die UI, Textbefehle
("/modell schwer", "/modell openrouter:anthropic/claude-sonnet-5"), Labels.
"""
from __future__ import annotations

import re
from typing import Any

TIER_LABELS = {
    "small": "Klein · schnell & billig",
    "medium": "Mittel · Standard",
    "heavy": "Schwer · beste Qualität",
    "code": "Code",
    "osint": "Recherche",
}
# Umgangssprachliche Wörter → Rolle (für Telegram/Chat-Befehle).
TIER_WORDS = {
    "klein": "small", "small": "small", "schnell": "small", "billig": "small", "mini": "small",
    "mittel": "medium", "medium": "medium", "standard": "medium", "normal": "medium",
    "schwer": "heavy", "heavy": "heavy", "gross": "heavy", "groß": "heavy", "stark": "heavy",
    "beste": "heavy", "claude": "heavy",
    "code": "code", "coding": "code", "codex": "code",
    "osint": "osint", "recherche": "osint",
}
RESET_WORDS = {"auto", "default", "reset", "zurück", "zurueck", "aus", "leer", "-"}

_MODEL_CMD = re.compile(r"^\s*/(?:modell|model)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)


def clean_pick(pick: Any) -> dict:
    """Nur bekannte Felder, nur Strings — die Wahl kommt aus dem Browser."""
    if not isinstance(pick, dict):
        return {}
    tier = str(pick.get("tier") or "").strip().lower()
    if tier in TIER_LABELS:
        return {"tier": tier}
    provider = str(pick.get("provider") or "").strip().lower()
    model = str(pick.get("model") or "").strip()
    if provider and model and re.fullmatch(r"[a-z0-9_.-]{1,40}", provider) and len(model) <= 120 \
            and not re.search(r"[\s<>\"'`]", model):
        return {"provider": provider, "model": model}
    return {}


def encode(pick: dict | None) -> str:
    """Pick → Wert für ein <select> (tier:heavy / model:openrouter|x/y / '')."""
    pick = clean_pick(pick)
    if pick.get("tier"):
        return f"tier:{pick['tier']}"
    if pick.get("provider"):
        return f"model:{pick['provider']}|{pick['model']}"
    return ""


def decode(value: str) -> dict:
    value = (value or "").strip()
    if value.startswith("tier:"):
        return clean_pick({"tier": value[5:]})
    if value.startswith("model:") and "|" in value:
        provider, _, model = value[6:].partition("|")
        return clean_pick({"provider": provider, "model": model})
    return {}


def label(pick: dict | None, snapshot: dict | None = None) -> str:
    """Kurzer, ehrlicher Name der Wahl für Pills/Status."""
    pick = clean_pick(pick)
    roles = (snapshot or {}).get("roles") or {}
    if pick.get("tier"):
        t = pick["tier"]
        entry = roles.get(t) or {}
        name = TIER_LABELS[t].split(" · ")[0]
        if entry.get("model"):
            return f"{name} ({entry.get('provider', '?')}/{entry['model']})"
        return f"{name} (nicht konfiguriert)"
    if pick.get("provider"):
        return f"{pick['provider']}/{pick['model']}"
    default = roles.get("medium") or {}
    if default.get("model"):
        return f"Standard ({default.get('provider', '?')}/{default['model']})"
    return "Standard"


def options(snapshot: dict, seen: list[dict] | None = None) -> list[dict]:
    """Auswahlliste: Standard, Rollen (nur konfigurierte als aktiv), zuletzt genutzte Modelle.
    Jede Option: {value, label, group, disabled}."""
    roles = snapshot.get("roles") or {}
    providers = snapshot.get("providers") or {}
    out: list[dict] = [{"value": "", "label": "Standard (wie eingestellt)", "group": "Automatisch",
                        "disabled": False}]
    for tier, text in TIER_LABELS.items():
        entry = roles.get(tier) or {}
        prov = providers.get(entry.get("provider") or "") or {}
        ok = bool(entry.get("model")) and bool(prov.get("configured"))
        if tier == "code" and not entry.get("provider"):
            ok, entry = True, roles.get("medium") or {}      # ohne Coding-Modell → mittel
        suffix = f" — {entry.get('provider')}/{entry.get('model')}" if entry.get("model") else " — nicht eingerichtet"
        out.append({"value": f"tier:{tier}", "label": text + suffix, "group": "Stufen", "disabled": not ok})
    known = set()
    for row in seen or []:
        prov, model = str(row.get("provider") or ""), str(row.get("model") or "")
        if not prov or not model or (prov, model) in known:
            continue
        known.add((prov, model))
        p = providers.get(prov) or {}
        out.append({"value": f"model:{prov}|{model}", "label": f"{prov}/{model}",
                    "group": "Zuletzt genutzt", "disabled": not p.get("configured", False)})
    return out


def parse_command(text: str, snapshot: dict | None = None) -> tuple[bool, dict | None, str]:
    """Erkennt '/modell …'. → (ist_befehl, neue_wahl_oder_None, Antworttext).
    `None` als Wahl heißt: nur Status anzeigen; `{}` = zurücksetzen."""
    m = _MODEL_CMD.match(text or "")
    if not m:
        return False, None, ""
    arg = m.group(1).strip()
    if not arg:
        return True, None, ""
    low = arg.lower()
    if low in RESET_WORDS:
        return True, {}, "Modell: wieder Standard."
    if low in TIER_WORDS:
        tier = TIER_WORDS[low]
        return True, {"tier": tier}, f"Modell: {TIER_LABELS[tier].split(' · ')[0]}."
    # provider:model oder provider/model
    prov, sep, model = arg.partition(":")
    if not sep:
        prov, sep, model = arg.partition(" ")
    pick = clean_pick({"provider": prov, "model": model.strip()}) if sep else {}
    if pick:
        return True, pick, f"Modell: {pick['provider']}/{pick['model']}."
    # nur ein Modellname ohne Anbieter → anhand des Snapshots zuordnen
    providers = (snapshot or {}).get("providers") or {}
    if len(providers) and arg and " " not in arg:
        configured = [n for n, p in providers.items() if p.get("configured") and p.get("kind") == "openai_compat"]
        if len(configured) == 1:
            pick = clean_pick({"provider": configured[0], "model": arg})
            if pick:
                return True, pick, f"Modell: {pick['provider']}/{pick['model']}."
    return True, None, ("Das kenne ich nicht. Beispiele: /modell schwer · /modell klein · "
                        "/modell openrouter:anthropic/claude-sonnet-5 · /modell auto")
