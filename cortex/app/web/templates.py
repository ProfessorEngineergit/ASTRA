"""ASTRA web UI — shared HTML scaffolding + the "Astral" design language.

No build step, no framework: a hand-rolled design system (CSS custom properties),
an inline SVG brand mark and a cosmic dark theme. Everything the admin + dashboard
render flows through page().
"""
from __future__ import annotations

import html


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


# ─── Brand mark ────────────────────────────────────────────────────────────────
# A four-pointed "stellar sparkle" inside a tilted orbit — ASTRA = the stars.
# Rendered inline so it inherits crispness at any size and needs no asset hosting.
def astra_mark(size: int = 30, glow: bool = True) -> str:
    gid = f"ag{size}"  # unique gradient id per size avoids <defs> collisions
    glow_filter = (
        f'<filter id="{gid}f" x="-40%" y="-40%" width="180%" height="180%">'
        f'<feGaussianBlur stdDeviation="1.1" result="b"/>'
        f'<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>'
        f'</filter>'
    ) if glow else ""
    filt = f'filter="url(#{gid}f)"' if glow else ""
    return f"""<svg width="{size}" height="{size}" viewBox="0 0 32 32" fill="none"
 xmlns="http://www.w3.org/2000/svg" class="astra-mark" aria-label="ASTRA">
  <defs>
    <linearGradient id="{gid}" x1="3" y1="3" x2="29" y2="29" gradientUnits="userSpaceOnUse">
      <stop stop-color="#818cf8"/><stop offset=".5" stop-color="#a78bfa"/>
      <stop offset="1" stop-color="#22d3ee"/>
    </linearGradient>
    {glow_filter}
  </defs>
  <ellipse cx="16" cy="16" rx="14" ry="6.2" transform="rotate(-32 16 16)"
    stroke="url(#{gid})" stroke-width="1.3" opacity=".55"/>
  <path d="M16 2.4c.9 7.3 4.3 11 13.6 13.6C20.3 18 16.9 21.5 16 29.6 15.1 21.5 11.7 18 2.4 16 11.7 13.4 15.1 9.7 16 2.4Z"
    fill="url(#{gid})" {filt}/>
  <path d="M25.5 5.5c.3 2 1.1 2.8 3 3.2-1.9.5-2.7 1.3-3 3.3-.3-2-1.1-2.8-3-3.3 1.9-.4 2.7-1.2 3-3.2Z"
    fill="#e9d5ff" opacity=".9"/>
</svg>"""


def favicon_link() -> str:
    """SVG favicon as a data URI — no static file route needed."""
    svg = astra_mark(32, glow=False).replace("\n", "").replace('"', "'")
    from urllib.parse import quote
    return f'<link rel="icon" href="data:image/svg+xml,{quote(svg)}">'


