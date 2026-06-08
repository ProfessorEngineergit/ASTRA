"""Secretary context ledger: compact files for contacts, threads, people, and groups."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import get_settings


def _root() -> Path:
    path = Path(get_settings().brain_data_dir) / "secretary"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.@+-]+", "_", value or "unknown").strip("_")
    return value[:120] or "unknown"


def _entry_preview(text: str, limit: int = 420) -> str:
    text = " ".join((text or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _load_tail(path: Path, limit: int = 60) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    out: list[dict] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_capsule(path: Path, entries: list[dict], title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    security = sorted({
        reason
        for item in entries
        for reason in (item.get("security_reasons") or [])
    })
    people = sorted({
        item.get("participant_display") or item.get("display") or item.get("handle") or ""
        for item in entries
        if item.get("participant_display") or item.get("display") or item.get("handle")
    })[:12]
    handles = sorted({
        value
        for item in entries
        for value in (item.get("handle"), item.get("participant_handle"), item.get("username"), item.get("participant_username"))
        if value
    })[:12]
    relationships = sorted({
        value for item in entries for value in (item.get("relationship"), item.get("relationship_note")) if value
    })[:12]
    groups = sorted({
        value for item in entries for value in (item.get("group_context"), item.get("group_id")) if value
    })[:12]
    threads = sorted({item.get("thread_id") for item in entries if item.get("thread_id")})[:12]
    recent = entries[-12:]
    compressed = _compressed_context(entries)
    lines = [
        f"# {title}",
        "",
        f"Updated: {datetime.now(timezone.utc).isoformat()}",
        f"Messages tracked: {len(entries)}",
    ]
    if people:
        lines.extend(["", "## People", *[f"- {p}" for p in people]])
    if handles:
        lines.extend(["", "## Handles", *[f"- {h}" for h in handles]])
    if relationships:
        lines.extend(["", "## Relationship", *[f"- {r}" for r in relationships]])
    if groups:
        lines.extend(["", "## Known From Groups", *[f"- {g}" for g in groups]])
    if threads:
        lines.extend(["", "## Linked Threads", *[f"- {t}" for t in threads]])
    if compressed:
        lines.extend(["", "## Context Capsule", compressed])
    if security:
        lines.extend(["", "## Security Notes", *[f"- {s}" for s in security]])
    lines.extend(["", "## Recent Context"])
    for item in recent:
        who = item.get("role", "user")
        ts = str(item.get("ts", ""))[:19]
        lines.append(f"- {ts} {who}: {_entry_preview(item.get('text', ''), 220)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _compressed_context(entries: list[dict]) -> str:
    if not entries:
        return ""
    recent = entries[-30:]
    by_role: dict[str, int] = {}
    words: dict[str, int] = {}
    for item in recent:
        by_role[item.get("role", "unknown")] = by_role.get(item.get("role", "unknown"), 0) + 1
        for word in re.findall(r"[A-Za-zÄÖÜäöüß0-9][A-Za-zÄÖÜäöüß0-9_-]{3,}", item.get("text", "").lower()):
            if word in {"dass", "nicht", "oder", "aber", "eine", "einen", "wenn", "dann", "bitte", "astra"}:
                continue
            words[word] = words.get(word, 0) + 1
    topics = ", ".join(word for word, _count in sorted(words.items(), key=lambda kv: (-kv[1], kv[0]))[:8])
    counts = ", ".join(f"{role}:{count}" for role, count in sorted(by_role.items()))
    last = _entry_preview(recent[-1].get("text", ""), 180)
    lines = [f"- Rolling window: {len(recent)} recent events ({counts})."]
    if topics:
        lines.append(f"- Recurring terms: {topics}.")
    if last:
        lines.append(f"- Latest signal: {last}")
    return "\n".join(lines)


def _append(entry: dict) -> None:
    root = _root()
    thread_file = root / "threads" / f"{_safe(entry['thread_id'])}.jsonl"
    thread_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    with thread_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    _write_capsule(
        root / "threads" / f"{_safe(entry['thread_id'])}.md",
        _load_tail(thread_file),
        f"Thread {entry['thread_id']}",
    )

    contact_key = f"{entry.get('channel')}:{entry.get('handle')}"
    contact_file = root / "contacts" / f"{_safe(contact_key)}.jsonl"
    contact_file.parent.mkdir(parents=True, exist_ok=True)
    with contact_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    _write_capsule(
        root / "contacts" / f"{_safe(contact_key)}.md",
        _load_tail(contact_file),
        f"Contact {contact_key}",
    )

    if entry.get("is_group"):
        group_key = f"{entry.get('channel')}:{entry.get('group_id') or entry.get('thread_id')}"
        group_file = root / "groups" / f"{_safe(group_key)}.jsonl"
        group_file.parent.mkdir(parents=True, exist_ok=True)
        with group_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        _write_capsule(
            root / "groups" / f"{_safe(group_key)}.md",
            _load_tail(group_file),
            f"Group {group_key}",
        )
        participant = entry.get("participant_handle")
        if participant:
            person_key = f"{entry.get('channel')}:{participant}"
            person_entry = {**entry, "handle": participant, "group_context": group_key}
            person_line = json.dumps(person_entry, ensure_ascii=False, sort_keys=True)
            person_file = root / "contacts" / f"{_safe(person_key)}.jsonl"
            person_file.parent.mkdir(parents=True, exist_ok=True)
            with person_file.open("a", encoding="utf-8") as f:
                f.write(person_line + "\n")
            _write_capsule(
                root / "contacts" / f"{_safe(person_key)}.md",
                _load_tail(person_file),
                f"Contact {person_key}",
            )


async def record_interaction(
    *,
    channel: str,
    thread_id: str,
    handle: str,
    role: str,
    text: str,
    display: str | None = None,
    meta: dict | None = None,
) -> None:
    meta = meta or {}
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "thread_id": thread_id,
        "handle": handle,
        "display": display,
        "role": role,
        "text": text,
        "is_group": bool(meta.get("is_group")),
        "group_id": meta.get("group_id"),
        "participant_handle": meta.get("participant_handle"),
        "participant_display": meta.get("participant_display"),
        "username": meta.get("username"),
        "participant_username": meta.get("participant_username"),
        "relationship": meta.get("relationship"),
        "relationship_note": meta.get("relationship_note"),
        "trust_tier": meta.get("trust_tier"),
        "security_reasons": meta.get("security_reasons") or [],
    }
    await asyncio.to_thread(_append, entry)
