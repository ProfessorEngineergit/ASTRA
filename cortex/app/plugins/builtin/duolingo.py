"""Duolingo — Streak lesen + „heute schon geübt?" (best effort).

WICHTIG / ehrliche Grenze: Duolingo hat KEINE offizielle API. Dieser Abruf nutzt
den inoffiziellen Profil-Endpoint für öffentliche Profile — er kann jederzeit
brechen oder sich ändern. Das Plugin meldet das offen über den Health-Status,
statt still zu scheitern. Für zuverlässige „heute erledigt"-Erkennung hilft ein
JWT (optional); ohne ihn ist `done` eine Schätzung aus dem Streak-Kalender.

Zweck hier: das mustergültige Beispiel für die Regelschicht (W4) — „erinnere mich
abends, wenn ich Duolingo noch nicht gemacht habe" → Ansage + Push + Google-Task.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx

from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.duolingo")

_API = "https://www.duolingo.com/2017-06-30/users"


class DuolingoPlugin(Plugin):
    slug = "duolingo"
    name = "Duolingo"
    description = "Streak & Tagesziel lesen — Grundlage für Übungs-Erinnerungen."
    category = PluginCategory.SCHOOL
    icon = "🦉"
    config_fields = [
        ConfigField("username", "Benutzername", required=True,
                    help="Dein öffentlicher Duolingo-Benutzername"),
        ConfigField("jwt", "JWT-Token (optional)", FieldType.PASSWORD, required=False, secret=True,
                    help="Nur nötig für zuverlässige 'heute erledigt'-Erkennung"),
    ]

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": "Mozilla/5.0"}
        if jwt := self.get("jwt"):
            h["Authorization"] = f"Bearer {jwt}"
        return h

    async def _fetch(self) -> dict[str, Any]:
        params = {"username": self.get("username"),
                  "fields": "streak,streakData,username,totalXp,xpGains"}
        async with httpx.AsyncClient(timeout=12, headers=self._headers()) as c:
            r = await c.get(_API, params=params)
            r.raise_for_status()
            data = r.json()
        users = data.get("users") if isinstance(data, dict) else None
        return (users[0] if users else data) or {}

    @staticmethod
    def _done_today(user: dict) -> bool | None:
        """Best-effort: hat der Streak heute schon Fortschritt? None = unbekannt."""
        sd = user.get("streakData") or {}
        cur = sd.get("currentStreak") or {}
        end = cur.get("endDate") or ""
        if end:
            try:
                return datetime.fromisoformat(end).date() >= date.today()
            except ValueError:
                pass
        # XP-Gewinne von heute (nur mit JWT verfügbar)
        gains = user.get("xpGains") or []
        if gains:
            today = datetime.now(timezone.utc).date()
            for g in gains:
                ts = g.get("time")
                if ts and datetime.fromtimestamp(ts, timezone.utc).date() == today:
                    return True
            return False
        return None

    async def health_check(self) -> HealthStatus:
        base = await super().health_check()
        if base.state.value != "ok":
            return base
        try:
            user = await self._fetch()
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(f"Duolingo (inoffiziell) nicht erreichbar: {e}")
        streak = user.get("streak") or (user.get("streakData") or {}).get("currentStreak", {}).get("length")
        note = "" if self.get("jwt") else " (ohne JWT ist 'heute erledigt' nur geschätzt)"
        return HealthStatus.ok(f"Streak: {streak or '?'} Tage{note}")

    def tools(self) -> list[Tool]:
        async def _status(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="Duolingo ist deaktiviert.")
            try:
                user = await self._fetch()
            except Exception as e:  # noqa: BLE001
                return tool_result(ok=False, source=self.slug,
                                   summary=f"Duolingo-Abruf fehlgeschlagen (inoffiziell): {e}",
                                   error={"message": str(e)})
            done = self._done_today(user)
            streak = user.get("streak") or \
                (user.get("streakData") or {}).get("currentStreak", {}).get("length")
            return tool_result(
                ok=True, source=self.slug,
                summary=(f"Streak {streak or '?'} Tage · heute "
                         + ("erledigt" if done else "offen" if done is False else "unklar")),
                data={"streak": streak, "done": bool(done), "done_known": done is not None,
                      "total_xp": user.get("totalXp")},
            )

        return [Tool(
            name="duolingo_status",
            description="Duolingo-Streak und ob heute schon geübt wurde (best effort).",
            parameters={"type": "object", "properties": {}},
            handler=_status, owner_only=True, source=self.slug,
            safety="private_read", intents=["status"],
        )]

    def rule_templates(self) -> list[dict]:
        return [{
            "name": "Duolingo-Erinnerung",
            "plugin_slug": self.slug,
            "trigger": {"type": "schedule", "at": "21:00", "days": [0, 1, 2, 3, 4, 5, 6]},
            "condition": {"type": "tool", "tool": "duolingo_status",
                          "expect": {"path": "data.done", "equals": False}},
            "actions": [
                {"type": "speak", "text": "Vergiss dein Duolingo nicht.", "where": "Schlafzimmer"},
                {"type": "notify", "text": "🦉 Duolingo heute noch offen.", "urgency": "normal"},
                {"type": "tool", "tool": "add_google_task", "args": {"title": "Duolingo machen"}},
            ],
        }]
