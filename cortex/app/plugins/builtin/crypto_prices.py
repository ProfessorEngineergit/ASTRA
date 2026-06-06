"""Crypto prices — current quotes via the free CoinGecko API (no key)."""
from __future__ import annotations

import httpx

from ...tools import Tool, ToolContext
from ..base import ConfigField, FieldType, HealthStatus, Plugin, PluginCategory

API = "https://api.coingecko.com/api/v3"


class CryptoPlugin(Plugin):
    slug = "crypto"
    name = "Krypto-Kurse"
    description = "Aktuelle Kurse (BTC, ETH, …) via CoinGecko — kostenlos, kein Key."
    category = PluginCategory.PRODUCTIVITY
    icon = "💰"
    config_fields = [
        ConfigField("coins", "Standard-Coins", default="bitcoin,ethereum",
                    help="CoinGecko-IDs, kommagetrennt (bitcoin, ethereum, solana …)"),
        ConfigField("currency", "Währung", type=FieldType.SELECT,
                    options=["eur", "usd", "gbp", "chf"], default="eur"),
    ]

    async def _prices(self, ids: str) -> dict:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get(f"{API}/simple/price",
                            params={"ids": ids, "vs_currencies": self.get("currency", "eur"),
                                    "include_24hr_change": "true"})
            return r.json()

    async def health_check(self) -> HealthStatus:
        if not self.is_toggled_on:
            return HealthStatus.disabled()
        try:
            d = await self._prices("bitcoin")
            return (HealthStatus.ok("CoinGecko erreichbar.") if "bitcoin" in d
                    else HealthStatus.error("Unerwartete Antwort."))
        except Exception as e:  # noqa: BLE001
            return HealthStatus.error(str(e))

    def _fmt(self, d: dict) -> str:
        cur = self.get("currency", "eur")
        sym = {"eur": "€", "usd": "$", "gbp": "£", "chf": "CHF"}.get(cur, cur)
        out = []
        for coin, v in d.items():
            price = v.get(cur)
            chg = v.get(f"{cur}_24h_change")
            arrow = "▲" if (chg or 0) >= 0 else "▼"
            out.append(f"• {coin.capitalize()}: {price:,.2f} {sym} {arrow}{abs(chg or 0):.1f}%")
        return "\n".join(out)

    async def briefing_section(self) -> str | None:
        if not self.enabled:
            return None
        try:
            d = await self._prices(self.get("coins", "bitcoin,ethereum"))
            return "💰 Krypto:\n" + self._fmt(d) if d else None
        except Exception:  # noqa: BLE001
            return None

    def tools(self) -> list[Tool]:
        async def _price(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return "Krypto-Plugin ist deaktiviert."
            ids = args.get("coins") or self.get("coins", "bitcoin,ethereum")
            d = await self._prices(ids)
            return self._fmt(d) if d else "Keine Kurse gefunden."

        return [Tool(
            name="crypto_price",
            description="Aktuelle Krypto-Kurse (CoinGecko-IDs, z. B. bitcoin,ethereum).",
            parameters={"type": "object", "properties": {
                "coins": {"type": "string", "description": "kommagetrennt, optional"}}},
            handler=_price, owner_only=True, source=self.slug,
        )]
