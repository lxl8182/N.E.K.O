import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState
from main_routers import sts2_companion_router as route


def test_text_voice_reload_and_stale_output_have_one_owner(monkeypatch):
    manager = SimpleNamespace(session=SimpleNamespace(_instructions="共享记忆", _conversation_history=[]),
        lanlan_prompt="宿主最终人格", takeover_owner=lambda: None,
        acquire_takeover=lambda *a, **kw: object(), release_takeover=lambda token: True,
        interrupt_ordinary_speech_for_takeover=AsyncMock(), mirror_user_input=AsyncMock(),
        mirror_assistant_speech=AsyncMock(return_value={"ok": True, "text_sent": True, "audio_completed": True}),
        websocket=SimpleNamespace(client_state=WebSocketState.CONNECTED), send_lanlan_response=AsyncMock(return_value=True))
    monkeypatch.setattr(route, "get_session_manager", lambda: {"小铃": manager})
    monkeypatch.setattr(route, "get_config_manager", lambda: SimpleNamespace(get_character_data=lambda: ("主人", "小铃")))
    monkeypatch.setattr(route, "is_route_slot_taken", lambda *a, **kw: False)
    monkeypatch.setattr(route, "_sessions", {})
    async def body(request):
        return request
    monkeypatch.setattr(route, "_body", body)
    async def run():
        payload = {"name": "小铃", "instance": "one", "claim": True, "anchor": {"run_id": "r"}}
        assert (await route.poll(payload))["persona"] == "宿主最终人格"
        assert await route._stream("小铃", {"input_type": "text", "data": "先集火", "request_id": "text"})
        assert await route._stream("小铃", {"input_type": "text", "data": "先集火", "request_id": "text"})
        assert await route._voice("小铃", "今天累了", request_id="voice")
        assert len((await route.poll(payload))["inputs"]) == 2
        new = {**payload, "instance": "two"}
        assert len((await route.poll(new))["inputs"]) == 2  # 重载仍保留未确认输入。
        with pytest.raises(route.HTTPException, match="expired or replaced"):
            await route.output({**payload, "line": "旧回复", "output_id": "old"})
        await route.output({**new, "line": "我在呢", "output_id": "reply", "wait_audio": True})
        await route.output({**new, "line": "我在呢", "output_id": "reply", "wait_audio": True})
        assert manager.mirror_assistant_speech.await_count == 1
        assert manager.mirror_assistant_speech.call_args.kwargs["metadata"]["mirror"]["kind"] == "sts2"
        assert manager.mirror_assistant_speech.call_args.kwargs["metadata"]["mirror"]["event"]["speech_kind"] == "chat"
        # 文本送达后TTS失败，普通回复仍只交付一次；战术重试只重做音频。
        manager.mirror_assistant_speech.return_value = {"ok": False, "text_sent": True, "audio_completed": False, "reason": "audio_completion_timeout"}
        failed = {**new, "line": "这次先稳住", "output_id": "audio-retry"}
        await asyncio.gather(route.output(failed), route.output(failed))
        assert manager.mirror_assistant_speech.await_count == 2
        assert (await route.output(failed))["text_sent"]
        manager.mirror_assistant_speech.return_value = {"ok": True, "text_sent": False, "audio_completed": True}
        assert (await route.output({**failed, "wait_audio": True}))["text_sent"]
        assert manager.mirror_assistant_speech.await_args.kwargs["mirror_text"] is False
        async def failed_after_text(*args, **kwargs):
            kwargs["on_text_delivery"]("sent-before-tts-failure", True)
            raise RuntimeError("tts worker lost")
        manager.mirror_assistant_speech.side_effect = failed_after_text
        one_reply = {**new, "line": "回应已经送到", "output_id": "text-before-tts"}
        assert (await route.output(one_reply))["text_sent"]
        count = manager.mirror_assistant_speech.await_count
        assert (await route.output(one_reply))["already_mirrored"]
        assert manager.mirror_assistant_speech.await_count == count
        manager.mirror_assistant_speech.side_effect = None
        notification = {**new, "line": "药水栏满了，已暂停。", "output_id": "failure", "system_notice": True}
        manager.websocket.client_state = WebSocketState.DISCONNECTED
        assert not (await route.output(notification))["ok"]
        manager.send_lanlan_response.assert_not_awaited()
        replacement = SimpleNamespace(**vars(manager))
        replacement.websocket = SimpleNamespace(client_state=WebSocketState.CONNECTED)
        monkeypatch.setattr(route, "get_session_manager", lambda: {"小铃": replacement})
        assert (await route.poll(new))["frontend_connected"]
        assert route._sessions["小铃"]["manager"] is replacement
        replacement.mirror_assistant_speech.return_value = {"ok": False, "reason": "tts_unavailable"}
        assert (await route.output(notification))["text_sent"]
        assert (await route.output(notification))["already_mirrored"]
        replacement.send_lanlan_response.assert_awaited_once()
        assert replacement.mirror_assistant_speech.await_args.kwargs["mirror_text"] is False
        assert (await route.poll({**new, "ack": ["text", "voice"]}))["inputs"] == []
        replacement.websocket.send_json = AsyncMock()
        replacement.mirror_assistant_speech.return_value = {"ok": True, "text_sent": True}
        music = {**new, "line": "我去找晴天。", "output_id": "music", "media_explicit": True,
                 "media_requests": [{"kind": "music", "query": "晴天", "artist": "周杰伦"}]}
        await route.output(music)
        await route.output(music)
        assert replacement.mirror_assistant_speech.call_args.kwargs["metadata"]["mirror"]["event"]["speech_kind"] == "chat"
        replacement.websocket.send_json.assert_awaited_once()
        assert replacement.websocket.send_json.await_args.args[0]["source"] == "request"
        completed = {**new, "output_id": "music", "result": {"kind": "music", "status": "dispatched", "name": "晴天"}}
        await route.media_result(completed)
        await route.media_result(completed)
        callbacks = (await route.poll(new))["callbacks"]
        assert len(callbacks) == 1 and callbacks[0]["result"]["status"] == "dispatched"
        from main_logic.music_playback import handle_music_playback_state
        replacement._takeover_active = True
        replacement._takeover_callback_sink = lambda callback: route._callback("小铃", callback)
        playing = {"state": "playing", "playback_id": "track-1", "playback_window_id": "window-1",
                   "playback_started_at": 1, "track": {"name": "晴天", "artist": "周杰伦"}}
        assert handle_music_playback_state(replacement, playing)
        assert not handle_music_playback_state(replacement, playing)
        callbacks = (await route.poll(new))["callbacks"]
        assert len(callbacks) == 1 and callbacks[0]["payload"]["metadata"]["state"] == "playing"
        assert await route._finalize("小铃") == 1
        assert not await route._voice("小铃", "回到普通聊天")
    asyncio.run(run())


