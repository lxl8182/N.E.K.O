"""Offer enabled proactive sources to the controller that owns the conversation."""
from __future__ import annotations

import asyncio
import time

from .contracts import ProactiveChatResult
from .decisions import _select_source_modes
from .sources import collect_proactive_sources
from utils.tokenize import truncate_to_tokens


async def offer_companion_proactive(command, name, log):
    from main_routers.sts2_companion_router import _active

    state = _active(name)
    if state is None:
        return None
    task = state.get("proactive_task")
    if (task and not task.done()) or any(c.get("kind") == "proactive_material" for c in state["callbacks"]):
        return ProactiveChatResult({"action": "pass", "reason_code": "COMPANION_PENDING"})
    selection = _select_source_modes(command, None, debug_force_invite=False)
    modes = list(selection.enabled_modes)
    if command.is_music_occupied or command.is_playing_music or command.music_cooldown:
        modes = [mode for mode in modes if mode != "music"]
    if not modes:
        return ProactiveChatResult({"action": "pass", "reason_code": "PASS_SOURCE_EMPTY"})

    async def collect():
        try:
            async with asyncio.timeout(40):
                sources = await collect_proactive_sources(command=command, enabled_modes=modes,
                                                         lanlan_name=name, log=log)
            # A late fetch must not enter a replacement instance or another run.
            if _active(name) is not state or state["anchor"].get("run_id") != anchor.get("run_id"):
                return
            materials = []
            for mode, source in sources.items():
                item = {"mode": mode}
                if source.get("placeholder"):
                    item["available"] = True
                else:
                    item["text"] = truncate_to_tokens(str(source.get("formatted_content") or ""), 300)
                    item["links"] = [{k: link[k] for k in ("title", "url", "source") if k in link}
                                     for link in source.get("links", [])[:3]]
                if source.get("screenshot_b64"):
                    if state["anchor"] == anchor:
                        item["image"] = "data:image/jpeg;base64," + source["screenshot_b64"]
                if mode == "vision":
                    item["text"] = str(source.get("window_title") or "")[:200]
                materials.append(item)
            if materials:
                state["callbacks"].append({"kind": "proactive_material", "materials": materials,
                                           "timestamp": time.time()})
        except (TimeoutError, OSError, ValueError) as exc:
            log.warning("Companion proactive collection failed: %s", type(exc).__name__)

    anchor = dict(state["anchor"])
    state["proactive_task"] = asyncio.create_task(collect())
    return ProactiveChatResult({"action": "queued", "reason_code": "COMPANION_QUEUED",
                               "next_schedule_fixed_mode": True})
