"""Server-rendered status dashboard — no build step, no JS framework.

Read-only control panel: capability status, recent threads, pending approvals and
the audit tail. Served at GET /dashboard. Keep it behind Caddy/VPN — it exposes
operational metadata (not secrets).
"""
from __future__ import annotations

import html
from datetime import datetime


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _badge(on: bool, label: str) -> str:
    color = "#1f9d55" if on else "#9aa0a6"
    dot = "●" if on else "○"
    return f'<span class="badge" style="color:{color}">{dot} {_esc(label)}</span>'


def _fmt_ts(ts) -> str:
    if isinstance(ts, datetime):
        return ts.strftime("%d.%m %H:%M")
    return _esc(ts)


_STATE_COLORS = {
    "deferred": "#d97706",
    "awaiting_approval": "#dc2626",
    "answered": "#1f9d55",
    "standdown": "#6b7280",
    "idle": "#6b7280",
}


def render(s, threads: list[dict], approvals: list[dict], audit: list[dict]) -> str:
    caps = " ".join([
        _badge(s.openai_enabled, "OpenAI"),
        _badge(s.telegram_enabled, "Telegram"),
        _badge(s.voice_enabled, "Voice/Whisper"),
        _badge(s.ha_enabled, "Home Assistant"),
        _badge(s.edupage_enabled, "EduPage"),
        _badge(s.rmv_enabled, "RMV"),
        _badge(s.google_tasks_enabled, "Google Tasks"),
        _badge(s.astra_briefing_enabled, f"Briefing {s.astra_briefing_time}"),
        _badge(not s.astra_dry_run, "Live" if not s.astra_dry_run else "DRY-RUN"),
    ])

    thread_rows = "".join(
        f"<tr><td>{_esc(t['who'])}</td><td>{_esc(t['channel'])}</td>"
        f"<td><span style='color:{_STATE_COLORS.get(t['state'], '#374151')}'>{_esc(t['state'])}</span></td>"
        f"<td>{_fmt_ts(t['last_event_at'])}</td></tr>"
        for t in threads
    ) or "<tr><td colspan=4 class='muted'>Noch keine Threads.</td></tr>"

    approval_rows = "".join(
        f"<tr><td>{_fmt_ts(a['created_at'])}</td><td>{_esc(a['question'])[:90]}</td></tr>"
        for a in approvals
    ) or "<tr><td colspan=2 class='muted'>Keine offenen Freigaben.</td></tr>"

    audit_rows = "".join(
        f"<tr><td>{_fmt_ts(a['ts'])}</td><td>{_esc(a['event_type'])}</td>"
        f"<td>{_esc(a.get('channel'))}</td><td class='muted'>{_esc(str(a.get('detail') or '')[:80])}</td></tr>"
        for a in audit
    ) or "<tr><td colspan=4 class='muted'>Kein Audit-Log.</td></tr>"

    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ASTRA · Dashboard</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; margin: 0;
         background:#0b0f17; color:#e5e7eb; }}
  header {{ padding:20px 28px; background:linear-gradient(90deg,#111827,#0b0f17);
            border-bottom:1px solid #1f2937; }}
  h1 {{ margin:0; font-size:20px; }} h1 small {{ color:#6b7280; font-weight:400; }}
  .caps {{ margin-top:10px; display:flex; flex-wrap:wrap; gap:14px; font-size:13px; }}
  .badge {{ white-space:nowrap; }}
  main {{ padding:24px 28px; display:grid; gap:24px; max-width:1100px; }}
  section {{ background:#111827; border:1px solid #1f2937; border-radius:12px; padding:16px 18px; }}
  h2 {{ margin:0 0 12px; font-size:14px; text-transform:uppercase; letter-spacing:.06em; color:#9ca3af; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ text-align:left; padding:7px 8px; border-bottom:1px solid #1f2937; }}
  th {{ color:#6b7280; font-weight:600; }}
  .muted {{ color:#6b7280; }}
  footer {{ padding:16px 28px; color:#4b5563; font-size:12px; }}
</style></head>
<body>
<header>
  <h1>🌌 ASTRA <small>· {_esc(s.astra_owner_name)} · {_esc(s.astra_timezone)}</small></h1>
  <div class="caps">{caps}</div>
</header>
<main>
  <section>
    <h2>Aktive Konversationen</h2>
    <table><tr><th>Kontakt</th><th>Kanal</th><th>Status</th><th>Zuletzt</th></tr>{thread_rows}</table>
  </section>
  <section>
    <h2>Offene Freigaben ({len(approvals)})</h2>
    <table><tr><th>Zeit</th><th>Frage</th></tr>{approval_rows}</table>
  </section>
  <section>
    <h2>Audit-Log</h2>
    <table><tr><th>Zeit</th><th>Event</th><th>Kanal</th><th>Detail</th></tr>{audit_rows}</table>
  </section>
</main>
<footer>ASTRA cortex · auto-refresh alle 30&nbsp;s · read-only</footer>
<script>setTimeout(() => location.reload(), 30000);</script>
</body></html>"""
