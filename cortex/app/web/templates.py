"""ASTRA web UI — shared HTML scaffolding + the OLED "Event Horizon" design language.

Pure-black (OLED) cinematic theme built around the ASTRA black-hole mark. No build
step, no framework: a hand-rolled design system (CSS custom properties), brand logos
via Simple Icons, and a translucent header that fades from solid black (behind the
logo) to blurred glass toward the nav.
"""
from __future__ import annotations

import html


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


# ─── Brand assets (served from /static) ────────────────────────────────────────
LOGO_LONG = "/static/astra-long.png"   # black-hole mark + ASTRA wordmark (banner)
LOGO_MARK = "/static/astra-mark.png"   # square black-hole mark
FAVICON = "/static/favicon.png"


def astra_mark(size: int = 64) -> str:
    """Square black-hole mark as an <img> (kept name for back-compat)."""
    return (f'<img src="{LOGO_MARK}" width="{size}" height="{size}" alt="ASTRA" '
            f'class="astra-mark" style="border-radius:{size // 4}px">')


def favicon_link() -> str:
    return f'<link rel="icon" href="{FAVICON}">'


# ─── Brand logos for plugin cards (Simple Icons, monochrome) ───────────────────
# slug → Simple Icons slug. Unknown/wrong slugs fall back to the plugin emoji via
# the <img> onerror handler, so this map can be generous.
BRAND_ICONS: dict[str, str] = {
    "slack": "slack", "discord": "discord", "matrix": "matrix", "mastodon": "mastodon",
    "gmail": "gmail", "spotify": "spotify", "jellyfin": "jellyfin", "plex": "plex",
    "youtube": "youtube", "lastfm": "lastdotfm", "github": "github", "gitlab": "gitlab",
    "gitea": "gitea", "todoist": "todoist", "trello": "trello", "notion": "notion",
    "obsidian": "obsidian", "nextcloud": "nextcloud", "home_assistant": "homeassistant",
    "proxmox": "proxmox", "docker": "docker", "portainer": "portainer", "grafana": "grafana",
    "pihole": "pihole", "adguard": "adguard", "ollama": "ollama", "immich": "immich",
    "readwise": "readwise", "sonarr": "sonarr", "radarr": "radarr", "qbittorrent": "qbittorrent",
    "n8n": "n8n", "moodle": "moodle", "ntfy": "ntfy",
    "deutsche_bahn": "deutschebahn", "google_maps": "googlemaps",
    "google_calendar": "googlecalendar", "google_tasks": "googletasks",
    "philips_hue": "philipshue", "homekit": "apple", "mqtt": "mqtt",
    "zigbee2mqtt": "zigbee2mqtt", "vaultwarden": "vaultwarden", "rss": "rss",
    "uptime_kuma": "uptimekuma", "linear": "linear", "crypto": "bitcoin",
    "jira": "jira", "tailscale": "tailscale", "cloudflare": "cloudflare",
    "truenas": "truenas", "vikunja": "vikunja", "audiobookshelf": "audiobookshelf",
}


def brand_icon(brand: str | None, emoji: str) -> str:
    """Icon box with a monochrome Simple-Icons brand logo, emoji fallback on error."""
    if brand:
        return (f'<span class="icon" data-emoji="{esc(emoji)}">'
                f'<img src="https://cdn.simpleicons.org/{brand}/d8dbe3" alt="" loading="lazy" '
                f'onerror="this.parentNode.textContent=this.parentNode.dataset.emoji"></span>')
    return f'<span class="icon">{esc(emoji)}</span>'


def icon_html(slug: str, emoji: str) -> str:
    """Render a plugin icon by slug: brand logo if mapped, else emoji."""
    return brand_icon(BRAND_ICONS.get(slug), emoji)


# ─── Labs: selectable UI fonts ─────────────────────────────────────────────────
# key → (display name, css family, Google-Fonts query or None for local @font-face)
FONTS: dict[str, tuple[str, str, str | None]] = {
    "inter": ("Inter", "'Inter'", "Inter:wght@400;500;600;700;800"),
    "exo2": ("Exo 2", "'Exo 2'", "Exo+2:wght@400;500;600;700;800"),
    "orbitron": ("Orbitron", "'Orbitron'", "Orbitron:wght@400;500;700;900"),
    "rajdhani": ("Rajdhani", "'Rajdhani'", "Rajdhani:wght@400;500;600;700"),
    "spacegrotesk": ("Space Grotesk", "'Space Grotesk'", "Space+Grotesk:wght@400;500;600;700"),
    "chakra": ("Chakra Petch", "'Chakra Petch'", "Chakra+Petch:wght@400;500;600;700"),
    "jetbrains": ("JetBrains Mono", "'JetBrains Mono'", "JetBrains+Mono:wght@400;500;700"),
}
_ACTIVE_FONT = "inter"


def _font_dir():
    from pathlib import Path
    return Path(__file__).parent / "static" / "fonts"


def local_fonts() -> dict[str, str]:
    """User-dropped fonts in static/fonts/ → {key: filename}."""
    d = _font_dir()
    out: dict[str, str] = {}
    if d.is_dir():
        for f in sorted(d.iterdir()):
            if f.suffix.lower() in (".woff2", ".woff", ".ttf", ".otf"):
                out[f"local_{f.stem.lower().replace(' ', '_')}"] = f.name
    return out


def font_choices() -> list[tuple[str, str]]:
    """(key, display) for the Labs picker — curated + any local files."""
    out = [(k, v[0]) for k, v in FONTS.items()]
    for key, fname in local_fonts().items():
        out.append((key, fname.rsplit(".", 1)[0]))
    return out


def set_font(key: str | None) -> None:
    global _ACTIVE_FONT
    if key and (key in FONTS or key in local_fonts()):
        _ACTIVE_FONT = key


