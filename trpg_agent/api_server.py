"""FastAPI 后端 — 为 TRPG Web 前端提供 API + SSE 流式输出。

启动方式:
    python -m trpg_agent.api_server

或:
    uvicorn trpg_agent.api_server:app --reload --port 8000
"""

import asyncio
import functools
import json
import logging
import os
from pathlib import Path
from typing import AsyncGenerator

from trpg_agent.config import (
    load_credentials,
    describe as describe_credentials,
    status as config_status,
)

# Env vars win over .env; see trpg_agent/config.py for the precedence rules.
load_credentials()
# print, not logging: uvicorn's default config filters this module's INFO logs,
# and the key source is something the operator must see at start-up.
print(f"[config] {describe_credentials()}", flush=True)

# 显式禁用 ChromaDB 遥测（必须在 import chromadb 之前设置）
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from trpg_agent.game_master import GameMaster
from trpg_agent.event_stream import StatusEvent
from trpg_agent import saves

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
#  Error handling
# ------------------------------------------------------------------

def api_errors(fn):
    """Return real HTTP 500s instead of a 200 body the frontend never checks.

    Endpoints used to swallow every exception into ``{"error": ...}`` with a
    200 status, so ``res.ok`` stayed true and most callers silently rendered
    nothing.  HTTPException is re-raised untouched so auth still returns 401.
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("API error in %s", fn.__name__)
            raise HTTPException(status_code=500, detail=str(e)) from e

    return wrapper


# ------------------------------------------------------------------
#  Auth
# ------------------------------------------------------------------

_API_TOKEN = os.environ.get("TRPG_API_TOKEN", "")
_auth_scheme = HTTPBearer(auto_error=False)


def _require_auth(credentials: HTTPAuthorizationCredentials | None = Depends(_auth_scheme)) -> None:
    """If TRPG_API_TOKEN is set, require Bearer token match on all endpoints."""
    if not _API_TOKEN:
        return  # auth disabled — open access for local dev
    if credentials is None or credentials.credentials != _API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing API token")


# ------------------------------------------------------------------
#  App
# ------------------------------------------------------------------

app = FastAPI(title="TRPG Agent API")

# "*" + allow_credentials=True is rejected by browsers, so credentials are only
# enabled when explicit origins are configured.
_cors_origins = [o.strip() for o in os.environ.get("TRPG_CORS_ORIGINS", "*").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
#  Global GameMaster instance (lazy init)
# ------------------------------------------------------------------

_gm: GameMaster | None = None
_config_path = os.environ.get("TRPG_CONFIG", "config.yaml")

# The GameMaster is a single global instance holding mutable game state, so all
# state-touching endpoints are serialised.  This is a single-player server.
_turn_lock = asyncio.Lock()


def _get_gm() -> GameMaster:
    """Get or create the global GameMaster instance."""
    global _gm
    if _gm is not None:
        return _gm

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    debug = os.environ.get("TRPG_DEBUG", "").lower() in ("true", "1", "yes")
    _gm = GameMaster(
        config_path=_config_path,
        llm_api_key=api_key,
        debug=debug,
    )
    return _gm


# ------------------------------------------------------------------
#  Health check
# ------------------------------------------------------------------


@app.get("/health")
async def health():
    """Health check — no auth required.

    Also reports whether an API key is configured, so the UI can warn instead
    of silently serving fallback narration.  The key itself is never returned,
    only a masked preview.
    """
    cfg = config_status()
    return {
        "status": "ok",
        "auth_enabled": bool(_API_TOKEN),
        "llm_available": cfg["has_api_key"],
        "key_source": cfg["key_source"],
        "key_preview": cfg["key_preview"],
        "model": cfg["model"],
    }


# ------------------------------------------------------------------
#  Startup
# ------------------------------------------------------------------

@app.post("/api/start")
@api_errors
async def api_start(body: dict = {}, auth=Depends(_require_auth)):
    """开始新游戏。支持自定义世界观、结构化 NPC 和玩家角色卡。"""
    # new_game_reset() already wipes memories, NPCs, scene and save file.
    # Dropping the instance as well forced a full rebuild (three ChromaDB
    # PersistentClients + embedding model + knowledge reindex + an LLM call),
    # which is slow and can collide on the same on-disk Chroma directory.
    async with _turn_lock:
        gm = _get_gm()

        custom_worldview = body.get("worldview", "") or ""
        custom_npc_setup = body.get("npc_setup", "") or ""
        world_options = body.get("world_options") or {}
        npc_specs = body.get("npcs") or []
        player_card = body.get("player") or None

        if not isinstance(world_options, dict):
            raise HTTPException(status_code=400, detail="world_options 必须是对象")
        if not isinstance(npc_specs, list):
            raise HTTPException(status_code=400, detail="npcs 必须是数组")
        if len(npc_specs) > 8:
            raise HTTPException(status_code=400, detail="初始 NPC 最多 8 个")

        # Player card must be applied before the reset so max HP is derived
        # from the new 体质 instead of the old config.yaml value.
        if player_card:
            if not isinstance(player_card, dict):
                raise HTTPException(status_code=400, detail="player 必须是对象")
            try:
                gm.apply_player_card(player_card)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e

        # A custom worldview replaces the packaged setting, so the bundled
        # scenario must not re-seed its cast during the reset.
        gm.new_game_reset(use_scenario=not custom_worldview.strip())

        # generate_opening() owns the world description/name derivation so the
        # CLI and the web start screen stay consistent.
        opening = gm.generate_opening(
            custom_worldview=custom_worldview,
            custom_npc_setup=custom_npc_setup,
            world_options=world_options,
            npc_specs=npc_specs,
        )
        return {
            "opening": opening,
            "suggestions": gm._last_suggestions or _extract_suggestions(opening),
            "state": gm.player_state.to_dict(),
            "npcs": _get_scene_npcs(gm),
            "player": {
                "name": gm.player.name,
                "attributes": dict(gm.player.attributes),
                "skills": list(gm.player.skills),
            },
            "world": {
                "name": gm.world.get("name", ""),
                "time_of_day": gm._time_of_day,
                "weather": gm._weather,
            },
        }


@app.post("/api/reroll_opening")
@api_errors
async def api_reroll_opening(auth=Depends(_require_auth)):
    """Regenerate the opening, keeping the world, player card and cast."""
    async with _turn_lock:
        gm = _get_gm()
        opening = gm.reroll_opening()
        return {
            "opening": opening,
            "suggestions": gm._last_suggestions or _extract_suggestions(opening),
            "state": gm.player_state.to_dict(),
            "npcs": _get_scene_npcs(gm),
        }


@app.get("/api/saves")
@api_errors
async def api_saves(auth=Depends(_require_auth)):
    """List all save slots, newest first."""
    return {"saves": saves.list_saves()}


@app.post("/api/load")
@api_errors
async def api_load(body: dict = {}, auth=Depends(_require_auth)):
    """加载存档。未指定 slot 时读取最近的一个。"""
    gm = _get_gm()
    slot = (body or {}).get("slot") or saves.latest_slot()
    if not slot:
        raise HTTPException(status_code=404, detail="没有找到存档")
    try:
        path = saves.slot_path(slot)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    async with _turn_lock:
        loaded = gm.load_save(path)
    if not loaded:
        raise HTTPException(status_code=404, detail=f"存档槽「{slot}」不存在")
    return {
        "loaded": True,
        "slot": slot,
        "state": gm.player_state.to_dict(),
        "npcs": _get_scene_npcs(gm),
        "lastResponse": gm._last_gm_response,
        "suggestions": list(gm._last_suggestions),
    }


@app.post("/api/save")
@api_errors
async def api_save(body: dict = {}, auth=Depends(_require_auth)):
    """保存游戏。未指定 slot 时新建一个带时间戳的槽位。"""
    gm = _get_gm()
    body = body or {}
    slot = body.get("slot") or saves.new_slot_id()
    label = (body.get("label") or "").strip()
    try:
        path = saves.slot_path(slot)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    async with _turn_lock:
        gm.save(path, label=label)
    return {"saved": True, "slot": slot, "label": label or gm.world.get("name", "冒险")}


@app.post("/api/save/delete")
@api_errors
async def api_save_delete(body: dict, auth=Depends(_require_auth)):
    """Delete one save slot."""
    slot = (body or {}).get("slot") or ""
    try:
        removed = saves.delete_save(slot)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not removed:
        raise HTTPException(status_code=404, detail=f"存档槽「{slot}」不存在")
    return {"deleted": True, "slot": slot}


@app.get("/api/status")
@api_errors
async def api_status(auth=Depends(_require_auth)):
    """获取玩家状态。"""
    gm = _get_gm()
    return {
        "state": gm.player_state.to_dict(),
        "npcs": _get_scene_npcs(gm),
    }


@app.get("/api/npcs")
@api_errors
async def api_npcs(auth=Depends(_require_auth)):
    """获取场景 NPC 列表。"""
    return {"npcs": _get_scene_npcs(_get_gm())}


@app.get("/api/npc/{name}")
@api_errors
async def api_npc_detail(name: str, auth=Depends(_require_auth)):
    """Full NPC inspection: card, personality, state, history, memories."""
    detail = _get_gm().get_npc_detail(name)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"找不到 NPC「{name}」")
    return detail


@app.get("/api/player")
@api_errors
async def api_player(auth=Depends(_require_auth)):
    """Current player character card."""
    gm = _get_gm()
    return {
        "name": gm.player.name,
        "core": list(gm.player.core),
        "attributes": dict(gm.player.attributes),
        "skills": list(gm.player.skills),
        "anchor": getattr(gm.player, "anchor", ""),
        "pathway": getattr(gm.player, "pathway", ""),
        "relations": dict(gm.player.relations or {}),
        "state": gm.player_state.to_dict(),
    }


@app.get("/api/knowledge")
@api_errors
async def api_knowledge(auth=Depends(_require_auth)):
    """获取知识库条目（简化版，返回最近添加的条目）。"""
    return {"knowledge": _get_gm().knowledge.list_all(limit=200)}


@app.post("/api/knowledge/add")
@api_errors
async def api_knowledge_add(body: dict, auth=Depends(_require_auth)):
    """Index a pasted block of setting text, chunked like a .md file."""
    text = (body or {}).get("text", "") or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    known_by = (body or {}).get("known_by") or "所有人"
    category = (body or {}).get("category") or ""

    gm = _get_gm()
    async with _turn_lock:
        added = gm.knowledge.add_text(
            text=text, known_by=known_by, category=category
        )
    # Return a listing that actually contains the new chunks: list_all() is
    # capped, so a freshly added entry could otherwise be invisible in the UI.
    return {"added": added, "knowledge": _recent_knowledge(gm, text)}


@app.post("/api/knowledge/delete")
@api_errors
async def api_knowledge_delete(body: dict, auth=Depends(_require_auth)):
    """Delete one knowledge entry by id."""
    entry_id = (body or {}).get("id", "") or ""
    if not entry_id:
        raise HTTPException(status_code=400, detail="id 不能为空")
    gm = _get_gm()
    async with _turn_lock:
        removed = gm.knowledge.delete(entry_id)
    if not removed:
        raise HTTPException(status_code=404, detail="条目不存在")
    return {"deleted": True, "knowledge": gm.knowledge.list_all(limit=200)}

@app.post("/api/command")
@api_errors
async def api_command(body: dict, auth=Depends(_require_auth)):
    """执行 ! 命令（如 !npc, !world）。"""
    command = body.get("command", "")
    if not command.strip():
        raise HTTPException(status_code=400, detail="command 不能为空")
    gm = _get_gm()
    async with _turn_lock:
        result = gm._handle_world_builder_command(command)
    return {"result": result}


# ------------------------------------------------------------------
#  Debug endpoint
# ------------------------------------------------------------------

@app.get("/api/debug")
@api_errors
async def api_debug(auth=Depends(_require_auth)):
    """获取 GM 调试日志并清空缓冲区。"""
    gm = _get_gm()
    logs = list(gm._debug_log)
    gm._debug_log.clear()
    return {"debug": gm.debug, "logs": logs}


@app.post("/api/debug/toggle")
@api_errors
async def api_debug_toggle(auth=Depends(_require_auth)):
    """切换调试模式。"""
    gm = _get_gm()
    gm.debug = not gm.debug
    return {"debug": gm.debug}


# ------------------------------------------------------------------
#  SSE Action endpoint
# ------------------------------------------------------------------

@app.post("/api/action")
async def api_action(body: dict, auth=Depends(_require_auth)):
    """发送玩家行动，SSE 真流式返回 GM 响应。"""
    action = body.get("action", "")
    if not action.strip():
        raise HTTPException(status_code=400, detail="action 不能为空")

    async def event_stream() -> AsyncGenerator[str, None]:
        def _sse(payload: dict) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        gm = _get_gm()
        try:
            # Serialise turns: GameMaster state is not thread/task safe.
            async with _turn_lock:
                async for chunk in gm.process_streaming(action):
                    if isinstance(chunk, StatusEvent):
                        yield _sse({
                            'type': chunk.type,
                            'tool': chunk.tool,
                            'display': chunk.display,
                            'result': chunk.result,
                            'npc_name': chunk.npc_name,
                            'npc_text': chunk.npc_text,
                            'roll_kind': chunk.roll_kind,
                            'roll_label': chunk.roll_label,
                            'roll_detail': chunk.roll_detail,
                            'roll_success': chunk.roll_success,
                        })
                    elif chunk:
                        yield _sse({'text': chunk})

                # After streaming: send final state from GameMaster
                response = gm._last_gm_response or ""
                yield _sse({
                    "done": True,
                    "suggestions": gm._last_suggestions or _extract_suggestions(response),
                    "state": gm.player_state.to_dict(),
                    "npcs": _get_scene_npcs(gm),
                    "gameOverPending": gm._game_over_pending,
                    "gameOverCause": gm._game_over_cause if gm._game_over_pending else "",
                })
        except Exception as e:
            # An SSE body cannot change its status code, so surface the failure
            # as a typed event instead of silently truncating the stream.
            logger.exception("SSE action failed")
            yield _sse({"type": "error", "message": str(e)})
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ------------------------------------------------------------------
#  Game over confirmation endpoints
# ------------------------------------------------------------------


@app.post("/api/confirm_game_over")
@api_errors
async def api_confirm_game_over(auth=Depends(_require_auth)):
    """Confirm a pending game over — executes actual cleanup."""
    gm = _get_gm()
    async with _turn_lock:
        result = gm._confirm_game_over()
    return {"result": result, "gameOver": gm._game_over}


@app.post("/api/cancel_game_over")
@api_errors
async def api_cancel_game_over(auth=Depends(_require_auth)):
    """Cancel a pending game over — resumes gameplay."""
    gm = _get_gm()
    async with _turn_lock:
        result = gm._cancel_game_over()
    return {"result": result}


# ------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------

def _recent_knowledge(gm: GameMaster, highlight: str = "") -> list[dict]:
    """Knowledge listing that surfaces just-added chunks first.

    ``list_all`` is capped at 200 entries out of a store that can hold
    thousands, so a newly imported document could otherwise be missing from
    the response the UI renders right after adding it.
    """
    entries = gm.knowledge.list_all(limit=1000)
    if highlight:
        # Match on the first non-empty line of the pasted text.
        probe = next(
            (ln.strip().lstrip("#").strip() for ln in highlight.splitlines() if ln.strip()),
            "",
        )[:20]
        if probe:
            entries.sort(key=lambda e: probe not in (e.get("content") or ""))
    return entries[:200]


def _get_scene_npcs(gm: GameMaster) -> list[dict]:
    """Get scene NPCs with their states."""
    npcs = []
    for name in gm.scene_npcs:
        npc = gm.npc_store.find_by_name(name)
        state = gm.npc_store.get_state(name)
        state_dict = state.get_state() if state else {}
        npcs.append({
            "name": name,
            "emotion": state_dict.get("emotion", "calm"),
            "trust": state_dict.get("trust", 0.5),
            "hp": state_dict.get("hp", 10),
            "max_hp": state_dict.get("max_hp", 10),
        })
    return npcs


def _extract_suggestions(text: str) -> list[str]:
    """Extract numbered suggestions from text."""
    import re
    suggestions = []
    for match in re.finditer(r'^\d+\.\s+(.+)$', text, re.MULTILINE):
        suggestions.append(match.group(1))
    return suggestions


# ------------------------------------------------------------------
#  Static files (built frontend)
# ------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent.parent / "web" / "dist"
if _STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


# ------------------------------------------------------------------
#  Run
# ------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
