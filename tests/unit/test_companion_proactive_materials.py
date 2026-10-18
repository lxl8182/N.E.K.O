import asyncio
from unittest.mock import AsyncMock, Mock

from main_logic.proactive_chat import companion
from main_logic.proactive_chat.contracts import ProactiveChatCommand
from main_routers import sts2_companion_router as route


def test_enabled_materials_are_queued_once_and_stale_owner_is_rejected(monkeypatch):
    state = {"callbacks": [], "anchor": {"run_id": "run", "turn": 1}}
    current = [state]
    monkeypatch.setattr(route, "_active", lambda name: current[0])
    fetch = AsyncMock(return_value={"news": {"formatted_content": "topic", "links": []},
                                   "music": {"placeholder": True}})
    monkeypatch.setattr(companion, "collect_proactive_sources", fetch)
    command = ProactiveChatCommand(enabled_modes=["news", "music"], enabled_modes_provided=True)

    async def run():
        result = await companion.offer_companion_proactive(command, "cat", Mock())
        assert result.body["action"] == "queued"
        state["anchor"]["turn"] = 2  # Fetching news must not wait for the game to stop.
        await state["proactive_task"]
        assert [m["mode"] for m in state["callbacks"][0]["materials"]] == ["news", "music"]
        assert (await companion.offer_companion_proactive(command, "cat", Mock())).body["action"] == "pass"
        assert fetch.await_count == 1
        state["callbacks"].clear()
        await companion.offer_companion_proactive(command, "cat", Mock())
        current[0] = {"callbacks": [], "anchor": {"run_id": "new"}}
        await state["proactive_task"]
        assert state["callbacks"] == [] and current[0]["callbacks"] == []
        current[0] = None
        assert await companion.offer_companion_proactive(command, "cat", Mock()) is None
    asyncio.run(run())