def _font_head() -> str:
    """<link>/@font-face + the --ui-font variable for the active font."""
    locals_ = local_fonts()
    faces = ""
    for key, fname in locals_.items():
        ext = fname.rsplit(".", 1)[-1].lower()
        fmt = {"woff2": "woff2", "woff": "woff", "ttf": "truetype", "otf": "opentype"}.get(ext, "woff2")
        faces += (f"@font-face{{font-family:'{key}';src:url('/static/fonts/{esc(fname)}') "
                  f"format('{fmt}');font-display:swap;}}")
    active = _ACTIVE_FONT
    if active in FONTS:
        _disp, family, query = FONTS[active]
        link = (f'<link href="https://fonts.googleapis.com/css2?family={query}&display=swap" '
                f'rel="stylesheet">') if query else ""
    else:
        family = f"'{active}'"
        link = ""
    return f'{link}<style>{faces}:root{{--ui-font:{family};}}</style>'


# ─── Design tokens + components ────────────────────────────────────────────────
_CSS = """
:root {
  color-scheme: dark;
  --bg: #000000;            /* true OLED black */
  --bg-2: #050507;
  --surface: #0a0a0d;
  --surface-2: #101015;
  --surface-3: #16161c;
  --border: #1c1c22;
  --border-soft: #141419;
  --hair: rgba(255,255,255,.07);
  --text: #f4f5f8;
  --text-dim: #9a9aa6;
  --text-faint: #5f5f6a;
  --accent: #f4f5f8;        /* platinum primary */
  --accent-ink: #07070a;
  --link: #aab4d6;
  --ring: rgba(170,180,214,.55);
  --star: #d9b25a;
  --ok: #36d399; --ok-bg: rgba(54,211,153,.10);
  --warn: #f5c451; --warn-bg: rgba(245,196,81,.10);
  --err: #fb7185; --err-bg: rgba(251,113,133,.10);
  --r-sm: 9px; --r: 13px; --r-lg: 18px; --r-xl: 26px;
  --shadow: 0 12px 40px rgba(0,0,0,.6);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  font-family: var(--ui-font, 'Inter'), ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
  margin: 0; min-height: 100vh; color: var(--text); background: var(--bg);
  -webkit-font-smoothing: antialiased; letter-spacing: -.01em;
}
/* extremely faint starfield — on-brand, never noisy on OLED */
body::before {
  content:''; position: fixed; inset: 0; z-index: 0; pointer-events: none; opacity: .35;
  background-image:
    radial-gradient(1px 1px at 14% 20%, #fff6, transparent),
    radial-gradient(1px 1px at 68% 12%, #fff4, transparent),
    radial-gradient(1.3px 1.3px at 86% 52%, #fff5, transparent),
    radial-gradient(1px 1px at 33% 74%, #fff3, transparent),
    radial-gradient(1px 1px at 54% 36%, #fff4, transparent),
    radial-gradient(1px 1px at 78% 82%, #fff3, transparent);
}
a { color: var(--link); text-decoration: none; transition: color .15s; }
a:hover { color: #c9d1ea; }

/* ── header: opaque black behind the logo, glass/blur toward the nav ── */
header.topbar {
  position: sticky; top: 0; z-index: 30; display: flex; align-items: center; gap: 18px;
  padding: 0 22px; height: 60px;
  background: linear-gradient(90deg, #000 0%, #000 32%, rgba(0,0,0,.62) 72%, rgba(0,0,0,.34) 100%);
  backdrop-filter: blur(22px) saturate(1.3); -webkit-backdrop-filter: blur(22px) saturate(1.3);
  border-bottom: 1px solid var(--hair);
}
.brand { display: flex; align-items: center; flex-shrink: 0; }
.brand img { height: 30px; width: auto; display: block; }
header.topbar nav { margin-left: auto; display: flex; gap: 4px; align-items: center; }
.navlink { padding: 8px 14px; border-radius: var(--r-sm); font-size: 14px; color: var(--text-dim);
  font-weight: 500; }
.navlink:hover { color: var(--text); background: rgba(255,255,255,.05); }
.navlink.active { color: var(--text); background: rgba(255,255,255,.08); }

main { position: relative; z-index: 1; padding: 30px 22px 64px; max-width: 1240px; margin: 0 auto; }

/* hero */
.hero { margin: 6px 0 28px; }
.hero h1 { margin: 0 0 7px; font-size: 32px; font-weight: 800; letter-spacing: -1px; }
.hero h1 .grad { color: #fff; }
.hero p { margin: 0; color: var(--text-dim); font-size: 15px; max-width: 620px; }
.stats { display: flex; gap: 22px; margin-top: 16px; flex-wrap: wrap; }
.stat { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-faint); }
.stat b { font-size: 20px; font-weight: 700; color: var(--text); letter-spacing: -.5px; }
.stat .dot { width: 7px; height: 7px; border-radius: 50%; }

/* toolbar / search / chips */
.toolbar { display: flex; flex-wrap: wrap; gap: 11px; align-items: center; margin-bottom: 16px; }
.searchwrap { position: relative; flex: 1; min-width: 220px; max-width: 440px; }
.searchwrap svg { position: absolute; left: 14px; top: 50%; transform: translateY(-50%);
  color: var(--text-faint); pointer-events: none; }
input[type=text], input[type=password], input[type=number], input[type=email], select {
  width: 100%; background: var(--surface); border: 1px solid var(--border); color: var(--text);
  border-radius: var(--r-sm); padding: 11px 13px; font-size: 14px; font-family: inherit;
  transition: border-color .15s, box-shadow .15s; }
.search { padding-left: 40px !important; }
input:focus, select:focus { outline: none; border-color: #34343f;
  box-shadow: 0 0 0 3px rgba(170,180,214,.12); }
input::placeholder { color: var(--text-faint); }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 24px; }
.chip { padding: 6px 14px; border: 1px solid var(--border); border-radius: 999px; font-size: 13px;
  cursor: pointer; background: var(--surface); color: var(--text-dim); user-select: none;
  transition: all .14s; white-space: nowrap; }
.chip:hover { border-color: #34343f; color: var(--text); }
.chip.active { background: var(--accent); border-color: var(--accent); color: var(--accent-ink);
  font-weight: 600; }
.switch { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-dim);
  cursor: pointer; user-select: none; }

/* category sections */
.section { margin-bottom: 34px; }
.section-head { display: flex; align-items: center; gap: 11px; margin: 0 0 15px;
  padding-bottom: 11px; border-bottom: 1px solid var(--hair); }
.section-head h2 { margin: 0; font-size: 13px; font-weight: 600; letter-spacing: .08em;
  text-transform: uppercase; color: var(--text-dim); }
.section-head .count { font-size: 11px; color: var(--text-faint); background: var(--surface-2);
  padding: 2px 9px; border-radius: 999px; border: 1px solid var(--border-soft); }

/* card grid */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(290px, 1fr)); gap: 14px; }
.card { position: relative; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r-lg); padding: 18px; display: flex; flex-direction: column; gap: 12px;
  transition: transform .16s, border-color .16s, box-shadow .16s, background .16s; }
.card:hover { transform: translateY(-3px); border-color: #2c2c36; background: var(--surface-2);
  box-shadow: var(--shadow); }
.card.on { border-color: rgba(54,211,153,.30); }
.card .top { display: flex; align-items: flex-start; gap: 13px; }
.card .icon { font-size: 26px; line-height: 1; flex-shrink: 0; width: 46px; height: 46px;
  display: grid; place-items: center; background: var(--surface-3); border: 1px solid var(--hair);
  border-radius: var(--r); }
.card .icon img { width: 24px; height: 24px; object-fit: contain; }
.card .meta { flex: 1; min-width: 0; }
.card h3 { margin: 0 0 3px; font-size: 15.5px; font-weight: 650; letter-spacing: -.2px; }
.card .cat { font-size: 11px; color: var(--text-faint); text-transform: uppercase; letter-spacing: .05em; }
.card p { margin: 0; font-size: 13px; color: var(--text-dim); line-height: 1.55; flex: 1; }
.star { flex-shrink: 0; cursor: pointer; font-size: 19px; line-height: 1; color: #2e2e36;
  background: none; border: none; padding: 2px; transition: color .14s, transform .14s; }
.star:hover { color: var(--star); transform: scale(1.15); }
.star.on { color: var(--star); }
.card .row { display: flex; align-items: center; gap: 9px; margin-top: 2px; }

/* badges */
.badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; padding: 4px 11px;
  border-radius: 999px; font-weight: 500; }
.badge::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.b-ok { background: var(--ok-bg); color: var(--ok); }
.b-off { background: rgba(255,255,255,.05); color: var(--text-faint); }
.b-err { background: var(--err-bg); color: var(--err); }
.b-soon { background: rgba(255,255,255,.05); color: var(--text-faint); }
.b-soon::before { animation: pulse 1.8s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity:.3 } 50% { opacity:1 } }

/* buttons */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px;
  background: var(--accent); color: var(--accent-ink); border: none; border-radius: var(--r-sm);
  padding: 10px 16px; font-size: 14px; font-weight: 600; font-family: inherit; cursor: pointer;
  transition: filter .15s, transform .1s, opacity .15s; }
.btn:hover { filter: brightness(.92); }
.btn:active { transform: translateY(1px); }
.btn:disabled { opacity: .48; cursor: not-allowed; transform: none; filter: none; }
.btn.secondary { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); }
.btn.secondary:hover { background: var(--surface-3); filter: none; }
.btn.ghost { background: transparent; color: var(--text-dim); border: 1px solid var(--border); }
.btn.ghost:hover { color: var(--text); background: rgba(255,255,255,.04); }
.btn.sm { padding: 7px 13px; font-size: 13px; }
.btn.block { width: 100%; }
.btn.danger, .btn.ghost.danger { color: #fecdd3; border-color: rgba(251,113,133,.34); }
.btn.danger { background: var(--err-bg); }
.btn.ghost.danger:hover, .btn.danger:hover { background: rgba(251,113,133,.14); color: #fff1f2; filter: none; }

/* forms / panels */
.panel { background: var(--surface); border: 1px solid var(--border); border-radius: var(--r-lg);
  padding: 22px 24px; }
.field { margin-bottom: 18px; }
.field label { display: block; font-size: 13.5px; font-weight: 550; margin-bottom: 7px; }
.field .help { font-size: 12px; color: var(--text-faint); margin-top: 6px; line-height: 1.5; }
.req { color: var(--err); }
.row { display: flex; gap: 12px; align-items: center; }
.note { font-size: 13px; color: var(--text-dim); }
.crumb { font-size: 13px; color: var(--text-faint); margin-bottom: 14px; }
hr { border: none; border-top: 1px solid var(--hair); margin: 20px 0; }

/* flash */
.flash { padding: 11px 15px; border-radius: var(--r-sm); margin-bottom: 18px; font-size: 14px;
  border: 1px solid transparent; }
.flash.ok { background: var(--ok-bg); color: #a7f3d0; border-color: rgba(54,211,153,.3); }
.flash.err { background: var(--err-bg); color: #fecdd3; border-color: rgba(251,113,133,.3); }

/* toggle */
.toggle { appearance: none; width: 46px; height: 26px; background: #26262e; border-radius: 999px;
  position: relative; cursor: pointer; transition: background .18s; flex-shrink: 0; }
.toggle:checked { background: #10b981; }
.toggle::after { content: ''; position: absolute; width: 20px; height: 20px; border-radius: 50%;
  background: #fff; top: 3px; left: 3px; transition: left .18s; box-shadow: 0 1px 3px rgba(0,0,0,.5); }
.toggle:checked::after { left: 23px; }
.toggle-row { display: flex; align-items: center; gap: 12px; padding: 14px 16px;
  background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: var(--r); }

/* auth */
.center { max-width: 400px; margin: 12vh auto 0; }
.auth-logo { display: flex; justify-content: center; margin-bottom: 26px; }
.auth-logo img { height: 64px; }
.center h2 { font-size: 19px; margin: 0 0 16px; text-align: center; font-weight: 650; }

/* segmented control (source filter) */
.seg { display: inline-flex; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r-sm); padding: 3px; gap: 2px; }
.seg-btn { background: none; border: none; color: var(--text-dim); font-size: 13px;
  font-family: inherit; padding: 6px 13px; border-radius: 6px; cursor: pointer; transition: all .12s; }
.seg-btn:hover { color: var(--text); }
.seg-btn.active { background: var(--surface-3); color: var(--text); }

/* source tags on card titles */
.tag-nativ, .tag-katalog { font-size: 9.5px; font-weight: 600; letter-spacing: .04em;
  text-transform: uppercase; padding: 2px 6px; border-radius: 5px; vertical-align: middle;
  margin-left: 4px; }
.tag-nativ { background: rgba(167,139,250,.16); color: #c4b5fd; }
.tag-katalog { background: rgba(255,255,255,.06); color: var(--text-faint); }
.card.cat-entry { opacity: .82; }
.card.cat-entry:hover { opacity: 1; }

/* brain (knowledge) editor cards */
details.card.brain { padding: 16px 18px; }
details.card.brain > summary { cursor: pointer; list-style: none; display: flex;
  align-items: flex-start; gap: 12px; }
details.card.brain > summary::-webkit-details-marker { display: none; }
details.card.brain > summary .meta { flex: 1; min-width: 0; }
details.card.brain .chev { color: var(--text-faint); transition: transform .15s; flex-shrink: 0; }
details.card.brain[open] .chev { transform: rotate(180deg); }
.brain-edit { width: 100%; min-height: 260px; resize: vertical; padding: 12px 14px;
  background: var(--bg-2); border: 1px solid var(--border); border-radius: var(--r-sm);
  color: var(--text); font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 13px;
  line-height: 1.55; }
.brain-edit:focus { outline: none; border-color: #34343f; box-shadow: 0 0 0 3px rgba(170,180,214,.12); }

/* system metrics */
.metrics { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px,1fr)); gap: 14px; }
.metric { background: var(--surface); border: 1px solid var(--border); border-radius: var(--r-lg); padding: 18px; }
.metric .k { font-size: 12px; color: var(--text-faint); text-transform: uppercase; letter-spacing: .05em; }
.metric .v { font-size: 26px; font-weight: 700; margin: 6px 0 4px; letter-spacing: -.5px; }
.metric .sub { font-size: 12px; color: var(--text-dim); }
.meter { height: 7px; border-radius: 999px; background: var(--surface-3); overflow: hidden; margin-top: 10px; }
.meter > i { display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg,#36d399,#a78bfa); }
.meter.warn > i { background: linear-gradient(90deg,#f5c451,#fb7185); }
.rec { display: flex; gap: 10px; align-items: flex-start; padding: 11px 14px; border-radius: var(--r);
  background: var(--surface-2); border: 1px solid var(--border-soft); margin-bottom: 8px; font-size: 13.5px; }
.rec.warn { border-color: rgba(245,196,81,.3); } .rec.ok { border-color: rgba(54,211,153,.25); }
.svc { display: flex; align-items: center; gap: 12px; padding: 12px 14px; border-bottom: 1px solid var(--hair); }
.svc:last-child { border-bottom: none; }
.svc .nm { font-weight: 550; } .svc .u { color: var(--text-faint); font-size: 12px; }
.svc .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-faint); }
.svc .dot.up { background: var(--ok); } .svc .dot.down { background: var(--err); }

/* chat */
main:has(.chat-shell) { max-width: 1740px; padding: 16px 18px 38px; }
.chat-shell { display: grid; grid-template-columns: minmax(330px, 380px) minmax(0,1fr); gap: 18px;
  height: calc(100vh - 60px - 36px); min-height: 720px; }
.chat-side { display: flex; flex-direction: column; gap: 14px; min-height: 0; padding: 16px;
  background: linear-gradient(180deg, rgba(16,16,21,.96), rgba(8,8,11,.98));
  border: 1px solid var(--border); border-radius: var(--r-lg); box-shadow: inset 0 1px 0 rgba(255,255,255,.035); }
.side-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.side-head > div { display: grid; gap: 2px; }
.side-head small { color: var(--text-faint); font-size: 10px; text-transform: uppercase; letter-spacing: .08em; }
.side-head b { font-size: 15px; letter-spacing: -.1px; }
.chat-tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 3px;
  background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: var(--r-sm); }
.chat-tabs a { display: flex; align-items: center; justify-content: center; gap: 7px; padding: 8px 9px;
  border-radius: 7px; color: var(--text-dim); font-size: 12.5px; font-weight: 650; }
.chat-tabs a.active { background: var(--surface-3); color: var(--text); }
.chat-tabs span { color: var(--text-faint); font-size: 11px; }
.threads { display: flex; flex-direction: column; gap: 6px; min-height: 0; overflow: auto; }
.thread-wrap { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 6px; align-items: stretch; }
.thread-wrap > button { border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--surface-2);
  color: var(--text-dim); font: 11px inherit; padding: 0 8px; cursor: pointer; }
.thread-wrap > button:hover { color: var(--text); background: var(--surface-3); }
.thread { display: grid; gap: 4px; padding: 11px 12px; border-radius: var(--r-sm); color: var(--text-dim);
  border: 1px solid transparent; transition: background .14s, border-color .14s, color .14s; }
.thread:hover { background: rgba(255,255,255,.045); color: var(--text); border-color: var(--hair); }
.thread.active { background: linear-gradient(180deg, rgba(255,255,255,.07), rgba(255,255,255,.035));
  border-color: #2b2b34; color: var(--text); }
.thread span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13.5px; font-weight: 600; }
.thread small, .arch-note { color: var(--text-faint); font-size: 11px; }
.source-tag { display: inline-flex; align-items: center; width: fit-content; padding: 2px 7px;
  border: 1px solid rgba(170,180,214,.24); border-radius: 999px; background: rgba(170,180,214,.08);
  color: #c9d1ea; font-size: 10.5px; font-weight: 650; letter-spacing: 0; text-transform: none; }
.thread .source-tag { margin-right: 5px; }
.chat-title h1 .source-tag { display: inline-flex !important; vertical-align: middle; margin-left: 6px;
  color: #c9d1ea !important; font-size: 10.5px !important; letter-spacing: 0 !important;
  text-transform: none !important; }
.perm-box { margin-top: auto; display: grid; gap: 12px; padding: 14px; background: #050507;
  border: 1px solid #24242c; border-radius: var(--r); box-shadow: inset 0 1px 0 rgba(255,255,255,.035); }
.perm-head { display: flex; align-items: center; gap: 10px; }
.perm-head small { display: block; color: var(--text-faint); font-size: 10px; text-transform: uppercase; letter-spacing: .08em; }
.perm-head strong { display: block; font-size: 13.5px; }
.perm-icon { width: 32px; height: 32px; border: 1px solid var(--border); border-radius: 10px;
  display: grid; place-items: center; color: var(--link); background: var(--surface); }
.perm-icon svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 2;
  stroke-linecap: round; stroke-linejoin: round; }
.perm-grid { display: grid; grid-template-columns: 78px minmax(0,1fr); gap: 8px 10px; align-items: center; }
.perm-grid label { font-size: 10px; color: var(--text-faint); text-transform: uppercase; letter-spacing: .07em; }
.perm-grid select { padding: 8px 10px; font-size: 12.5px; background: var(--surface); }
.perm-status { display: inline-flex; align-items: center; gap: 8px; width: fit-content;
  color: var(--text-dim); background: rgba(255,255,255,.045); border: 1px solid var(--border-soft);
  border-radius: 999px; padding: 5px 9px; font-size: 11.5px; }
.perm-status span { width: 7px; height: 7px; border-radius: 999px; background: var(--link); }
.perm-status.mode-ask span { background: var(--warn); }
.perm-status.mode-auto span { background: var(--link); }
.perm-status.mode-bypass span { background: var(--err); }
.chat-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--r-lg); overflow: hidden;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.035), 0 24px 70px rgba(0,0,0,.42); }
.chat-title { display: flex; justify-content: space-between; align-items: center; gap: 12px;
  padding: 18px 24px; border-bottom: 1px solid var(--hair);
  background: linear-gradient(180deg, rgba(255,255,255,.035), rgba(255,255,255,.012)); }
.chat-title-actions { display: flex; gap: 8px; align-items: center; }
.chat-title span { display: block; color: var(--text-faint); font-size: 11px; text-transform: uppercase; letter-spacing: .07em; }
.chat-title h1 { margin: 2px 0 0; font-size: 18px; }
.chat-state { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 8px; }
.mode-pill { display: inline-flex !important; width: fit-content; padding: 4px 8px; border-radius: 999px;
  border: 1px solid var(--border-soft); background: rgba(255,255,255,.04); color: var(--text-dim) !important;
  font-size: 10.5px !important; letter-spacing: 0 !important; text-transform: none !important; }
.mode-pill.mode-ask { border-color: rgba(245,196,81,.24); color: #f8dfa0 !important; }
.mode-pill.mode-bypass { border-color: rgba(251,113,133,.28); color: #fecdd3 !important; }
.chat-log { flex: 1; overflow-y: auto; padding: 30px clamp(24px, 5vw, 70px);
  display: flex; flex-direction: column; gap: 20px;
  background: radial-gradient(circle at 50% 0, rgba(255,255,255,.035), transparent 38%); }
.msg-row { display: flex; flex-direction: column; gap: 7px; max-width: min(960px, 92%); }
.msg-row.user { align-self: flex-end; align-items: flex-end; }
.msg-row.bot, .msg-row.typing { align-self: flex-start; align-items: flex-start; }
.msg-row.sys { align-self: center; }
.msg { padding: 14px 16px; border-radius: 16px; font-size: 14.5px; line-height: 1.56;
  white-space: pre-wrap; overflow-wrap: anywhere; box-shadow: 0 10px 30px rgba(0,0,0,.22); }
.msg.user { background: linear-gradient(180deg, #f4f5f8, #d8dbe3); color: var(--accent-ink);
  border-bottom-right-radius: 5px; box-shadow: 0 14px 34px rgba(255,255,255,.08); }
.msg.bot { background: #0d0d11; border: 1px solid #23232b; border-bottom-left-radius: 5px; }
.msg.sys { color: var(--text-faint); font-size: 12px; background: none; }
.msg.typing { color: var(--text-faint); background: transparent; border: 1px solid var(--border-soft); }
.msg-actions { display: flex; gap: 6px; opacity: .18; transition: opacity .14s; }
.msg-row:hover .msg-actions { opacity: 1; }
.icon-btn { width: 30px; height: 30px; display: inline-grid; place-items: center; flex: 0 0 auto;
  border: 1px solid var(--border); color: var(--text-faint); background: var(--surface);
  border-radius: 8px; padding: 0; cursor: pointer; transition: color .14s, background .14s, border-color .14s, transform .1s; }
.icon-btn svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 2;
  stroke-linecap: round; stroke-linejoin: round; }
.icon-btn:hover { color: var(--text); background: var(--surface-2); border-color: #30303a; }
.icon-btn:active { transform: translateY(1px); }
.icon-btn.copied { color: var(--ok); border-color: rgba(54,211,153,.36); background: var(--ok-bg); }
.icon-btn.copy-fail { color: var(--err); border-color: rgba(251,113,133,.34); background: var(--err-bg); }
.icon-btn[data-delete]:hover { color: var(--err); border-color: rgba(251,113,133,.34); background: var(--err-bg); }
.title-icon { width: 34px; height: 34px; }
.title-icon svg { width: 17px; height: 17px; }
.action-card { display: grid; gap: 12px; margin-top: 12px; padding: 14px; background: #050507;
  border: 1px solid rgba(245,196,81,.26); border-radius: var(--r);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.035); }
.action-head { display: flex; align-items: center; gap: 11px; color: var(--text); }
.action-head small { display: block; color: #f8dfa0; font-size: 10px; letter-spacing: .08em;
  text-transform: uppercase; margin-bottom: 2px; }
.action-head b { display: block; font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--text); }
.action-mark { width: 34px; height: 34px; border: 1px solid rgba(245,196,81,.24);
  border-radius: 11px; display: grid; place-items: center; color: #f8dfa0; background: rgba(245,196,81,.06); }
.action-mark svg { width: 17px; height: 17px; fill: none; stroke: currentColor; stroke-width: 2;
  stroke-linecap: round; stroke-linejoin: round; }
.action-card p { margin: -2px 0 0; color: var(--text-dim); font-size: 12.5px; }
.action-msg { margin: 0; padding: 9px 12px; border-left: 2px solid rgba(245,196,81,.5);
  background: rgba(255,255,255,.03); border-radius: 0 8px 8px 0; color: var(--text);
  font-size: 13px; white-space: pre-wrap; }
.action-details { border: 1px solid var(--border-soft); border-radius: 10px; padding: 8px 10px;
  background: rgba(255,255,255,.025); }
.action-details summary { cursor: pointer; color: var(--text-dim); font-size: 12px; }
.action-details pre { margin: 9px 0 0; max-height: 180px; overflow: auto; color: var(--text-dim);
  font: 12px/1.45 'JetBrains Mono', monospace; white-space: pre-wrap; }
.action-row { display: flex; gap: 8px; align-items: center; }
.tool-cards { display: grid; gap: 8px; margin-top: 12px; }
.tool-card { border: 1px solid var(--border); background: #050507; border-radius: 8px; padding: 8px 10px; }
.tool-card.ok { border-color: rgba(54,211,153,.28); }
.tool-card.warn { border-color: rgba(245,196,81,.32); }
.tool-card summary { cursor: pointer; display: flex; gap: 10px; align-items: center; color: var(--text-dim); }
.tool-card summary b { color: var(--text); font-family: 'JetBrains Mono', monospace; font-size: 12px; }
.tool-card summary span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.tool-card pre { margin: 8px 0 0; max-height: 220px; overflow: auto; color: var(--text-dim);
  font: 11.5px/1.45 'JetBrains Mono', monospace; white-space: pre-wrap; }
.chat-input { display: flex; gap: 10px; align-items: flex-end; padding: 16px 22px; border-top: 1px solid var(--hair);
  background: linear-gradient(180deg, rgba(255,255,255,.018), rgba(0,0,0,.34)); }
.chat-input textarea { flex: 1; resize: none; min-height: 50px; max-height: 220px;
  background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--r);
  padding: 12px 13px; font: inherit; }
.chat-input textarea:focus { outline: none; border-color: #34343f; box-shadow: 0 0 0 3px rgba(170,180,214,.12); }
.send-btn svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 2;
  stroke-linecap: round; stroke-linejoin: round; }
.chat-input.archived { justify-content: space-between; align-items: center; }
.chat-input.archived p { margin: 0; color: var(--text-dim); font-size: 13px; }
.upload-btn { flex: 0 0 auto; margin-bottom: 4px; }
.attachments { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.attachment { display: inline-flex; align-items: center; gap: 7px; max-width: 260px; padding: 7px 9px;
  border: 1px solid var(--hair); border-radius: 8px; background: rgba(255,255,255,.035); color: var(--text); text-decoration: none; }
.attachment b { font-size: 10px; color: var(--text-faint); text-transform: uppercase; }
.attachment span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.confirm-modal { position: fixed; inset: 0; z-index: 80; display: grid; place-items: center;
  padding: 22px; background: rgba(0,0,0,.72); backdrop-filter: blur(16px); }
.confirm-modal[hidden] { display: none; }
.confirm-dialog { width: min(460px, 100%); display: grid; grid-template-columns: auto minmax(0,1fr);
  gap: 14px; padding: 18px; border: 1px solid #2a2a32; border-radius: var(--r-lg);
  background: linear-gradient(180deg, #111116, #070709); box-shadow: 0 24px 90px rgba(0,0,0,.72); }
.confirm-mark { width: 38px; height: 38px; border-radius: 12px; border: 1px solid var(--border);
  display: grid; place-items: center; color: var(--link); background: var(--surface); }
.confirm-mark svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2;
  stroke-linecap: round; stroke-linejoin: round; }
.confirm-dialog h2 { margin: 0 0 5px; font-size: 17px; }
.confirm-dialog p { margin: 0; color: var(--text-dim); font-size: 13px; line-height: 1.45; }
.confirm-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }

/* secretary */
.secretary-grid { display: grid; grid-template-columns: minmax(0,1.2fr) minmax(320px,.8fr); gap: 16px; }
.secretary-cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
@media (max-width: 720px) { .secretary-cards { grid-template-columns: 1fr; } }
.secretary-card { display: grid; gap: 12px; padding: 16px; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--r-lg); }
.secretary-card.active { border-color: rgba(170,180,214,.34); box-shadow: inset 0 1px 0 rgba(255,255,255,.04), 0 18px 42px rgba(0,0,0,.28); }
.secretary-card h3 { margin: 0; font-size: 15px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.secretary-card p { margin: 0; color: var(--text-dim); font-size: 12.5px; line-height: 1.45; }
.secretary-card .mini { color: var(--text-faint); font-size: 11px; }
.install-head { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; }
.install-head span:first-child { color: var(--text-faint); font-size: 10px; text-transform: uppercase; letter-spacing: .08em; }
.install-actions { display: flex; gap: 9px; align-items: center; flex-wrap: wrap; }
.install-config { display: grid; gap: 10px; border-top: 1px solid var(--border-soft); padding-top: 12px; }
.install-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px,1fr)); gap: 9px; }
.setup-field { display: grid; gap: 5px; }
.setup-field input, .setup-field select { padding: 9px 10px; font-size: 12.5px; }
.secretary-row { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }
.secretary-row label, .secretary-card label { color: var(--text-faint); font-size: 10px;
  text-transform: uppercase; letter-spacing: .07em; }
.secretary-switch { display: inline-flex; align-items: center; gap: 8px; color: var(--text-dim);
  font-size: 12px; text-transform: none !important; letter-spacing: 0 !important; }
.secretary-switch input { width: auto; }
.setup-chat { display: grid; gap: 10px; padding: 16px; background: #050507; border: 1px solid var(--border);
  border-radius: var(--r-lg); }
.setup-coach { position: sticky; top: 82px; align-self: start; }
.setup-bubble { padding: 12px 13px; border: 1px solid var(--border-soft); border-radius: 14px;
  background: rgba(255,255,255,.035); color: var(--text-dim); font-size: 13px; line-height: 1.48; }
.setup-bubble strong { color: var(--text); }
.setup-chatbox { display: grid; gap: 8px; margin-top: 4px; padding: 10px; background: #050507;
  border: 1px solid var(--border-soft); border-radius: var(--r-sm); }
.setup-log { display: grid; gap: 7px; max-height: 210px; overflow: auto; }
.setup-msg { display: grid; gap: 2px; max-width: 92%; padding: 8px 9px; border-radius: 10px;
  background: rgba(255,255,255,.035); border: 1px solid var(--border-soft); }
.setup-msg.user { justify-self: end; background: rgba(244,245,248,.12); border-color: rgba(244,245,248,.18); }
.setup-msg b { color: var(--text); font-size: 10px; text-transform: uppercase; letter-spacing: .07em; }
.setup-msg span { color: var(--text-dim); font-size: 12px; line-height: 1.4; }
.setup-input { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 7px; }
.setup-input input { padding: 8px 9px; font-size: 12.5px; }
.pairing-panel { display: grid; gap: 10px; padding: 11px; border: 1px solid var(--border-soft);
  border-radius: var(--r-sm); background: rgba(255,255,255,.025); }
.pairing-panel b { display: block; font-size: 12.5px; }
.pairing-panel span { display: block; color: var(--text-dim); font-size: 12px; line-height: 1.4; }
.pairing-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.qr-box { min-height: 52px; display: grid; place-items: center; color: var(--text-faint);
  border: 1px dashed var(--border); border-radius: var(--r-sm); padding: 10px; text-align: center; font-size: 12px; }
.qr-box img { max-width: 190px; width: 100%; height: auto; border-radius: 8px; background: #fff; padding: 8px; }
.qr-box small { display: block; margin-top: 6px; color: var(--text-faint); }
.policy-stack { display: grid; gap: 9px; }
.policy-line { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px;
  border: 1px solid var(--border-soft); border-radius: var(--r-sm); background: rgba(255,255,255,.025); }
.policy-line b { color: var(--text); font-size: 12.5px; }
.policy-line span { color: var(--text-dim); font-size: 12px; line-height: 1.35; }

/* settings / labs */
.settings-hero { display: flex; align-items: flex-start; justify-content: space-between;
  gap: 18px; margin: 6px 0 28px; }
.settings-tabs { position: sticky; top: 60px; z-index: 8; display: flex; gap: 6px; flex-wrap: wrap;
  margin: -12px 0 16px; padding: 7px; background: rgba(5,5,7,.78);
  border: 1px solid var(--hair); border-radius: var(--r); backdrop-filter: blur(18px); }
.settings-tabs a { padding: 7px 12px; border-radius: var(--r-sm); color: var(--text-dim);
  font-size: 12.5px; font-weight: 650; }
.settings-tabs a:hover { color: var(--text); background: rgba(255,255,255,.055); }
.settings-tabs ~ form .panel[id] { scroll-margin-top: 126px; }
.github-capsule { position: relative; width: 42px; height: 42px; display: grid; place-items: center;
  flex-shrink: 0; color: var(--text); background: var(--surface); border: 1px solid var(--border);
  border-radius: 999px; transition: transform .18s, border-color .18s, background .18s; }
.github-capsule svg { width: 21px; height: 21px; transition: transform .2s; }
.github-capsule:hover { transform: translateY(-2px); border-color: #34343f; background: var(--surface-2); }
.github-capsule:hover svg { transform: rotate(-8deg) scale(1.08); }
.gh-pop { position: absolute; top: 48px; right: 0; display: grid; gap: 1px; min-width: 112px;
  padding: 9px 11px; color: var(--text); background: var(--surface-2); border: 1px solid var(--border);
  border-radius: var(--r-sm); box-shadow: var(--shadow); opacity: 0; transform: translateY(-4px);
  pointer-events: none; transition: opacity .16s, transform .16s; }
.gh-pop::before { content: ''; position: absolute; top: -5px; right: 14px; width: 9px; height: 9px;
  background: var(--surface-2); border-left: 1px solid var(--border); border-top: 1px solid var(--border);
  transform: rotate(45deg); }
.github-capsule:hover .gh-pop { opacity: 1; transform: translateY(0); }
.gh-pop b { font-size: 13px; } .gh-pop small { color: var(--text-dim); font-size: 11px; }
/* plain OLED panel — no decorative wash (matches channels / setup coach) */
.labs-console { overflow: hidden; position: relative; }
.labs-console > * { position: relative; z-index: 1; }

/* QR-first channel pairing + collapsible advanced fields */
.pairing-panel.primary { background: var(--surface-2); border: 1px solid var(--border-soft);
  border-radius: var(--r); padding: 15px; display: grid; gap: 12px; margin: 4px 0 2px; }
.pairing-panel.primary span { color: var(--text-dim); font-size: 12.5px; line-height: 1.5; }
.qr-box { display: grid; place-items: center; gap: 6px; min-height: 8px; }
.qr-box img { width: 232px; height: 232px; border-radius: 12px; background: #fff; padding: 8px; }
.qr-box small { color: var(--text-faint); font-size: 11px; }
details.adv { margin-top: 12px; border-top: 1px solid var(--hair); padding-top: 10px; }
details.adv > summary { cursor: pointer; list-style: none; font-size: 13px; font-weight: 600;
  color: var(--text-dim); }
details.adv > summary::-webkit-details-marker { display: none; }
details.adv > summary::before { content: '▸ '; color: var(--text-faint); }
details.adv[open] > summary::before { content: '▾ '; }
details.adv[open] > summary { margin-bottom: 12px; }
.waha-test-box .setup-log { display: flex; flex-direction: column; gap: 6px; margin-top: 8px;
  max-height: 280px; overflow-y: auto; }
.waha-test-box .setup-msg.user { align-self: flex-end; }
.email-account { border: 1px solid var(--hair); border-radius: 12px; padding: 12px;
  margin-bottom: 12px; background: rgba(255,255,255,0.02); }
.email-account-head { display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 10px; font-size: 13px; }
.contact-rule-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
  margin-bottom: 6px; }
.contact-rule-row select { flex: 1; min-width: 100px; }
.contact-rule-row input { min-width: 80px; }
.install-policy { display: flex; flex-wrap: wrap; gap: 14px; align-items: center;
  margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--hair); }
.install-policy .setup-field { margin: 0; }
.labs-head { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
.labs-head .beaker { color: #d8dbe3; flex-shrink: 0; filter: drop-shadow(0 0 16px rgba(216,219,227,.16)); }
.labs-head h2 { margin: 0 0 4px; font-size: 19px; }
.labs-head p { margin: 0; color: var(--text-dim); font-size: 13px; line-height: 1.5; }
.lab-eyebrow { color: #c7ccda; font: 700 10px 'JetBrains Mono', monospace; letter-spacing: .08em;
  text-transform: uppercase; margin-bottom: 5px; }
.labs-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 12px; }
.lab-tile { min-height: 174px; display: flex; flex-direction: column; gap: 8px; padding: 15px;
  background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: var(--r);
  transition: border-color .16s, background .16s, transform .16s; }
.lab-tile:hover { transform: translateY(-2px); border-color: #30303a; background: var(--surface-3); }
.lab-title { font-weight: 650; font-size: 14px; }
.lab-tile p { margin: 0 0 auto; color: var(--text-dim); font-size: 12.5px; line-height: 1.45; }
.font-preview { display: grid; gap: 5px; margin: 0 0 14px; padding: 13px 15px;
  background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: var(--r); }
.font-preview span { color: var(--text-faint); font: 700 10px 'JetBrains Mono', monospace; letter-spacing: .08em; }
.font-preview strong { font-family: var(--ui-font-preview, var(--ui-font)); font-size: 17px; font-weight: 700; }
.address-box { position: relative; flex: 1; min-width: 260px; }
.address-results { position: absolute; left: 0; right: 0; top: calc(100% + 6px); z-index: 20;
  max-height: 280px; overflow: auto; background: var(--surface-2); border: 1px solid var(--border);
  border-radius: var(--r); box-shadow: var(--shadow); padding: 6px; }
.address-results button { width: 100%; display: grid; gap: 3px; text-align: left; padding: 10px 11px;
  border: none; border-radius: var(--r-sm); color: var(--text); background: transparent; cursor: pointer;
  font-family: inherit; }
.address-results button:hover { background: rgba(255,255,255,.06); }
.address-results b { font-size: 12.5px; line-height: 1.35; font-weight: 600; }
.address-results span, .addr-empty { color: var(--text-faint); font-size: 11.5px; }
.geo-status { min-height: 18px; margin: -2px 0 10px; }
.geo-status[data-kind="ok"] { color: #a7f3d0; }
.geo-status[data-kind="warn"] { color: #fcd34d; }
.region-strip { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 14px; }
.region-strip span { font-size: 12px; color: var(--text-dim); background: var(--surface-2);
  border: 1px solid var(--border-soft); border-radius: 999px; padding: 5px 10px; }
.region-strip b { color: var(--text); font-weight: 600; }
.save-pulse { animation: savePulse 900ms ease-out; }
@keyframes savePulse { 0% { box-shadow: 0 0 0 0 rgba(54,211,153,.38); }
  100% { box-shadow: 0 0 0 14px rgba(54,211,153,0); } }

/* updates / hyperspace */
#hyper { position: fixed; inset: 0; z-index: 999; background: #000; display: none; }
#hyper.on { display: block; }
.commit { padding: 12px 14px; border-left: 2px solid var(--border); margin-left: 6px; }
.commit .h { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--link); }
.commit .m { font-size: 14px; margin-top: 3px; }

/* empty state */
.empty { text-align: center; color: var(--text-faint); padding: 54px 20px; font-size: 14px; }

@media (max-width: 560px) {
  main { padding: 22px 15px 50px; }
  .hero h1 { font-size: 25px; }
  header.topbar { padding: 0 15px; gap: 10px; overflow: hidden; }
  .brand img { height: 26px; }
  header.topbar nav { min-width: 0; max-width: calc(100vw - 106px); overflow-x: auto;
    scrollbar-width: none; }
  header.topbar nav::-webkit-scrollbar { display: none; }
  .navlink { flex: 0 0 auto; }
  main:has(.chat-shell) { padding: 14px 12px 38px; }
  .chat-shell { grid-template-columns: 1fr; height: auto; min-height: 0; }
  .chat-side { max-height: 46vh; }
  .chat-log { min-height: 54vh; padding: 20px 14px; }
  .msg-row { max-width: 96%; }
  .chat-input { padding: 13px; flex-wrap: wrap; }
  .chat-input textarea { flex-basis: 100%; }
  .settings-hero { align-items: center; }
  .settings-tabs { top: 60px; overflow-x: auto; flex-wrap: nowrap; }
  .secretary-grid { grid-template-columns: 1fr; }
  .secretary-row { grid-template-columns: 1fr; }
  .location-row { flex-direction: column; align-items: stretch !important; }
  .address-box { width: 100%; min-width: 0; }
  .github-capsule { width: 38px; height: 38px; }
}
"""


