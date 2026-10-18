# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cross-process fact-extraction gate ("事实闸门").

A running game/companion plugin (currently ``sts2_autoplay``) drives long
stretches of game-flavoured dialogue through the host. Those turns are wanted
in ``recent.json`` (so the character can talk about the current run) but must
**not** settle into long-term ``facts.json``: the extracted rows are pure
game state — character names, card picks, HP numbers — that have nothing to do
with the persona and only pollute later recall.

The host cannot tell game turns apart from a plugin-side flag by inspecting
persisted messages alone (the plugin's push_message turns land as ordinary
``ai`` rows with no source metadata), and the plugin has no SDK surface to
disable memory. So the two sides agree on one file under the shared data root:
while the plugin is active it keeps refreshing

    <memory_dir>/.fact_suppression.json

and the fact-extraction entries read it before calling the extraction LLM. A
TTL makes the gate self-healing: a plugin crash (or an uninstalled plugin)
stops refreshing the file, the TTL lapses, and fact extraction resumes on its
own — no cleanup step required, and the flag can never stick.

The gate only suppresses **fact extraction**. Recent history, time-indexed
storage, reflections and the persona card are untouched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

GATE_FILENAME = ".fact_suppression.json"

# The plugin refreshes every ``DEFAULT_REFRESH_SECONDS``; a TTL of several
# refresh windows means a couple of missed beats (GC pause, slow tick) do not
# flap the gate back on mid-session, while a dead plugin unblocks extraction
# within ~1.5 minutes.
DEFAULT_REFRESH_SECONDS = 20.0
DEFAULT_TTL_SECONDS = 90.0

_cached_path: Path | None = None
_cached_signature: tuple[int, int] | None = None
_cached_result: bool = False


def gate_path(memory_dir: Any) -> Path:
    """Return the gate file path for a memory directory."""
    return Path(memory_dir) / GATE_FILENAME


def _coerce_epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def read_gate(memory_dir: Any) -> dict[str, Any] | None:
    """Return the parsed gate payload, or None when absent/unreadable.

    Malformed or missing files are treated as "no gate" — the safe default is
    to keep extracting facts.
    """
    try:
        payload = json.loads(gate_path(memory_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def is_suppressed(memory_dir: Any, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> bool:
    """Whether fact extraction should be skipped right now.

    Only an explicit ``suppress: true`` within the TTL window suppresses. Every
    other outcome (missing file, malformed JSON, stale heartbeat, missing
    timestamp) returns False so the default behaviour is unchanged.

    A tiny mtime+size cache keeps the per-turn call site from re-parsing an
    unchanged small file on every extraction.
    """
    global _cached_path, _cached_signature, _cached_result

    path = gate_path(memory_dir)
    try:
        stat = path.stat()
    except OSError:
        _cached_path, _cached_signature, _cached_result = path, None, False
        return False

    signature = (stat.st_mtime_ns, stat.st_size)
    if path == _cached_path and signature == _cached_signature:
        return _cached_result

    payload = read_gate(memory_dir)
    if payload is None or payload.get("suppress") is not True:
        result = False
    else:
        stamp = _coerce_epoch(payload.get("updated_at"))
        if stamp is None:
            stamp = stat.st_mtime
        result = (time.time() - stamp) <= ttl_seconds

    _cached_path, _cached_signature, _cached_result = path, signature, result
    return result


def write_gate(
    memory_dir: Any,
    *,
    suppress: bool,
    source: str = "",
    reason: str = "",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write the gate heartbeat. Used by tests and by any in-process producer.

    The on-disk plugin writes its own copy directly (it runs in a separate
    process); this helper keeps the wire format in one place.
    """
    import os

    path = gate_path(memory_dir)
    payload: dict[str, Any] = {
        "suppress": bool(suppress),
        "source": str(source or ""),
        "reason": str(reason or ""),
        "pid": os.getpid(),
        "updated_at": time.time(),
    }
    if extra:
        payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


__all__ = [
    "GATE_FILENAME",
    "DEFAULT_REFRESH_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "gate_path",
    "read_gate",
    "is_suppressed",
    "write_gate",
]
