"""Save-slot management — multiple named saves instead of one fixed file.

The legacy single file ``data/save.json`` is still readable and is surfaced as
a slot named ``legacy`` so existing games are not lost.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

SAVES_DIR = "data/saves"
LEGACY_SAVE = "data/save.json"
LEGACY_SLOT = "legacy"

_SLOT_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def _sanitize_slot(slot: str) -> str:
    """Validate a slot id so it can never escape the saves directory."""
    slot = (slot or "").strip()
    if not _SLOT_RE.match(slot):
        raise ValueError("存档槽 id 只能包含字母、数字、下划线和连字符（1-40 字符）")
    return slot


def slot_path(slot: str) -> str:
    """Resolve a slot id to its on-disk path."""
    if slot == LEGACY_SLOT:
        return LEGACY_SAVE
    return os.path.join(SAVES_DIR, f"{_sanitize_slot(slot)}.json")


def new_slot_id() -> str:
    """Generate a timestamp-based slot id."""
    from datetime import datetime

    return datetime.now().strftime("save-%Y%m%d-%H%M%S")


def _read_meta(path: str, slot: str) -> Dict[str, Any]:
    """Read just the descriptive fields of a save, tolerating old formats."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {
            "slot": slot,
            "label": "(无法读取)",
            "broken": True,
            "updated_at": "",
            "world": "",
            "player": "",
            "turn_count": 0,
            "scene_npcs": [],
        }

    card = data.get("player_card") or {}
    state = data.get("player_state") or {}
    world = data.get("world") or {}
    return {
        "slot": slot,
        "label": data.get("label") or world.get("name") or "冒险",
        "broken": False,
        "updated_at": data.get("updated_at", ""),
        "world": world.get("name", ""),
        "player": card.get("name", ""),
        "hp": state.get("hp"),
        "max_hp": state.get("max_hp"),
        "turn_count": data.get("turn_count", 0),
        "scene_npcs": data.get("scene_npcs") or [],
        "legacy": slot == LEGACY_SLOT,
    }


def list_saves() -> List[Dict[str, Any]]:
    """List every save slot, newest first."""
    entries: List[Dict[str, Any]] = []

    if os.path.exists(LEGACY_SAVE):
        entries.append(_read_meta(LEGACY_SAVE, LEGACY_SLOT))

    if os.path.isdir(SAVES_DIR):
        for fname in os.listdir(SAVES_DIR):
            if not fname.endswith(".json"):
                continue
            slot = fname[:-5]
            if not _SLOT_RE.match(slot):
                continue
            entries.append(_read_meta(os.path.join(SAVES_DIR, fname), slot))

    entries.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    return entries


def delete_save(slot: str) -> bool:
    """Delete one save slot.  Returns False when it did not exist."""
    path = slot_path(slot)
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


def latest_slot() -> str | None:
    """Return the most recently updated slot id, if any."""
    saves = list_saves()
    return saves[0]["slot"] if saves else None
