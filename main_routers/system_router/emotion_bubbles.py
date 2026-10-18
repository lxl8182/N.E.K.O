"""Character-scoped image sets for the avatar's emotion bubble."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from pathlib import Path

from fastapi import Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError

from ._shared import router, logger, _validate_local_mutation_request
from ..shared_state import get_config_manager
from utils.config_manager.reserved_schema import get_character_uid
from utils.cloudsave_runtime import cloudsave_writable_transaction
from utils.file_utils import atomic_write_json
from config.prompts.prompts_emotion import BUBBLE_EMOTIONS

EMOTIONS = BUBBLE_EMOTIONS
_lock = asyncio.Lock()
_templates = Jinja2Templates(directory="templates")


def _context(name):
    cfg = get_config_manager()
    name = name or cfg.get_character_data()[1]
    characters = cfg.load_characters(require_authoritative=True).get("猫娘", {})
    if name not in characters:
        raise HTTPException(404, "Character not found")
    uid = get_character_uid(characters[name])
    if not uid:
        raise HTTPException(409, "Character has no UID")
    directory = Path(cfg.app_docs_dir) / "avatar_tools" / "emotion_bubbles"
    path = directory / "config.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return cfg, name, uid, directory, data


def _record(name):
    _, name, uid, _, data = _context(name)
    mapping = data.get(uid, {})
    return {"name": name, "uid": uid, "images": {
        emotion: [f"/api/emotion/bubbles/image/{uid}/{file}" for file in mapping.get(emotion, [])]
        for emotion in EMOTIONS}, "image_names": {
            f"/api/emotion/bubbles/image/{uid}/{file}": data.get("_names", {}).get(file, "")
            for emotion in EMOTIONS for file in mapping.get(emotion, [])}}


def _image_file(payload):
    if not payload or len(payload) > 8 * 1024 * 1024:
        raise HTTPException(413, "Image must be at most 8 MB")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            extension = {"PNG": "png", "GIF": "gif", "JPEG": "jpg", "WEBP": "webp"}.get(image.format)
            if extension is None:
                raise HTTPException(400, "Unsupported image format")
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, "Invalid image") from exc
    return hashlib.sha256(payload).hexdigest() + "." + extension


def _mutate(name, emotion, files=(), remove=None):
    cfg, name, uid, directory, data = _context(name)
    with cloudsave_writable_transaction(cfg, operation="emotion_bubbles", target=uid):
        mapping = data.setdefault(uid, {})
        selected = mapping.setdefault(emotion, [])
        asset_dir = directory / uid
        asset_dir.mkdir(parents=True, exist_ok=True)
        for filename, payload in files:
            if filename not in selected:
                (asset_dir / filename).write_bytes(payload)
                selected.append(filename)
        if remove in selected:
            selected.remove(remove)
        atomic_write_json(directory / "config.json", data, ensure_ascii=False, indent=2)
        if remove and not any(remove in names for names in mapping.values()):
            (asset_dir / remove).unlink(missing_ok=True)
    return _record(name)


@router.get("/emotion/bubbles/page")
async def emotion_bubbles_page(request: Request):
    return _templates.TemplateResponse(request, "emotion_bubbles.html", {})


@router.get("/emotion/bubbles")
async def get_emotion_bubbles(name: str = ""):
    return await asyncio.to_thread(_record, name)


@router.post("/emotion/bubbles/upload")
async def upload_emotion_bubbles(request: Request):
    rejected = _validate_local_mutation_request(request)
    if rejected is not None:
        return rejected
    async with request.form(max_files=20, max_fields=2) as form:
        emotion = form.get("emotion")
        name = form.get("name", "")
        if emotion not in EMOTIONS or not isinstance(name, str):
            raise HTTPException(400, "Invalid emotion or character")
        files = []
        for file in form.getlist("images"):
            if not hasattr(file, "read"):
                raise HTTPException(400, "Expected image file")
            payload = await file.read(8 * 1024 * 1024 + 1)
            filename = await asyncio.to_thread(_image_file, payload)
            files.append((filename, payload))
        if not files:
            raise HTTPException(400, "No images uploaded")
        async with _lock:
            return await asyncio.to_thread(_mutate, name, emotion, files)


@router.post("/emotion/bubbles/event")
async def emotion_bubble_event(request: Request):
    rejected = _validate_local_mutation_request(request)
    if rejected is not None:
        return rejected
    data = await request.json()
    if (not isinstance(data, dict) or data.get("status") not in ("displayed", "default", "skipped", "load_failed", "config_failed", "hidden")
            or data.get("kind") not in ("chat", "tactical")
            or type(data.get("probability")) not in (int, float)
            or data["probability"] not in (0.15, 0.30, 1)
            or any(not isinstance(data.get(key, ""), str) or len(data.get(key, "")) > 128
                   for key in ("name", "turn_id", "emotion", "bubble_emotion", "image"))):
        raise HTTPException(400, "Invalid bubble event")
    logger.info("[EmotionBubble] name=%s turn=%s kind=%s status=%s probability=%s emotion=%s bubble=%s image=%s",
                data.get("name", ""), data.get("turn_id", ""), data["kind"], data["status"],
                data["probability"], data.get("emotion", ""), data.get("bubble_emotion", ""), data.get("image", ""))
    return {"ok": True}


@router.delete("/emotion/bubbles")
async def delete_emotion_bubble(request: Request):
    rejected = _validate_local_mutation_request(request)
    if rejected is not None:
        return rejected
    data = await request.json()
    if (not isinstance(data, dict) or data.get("emotion") not in EMOTIONS
            or not isinstance(data.get("name", ""), str)):
        raise HTTPException(400, "Invalid emotion")
    filename = str(data.get("url", "")).rsplit("/", 1)[-1]
    if not re.fullmatch(r"[a-f0-9]{64}\.(png|gif|jpg|webp)", filename):
        raise HTTPException(400, "Invalid image reference")
    async with _lock:
        return await asyncio.to_thread(_mutate, data.get("name", ""), data["emotion"], (), filename)


@router.get("/emotion/bubbles/image/{uid}/{filename}")
async def emotion_bubble_image(uid: str, filename: str):
    if not re.fullmatch(r"[a-f0-9]{32}", uid) or not re.fullmatch(r"[a-f0-9]{64}\.(png|gif|jpg|webp)", filename):
        raise HTTPException(404, "Image not found")
    path = Path(get_config_manager().app_docs_dir) / "avatar_tools" / "emotion_bubbles" / uid / filename
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, "Image not found")
    return FileResponse(path)
