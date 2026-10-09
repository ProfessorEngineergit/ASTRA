"""Bildgenerierung (OpenAI Images) — Bilder erzeugen und auf dem Wand-Display zeigen.

Kostet pro Bild Geld, darum wie `astra_model_run` als `external_send` eingestuft: im
Web-Chat erst nach deinem Klick auf „Ausführen“, sonst nur auf deinen ausdrücklichen
Wunsch (owner-only) — und mit Tageslimit. Das Bild landet im brain_data-Volume
(`uploads/web_chat`, im Admin unter /admin/uploads/<name>), nie im LLM-Kontext: das
Tool-Ergebnis enthält nur den Verweis `astra-file:<name>`.
"""
from __future__ import annotations

import base64
import logging
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from ... import db, usage
from ...config import get_settings
from ...display import cards as cardlib
from ...display import service as display
from ...display import speech
from ...tools import Tool, ToolContext, tool_result
from ..base import ConfigField, FieldType, Plugin, PluginCategory

log = logging.getLogger("astra.plugin.image_generation")

SIZES = ["1024x1024", "1536x1024", "1024x1536", "auto"]


class ImageGenerationPlugin(Plugin):
    slug = "image_generation"
    name = "Bildgenerierung (OpenAI)"
    description = "Bilder per OpenAI erzeugen — auf Wunsch direkt aufs Wand-Display oder Whiteboard."
    category = PluginCategory.INFRA_AI
    icon = "🎨"
    config_fields = [
        ConfigField("model", "Modell", default="gpt-image-1",
                    help="z. B. gpt-image-1 oder dall-e-3 (nutzt den OpenAI-Key von ASTRA)"),
        ConfigField("size", "Format", FieldType.SELECT, default="1536x1024", options=SIZES),
        ConfigField("quality", "Qualität", FieldType.SELECT, default="medium",
                    options=["low", "medium", "high", "auto"], help="Höher = teurer"),
        ConfigField("daily_limit", "Max. Bilder pro Tag", FieldType.NUMBER, default=20,
                    help="Schutz vor Kostenexplosion; 0 = kein Limit"),
    ]

    async def _count_today(self, bump: bool = False) -> int:
        day = datetime.now(ZoneInfo(get_settings().astra_timezone)).date().isoformat()
        key = f"image_gen_count:{day}"
        n = int(await db.get_setting(key, 0) or 0)
        if bump:
            n += 1
            await db.set_setting(key, n)
        return n

    async def generate(self, prompt: str, *, size: str | None = None) -> tuple[str, dict]:
        """Erzeugt ein Bild, speichert es und gibt (Dateiname, Metadaten) zurück."""
        key = speech.openai_key()
        if not key:
            raise RuntimeError("Kein OpenAI-Key konfiguriert.")
        model = str(self.get("model") or "gpt-image-1")
        kwargs: dict = {"model": model, "prompt": prompt, "n": 1,
                        "size": size if size in SIZES else str(self.get("size") or "1024x1024")}
        if model.startswith("dall-e"):
            kwargs["response_format"] = "b64_json"
            if kwargs["size"] in ("1536x1024", "auto"):
                kwargs["size"] = "1792x1024" if model == "dall-e-3" else "1024x1024"
            elif kwargs["size"] == "1024x1536":
                kwargs["size"] = "1024x1792" if model == "dall-e-3" else "1024x1024"
            ext = "png"
        else:
            kwargs["quality"] = str(self.get("quality") or "medium")
            kwargs["output_format"] = "jpeg"
            ext = "jpg"
        started = time.perf_counter()
        resp = await AsyncOpenAI(api_key=key).images.generate(**kwargs)
        item = resp.data[0]
        b64 = getattr(item, "b64_json", None)
        if not b64:
            raise RuntimeError("Die API hat kein Bild geliefert.")
        name = f"img-{uuid.uuid4().hex[:16]}.{ext}"
        (cardlib.media_dir() / name).write_bytes(base64.b64decode(b64))
        u = getattr(resp, "usage", None)
        try:
            await usage.record(usage.build_event(
                provider="openai", model=model, role="image",
                prompt_tokens=getattr(u, "input_tokens", None) if u else None,
                completion_tokens=getattr(u, "output_tokens", None) if u else None,
                started=started, fallback_in=prompt))
        except Exception:  # noqa: BLE001
            pass
        return name, {"model": model, "size": kwargs["size"],
                      "revised_prompt": getattr(item, "revised_prompt", None)}

    def tools(self) -> list[Tool]:
        async def _generate(args: dict, ctx: ToolContext) -> str:
            if not self.enabled:
                return tool_result(ok=False, source=self.slug, summary="Bildgenerierung ist deaktiviert.")
            prompt = str(args.get("prompt") or "").strip()
            if not prompt:
                return tool_result(ok=False, source=self.slug, summary="prompt fehlt.")
            limit = int(self.get("daily_limit") or 0)
            if limit and await self._count_today() >= limit:
                return tool_result(ok=False, source=self.slug,
                                   summary=f"Tageslimit von {limit} Bildern erreicht (Plugin-Einstellungen).")
            try:
                name, meta = await self.generate(prompt[:4000], size=args.get("size"))
            except Exception as e:  # noqa: BLE001
                log.warning("Bildgenerierung fehlgeschlagen: %s", e)
                return tool_result(ok=False, source=self.slug, summary=f"Bild konnte nicht erzeugt werden: {e}",
                                   error={"type": type(e).__name__, "message": str(e)})
            await self._count_today(bump=True)
            ref = cardlib.file_ref(name)
            caption = str(args.get("caption") or "").strip()[:300]
            card = cardlib.validate_card({"type": "image", "title": caption or "Bild",
                                          "data": {"src": ref, "alt": prompt[:300],
                                                   **({"caption": caption} if caption else {})}})
            data: dict = {"file": name, "url": f"/admin/uploads/{name}", "ref": ref, **meta}
            shown = []
            if args.get("show_on_display", True):
                if ctx.channel == "display":
                    data["display"] = {"cards": [card]}
                    shown.append("auf dem Display")
                elif display.connected():
                    display.publish("card", {"card": card})
                    shown.append("auf dem Display")
            if args.get("board") and display.connected():
                op = {"op": "add_image", "src": ref, **({"caption": caption} if caption else {})}
                display.publish("board", cardlib.validate_board_op(op))
                shown.append("auf dem Whiteboard")
            where = (" und " + " und ".join(shown)) if shown else ""
            await db.audit("image_generated", actor="astra", channel=ctx.channel,
                           detail={"file": name, "model": meta.get("model"), "prompt": prompt[:200]})
            return tool_result(ok=True, source=self.slug, data=data,
                               summary=f"Bild erstellt{where} — {data['url']} (Verweis {ref}).")

        return [Tool(
            name="generate_image",
            description=("Erzeuge ein Bild aus einer Beschreibung (OpenAI, kostet pro Bild). Zeigt es auf dem "
                         "Wand-Display, wenn eines verbunden ist (show_on_display, Standard an); board=true legt "
                         "es zusätzlich aufs Whiteboard. Nur auf Bahrians ausdrücklichen Wunsch."),
            parameters={"type": "object", "properties": {
                "prompt": {"type": "string", "description": "Bildbeschreibung (gern ausführlich, Englisch ok)"},
                "caption": {"type": "string"},
                "size": {"type": "string", "enum": SIZES},
                "show_on_display": {"type": "boolean"},
                "board": {"type": "boolean"}},
                "required": ["prompt"]},
            handler=_generate, owner_only=True, source=self.slug,
            safety="external_send", intents=["control"],
            examples=["Mal mir einen Roboter im Bauhaus-Stil aufs Display"],
        )]
