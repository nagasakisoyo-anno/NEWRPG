"""OpenAI Agents SDK configuration — DeepSeek client injection + GameContext.

Single-pipeline architecture: the only entry point is the Orchestrator
(see :mod:`trpg_agent.orchestrator`), which routes to Judge / NPC / Narrator.
The legacy single-GM-Agent pipeline has been removed; both the CLI and the
web API now drive the exact same agent graph.
"""

import os
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from agents import (
    set_default_openai_client,
    set_default_openai_api,
    set_tracing_disabled,
)


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

# DeepSeek retired the `deepseek-v4-flash` alias; `deepseek-chat` is the stable
# OpenAI-compatible name.  Override with TRPG_MODEL for any other deployment.
DEFAULT_MODEL = os.environ.get("TRPG_MODEL", "deepseek-chat")
DEFAULT_BASE_URL = os.environ.get("TRPG_BASE_URL", "https://api.deepseek.com")


# ---------------------------------------------------------------------------
# GameContext — shared state injected into all tools via RunContextWrapper
# ---------------------------------------------------------------------------

@dataclass
class GameContext:
    """Mutable game state accessible by all @function_tool functions via ctx.context."""

    player_state: Any          # StateMachine
    npc_store: Any             # NPCStore
    memory: Any                # MemoryStore
    knowledge: Any             # KnowledgeBase
    scene_npcs: list[str]      # names of NPCs currently in the scene
    time_of_day: str
    weather: str
    player_name: str
    player_card: str           # player character summary text
    player_skills: list[dict[str, Any]] = field(default_factory=list)  # [{name, value}, ...]
    player_attributes: dict[str, int] = field(default_factory=dict)    # {力量, 敏捷, ...}
    npc_agents: dict[str, Any] = field(default_factory=dict)   # NPC name → Agent
    judge_agent: Any = None     # Judge Agent instance (set by GameMaster)
    narrator_agent: Any = None  # Narrator Agent instance (set by GameMaster)
    history_messages: list[dict[str, str]] = field(default_factory=list)
    llm: Any = None            # LLM instance (NPC legacy fallback + memory extraction)
    game_over: bool = False    # mutable flag — game truly ended (after confirmation)
    game_over_pending: bool = False  # game_over tool was called, awaiting confirmation
    game_over_cause: str = ""        # reason for game over (for confirmation prompt)
    debug: bool = False        # emit per-tool debug entries
    debug_log: list[str] = field(default_factory=list)  # collected debug lines
    recent_events: list[str] = field(default_factory=list)  # last N event summaries
    player_action: str = ""     # what the player just did (passed to Judge/Narrator)
    world_setting: str = ""     # world description from config.yaml (passed to Narrator)
    # Narrator output captured directly at the tool boundary.  The Orchestrator
    # is only asked to relay this JSON and an LLM may paraphrase or truncate it,
    # so the authoritative copy is intercepted here instead of trusting
    # the Orchestrator's final_output.
    narrator_output: str = ""
    # NPC lines produced this turn (invoke_npc / invoke_npcs / broadcast_event),
    # so the UI can render them as separate chat bubbles regardless of pipeline.
    npc_messages: list[dict[str, str]] = field(default_factory=list)
    # Dice/check results produced this turn.  The Narrator is forbidden from
    # writing numbers into the prose, so this is the only way the player can
    # actually see what was rolled.
    dice_rolls: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DeepSeek client setup
# ---------------------------------------------------------------------------

def configure_deepseek() -> None:
    """Inject DeepSeek as the default OpenAI client (Chat Completions API).

    Must be called once at startup, before any Agent / Runner usage.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY environment variable not set")

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=DEFAULT_BASE_URL,
        timeout=90.0,
        max_retries=2,
    )
    set_default_openai_client(client)
    set_default_openai_api("chat_completions")
    set_tracing_disabled(True)
