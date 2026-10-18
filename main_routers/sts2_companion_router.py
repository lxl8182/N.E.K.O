"""尖塔同伴会话：宿主拥有输入队列和渲染，插件拥有唯一对话/动作决策。"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from uuid import uuid4

from fastapi import APIRouter, Request, HTTPException

from main_routers.shared_state import get_session_manager, get_config_manager
from main_logic.mirror_meta import build_mirror_meta
from main_routers.system_router._shared import _validate_local_mutation_request
from utils.external_route_registry import ExternalRouteKind, register_external_route_kind, is_route_slot_taken
from utils.logger_config import get_module_logger

router = APIRouter(prefix="/api/sts2-companion", tags=["sts2-companion"])
logger = get_module_logger(__name__)
_sessions: dict[str, dict] = {}
_LEASE_SECONDS = 90


def _active(name: str) -> dict | None:
    state = _sessions.get(name)
    if state and state["expires"] > time.monotonic():
        return state
    if state:
        _release(name)
    return None


def _release(name: str) -> None:
    state = _sessions.get(name)
    if not state or state["expires"] == 0:
        return
    state["expires"] = 0
    state["manager"].release_takeover(state["token"])
    for key in ("speech_task", "proactive_task"):
        task = state.get(key)
        if task and not task.done():
            task.cancel()
    # 未确认输入留在宿主队列；下次同一角色接管时继续投递。


async def _watch_lease(name: str, instance: str) -> None:
    while (state := _sessions.get(name)) and state["instance"] == instance:
        await asyncio.sleep(2)
        if state["expires"] <= time.monotonic():
            _release(name)
            return


async def _enqueue(name: str, text: str, *, request_id: str = "", source: str = "text", **_: object) -> bool:
    state = _active(name)
    if state is None:
        return False
    text = str(text or "").strip()
    if not text:
        return True
    request_id = request_id or str(uuid4())
    if request_id in state["seen"]:
        return True
    item = {"request_id": request_id, "content": text[:6000], "timestamp": time.time(),
            "anchor": dict(state["anchor"]), "source": source}
    state["inbox"].append(item)
    state["seen"].append(request_id)
    await state["manager"].mirror_user_input(text, metadata=build_mirror_meta(
        source="sts2_companion", kind="sts2", session_id=state["instance"]),
                                              request_id=request_id, send_to_frontend=source == "voice")
    return True


async def _stream(name: str, message: dict) -> bool:
    if message.get("input_type") == "audio":
        return _active(name) is not None
    if message.get("input_type") != "text":
        return False
    return await _enqueue(name, message.get("data", ""), request_id=message.get("request_id", ""))


async def _voice(name: str, transcript: str, **kwargs: object) -> bool:
    return await _enqueue(name, transcript, source="voice", **kwargs)


async def _finalize(name: str) -> int:
    if _active(name) is None:
        return 0
    _release(name)
    return 1


def _callback(name: str, payload: dict) -> bool:
    state = _active(name)
    if state is None:
        return False
    # 已接管的同伴输出不能再变成普通聊天的一轮独立回答。
    state["callbacks"].append({"payload": payload, "timestamp": time.time()})
    return True


async def _body(request: Request) -> dict:
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected object")
    rejected = _validate_local_mutation_request(request, payload=data)
    if rejected is not None:
        raise HTTPException(403, "Local request requires CSRF and Origin")
    if not isinstance(data.get("instance"), str) or not data["instance"]:
        raise HTTPException(400, "Missing instance")
    if (not isinstance(data.get("name"), str)
            or not isinstance(data.get("anchor", {}), dict)
            or not isinstance(data.get("ack", []), list)
            or any(not isinstance(i, str) for i in data.get("ack", []))
            or not isinstance(data.get("checkpoint", {}), dict)
            or any(type(data[key]) is not bool for key in ("claim", "restore", "wait_audio", "reset_run", "system_notice", "media_explicit") if key in data)):
        raise HTTPException(400, "Invalid companion payload")
    return data


def _owned(data: dict) -> dict:
    state = _active(data.get("name", ""))
    if state is None or state["instance"] != data["instance"]:
        raise HTTPException(409, "Companion instance expired or replaced")
    return state


@router.post("/poll")
async def poll(request: Request):
    data = await _body(request)
    name, instance = data.get("name"), data["instance"]
    manager = get_session_manager().get(name)
    character = get_config_manager().get_character_data()
    if not manager or name != character[1]:
        raise HTTPException(409, "Character is not current")
    state = _active(name)
    if state is not None and state["manager"] is not manager:
        _release(name)
        state = None
    if state is not None and state["instance"] != instance and data.get("claim") is True:
        _release(name)
        state = None
    if state is None or state["instance"] != instance:
        if state is not None:
            raise HTTPException(409, "Another companion instance owns the session")
        if is_route_slot_taken(name, kind="sts2_companion", takeover_owner=manager.takeover_owner()):
            raise HTTPException(409, "Character already has an external controller")
        previous = _sessions.get(name) or {}
        state = {"instance": instance, "expires": time.monotonic() + _LEASE_SECONDS, "manager": manager,
                 "inbox": previous.get("inbox", []), "seen": previous.get("seen", deque(maxlen=512)),
                 "callbacks": [], "anchor": {}, "run_id": previous.get("run_id"), "outputs": previous.get("outputs", {}),
                 "checkpoint": previous.get("checkpoint", {}),
                 "background": previous.get("background", str(getattr(manager.session, "_instructions", "") or "")[:16000]),
                 "dialogue_seed": previous.get("dialogue_seed", [
                     {"role": "user" if m.type == "human" else "assistant", "content": m.content}
                     for m in getattr(manager.session, "_conversation_history", [])[-16:]
                     if getattr(m, "type", "") in {"human", "ai"} and isinstance(m.content, str) and m.content])}
        state["token"] = manager.acquire_takeover("sts2_companion", _voice,
                                                  callback_sink=lambda p: _callback(name, p))
        _sessions[name] = state
        await manager.interrupt_ordinary_speech_for_takeover()
        asyncio.create_task(_watch_lease(name, instance))
    state["expires"] = time.monotonic() + _LEASE_SECONDS
    state["anchor"] = data.get("anchor") or {}
    run_id = state["anchor"].get("run_id")
    if run_id and state["run_id"] and run_id != state["run_id"]:
        state["checkpoint"].clear()
        state["dialogue_seed"] = []
        state["callbacks"].clear()
    if run_id:
        state["run_id"] = run_id
    acknowledged = set(data.get("ack") or [])
    state["inbox"] = [i for i in state["inbox"] if i["request_id"] not in acknowledged]
    # 使用普通会话已经渲染好的主人格，避免插件重新拼出另一份人格。
    persona = manager.lanlan_prompt
    if isinstance(data.get("checkpoint"), dict):
        checkpoint = data["checkpoint"]
        checkpoint_run = ((checkpoint.get("session") or {}).get("current_run") or {}).get("run_id")
        if checkpoint_run == run_id:
            state["checkpoint"].update(checkpoint)
    callbacks, state["callbacks"] = state["callbacks"], []
    ws = manager.websocket
    return {"ok": True, "instance": instance, "persona": persona, "inputs": state["inbox"], "callbacks": callbacks,
            "frontend_connected": bool(ws and ws.client_state == ws.client_state.CONNECTED),
            "frontend_connection": str(id(ws)),
            "background": state["background"], "dialogue_seed": state["dialogue_seed"] if data.get("restore") else [],
            "checkpoint": state["checkpoint"] if data.get("restore") else {}}


@router.post("/release")
async def release(request: Request):
    data = await _body(request)
    state = _sessions.get(data.get("name"))
    if state and state["instance"] == data["instance"]:
        _release(data["name"])
        if data.get("reset_run"):
            state["checkpoint"].clear()
            state["dialogue_seed"] = []
            state["callbacks"].clear()
            state["run_id"] = None
    return {"ok": True}


@router.post("/output")
async def output(request: Request):
    data = await _body(request)
    state = _owned(data)
    line = data.get("line")
    output_id = data.get("output_id")
    if not isinstance(line, str) or not line.strip() or len(line) > 2000 or not isinstance(output_id, str):
        raise HTTPException(400, "Invalid output")
    media = data.get("media_requests") or []
    if (not isinstance(media, list) or len(media) > 2 or any(
            not isinstance(item, dict) or item.get("kind") not in {"music", "meme"}
            or not isinstance(item.get("query"), str) or not 1 <= len(item["query"].strip()) <= 200
            or not isinstance(item.get("artist", ""), str) or len(item.get("artist", "")) > 100
            for item in media)):
        raise HTTPException(400, "Invalid media request")
    previous = state["outputs"].get(output_id)
    if previous and previous.get("text_sent") and (not data.get("wait_audio") or previous.get("audio_completed") is True):
        return {**previous, "already_mirrored": True}
    inflight = state.setdefault("output_tasks", {})
    if output_id in inflight:
        result = await asyncio.shield(inflight[output_id])
        _owned(data)
        return {**result, "already_mirrored": True}
    if data.get("system_notice"):
        manager = state["manager"]
        ws = manager.websocket
        if ws is None or ws.client_state != ws.client_state.CONNECTED:
            return {"ok": False, "reason": "frontend_disconnected", "text_sent": False}
        sent = await manager.send_lanlan_response(line, is_first_chunk=True, turn_id=output_id,
            request_id=output_id, track_ai_turn=False, cache_for_new_session=False,
            metadata=build_mirror_meta(source="sts2_companion", kind="sts2", session_id=state["instance"],
                                      event=data.get("anchor") or {}))
        result = {"ok": bool(sent), "text_sent": bool(sent)}
        if sent:
            state["outputs"][output_id] = result
            await manager.mirror_assistant_speech(line, metadata=build_mirror_meta(
                source="sts2_companion", kind="sts2", session_id=state["instance"], event=data.get("anchor") or {}),
                request_id=output_id, mirror_text=False, interrupt_audio=False)
        return result
    # 先登记，避免客户端重试把已发布文本重复播出。
    state["outputs"][output_id] = {"ok": False, "audio_completed": False, "reason": "output_pending",
                                   "text_sent": bool(previous and previous.get("text_sent"))}
    if media and previous is None:
        ws = state["manager"].websocket
        if ws is None or ws.client_state != ws.client_state.CONNECTED:
            state["outputs"].pop(output_id, None)
            return {"ok": False, "reason": "frontend_disconnected"}
        await ws.send_json({"type": "companion_media", "name": data["name"], "instance": state["instance"],
                            "output_id": output_id, "requests": media,
                            "source": "request" if data.get("media_explicit") else "proactive"})
    if len(state["outputs"]) > 128:
        state["outputs"].pop(next(iter(state["outputs"])))
    task = asyncio.create_task(state["manager"].mirror_assistant_speech(
        line, metadata=build_mirror_meta(source="sts2_companion", kind="sts2", session_id=state["instance"],
                                      event={**(data.get("anchor") or {}), "speech_kind": "tactical" if data.get("wait_audio")
                                             else (data.get("anchor") or {}).get("speech_kind", "chat")}), request_id=output_id,
        wait_for_audio_completion=bool(data.get("wait_audio")), interrupt_audio=False,
        mirror_text=not bool(previous and previous.get("text_sent")),
        on_text_delivery=lambda speech_id, sent: state["outputs"][output_id].update(text_sent=sent, speech_id=speech_id)))
    state["speech_task"] = task
    inflight[output_id] = task
    try:
        result = await task
    except Exception as exc:
        logger.exception("[sts2_output_failed] output_id=%s", output_id)
        result = {**state["outputs"][output_id], "ok": False, "reason": "speech_delivery_failed", "error": str(exc)}
    finally:
        inflight.pop(output_id, None)
        if state.get("speech_task") is task:
            state.pop("speech_task", None)
    _owned(data)
    if state["outputs"][output_id].get("text_sent"):
        result["text_sent"] = True
    state["outputs"][output_id] = result
    return result


@router.post("/media-result")
async def media_result(request: Request):
    data = await _body(request)
    state = _owned(data)
    result = data.get("result")
    if not isinstance(result, dict) or result.get("kind") not in {"music", "meme"}:
        raise HTTPException(400, "Invalid media result")
    output_id = data.get("output_id")
    if not isinstance(output_id, str) or output_id not in state["outputs"]:
        raise HTTPException(409, "Unknown media output")
    key = "media:" + output_id + ":" + result["kind"] + ":" + str(result.get("status", ""))[:200] + ":" + str(result.get("name", ""))[:200]
    if key not in state["seen"]:
        state["seen"].append(key)
        state["callbacks"].append({"kind": "media_result", "result": {
            k: str(result[k])[:200] for k in ("kind", "status", "name", "artist") if k in result},
            "timestamp": time.time()})
    return {"ok": True}


@router.post("/meme-search")
async def meme_search(request: Request):
    data = await _body(request)
    _owned(data)
    query = data.get("query")
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
        raise HTTPException(400, "Invalid search query")
    from utils.meme_fetcher import fetch_meme_content
    async with asyncio.timeout(40):
        result = await fetch_meme_content(keyword=query, limit=3)
    _owned(data)
    return {"success": result.get("success", False), "data": [
        {key: item[key] for key in ("title", "url", "type") if key in item}
        for item in result.get("data", [])[:3]]}


register_external_route_kind(ExternalRouteKind(
    kind="sts2_companion", is_active=lambda name: _active(name) is not None,
    route_stream_message=_stream, route_voice_transcript=_voice, on_start_session=None,
    finalize_for_character=_finalize, current_instance=lambda name: (_active(name) or {}).get("instance"),
    audio_passthrough=True))
