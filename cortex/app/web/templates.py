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


def icon_html(slug: str, emoji: str) -> str:
    """Render a plugin icon: monochrome brand logo, else the emoji fallback."""
    brand = BRAND_ICONS.get(slug)
    if brand:
        return (f'<span class="icon" data-emoji="{esc(emoji)}">'
                f'<img src="https://cdn.simpleicons.org/{brand}/d8dbe3" alt="" loading="lazy" '
                f'onerror="this.parentNode.textContent=this.parentNode.dataset.emoji"></span>')
    return f'<span class="icon">{esc(emoji)}</span>'


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
  font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
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
.btn.secondary { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); }
.btn.secondary:hover { background: var(--surface-3); filter: none; }
.btn.ghost { background: transparent; color: var(--text-dim); border: 1px solid var(--border); }
.btn.ghost:hover { color: var(--text); background: rgba(255,255,255,.04); }
.btn.sm { padding: 7px 13px; font-size: 13px; }
.btn.block { width: 100%; }

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

/* empty state */
.empty { text-align: center; color: var(--text-faint); padding: 54px 20px; font-size: 14px; }

@media (max-width: 560px) {
  main { padding: 22px 15px 50px; }
  .hero h1 { font-size: 25px; }
  header.topbar { padding: 0 15px; }
  .brand img { height: 26px; }
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
            f'{navlink("/admin/settings", "Einstellungen", "settings")}'
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
<header class="topbar"><a class="brand" href="/admin"><img src="{LOGO_LONG}" alt="ASTRA"></a>{navhtml}</header>
<main>{body}</main></body></html>"""
