"""Command policy — die Sicherheitsgrenze für alles, was ASTRA im HomeLab ausführt.

Bahrians Vorgabe: **Allow-List autonom, alles andere fragt.** Dazu kommt eine dritte
Stufe, die gar nicht erst gefragt wird: destruktive Muster sind hart blockiert.

Reine Logik, kein I/O — genau deshalb testbar. Sie wird von `ops_exec` (W8) und vom
Job-Worker (W7) benutzt, damit beide dieselbe Grenze haben und nicht zwei Meinungen.

Ergebnis: "allow" (autonom) · "approve" (Freigabe nötig) · "block" (nie).
"""
from __future__ import annotations

import re
import shlex

ALLOW = "allow"
APPROVE = "approve"
BLOCK = "block"

# Harmlose Lese-/Statusbefehle. Bewusst eng: was hier nicht steht, wird gefragt.
_ALLOW_EXACT = frozenset({
    "uptime", "whoami", "hostname", "date", "df", "free", "uname", "id",
    "ps", "top", "vmstat", "iostat", "lsblk", "lscpu", "dmesg",
})
_ALLOW_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("docker", "ps"), ("docker", "logs"), ("docker", "stats"), ("docker", "inspect"),
    ("docker", "compose", "ps"), ("docker", "compose", "logs"),
    ("docker", "restart"),                       # neu starten ist reversibel
    ("systemctl", "status"), ("systemctl", "list-units"),
    ("journalctl",), ("git", "status"), ("git", "log"), ("git", "diff"),
    ("cat",), ("head",), ("tail",), ("ls",), ("stat",), ("grep",), ("which",),
    ("ping",), ("curl", "-I"), ("pct", "list"), ("qm", "list"), ("zpool", "status"),
)

# Muster, die nie autonom UND nie per Freigabe laufen — zu leicht katastrophal.
_BLOCK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf]",      # rm -rf / rm -fr …
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^|]*\bof=/dev/",
    r"\bshred\b", r"\bwipefs\b", r"\bblkdiscard\b",
    r"\bpvremove\b", r"\bvgremove\b", r"\blvremove\b",
    r"\bzpool\s+destroy\b", r"\bzfs\s+destroy\b",
    r":\(\)\s*\{.*\};\s*:",                    # fork bomb
    r"\bchmod\s+-R\s+777\s+/\s*$",
    r">\s*/dev/sd[a-z]",
    r"\bdocker\s+system\s+prune\b.*(-a|--all)",
    r"\bdocker\s+volume\s+rm\b",               # volumes = Bahrians Daten
    r"\buserdel\b", r"(^|\s)passwd(\s|$)",     # das Kommando, nicht der Pfad
    r"\bhistory\s+-c\b", r"\bshutdown\b", r"\breboot\b.*-f",
    r"/etc/(shadow|sudoers|passwd)",           # Zugangsdaten-Dateien, auch lesend
))

# Ketten-/Umleitungszeichen: dann ist der Befehl kein einzelner mehr → Freigabe.
_CHAINING = re.compile(r"(\|\||&&|;|\||>|<|`|\$\()")


def classify(command: str) -> tuple[str, str]:
    """(decision, reason) für einen Shell-Befehl. Nie Exception, immer Entscheidung."""
    cmd = (command or "").strip()
    if not cmd:
        return BLOCK, "leerer Befehl"

    for pat in _BLOCK_PATTERNS:
        if pat.search(cmd):
            return BLOCK, f"destruktives Muster ({pat.pattern})"

    # sudo entfernt die Harmlosigkeit — immer fragen.
    if re.match(r"^\s*sudo\b", cmd):
        return APPROVE, "sudo"

    if _CHAINING.search(cmd):
        # Verkettung kann eine Allow-List trivial umgehen ("uptime; rm -rf /").
        return APPROVE, "verkettet/umgeleitet"

    try:
        parts = shlex.split(cmd)
    except ValueError:
        return APPROVE, "nicht eindeutig parsebar"
    if not parts:
        return BLOCK, "leerer Befehl"

    head = parts[0].rsplit("/", 1)[-1]
    if head in _ALLOW_EXACT:
        return ALLOW, "Statusbefehl"
    for prefix in _ALLOW_PREFIXES:
        if tuple(p.rsplit("/", 1)[-1] for p in parts[:len(prefix)]) == prefix:
            return ALLOW, "Allow-List: " + " ".join(prefix)
    return APPROVE, "nicht auf der Allow-List"


def is_allowed_autonomously(command: str) -> bool:
    return classify(command)[0] == ALLOW


def describe(command: str) -> str:
    decision, reason = classify(command)
    label = {ALLOW: "läuft autonom", APPROVE: "braucht deine Freigabe",
             BLOCK: "wird blockiert"}[decision]
    return f"{label} ({reason})"
