import io
from contextlib import nullcontext
from types import SimpleNamespace

from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient
from main_routers.system_router import emotion_bubbles as bubbles


def test_multiple_gifs_persist_per_character_and_removal_keeps_shared_assets(tmp_path, monkeypatch):
    def character_uid(record):
        return record["uid"]
    cfg = SimpleNamespace(app_docs_dir=tmp_path, get_character_data=lambda: ("master", "cat"),
        load_characters=lambda **kwargs: {"猫娘": {"cat": {"uid": "a" * 32}, "other": {"uid": "b" * 32}}})
    monkeypatch.setattr(bubbles, "get_config_manager", lambda: cfg)
    monkeypatch.setattr(bubbles, "get_character_uid", character_uid)
    monkeypatch.setattr(bubbles, "cloudsave_writable_transaction", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(bubbles, "_validate_local_mutation_request", lambda request: None)
    files = []
    for color in ("red", "blue"):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), color).save(buffer, format="GIF", save_all=True,
            append_images=[Image.new("RGB", (8, 8), "green")], duration=100, loop=0)
        payload = buffer.getvalue()
        files.append((bubbles._image_file(payload), payload))
    app = FastAPI()
    app.include_router(bubbles.router)
    client = TestClient(app)
    assert client.get('/api/emotion/bubbles/page').status_code == 200
    result = client.post('/api/emotion/bubbles/upload', data={'name': 'cat', 'emotion': 'happy'},
        files=[('images', (filename, payload, 'image/gif')) for filename, payload in files]).json()
    assert len(result["images"]["happy"]) == 2
    assert client.get(result['images']['happy'][0]).content == files[0][1]
    assert bubbles._record("other")["images"]["happy"] == []
    bubbles._mutate("cat", "surprised", files[:1])
    assert client.request('DELETE', '/api/emotion/bubbles', json={'name': 'cat', 'emotion': 'happy',
        'url': result['images']['happy'][0]}).status_code == 200
    path = tmp_path / "avatar_tools" / "emotion_bubbles" / ("a" * 32) / files[0][0]
    assert path.read_bytes() == files[0][1]
    with Image.open(path) as image:
        assert image.n_frames == 2
    bubbles._mutate("cat", "surprised", remove=files[0][0])
    assert not path.exists()


def test_detailed_bubble_emotion_keeps_avatar_emotion_and_rejects_unknown_label(monkeypatch):
    import asyncio
    import json
    from unittest.mock import AsyncMock, MagicMock
    from main_routers.system_router import emotion
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.ainvoke = AsyncMock(side_effect=[SimpleNamespace(content=json.dumps({
        'emotion': 'happy', 'confidence': .9, 'bubble_emotion': label})) for label in ('shy', 'unsupported')])
    cfg = SimpleNamespace(aget_model_api_config=AsyncMock(return_value={
        'api_key': 'test', 'model': 'test', 'base_url': 'http://localhost', 'provider_type': 'openai'}))
    monkeypatch.setattr(emotion, 'get_config_manager', lambda: cfg)
    monkeypatch.setattr(emotion, '_validate_local_mutation_request', lambda request: None)
    monkeypatch.setattr(emotion, '_resolve_emotion_prompt_language', lambda *args: 'zh')
    monkeypatch.setattr(emotion, '_push_emotion_update', MagicMock())
    monkeypatch.setattr(emotion, 'create_chat_llm_async', AsyncMock(return_value=client))
    request = SimpleNamespace(json=AsyncMock(return_value={'text': '好喜欢你呀', 'lanlan_name': 'cat'}))
    result = asyncio.run(emotion.emotion_analysis(request))
    assert result['emotion'] == 'happy' and result['bubble_emotion'] == 'shy'
    result = asyncio.run(emotion.emotion_analysis(request))
    assert result['emotion'] == 'happy' and 'bubble_emotion' not in result


def test_bubble_events_record_actual_display_and_probability_skip(monkeypatch):
    from unittest.mock import MagicMock
    logger = MagicMock()
    monkeypatch.setattr(bubbles, "logger", logger)
    monkeypatch.setattr(bubbles, "_validate_local_mutation_request", lambda request: None)
    app = FastAPI()
    app.include_router(bubbles.router)
    client = TestClient(app)
    event = {"name": "cat", "turn_id": "one", "kind": "chat", "status": "displayed",
             "probability": 1, "emotion": "happy", "bubble_emotion": "shy", "image": "test.gif"}
    assert client.post('/api/emotion/bubbles/event', json=event).json() == {"ok": True}
    assert logger.info.call_args.args[4] == "displayed"
    assert logger.info.call_args.args[-1] == "test.gif"
    event.update(kind="tactical", status="skipped", probability=.3, image="")
    assert client.post('/api/emotion/bubbles/event', json=event).status_code == 200
    assert logger.info.call_args.args[4] == "skipped"
    assert client.post('/api/emotion/bubbles/event', json={**event, "probability": 2}).status_code == 400


def test_normal_session_exposes_music_commands_with_and_without_agent(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from main_logic.core.notify import NotifyMixin
    import memory.user_directives as directives
    import memory.anti_repeat as repeat
    monkeypatch.setattr(directives, 'get_user_directives_manager', lambda: SimpleNamespace(
        render_prompt_block=lambda *args: ''))
    monkeypatch.setattr(repeat, 'get_anti_repeat_corpus', lambda: SimpleNamespace(
        apreload=AsyncMock(), top_recent_topics=lambda *args: []))
    for agent_enabled in (False, True):
        session = SimpleNamespace(user_language='zh', lanlan_name='cat', lanlan_prompt='custom persona',
            _is_agent_enabled=lambda: agent_enabled, _fetch_active_agent_tasks_prompt=AsyncMock(return_value=''))
        prompt = asyncio.run(NotifyMixin._build_initial_prompt(session))
        assert 'custom persona' in prompt
        assert '[play_music:' in prompt and '不依赖Agent插件' in prompt
        assert '不反复追问' in prompt and '播放器确认开始播放前' in prompt
