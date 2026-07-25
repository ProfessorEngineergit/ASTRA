"""Jobs — das kleine Modell beauftragt das große, mit klarem Rechte-Umschlag.

Bahrians Bild: „ich möchte nicht, dass das kleine AI-Modell das macht, sondern dass
es Cloud beauftragen kann, Berechtigungen laufen darüber zurück."

So läuft es:
  1. `delegate_job` legt einen Job an: Ziel, Art, **Umschlag** (welche Hosts, darf
     geschrieben werden, Budget).
  2. Der Worker lässt die `reason`-Rolle (Claude) einen konkreten Plan schreiben —
     inklusive der Befehle, die nötig wären.
  3. Jeder vorgeschlagene Befehl läuft durch `ops_policy`: Allow-List darf autonom,
     alles andere erzeugt eine `approvals`-Zeile mit Telegram-Knopf, Destruktives
     wird gar nicht erst angeboten.
Der Umschlag ist der Punkt: das große Modell darf denken, aber nicht mehr anfassen,
als der Job erlaubt.
"""
from __future__ import annotations

import json
import logging

from . import db, ops_policy
from .models import get_gateway

log = logging.getLogger("astra.jobs")

_PLANNER_SYSTEM = """\
Du planst eine Aufgabe in Bahrians selbst gehostetem HomeLab (Proxmox, Docker,
Debian-LXCs, Home Assistant). Antworte NUR mit JSON dieser Form:

{"analysis": "kurze Lagebeurteilung",
 "steps": [{"why": "wozu", "command": "ein einzelner Shell-Befehl", "host": "hostname"}],
 "risk": "low|medium|high"}

Regeln:
- Ein Befehl pro Schritt, keine Verkettung mit && oder ;.
- Bevorzuge lesende Diagnose vor Eingriffen; schlage den kleinsten wirksamen Eingriff vor.
- Nichts Destruktives (kein rm -rf, kein mkfs, kein Löschen von Volumes/Pools).
- Halte dich strikt an den erlaubten Rechte-Umschlag im Auftrag.
"""


def _parse_plan(raw: str) -> dict:
    """Tolerantes JSON-Parsen (Modelle packen gern ```json drumherum)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.strip("`")
        text = text.removeprefix("json").strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {}


def envelope_allows(envelope: dict, host: str) -> bool:
    """Darf der Job diesen Host überhaupt anfassen? Leere Hostliste = nur lesen/planen."""
    hosts = [h.lower() for h in (envelope or {}).get("hosts") or []]
    if not hosts:
        return False
    return "*" in hosts or (host or "").lower() in hosts


def review_steps(steps: list, envelope: dict) -> list[dict]:
    """Jeden geplanten Schritt gegen Policy UND Umschlag prüfen. Rein — testbar."""
    reviewed: list[dict] = []
    write_ok = bool((envelope or {}).get("write"))
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        command = str(step.get("command") or "").strip()
        host = str(step.get("host") or "").strip()
        decision, reason = ops_policy.classify(command)
        if decision != ops_policy.BLOCK:
            if not envelope_allows(envelope, host):
                decision, reason = ops_policy.BLOCK, f"Host '{host}' nicht im Rechte-Umschlag"
            elif decision == ops_policy.ALLOW and not write_ok and not _is_read_only(command):
                decision, reason = ops_policy.APPROVE, "Umschlag erlaubt kein Schreiben"
        reviewed.append({"command": command, "host": host, "why": step.get("why", ""),
                         "decision": decision, "reason": reason})
    return reviewed


def _is_read_only(command: str) -> bool:
    """Grober Lesetest für read-only-Umschläge (restart/start/stop sind Schreiben)."""
    lowered = (command or "").lower()
    return not any(w in lowered for w in
                   (" restart", " start", " stop", " up ", " down", " apply", " set ",
                    " install", " remove", " write", " chmod", " chown", " mv ", " cp "))


async def run_job(job_id: int) -> str:
    """Plan holen, Schritte prüfen, Freigaben anlegen. Führt selbst nichts aus —
    ausgeführt wird über ops_exec (W8), sobald freigegeben."""
    job = await db.get_job(job_id)
    if not job:
        return "Job nicht gefunden."
    await db.update_job(job_id, status="running")
    gw = get_gateway()
    envelope = job.get("envelope") or {}
    prompt = (f"Auftrag: {job['goal']}\n"
              f"Art: {job['kind']}\n"
              f"Rechte-Umschlag (verbindlich): {json.dumps(envelope, ensure_ascii=False)}")
    try:
        raw = await gw.reason(_PLANNER_SYSTEM, prompt)
    except Exception as e:  # noqa: BLE001
        await db.update_job(job_id, status="failed", result=f"Planung fehlgeschlagen: {e}")
        return f"Planung fehlgeschlagen: {e}"

    plan = _parse_plan(raw)
    reviewed = review_steps(plan.get("steps") or [], envelope)
    lines = [f"Analyse: {plan.get('analysis', '—')}", f"Risiko: {plan.get('risk', '?')}", ""]
    pending = 0
    for i, step in enumerate(reviewed, 1):
        mark = {"allow": "✅ autonom", "approve": "🟡 Freigabe", "block": "⛔ blockiert"}[step["decision"]]
        lines.append(f"{i}. [{mark}] {step['host']}: {step['command']}  — {step['reason']}")
        if step["decision"] == ops_policy.APPROVE:
            pending += 1
    summary = "\n".join(lines)
    await db.update_job(job_id, status="done", plan=summary,
                        result=f"{len(reviewed)} Schritte, {pending} brauchen Freigabe")
    await db.audit("job_planned", detail={"job": job_id, "steps": len(reviewed), "pending": pending})
    return summary
