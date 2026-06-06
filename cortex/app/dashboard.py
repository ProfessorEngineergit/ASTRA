"""Server-rendered status dashboard — shares the Astral design language.

Read-only control panel: capability status, recent threads, pending approvals and
the audit tail. Served at GET /dashboard. Keep it behind VPN/LAN — it exposes
operational metadata (not secrets).
"""
from __future__ import annotations

from datetime import datetime

from .web.templates import esc, page


def _fmt_ts(ts) -> str:
    if isinstance(ts, datetime):
        return ts.strftime("%d.%m %H:%M")
    return esc(ts)


_STATE_COLORS = {
    "deferred": "#fbbf24",
    "awaiting_approval": "#fb7185",
    "answered": "#34d399",
    "standdown": "#64748b",
    "idle": "#64748b",
}


def _cap(on: bool, label: str) -> str:
    cls = "b-ok" if on else "b-off"
    return f'<span class="badge {cls}">{esc(label)}</span>'


def render(s, threads: list[dict], approvals: list[dict], audit: list[dict]) -> str:
    caps = "".join([
        _cap(s.openai_enabled, "OpenAI"),
        _cap(s.telegram_enabled, "Telegram"),
        _cap(s.voice_enabled, "Voice/Whisper"),
        _cap(s.ha_enabled, "Home Assistant"),
        _cap(s.edupage_enabled, "EduPage"),
        _cap(s.rmv_enabled, "RMV"),
        _cap(s.google_tasks_enabled, "Google Tasks"),
        _cap(s.astra_briefing_enabled, f"Briefing {esc(s.astra_briefing_time)}"),
        _cap(not s.astra_dry_run, "Live" if not s.astra_dry_run else "DRY-RUN"),
    ])

    thread_rows = "".join(
        f"<tr><td>{esc(t['who'])}</td><td>{esc(t['channel'])}</td>"
        f"<td><span style='color:{_STATE_COLORS.get(t['state'], '#cbd5e1')}'>{esc(t['state'])}</span></td>"
        f"<td>{_fmt_ts(t['last_event_at'])}</td></tr>"
        for t in threads
    ) or "<tr><td colspan=4 class='muted'>Noch keine Threads.</td></tr>"

    approval_rows = "".join(
        f"<tr><td>{_fmt_ts(a['created_at'])}</td><td>{esc(a['question'])[:90]}</td></tr>"
        for a in approvals
    ) or "<tr><td colspan=2 class='muted'>Keine offenen Freigaben.</td></tr>"

    audit_rows = "".join(
        f"<tr><td>{_fmt_ts(a['ts'])}</td><td>{esc(a['event_type'])}</td>"
        f"<td>{esc(a.get('channel'))}</td><td class='muted'>{esc(str(a.get('detail') or '')[:80])}</td></tr>"
        for a in audit
    ) or "<tr><td colspan=4 class='muted'>Kein Audit-Log.</td></tr>"

    extra_css = """
    <style>
      .dash-grid { display:grid; gap:18px; }
      table { width:100%; border-collapse:collapse; font-size:13px; }
      th,td { text-align:left; padding:9px 10px; border-bottom:1px solid var(--border-soft); }
      th { color:var(--text-faint); font-weight:600; text-transform:uppercase; font-size:11px;
           letter-spacing:.05em; }
      .muted { color:var(--text-faint); }
      .capwrap { display:flex; flex-wrap:wrap; gap:8px; }
      .panel h2 { margin:0 0 14px; font-size:13px; text-transform:uppercase; letter-spacing:.06em;
                  color:var(--text-dim); }
    </style>"""

    body = f"""{extra_css}
    <div class="hero">
      <h1><span class="grad">Status</span></h1>
      <p>{esc(s.astra_owner_name)} · {esc(s.astra_timezone)} · Auto-Refresh alle 30&nbsp;s</p>
    </div>
    <div class="dash-grid">
      <div class="panel"><h2>Aktive Fähigkeiten</h2><div class="capwrap">{caps}</div></div>
      <div class="panel"><h2>Aktive Konversationen</h2>
        <table><tr><th>Kontakt</th><th>Kanal</th><th>Status</th><th>Zuletzt</th></tr>{thread_rows}</table>
      </div>
      <div class="panel"><h2>Offene Freigaben ({len(approvals)})</h2>
        <table><tr><th>Zeit</th><th>Frage</th></tr>{approval_rows}</table>
      </div>
      <div class="panel"><h2>Audit-Log</h2>
        <table><tr><th>Zeit</th><th>Event</th><th>Kanal</th><th>Detail</th></tr>{audit_rows}</table>
      </div>
    </div>
    <script>setTimeout(() => location.reload(), 30000);</script>"""
    return page("Status", body, active="status")
