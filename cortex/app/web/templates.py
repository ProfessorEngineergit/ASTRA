"""Shared HTML scaffolding for the web admin — dark theme, no build step."""
from __future__ import annotations

import html


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; margin: 0;
       background:#060b14; color:#e5e7eb; }
a { color:#60a5fa; text-decoration:none; } a:hover { text-decoration:underline; }
header { padding:14px 28px; background:#0d1117;
         border-bottom:1px solid #1f2937; display:flex; align-items:center; gap:18px; }
header h1 { margin:0; font-size:18px; font-weight:700; letter-spacing:-.3px; }
header nav { margin-left:auto; display:flex; gap:10px; align-items:center; font-size:14px; }
main { padding:24px 28px; max-width:1200px; margin:0 auto; }
.toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
input[type=text], input[type=password], input[type=number], select {
  background:#0d1117; border:1px solid #2d3748; color:#e5e7eb; border-radius:8px;
  padding:9px 11px; font-size:14px; width:100%; transition:border-color .15s; }
input:focus, select:focus { outline:none; border-color:#3b82f6;
  box-shadow:0 0 0 3px rgba(59,130,246,.15); }
.search { max-width:340px; background:#0d1117; }
/* category chips */
.chip { padding:5px 14px; border:1px solid #2d3748; border-radius:999px; font-size:13px;
        cursor:pointer; background:#0d1117; user-select:none; transition:all .12s; }
.chip:hover { border-color:#4b5563; }
.chip.active { background:#1d4ed8; border-color:#2563eb; color:#fff; }
/* card grid */
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(270px,1fr)); gap:14px; }
.card { background:#0d1117; border:1px solid #1f2937; border-radius:16px; padding:18px;
        display:flex; flex-direction:column; gap:10px;
        transition:border-color .15s, box-shadow .15s; cursor:default; }
.card:hover { border-color:#3b4a63; box-shadow:0 4px 24px rgba(0,0,0,.4); }
.card .top { display:flex; align-items:center; gap:12px; }
.card .icon { font-size:32px; line-height:1; flex-shrink:0; }
.card .meta { flex:1; min-width:0; }
.card h3 { margin:0 0 2px; font-size:15px; font-weight:600; white-space:nowrap;
           overflow:hidden; text-overflow:ellipsis; }
.card .cat { font-size:11px; color:#6b7280; text-transform:uppercase; letter-spacing:.05em; }
.card p { margin:0; font-size:13px; color:#9ca3af; flex:1; line-height:1.5; }
.star { flex-shrink:0; cursor:pointer; font-size:20px; color:#374151; background:none;
        border:none; padding:0; transition:color .12s; }
.star:hover { color:#fbbf24; }
.star.on { color:#fbbf24; }
/* status badges */
.badge { display:inline-flex; align-items:center; gap:5px; font-size:12px;
         padding:3px 10px; border-radius:999px; font-weight:500; }
.badge::before { content:''; width:6px; height:6px; border-radius:50%;
                 background:currentColor; opacity:.8; }
.b-ok { background:#052e16; color:#4ade80; }
.b-off { background:#1c1f26; color:#6b7280; }
.b-err { background:#450a0a; color:#f87171; }
.card .row { display:flex; gap:8px; align-items:center; margin-top:4px; }
/* buttons */
.btn { display:inline-flex; align-items:center; justify-content:center;
       background:#1d4ed8; color:#fff; border:none; border-radius:8px;
       padding:8px 14px; font-size:13px; font-weight:500; cursor:pointer; }
.btn.secondary { background:#1f2937; color:#d1d5db; }
.btn:hover { filter:brightness(1.12); }
.btn.sm { padding:5px 11px; font-size:12px; }
/* forms */
.field { margin-bottom:18px; }
.field label { display:block; font-size:13px; font-weight:500; margin-bottom:6px; }
.field .help { font-size:12px; color:#6b7280; margin-top:5px; line-height:1.4; }
.row { display:flex; gap:12px; align-items:center; }
.note { font-size:13px; color:#9ca3af; }
.flash { padding:10px 14px; border-radius:8px; margin-bottom:16px; font-size:14px; }
.flash.ok { background:#052e16; color:#a7f3d0; border:1px solid #166534; }
.flash.err { background:#450a0a; color:#fecaca; border:1px solid #7f1d1d; }
.center { max-width:420px; margin:8vh auto; }
/* toggle switch */
.toggle { width:46px; height:26px; appearance:none; background:#374151; border-radius:999px;
          position:relative; cursor:pointer; transition:background .15s; }
.toggle:checked { background:#16a34a; }
.toggle::after { content:''; position:absolute; width:20px; height:20px; border-radius:50%;
  background:#fff; top:3px; left:3px; transition:left .15s;
  box-shadow:0 1px 3px rgba(0,0,0,.4); }
.toggle:checked::after { left:23px; }
/* divider */
hr { border:none; border-top:1px solid #1f2937; margin:20px 0; }
"""


def page(title: str, body: str, *, nav: bool = True) -> str:
    navhtml = (
        '<nav><a href="/admin">Plugins</a><a href="/dashboard">Status</a>'
        '<form method="post" action="/admin/logout" style="display:inline">'
        '<button class="btn secondary" style="padding:4px 10px">Logout</button></form></nav>'
        if nav else ""
    )
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · ASTRA</title><style>{_CSS}</style></head><body>
<header><h1>🌌 ASTRA</h1>{navhtml}</header>
<main>{body}</main></body></html>"""