def test_checkpoint_restores_only_same_run_and_main_menu_clears_seed(monkeypatch):
    manager = SimpleNamespace(session=SimpleNamespace(_instructions="长期背景", _conversation_history=[
        SimpleNamespace(type="human", content="旧局的对话")]), lanlan_prompt="主人格", takeover_owner=lambda: None,
        acquire_takeover=lambda *a, **kw: object(), release_takeover=lambda token: True,
        interrupt_ordinary_speech_for_takeover=AsyncMock(), mirror_user_input=AsyncMock(),
        websocket=SimpleNamespace(client_state=WebSocketState.CONNECTED))
    monkeypatch.setattr(route, "get_session_manager", lambda: {"小铃": manager})
    monkeypatch.setattr(route, "get_config_manager", lambda: SimpleNamespace(get_character_data=lambda: ("主人", "小铃")))
    monkeypatch.setattr(route, "is_route_slot_taken", lambda *a, **kw: False)
    monkeypatch.setattr(route, "_sessions", {})

    async def body(request):
        return request

    monkeypatch.setattr(route, "_body", body)

    async def run():
        old = {"name": "小铃", "instance": "one", "anchor": {"run_id": "old"}, "restore": True}
        checkpoint = {"session": {"current_run": {"run_id": "old"}}, "model_history": [{"role": "user", "content": "旧牌序"}]}
        await route.poll({**old, "checkpoint": checkpoint})
        reloaded = {**old, "instance": "two", "claim": True}
        assert (await route.poll(reloaded))["checkpoint"] == checkpoint
        fresh = {**reloaded, "anchor": {"run_id": "new"}}
        result = await route.poll({**fresh, "checkpoint": checkpoint})
        assert result["checkpoint"] == {} and result["dialogue_seed"] == []
        await route.poll({**fresh, "checkpoint": {"session": {"current_run": {"run_id": "new"}}, "model_history": ["new"]}})
        await route.release({**fresh, "reset_run": True})
        result = await route.poll({**fresh, "instance": "three", "anchor": {"run_id": None}})
        assert result["checkpoint"] == {} and result["dialogue_seed"] == []
        assert result["persona"] == "主人格" and result["background"] == "长期背景"

    asyncio.run(run())


def test_output_keeps_anchor_speech_kind_while_waiting_audio(monkeypatch):
    manager = SimpleNamespace(session=SimpleNamespace(_instructions="共享记忆", _conversation_history=[]),
        lanlan_prompt="宿主最终人格", takeover_owner=lambda: None,
        acquire_takeover=lambda *a, **kw: object(), release_takeover=lambda token: True,
        interrupt_ordinary_speech_for_takeover=AsyncMock(), mirror_user_input=AsyncMock(),
        mirror_assistant_speech=AsyncMock(return_value={"ok": True, "text_sent": True, "audio_completed": True}),
        websocket=SimpleNamespace(client_state=WebSocketState.CONNECTED), send_lanlan_response=AsyncMock(return_value=True))
    monkeypatch.setattr(route, "get_session_manager", lambda: {"小铃": manager})
    monkeypatch.setattr(route, "get_config_manager", lambda: SimpleNamespace(get_character_data=lambda: ("主人", "小铃")))
    monkeypatch.setattr(route, "is_route_slot_taken", lambda *a, **kw: False)
    monkeypatch.setattr(route, "_sessions", {})

    async def body(request):
        return request

    monkeypatch.setattr(route, "_body", body)

    async def run():
        payload = {"name": "小铃", "instance": "one", "claim": True, "anchor": {"run_id": "r"}}
        await route.poll(payload)
        await route.output({**payload, "line": "今天好累", "output_id": "chat-1", "wait_audio": True,
                            "anchor": {"run_id": "r", "speech_kind": "chat"}})
        mirror = manager.mirror_assistant_speech.await_args.kwargs
        assert mirror["metadata"]["mirror"]["event"]["speech_kind"] == "chat"
        assert mirror["wait_for_audio_completion"] is True
        await route.output({**payload, "line": "这回合先防住", "output_id": "tac-1", "wait_audio": True,
                            "anchor": {"run_id": "r", "speech_kind": "tactical"}})
        mirror = manager.mirror_assistant_speech.await_args.kwargs
        assert mirror["metadata"]["mirror"]["event"]["speech_kind"] == "tactical"
        assert mirror["wait_for_audio_completion"] is True

    asyncio.run(run())