# ─── Design tokens + components ─────────────────────────────────────────────────
_CSS = """
:root {
  color-scheme: dark;
  /* surfaces */
  --bg: #070a14;
  --bg-2: #0a0f1c;
  --surface: #0e1424;
  --surface-2: #131b2e;
  --border: #1e293b;
  --border-soft: #1a2336;
  /* text */
  --text: #e8edf7;
  --text-dim: #94a3b8;
  --text-faint: #64748b;
  /* brand aurora */
  --aurora-1: #818cf8;
  --aurora-2: #a78bfa;
  --aurora-3: #22d3ee;
  --accent: #6366f1;
  --accent-hover: #7c7ff5;
  --star: #fbbf24;
  /* status */
  --ok: #34d399; --ok-bg: #052e1d;
  --warn: #fbbf24; --warn-bg: #2e2606;
  --err: #fb7185; --err-bg: #2e0a12;
  /* shape */
  --r-sm: 8px; --r: 12px; --r-lg: 18px; --r-xl: 24px;
  --shadow: 0 8px 30px rgba(0,0,0,.45);
  --glow: 0 0 0 1px rgba(129,140,248,.25), 0 8px 40px rgba(99,102,241,.18);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
  margin: 0; min-height: 100vh; color: var(--text);
  background:
    radial-gradient(1200px 600px at 80% -10%, rgba(99,102,241,.10), transparent 60%),
    radial-gradient(900px 500px at -10% 10%, rgba(34,211,238,.08), transparent 55%),
    var(--bg);
  -webkit-font-smoothing: antialiased;
}
/* faint starfield */
body::before {
  content:''; position: fixed; inset: 0; z-index: 0; pointer-events: none; opacity: .5;
  background-image:
    radial-gradient(1.4px 1.4px at 12% 22%, #fff7, transparent),
    radial-gradient(1.2px 1.2px at 67% 14%, #fff5, transparent),
    radial-gradient(1.3px 1.3px at 88% 56%, #fff6, transparent),
    radial-gradient(1px 1px at 34% 72%, #fff4, transparent),
    radial-gradient(1.5px 1.5px at 52% 38%, #fff5, transparent),
    radial-gradient(1px 1px at 78% 82%, #fff4, transparent),
    radial-gradient(1.2px 1.2px at 22% 88%, #fff5, transparent);
}
a { color: var(--aurora-1); text-decoration: none; transition: color .15s; }
a:hover { color: var(--aurora-2); }

/* header */
header.topbar {
  position: sticky; top: 0; z-index: 20;
  display: flex; align-items: center; gap: 18px;
  padding: 13px 26px;
  background: rgba(10,15,28,.72); backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--border-soft);
}
.brand { display: flex; align-items: center; gap: 11px; font-weight: 700;
  font-size: 18px; letter-spacing: -.4px; }
.brand .word { background: linear-gradient(92deg, var(--aurora-1), var(--aurora-3));
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
.astra-mark { display: block; flex-shrink: 0; }
header.topbar nav { margin-left: auto; display: flex; gap: 6px; align-items: center; }
header.topbar nav a { padding: 7px 13px; border-radius: var(--r-sm); font-size: 14px;
  color: var(--text-dim); }
header.topbar nav a:hover { color: var(--text); background: var(--surface-2); }
header.topbar nav a.active { color: var(--text); background: var(--surface-2); }

main { position: relative; z-index: 1; padding: 28px 26px 60px; max-width: 1220px; margin: 0 auto; }

/* hero */
.hero { margin: 8px 0 26px; }
.hero h1 { margin: 0 0 6px; font-size: 30px; font-weight: 800; letter-spacing: -.8px; }
.hero h1 .grad { background: linear-gradient(92deg, var(--aurora-1), var(--aurora-2) 40%, var(--aurora-3));
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
.hero p { margin: 0; color: var(--text-dim); font-size: 15px; }
.stats { display: flex; gap: 18px; margin-top: 14px; flex-wrap: wrap; }
.stat { display: flex; align-items: baseline; gap: 7px; font-size: 13px; color: var(--text-faint); }
.stat b { font-size: 19px; font-weight: 700; color: var(--text); }
.stat .dot { width: 8px; height: 8px; border-radius: 50%; align-self: center; }

/* toolbar / search / chips */
.toolbar { display: flex; flex-wrap: wrap; gap: 11px; align-items: center; margin-bottom: 14px; }
.searchwrap { position: relative; flex: 1; min-width: 220px; max-width: 420px; }
.searchwrap svg { position: absolute; left: 13px; top: 50%; transform: translateY(-50%);
  color: var(--text-faint); pointer-events: none; }
input[type=text], input[type=password], input[type=number], input[type=email], select {
  width: 100%; background: var(--surface); border: 1px solid var(--border);
  color: var(--text); border-radius: var(--r-sm); padding: 11px 13px; font-size: 14px;
  font-family: inherit; transition: border-color .15s, box-shadow .15s; }
.search { padding-left: 38px !important; }
input:focus, select:focus { outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(99,102,241,.18); }
input::placeholder { color: var(--text-faint); }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 22px; }
.chip { padding: 6px 14px; border: 1px solid var(--border); border-radius: 999px;
  font-size: 13px; cursor: pointer; background: var(--surface); color: var(--text-dim);
  user-select: none; transition: all .14s; white-space: nowrap; }
.chip:hover { border-color: #2f3e5c; color: var(--text); }
.chip.active { background: linear-gradient(92deg, var(--accent), #5b6cf0);
  border-color: transparent; color: #fff; box-shadow: 0 4px 14px rgba(99,102,241,.35); }
.switch { display: inline-flex; align-items: center; gap: 8px; font-size: 13px;
  color: var(--text-dim); cursor: pointer; user-select: none; }

/* category sections */
.section { margin-bottom: 30px; }
.section-head { display: flex; align-items: center; gap: 10px; margin: 0 0 14px;
  padding-bottom: 9px; border-bottom: 1px solid var(--border-soft); }
.section-head h2 { margin: 0; font-size: 14px; font-weight: 600; letter-spacing: .02em;
  text-transform: uppercase; color: var(--text-dim); }
.section-head .count { font-size: 12px; color: var(--text-faint);
  background: var(--surface-2); padding: 2px 9px; border-radius: 999px; }

/* card grid */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }
.card { position: relative; background: linear-gradient(180deg, var(--surface), var(--bg-2));
  border: 1px solid var(--border); border-radius: var(--r-lg); padding: 18px;
  display: flex; flex-direction: column; gap: 11px;
  transition: transform .16s, border-color .16s, box-shadow .16s; }
.card:hover { transform: translateY(-3px); border-color: #2f3e5c; box-shadow: var(--shadow); }
.card.on { border-color: rgba(52,211,153,.35); }
.card .top { display: flex; align-items: flex-start; gap: 13px; }
.card .icon { font-size: 30px; line-height: 1; flex-shrink: 0;
  width: 48px; height: 48px; display: grid; place-items: center;
  background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: var(--r); }
.card .meta { flex: 1; min-width: 0; }
.card h3 { margin: 0 0 3px; font-size: 15.5px; font-weight: 650; }
.card .cat { font-size: 11px; color: var(--text-faint); text-transform: uppercase; letter-spacing: .05em; }
.card p { margin: 0; font-size: 13px; color: var(--text-dim); line-height: 1.55; flex: 1; }
.star { flex-shrink: 0; cursor: pointer; font-size: 19px; line-height: 1; color: #334155;
  background: none; border: none; padding: 2px; transition: color .14s, transform .14s; }
.star:hover { color: var(--star); transform: scale(1.15); }
.star.on { color: var(--star); }
.card .row { display: flex; align-items: center; gap: 9px; margin-top: 2px; }

/* badges */
.badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
  padding: 4px 11px; border-radius: 999px; font-weight: 500; }
.badge::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.b-ok { background: var(--ok-bg); color: var(--ok); }
.b-off { background: #161d2e; color: var(--text-faint); }
.b-err { background: var(--err-bg); color: var(--err); }
.b-soon { background: #161d2e; color: var(--text-faint); }
.b-soon::before { animation: pulse 1.8s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: .35 } 50% { opacity: 1 } }

/* buttons */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  background: linear-gradient(92deg, var(--accent), #5b6cf0); color: #fff; border: none;
  border-radius: var(--r-sm); padding: 10px 16px; font-size: 14px; font-weight: 550;
  font-family: inherit; cursor: pointer; transition: filter .15s, transform .1s, box-shadow .15s; }
.btn:hover { filter: brightness(1.08); box-shadow: 0 6px 20px rgba(99,102,241,.35); }
.btn:active { transform: translateY(1px); }
.btn.secondary { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); }
.btn.secondary:hover { background: #1a2338; box-shadow: none; }
.btn.ghost { background: transparent; color: var(--text-dim); border: 1px solid var(--border); }
.btn.sm { padding: 7px 13px; font-size: 13px; }
.btn.block { width: 100%; }

/* forms */
.panel { background: linear-gradient(180deg, var(--surface), var(--bg-2));
  border: 1px solid var(--border); border-radius: var(--r-lg); padding: 22px 24px; }
.field { margin-bottom: 18px; }
.field label { display: block; font-size: 13.5px; font-weight: 550; margin-bottom: 7px; }
.field .help { font-size: 12px; color: var(--text-faint); margin-top: 6px; line-height: 1.5; }
.req { color: var(--err); }
.row { display: flex; gap: 12px; align-items: center; }
.note { font-size: 13px; color: var(--text-dim); }
.crumb { font-size: 13px; color: var(--text-faint); margin-bottom: 14px; }
hr { border: none; border-top: 1px solid var(--border-soft); margin: 20px 0; }

/* flash */
.flash { padding: 11px 15px; border-radius: var(--r-sm); margin-bottom: 18px; font-size: 14px;
  border: 1px solid transparent; }
.flash.ok { background: var(--ok-bg); color: #a7f3d0; border-color: #14613f; }
.flash.err { background: var(--err-bg); color: #fecdd3; border-color: #7f1d2e; }

/* toggle switch */
.toggle { appearance: none; width: 46px; height: 26px; background: #2a3550; border-radius: 999px;
  position: relative; cursor: pointer; transition: background .18s; flex-shrink: 0; }
.toggle:checked { background: linear-gradient(92deg, #10b981, #34d399); }
.toggle::after { content: ''; position: absolute; width: 20px; height: 20px; border-radius: 50%;
  background: #fff; top: 3px; left: 3px; transition: left .18s; box-shadow: 0 1px 3px rgba(0,0,0,.4); }
.toggle:checked::after { left: 23px; }
.toggle-row { display: flex; align-items: center; gap: 12px; padding: 14px 16px;
  background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: var(--r); }

/* auth (centered) */
.center { max-width: 410px; margin: 11vh auto 0; }
.auth-logo { display: flex; flex-direction: column; align-items: center; gap: 14px; margin-bottom: 26px; }
.auth-logo .word { font-size: 26px; font-weight: 800; letter-spacing: -.5px;
  background: linear-gradient(92deg, var(--aurora-1), var(--aurora-3));
  -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
.center h2 { font-size: 19px; margin: 0 0 16px; text-align: center; font-weight: 650; }

/* empty state */
.empty { text-align: center; color: var(--text-faint); padding: 50px 20px; font-size: 14px; }

@media (max-width: 560px) {
  main { padding: 20px 16px 48px; }
  .hero h1 { font-size: 24px; }
  header.topbar { padding: 11px 16px; }
}
"""


def _logo_block() -> str:
    return f'<div class="brand">{astra_mark(28)}<span class="word">ASTRA</span></div>'


def page(title: str, body: str, *, nav: bool = True, active: str = "") -> str:
    def navlink(href: str, label: str, key: str) -> str:
        cls = " active" if active == key else ""
        return f'<a href="{href}" class="navlink{cls}">{label}</a>'

    navhtml = ""
    if nav:
        navhtml = (
            '<nav>'
            f'{navlink("/admin", "Plugins", "plugins")}'
            f'{navlink("/dashboard", "Status", "status")}'
            '<form method="post" action="/admin/logout" style="display:inline;margin-left:4px">'
            '<button class="btn ghost sm" type="submit">Logout</button></form>'
            '</nav>'
        )
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · ASTRA</title>{favicon_link()}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;550;600;650;700;800&display=swap" rel="stylesheet">
<style>{_CSS}</style></head><body>
<header class="topbar">{_logo_block()}{navhtml}</header>
<main>{body}</main></body></html>"""
