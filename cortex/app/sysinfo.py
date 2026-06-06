"""Container performance metrics — dependency-free (/proc, cgroup, shutil).

Returns a snapshot dict the System page renders, plus recommendations. Everything
is best-effort: on non-Linux dev machines fields gracefully fall back to None.
"""
from __future__ import annotations

import os
import shutil
import time


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _mem() -> dict:
    """Memory used/limit in bytes, respecting cgroup limits when present."""
    total = avail = None
    info = _read("/proc/meminfo")
    if info:
        for line in info.splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) * 1024
    # cgroup v2/v1 container limit (often lower than host MemTotal)
    limit = None
    cg2 = _read("/sys/fs/cgroup/memory.max")
    if cg2 and cg2.strip().isdigit():
        limit = int(cg2.strip())
    else:
        cg1 = _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if cg1 and cg1.strip().isdigit():
            v = int(cg1.strip())
            if v < (1 << 62):
                limit = v
    used_cg = None
    cu = _read("/sys/fs/cgroup/memory.current") or _read(
        "/sys/fs/cgroup/memory/memory.usage_in_bytes")
    if cu and cu.strip().isdigit():
        used_cg = int(cu.strip())

    if limit and total and limit < total:
        used = used_cg if used_cg is not None else (total - (avail or 0))
        return {"used": used, "total": limit, "scope": "Container-Limit"}
    if total is not None:
        used = total - (avail or 0)
        return {"used": used, "total": total, "scope": "Host"}
    return {"used": None, "total": None, "scope": None}


def _uptime() -> float | None:
    up = _read("/proc/uptime")
    if up:
        try:
            return float(up.split()[0])
        except ValueError:
            return None
    return None


def _proc_count() -> int | None:
    try:
        return sum(1 for d in os.listdir("/proc") if d.isdigit())
    except OSError:
        return None


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "n/a"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_dur(sec: float | None) -> str:
    if not sec:
        return "n/a"
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return (f"{d}d " if d else "") + f"{h}h {m}m"


def snapshot() -> dict:
    mem = _mem()
    cpu_count = os.cpu_count() or 1
    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        load1 = load5 = load15 = None
    try:
        du = shutil.disk_usage("/")
        disk = {"used": du.used, "total": du.total}
    except OSError:
        disk = {"used": None, "total": None}

    mem_pct = (mem["used"] / mem["total"] * 100) if mem["used"] and mem["total"] else None
    disk_pct = (disk["used"] / disk["total"] * 100) if disk["used"] and disk["total"] else None
    load_pct = (load1 / cpu_count * 100) if load1 is not None else None

    recs: list[dict] = []
    if mem_pct is not None and mem_pct > 88:
        recs.append({"level": "warn", "text": f"RAM bei {mem_pct:.0f}% — dem LXC mehr Arbeitsspeicher zuweisen (Proxmox → Ressourcen)."})
    if disk_pct is not None and disk_pct > 85:
        recs.append({"level": "warn", "text": f"Speicher bei {disk_pct:.0f}% — alte Docker-Images aufräumen (`docker system prune`) oder Disk vergrößern."})
    if load_pct is not None and load_pct > 100:
        recs.append({"level": "warn", "text": f"CPU-Last über {cpu_count} Kerne hinaus — mehr vCPUs zuweisen oder Plugin-Polling reduzieren."})
    if not recs:
        recs.append({"level": "ok", "text": "Alles im grünen Bereich — keine Engpässe erkannt."})

    return {
        "mem": {**mem, "pct": mem_pct, "used_h": _fmt_bytes(mem["used"]), "total_h": _fmt_bytes(mem["total"])},
        "disk": {**disk, "pct": disk_pct, "used_h": _fmt_bytes(disk["used"]), "total_h": _fmt_bytes(disk["total"])},
        "cpu": {"count": cpu_count, "load1": load1, "load5": load5, "load15": load15, "load_pct": load_pct},
        "uptime_h": _fmt_dur(_uptime()),
        "procs": _proc_count(),
        "ts": time.strftime("%H:%M:%S"),
        "recommendations": recs,
    }