def page(title: str, body: str, *, nav: bool = True, active: str = "") -> str:
    def navlink(href: str, label: str, key: str) -> str:
        cls = " active" if active == key else ""
        return f'<a href="{href}" class="navlink{cls}">{label}</a>'

    navhtml = ""
    if nav:
        navhtml = (
            '<nav>'
            f'{navlink("/admin", "Plugins", "plugins")}'
            f'{navlink("/admin/chat", "Chat", "chat")}'
            f'{navlink("/admin/brain", "Wissen", "brain")}'
            f'{navlink("/admin/secretary", "Secretary", "secretary")}'
            f'{navlink("/admin/osint", "Recon", "osint")}'
            f'{navlink("/admin/system", "System", "system")}'
            f'{navlink("/admin/settings", "Einstellungen", "settings")}'
            f'{navlink("/admin/update", "Update", "update")}'
            '<form method="post" action="/admin/logout" style="display:inline;margin-left:4px">'
            '<button class="btn ghost sm" type="submit">Logout</button></form>'
            '</nav>'
        )
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · ASTRA</title>{favicon_link()}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
{_font_head()}
<style>{_CSS}</style></head><body>
<header class="topbar"><a class="brand" href="/admin"><img src="{LOGO_LONG}" alt="ASTRA"></a>{navhtml}</header>
<main>{body}</main></body></html>"""
