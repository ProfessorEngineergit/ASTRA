"""Shared HTML scaffolding for the web admin — dark theme, no build step."""
from __future__ import annotations

import html


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; margin: 0;
       background:#0b0f17; color:#e5e7eb; }
a { color:#60a5fa; text-decoration:none; } a:hover { text-decoration:underline; }
header { padding:16px 28px; background:linear-gradient(90deg,#111827,#0b0f17);
         border-bottom:1px solid #1f2937; display:flex; align-items:center; gap:18px; }
header h1 { margin:0; font-size:18px; }
header nav { margin-left:auto; display:flex; gap:16px; font-size:14px; }
main { padding:24px 28px; max-width:1150px; margin:0 auto; }
.toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:20px; }
input[type=text], input[type=password], input[type=number], select {
  background:#0b0f17; border:1px solid #334155; color:#e5e7eb; border-radius:8px;
  padding:9px 11px; font-size:14px; width:100%; }
input:focus, select:focus { outline:none; border-color:#3b82f6; }
.search { max-width:320px; }
.chip { padding:6px 12px; border:1px solid #334155; border-radius:999px; font-size:13px;
        cursor:pointer; background:#111827; user-select:none; }
.chip.active { background:#1d4ed8; border-color:#1d4ed8; color:#fff; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:16px; }
.card { background:#111827; border:1px solid #1f2937; border-radius:14px; padding:16px;
        display:flex; flex-direction:column; gap:8px; }
.card .top { display:flex; align-items:center; gap:10px; }
.card .icon { font-size:26px; }
.card h3 { margin:0; font-size:15px; }
.card p { margin:0; font-size:13px; color:#9ca3af; flex:1; }
.star { margin-left:auto; cursor:pointer; font-size:18px; color:#6b7280; background:none;
        border:none; } .star.on { color:#fbbf24; }
.badge { display:inline-block; font-size:12px; padding:2px 9px; border-radius:999px; }
.b-ok { background:#064e3b; color:#6ee7b7; } .b-off { background:#1f2937; color:#9ca3af; }
.b-err { background:#7f1d1d; color:#fca5a5; }
.btn { display:inline-block; background:#1d4ed8; color:#fff; border:none; border-radius:8px;
       padding:9px 14px; font-size:14px; cursor:pointer; text-align:center; }
.btn.secondary { background:#374151; } .btn:hover { filter:brightness(1.1); }
.field { margin-bottom:16px; } .field label { display:block; font-size:13px; margin-bottom:6px; }
.field .help { font-size:12px; color:#6b7280; margin-top:4px; }
.row { display:flex; gap:12px; align-items:center; }
.note { font-size:13px; color:#9ca3af; }
.flash { padding:10px 14px; border-radius:8px; margin-bottom:16px; font-size:14px; }
.flash.ok { background:#064e3b; color:#a7f3d0; } .flash.err { background:#7f1d1d; color:#fecaca; }
.center { max-width:420px; margin:8vh auto; }
.toggle { width:46px; height:26px; appearance:none; background:#374151; border-radius:999px;
          position:relative; cursor:pointer; } .toggle:checked { background:#16a34a; }
.toggle::after { content:''; position:absolute; width:20px; height:20px; border-radius:50%;
  background:#fff; top:3px; left:3px; transition:.15s; } .toggle:checked::after { left:23px; }
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
