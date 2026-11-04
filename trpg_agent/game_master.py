"""GM 核心调度 — GM Agent 工具调用架构。

:class:`GameMaster` 是 TRPG Agent 的顶层入口，负责：

- GM Agent (LLM) 作为唯一入口分析玩家输入
- 工具调用循环：GM 可调用骰子、检定、战斗、NPC 操作等工具
- 统一 JSON 响应格式 → 渲染为玩家可见文本

典型用法::

    gm = GameMaster("config.yaml")
    reply = gm.process("我推开酒馆的门")
    print(reply)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import deque
from collections.abc import AsyncGenerator
from typing import Any, Dict, List, Optional

import yaml

from agents import Runner

from trpg_agent.event_stream import StatusEvent, AgentStatusHooks
from trpg_agent.agent_config import (
    GameContext,
    configure_deepseek,
)
from trpg_agent.orchestrator import (
    build_orchestrator_instructions,
    build_orchestrator_input,
)
from trpg_agent.character import Character
from trpg_agent.dice import roll
from trpg_agent.llm import LLM
from trpg_agent.memory import MemoryStore
from trpg_agent.npc import NPCStore
from trpg_agent.rag import KnowledgeBase
from trpg_agent.state import StateMachine, calc_max_hp

# Dice expression embedded in user text (e.g. "3d6+2", "d20") — used by _fallback_response()
_DICE_EXPR_RE = re.compile(r"(\d*)d(\d+)(?:\s*\+\s*(\d+))?")

# Bumped when the save payload grows fields that older loaders would miss.
SAVE_VERSION = 2


class GameMaster:
    """GM 核心调度 — GM Agent 工具调用架构。

    Parameters
    ----------
    config_path : str
        角色 YAML 配置文件路径。
    llm_api_key : str, optional
        LLM API 密钥。若为 ``None`` 则从环境变量 ``DEEPSEEK_API_KEY`` 读取。
    knowledge_dir : str, optional
        知识文件所在目录，默认为 ``"data/knowledge"``。
    """

    def __init__(
        self,
        config_path: str,
        llm_api_key: Optional[str] = None,
        knowledge_dir: str = "data/knowledge",
        debug: bool = False,
        status_fn: object = None,
    ) -> None:
        self.debug = debug
        self._debug_log: List[str] = []
        status = status_fn or (lambda msg: None)

        # -- LLM (第一步连接，后续 NPC 生成等需要用到) --
        # Only an explicit, non-empty key overrides the environment: the API
        # server passes os.environ.get(..., "") which would otherwise wipe a
        # perfectly good key with an empty string.
        if llm_api_key:
            os.environ["DEEPSEEK_API_KEY"] = llm_api_key

        status("连接 LLM...")
        self.llm: Optional[LLM] = None
        self._llm_available = False
        try:
            self.llm = LLM()
            self._llm_available = True
            # Configure OpenAI Agents SDK to use DeepSeek
            configure_deepseek()
        except Exception as e:
            # Keep running in fallback mode, but never swallow the reason:
            # a missing key and a bad base_url used to look identical.
            self._debug(f"[LLM] 连接失败，进入兜底模式: {type(e).__name__}: {e}")

        # -- Player --
        status("加载角色卡...")
        self.player = Character.load(config_path)

        # -- World (from config) --
        config_data: Dict[str, Any] = {}
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                config_data = loaded
            self.world: Dict[str, Any] = config_data.get("world", {})
        except Exception:
            self.world = {}

        # -- NPC Store --
        status("初始化 NPC 存储 (ChromaDB)...")
        self.npc_store = NPCStore()
        self.scene_npcs: List[str] = []

        # -- Subsystems --
        self.player_state = StateMachine(
            max_hp=calc_max_hp(self.player.attributes),
        )

        status("初始化记忆存储 (ChromaDB)...")
        self.memory = MemoryStore()

        status("初始化知识库 (ChromaDB)...")
        self.knowledge = KnowledgeBase()

        # -- Load knowledge files (config.yaml knowledge.files wins when set) --
        status("加载世界知识文件并建立索引...")
        knowledge_paths = [knowledge_dir]
        if knowledge_dir == "data/knowledge":
            cfg_files = (config_data.get("knowledge") or {}).get("files") or []
            if isinstance(cfg_files, list) and cfg_files:
                knowledge_paths = [str(p) for p in cfg_files]
        for kp in knowledge_paths:
            self.knowledge.load_from_dir(kp)

        # -- Recent events queue --
        self._recent_events: deque = deque(maxlen=10)

        # -- World state tracking (set by opening/LGM; defaults for empty state) --
        self._time_of_day: str = "清晨"
        self._weather: str = "薄雾"

        # -- Game over flag (two-phase: pending → confirm → cleanup) --
        self._game_over: bool = False
        self._game_over_pending: bool = False
        self._game_over_cause: str = ""

        # -- Previous turn response (for suggestion continuity) --
        self._last_gm_response: str = ""
        self._last_suggestions: list[str] = []
        self._last_user_input: str = ""
        # NPC lines produced in the most recent turn (for UI replay / API)
        self._last_npc_messages: list[dict[str, str]] = []
        # Dice/check results from the most recent turn (for UI replay / API)
        self._last_dice_rolls: list[dict[str, Any]] = []
        # Last new-game setup, so the opening can be re-rolled without
        # re-entering the whole world/NPC form.
        self._last_setup: Dict[str, Any] = {}

        # -- Turn counter (drives the periodic autonomous NPC tick) --
        self._turn_count: int = 0
        self._last_scene_context: str = ""

        # -- Register backstory NPCs (LLM must be connected first) --
        if self._llm_available:
            self._register_backstory_npcs()
        else:
            self._debug("[DEBUG] LLM 未连接，跳过后设故事 NPC 注册")

        # -- World Builder (LLM-driven NPC/world creation) --
        self.world_builder = None
        if self._llm_available:
            from trpg_agent.world_builder import WorldBuilder
            self.world_builder = WorldBuilder(
                llm=self.llm,
                npc_store=self.npc_store,
                knowledge=self.knowledge,
                debug=self.debug,
            )

        # -- OpenAI Agents SDK: single Orchestrator pipeline --
        self.history_messages: list[dict[str, str]] = []
        self._npc_agents: dict[str, Any] = {}

        # -- Multi-Agent: Orchestrator + Judge + Narrator (the only pipeline) --
        self._judge_agent = None
        self._narrator_agent = None
        self._orchestrator_agent = None
        if self._llm_available:
            from trpg_agent.judge import create_judge_agent
            self._judge_agent = create_judge_agent()

            from trpg_agent.narrator import create_narrator_agent
            self._narrator_agent = create_narrator_agent()

            from trpg_agent.tools import (
                get_player_state, get_npc_state,
                create_npc, invoke_npc, invoke_npcs, remove_npc, set_scene, game_over,
                search_knowledge, search_memory,
                invoke_judge, invoke_narrator, broadcast_event,
            )
            from trpg_agent.orchestrator import create_orchestrator_agent
            # The Orchestrator is a router: it must NOT own dice/check tools,
            # otherwise it bypasses the Judge and does mechanics itself.
            self._orchestrator_agent = create_orchestrator_agent(tools=[
                invoke_judge, invoke_narrator, broadcast_event,
                invoke_npc, invoke_npcs,
                search_memory, search_knowledge,
                create_npc, remove_npc, set_scene,
                get_player_state, get_npc_state, game_over,
            ])
            self._debug("[Agent] Orchestrator + Judge + Narrator 已就绪")

        # -- Sync NPC Agents for any NPCs already in the store (backstory, etc.) --
        if self._llm_available:
            self._sync_npc_agents()

    def _debug(self, msg: str) -> None:
        """Log a debug message. Prints to stdout and stores in an internal buffer
        that the API server can expose via /api/debug."""
        if self.debug:
            print(msg)
        self._debug_log.append(msg)
        # 防止 CLI 长时间运行时缓冲区无限增长
        if len(self._debug_log) > 500:
            self._debug_log = self._debug_log[-300:]

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Estimate token count for Chinese-mixed text.

        Chinese characters: ~1.8 chars/token.  English: ~4 chars/token.
        """
        if not text:
            return 0
        cjk = sum(1 for c in text if '一' <= c <= '鿿' or '　' <= c <= '〿')
        other = len(text) - cjk
        return max(1, int(cjk / 1.8 + other / 4))

    # ===================================================================
    #  SOFT-DELETED: embedding-based event classification
    #  GM Agent 现在通过工具调用自行判断事件类型。
    # ===================================================================
    #
    # def _init_event_vectors(self) -> None:
    #     """Pre-compute event description embeddings for fast similarity matching."""
    #     ...
    #
    # def _classify_event(self, user_input: str) -> str | None:
    #     """Classify event type by embedding similarity."""
    #     ...

    # ===================================================================
    #  SOFT-DELETED: regex-based intent detection
    # ===================================================================
    #
    # def _detect_intent(self, user_input: str) -> str:
    #     """通过正则匹配识别用户意图（不调 LLM）。"""
    #     ...

    # ===================================================================
    #  SOFT-DELETED: intent handlers (dice, info, event)
    #  骰子/状态查询/事件判定现在由 GM Agent 通过工具调用处理。
    # ===================================================================
    #
    # def _handle_dice(self, user_input: str) -> str:
    #     """处理骰子投掷意图。"""
    #     ...
    #
    # def _handle_info(self) -> str:
    #     """处理角色信息/状态查询意图。"""
    #     ...
    #
    # def _handle_event(self, user_input: str) -> dict:
    #     """处理事件触发意图。"""
    #     ...

    # ------------------------------------------------------------------
    #  Backstory NPC registration
    # ------------------------------------------------------------------

    def _generate_npc_attributes(
        self, name: str, core: list[str], personality_tone: str
    ) -> dict[str, int]:
        """Generate NPC attributes based on role description via LLM.

        Falls back to balanced defaults (10) if LLM is unavailable.
        """
        from trpg_agent.npc import generate_npc_attributes

        llm = self.llm if self._llm_available else None
        return generate_npc_attributes(llm, name, core, personality_tone)

    def _register_backstory_npcs(self, use_scenario: bool = True) -> None:
        """Extract and register NPCs from the player's backstory using LLM.

        Parameters
        ----------
        use_scenario : bool
            Whether to also seed NPCs from the bundled scenario in the
            knowledge base.  Disabled for a custom worldview, otherwise the
            packaged cast shows up in an unrelated setting.
        """
        if not self._llm_available or not self.llm:
            return

        anchor = self.player.anchor
        core_text = "\n".join(self.player.core)
        rel_text = (
            "\n".join(f"- {k}: {v}" for k, v in self.player.relations.items())
            if self.player.relations
            else "（无）"
        )

        scenario_text = ""
        if use_scenario:
            scenario_knowledge = self.knowledge.query(
                "剧本 关键NPC 节点0 开场", self.player.name
            )
            scenario_text = "\n".join(scenario_knowledge[:3]) if scenario_knowledge else ""

        system = (
            "从以下TRPG角色背景和剧本中提取所有有名字、有身份的NPC。\n"
            "已故角色不要提取。路人描述不要提取。\n"
            "主角的关系列表中已声明的NPC必须提取。剧本关键NPC必须提取。如果有重复的，合并为一个。\n"
            "以 JSON 对象格式返回，形如 {\"npcs\": [{\"name\": ..., \"core\": [...], \"personality_tone\": ..., \"relations\": {...}}]}，每个NPC包含 name, core, personality_tone, relations。\n"
            '示例: {"npcs": [{"name": "艾莉西亚", "core": ["玩家的妹妹"], '
            '"personality_tone": "温柔但倔强", "relations": {"罗恩·瓦尔特": "哥哥"}}]}'
        )
        user_content = f"主角: {self.player.name}\n背景:\n锚: {anchor}\n核心:\n{core_text}\n\n主角的关系:\n{rel_text}"
        if scenario_text:
            user_content += f"\n\n剧本信息:\n{scenario_text}"
        messages = [
            {"role": "user", "content": user_content},
        ]

        try:
            result = self.llm.chat_json(system=system, messages=messages)
            npcs = result.get("npcs", []) if isinstance(result, dict) else []
            if not isinstance(npcs, list):
                npcs = []

            for npc_data in npcs:
                name = (npc_data.get("name", "") or "").strip()
                core = npc_data.get("core", [])
                tone = (npc_data.get("personality_tone", "") or "").strip()
                relations = npc_data.get("relations", {})

                if not name or len(name) < 1:
                    continue
                if not isinstance(core, list) or not core:
                    core = [f"{name} — 玩家角色的关联人物"]
                if not tone:
                    tone = "正常"

                existing = self.npc_store.find_by_name(name)
                if existing is not None:
                    if name not in self.scene_npcs:
                        self.scene_npcs.append(name)
                    continue

                if not isinstance(relations, dict):
                    relations = {}

                attributes = self._generate_npc_attributes(name, core, tone)

                self.npc_store.create(
                    name=name,
                    core=core,
                    attributes=attributes,
                    relations=relations,
                    personality={
                        "tone": tone,
                        "verbal_tics": "无特殊语言习惯",
                        "emotion_map": {
                            "calm": f"以{tone}的态度说话",
                            "wary": "警惕地观察",
                            "hostile": "表现出敌意",
                        },
                    },
                )
                self.scene_npcs.append(name)

                self._debug(f"[DEBUG] 注册后设故事 NPC: {name} (语调: {tone})")

        except Exception as e:
            self._debug(f"[DEBUG] 后设故事 NPC 提取失败: {e}")

    @staticmethod
    def _default_personality_for(relation: str) -> Dict[str, Any]:
        """Return a default personality dict for a relationship keyword."""
        tone_map = {
            "妹妹": "温柔关爱",
            "弟弟": "活泼依赖",
            "母亲": "慈爱关怀",
            "父亲": "沉默寡言",
            "哥哥": "保护欲强",
            "姐姐": "温柔体贴",
            "女儿": "天真烂漫",
            "儿子": "活泼好动",
            "妻子": "温柔坚定",
            "丈夫": "沉稳可靠",
            "朋友": "友好随和",
            "师父": "严厉但关心",
        }
        tone = tone_map.get(relation, "正常")
        return {
            "tone": tone,
            "verbal_tics": "无特殊语言习惯",
            "emotion_map": {
                "calm": f"平静地表现{GameMaster._kw_to_behaviour(relation, 'calm')}",
                "wary": f"警惕地观察四周",
                "hostile": f"表现出敌意和抗拒",
            },
            "catchphrases": [],
        }

    @staticmethod
    def _kw_to_behaviour(relation: str, emotion: str) -> str:
        """Map relation keyword to calm-state behaviour description."""
        behaviour_map = {
            "妹妹": "出依赖和亲近",
            "弟弟": "出活泼和好奇",
            "母亲": "出关爱和担忧",
            "父亲": "出沉稳和保护欲",
            "哥哥": "出保护欲",
            "姐姐": "出关心",
            "女儿": "出天真和依赖",
            "儿子": "出活泼",
            "妻子": "出温柔",
            "丈夫": "出可靠",
            "朋友": "出友好",
            "师父": "出教导的态度",
        }
        return behaviour_map.get(relation, f"出{emotion}的情绪")


    # ------------------------------------------------------------------
    #  Context builder
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    #  Shared turn scaffolding (used by BOTH the sync and streaming paths)
    # ------------------------------------------------------------------

    def _pre_dispatch(self, user_input: str) -> Optional[str]:
        """Handle everything that must happen before the Orchestrator runs.

        Returns a ready-made reply string when the turn is fully handled here
        (game-over gate, world-builder command), otherwise ``None``.

        Both pipelines call this, so CLI and Web can never drift apart on
        which commands are recognised.
        """
        from trpg_agent.tools import clear_tool_cache

        clear_tool_cache()

        # ---- Game over confirmation gate ----
        if self._game_over_pending:
            if user_input.lower() in ("/confirm", "确认"):
                return self._confirm_game_over()
            return self._cancel_game_over()

        # ---- World builder commands (!npc / !world / !simulate) ----
        if self.world_builder and user_input.startswith("!"):
            world_result = self._handle_world_builder_command(user_input)
            if world_result:
                return self._narrate_world_builder_result(world_result, user_input)

        return None

    def _build_orchestrator_payload(self, user_input: str) -> tuple[str, GameContext]:
        """Build the Orchestrator input text and the GameContext for this turn."""
        scene_npcs_text = ", ".join(self.scene_npcs) if self.scene_npcs else "无"
        self._debug(f"\n{'─'*50}\n[Orch] 玩家: {user_input}")
        self._debug(
            f"[Orch] 场景: {scene_npcs_text}  |  {self._time_of_day}  ·  {self._weather}"
        )

        state = self.player_state.get_state()
        scene_info = (
            f"时间: {self._time_of_day}  天气: {self._weather}  场景NPC: {scene_npcs_text}\n"
            f"HP {state['hp']}/{state['max_hp']}  情绪 {state['emotion']}  "
            f"信任 {state['trust']}  体力 {state['stamina']}"
        )
        orch_input = build_orchestrator_input(
            player_input=user_input,
            player_name=self.player.name,
            scene_context=scene_info,
            history_text=self._format_history(),
            last_gm_response=self._last_gm_response,
        )

        game_ctx = self._build_game_context(player_action=user_input)

        self._orchestrator_agent.instructions = build_orchestrator_instructions(
            time_of_day=self._time_of_day,
            weather=self._weather,
            scene_npcs=scene_npcs_text,
            world_setting=self.world.get("description", self.world.get("name", "")),
            player_name=self.player.name,
            player_card=self.player.summary(),
        )
        return orch_input, game_ctx

    @staticmethod
    def _resolve_narration(game_ctx: GameContext, response: str) -> tuple[str, list[str]]:
        """Prefer the Narrator's intercepted JSON over the Orchestrator's relay.

        The Orchestrator is instructed to pass the Narrator output through
        verbatim, but it is still an LLM and may paraphrase or truncate it.
        ``invoke_narrator`` records the authoritative text on the context.
        """
        narrator_raw = getattr(game_ctx, "narrator_output", "") or ""
        if narrator_raw:
            narration, suggestions = GameMaster._parse_gm_output(narrator_raw)
            if narration:
                return narration, suggestions
        return GameMaster._parse_gm_output(response)

    def _record_turn(self, user_input: str, response: str, game_ctx: GameContext) -> None:
        """Common bookkeeping: history, trimming and state write-back."""
        self.history_messages.append({"role": "user", "content": user_input})
        self.history_messages.append({"role": "assistant", "content": response})
        self._trim_history()
        self._sync_from_context(game_ctx)

    def _build_input(self, user_input: str) -> str:
        """Build a compact input text for the GM Agent.

        Memory retrieval is NOT done here — the GM calls `search_memory` as a tool
        when it actually needs to recall past events.  This keeps the context window
        lean and avoids injecting the same old memories every turn.
        """
        parts: list[str] = []

        # -- Current state summary (lightweight, always relevant) --
        state = self.player_state.get_state()
        npc_text = ", ".join(self.scene_npcs) if self.scene_npcs else "无"
        parts.append(
            f"## 当前状态\n"
            f"时间: {self._time_of_day}  天气: {self._weather}  |  场景NPC: {npc_text}\n"
            f"HP {state['hp']}/{state['max_hp']}  情绪 {state['emotion']}  "
            f"信任 {state['trust']}  体力 {state['stamina']}"
        )

        # -- Recent history (last 3 exchanges, for multi-turn continuity) --
        history_text = self._format_history()
        if history_text:
            parts.append(history_text)

        # -- Previous turn (suggestion continuity) --
        if self._last_gm_response:
            parts.append(f"## 上一轮 GM 回应\n{self._last_gm_response}")

        # -- Player input --
        parts.append(f"玩家: {user_input}")
        parts.append("请按系统提示词中定义的 JSON 格式回复（只包含 narration 和 suggestions）。")

        return "\n\n".join(parts)

    def _build_game_context(self, player_action: str = "") -> GameContext:
        """Build the GameContext dataclass with current game state."""
        return GameContext(
            player_state=self.player_state,
            npc_store=self.npc_store,
            memory=self.memory,
            knowledge=self.knowledge,
            scene_npcs=self.scene_npcs,
            time_of_day=self._time_of_day,
            weather=self._weather,
            player_name=self.player.name,
            player_card=self.player.summary(),
            player_skills=self.player.skills,
            player_attributes=self.player.attributes,
            npc_agents=self._npc_agents,
            judge_agent=self._judge_agent,
            narrator_agent=self._narrator_agent,
            history_messages=[],  # don't pass — SDK may leak stale tool_calls to DeepSeek
            llm=self.llm,
            game_over=self._game_over,
            game_over_pending=self._game_over_pending,
            game_over_cause=self._game_over_cause,
            debug=self.debug,
            debug_log=[],
            recent_events=list(self._recent_events),
            player_action=player_action,
            world_setting=self.world.get("description", self.world.get("name", "")),
        )

    def _sync_from_context(self, game_ctx: GameContext) -> None:
        """Copy state mutated inside tool calls back onto the GameMaster."""
        self.scene_npcs[:] = list(game_ctx.scene_npcs)
        self._time_of_day = game_ctx.time_of_day
        self._weather = game_ctx.weather
        self._game_over = game_ctx.game_over
        self._game_over_pending = game_ctx.game_over_pending
        self._game_over_cause = game_ctx.game_over_cause

    @staticmethod
    def _parse_gm_output(raw: str) -> tuple[str, list[str]]:
        """Extract (narration, suggestions) from a model reply.

        Shared by the multi-agent, legacy and streaming pipelines so the three
        paths can never drift apart again.  Accepts plain JSON, fenced JSON,
        JSON embedded in prose, and finally bare numbered suggestion lines.
        """
        text = (raw or "").strip()
        if not text:
            return "", []

        candidates = [text]
        fence = re.match(r'^```(?:json)?\s*\n?(.*?)\n?```$', text, re.DOTALL)
        if fence:
            candidates.append(fence.group(1).strip())

        def _as_pair(obj: object) -> tuple[str, list[str]] | None:
            if not isinstance(obj, dict):
                return None
            narration = obj.get("narration")
            if not isinstance(narration, str) or not narration.strip():
                return None
            suggestions = obj.get("suggestions")
            return narration, suggestions if isinstance(suggestions, list) else []

        for cand in candidates:
            try:
                # strict=False: narration often contains literal newlines, which
                # strict JSON rejects as control characters.
                pair = _as_pair(json.loads(cand, strict=False))
            except (json.JSONDecodeError, TypeError):
                pair = None
            if pair:
                return pair

        embedded = re.search(r'\{[^{}]*"narration"[^{}]*\}', text, re.DOTALL)
        if embedded:
            try:
                pair = _as_pair(json.loads(embedded.group(0), strict=False))
            except (json.JSONDecodeError, TypeError):
                pair = None
            if pair:
                return pair

        keep: list[str] = []
        suggestions: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if re.match(r'^\d+\.\s+', stripped):
                suggestions.append(re.sub(r'^\d+\.\s+', '', stripped))
            elif stripped.startswith("**建议"):
                continue
            else:
                keep.append(line)

        narration = "\n".join(keep).strip() or text
        return narration, suggestions

    @staticmethod
    def _clean_narration(text: str) -> str:
        """Drop trailing suggestion blocks so memories never store UI hints."""
        lines = (text or "").split("\n")
        keep: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("**建议") or stripped.startswith("建议："):
                break
            keep.append(line)
        return "\n".join(keep).strip()

    def _format_history(self) -> str:
        """Format recent conversation history for context."""
        if not self.history_messages:
            return ""
        lines = ["## 最近对话"]
        for msg in self.history_messages[-6:]:  # last 3 exchanges
            role = "玩家" if msg["role"] == "user" else "GM"
            content = msg["content"][:200]
            lines.append(f"{role}: {content}")
        return "\n".join(lines) + "\n\n"

    def _trim_history(self, max_exchanges: int = 5) -> None:
        """Keep only the most recent N exchanges in history."""
        max_messages = max_exchanges * 2  # user + assistant per exchange
        if len(self.history_messages) > max_messages:
            self.history_messages = self.history_messages[-max_messages:]

    def _admit_new_npcs_to_scene(self) -> None:
        """Put every stored-but-absent NPC into the scene and give it an Agent.

        ``WorldBuilder`` only persists the character card, so a ``!npc``
        creation used to leave the NPC unreachable: not in ``scene_npcs`` and
        with no entry in ``_npc_agents``, which meant ``invoke_npc`` reported
        "找不到 NPC" for a character the player had just created.
        """
        try:
            for npc in self.npc_store.all():
                if npc.name not in self.scene_npcs:
                    self.scene_npcs.append(npc.name)
                    self._debug(f"[场景] 新 NPC 入场: {npc.name}")
        except Exception as e:
            self._debug(f"[场景] NPC 入场失败: {e}")
        self._sync_npc_agents()

    def _sync_npc_agents(self) -> None:
        """Ensure every NPC in the store has a corresponding Agent in the pool.

        Called at startup (after backstory NPC registration) and after loading a save,
        so that initial NPCs don't have to wait for a create_npc tool call to get
        their autonomous Agent.
        """
        if not self._llm_available:
            return

        from trpg_agent.npc_agent import create_npc_agent

        for npc_char in self.npc_store.all():
            name = npc_char.name
            if name not in self._npc_agents:
                try:
                    self._npc_agents[name] = create_npc_agent(
                        name=name,
                        personality_prompt=npc_char.build_personality_prompt(),
                        npc_store=self.npc_store,
                    )
                    self._debug(f"[DEBUG] 同步 NPC Agent: {name}")
                except Exception as e:
                    self._debug(f"[DEBUG] NPC Agent 创建失败 ({name}): {e}")

    # ===================================================================
    #  Single pipeline: Orchestrator → (Judge + NPCs parallel) → Narrator
    #
    #  Both entry points below drive the SAME agent graph:
    #    process()            — synchronous, used by the CLI (testing surface)
    #    process_streaming()  — async + SSE status events, used by the web UI
    #
    #  They share _pre_dispatch / _build_orchestrator_payload / _record_turn /
    #  _resolve_narration / _post_turn, so the two front-ends can never drift
    #  into behaving like different games again.
    # ===================================================================

    def _turn_tail(self, narration: str) -> str:
        """Advance the turn counter and run the periodic NPC tick (sync path)."""
        self._turn_count += 1
        if self._turn_count % 3 == 0 and self._orchestrator_agent is not None:
            self._debug("[Orch] NPC tick...")
            tick_text = self._run_npc_tick()
            if tick_text and tick_text.strip():
                return narration + "\n\n" + tick_text
        return narration

    def _log_turn_state(self) -> None:
        if not self.debug:
            return
        state = self.player_state.get_state()
        self._debug(
            f"[状态] HP {state['hp']}/{state['max_hp']}  |  "
            f"情绪 {state['emotion']}  |  信任 {state['trust']}  |  "
            f"体力 {state['stamina']}  |  在场 {self.scene_npcs or '无'}"
        )

    def process(self, user_input: str, console=None) -> str:
        """Single turn entry point (synchronous) — Orchestrator pipeline.

        Parameters
        ----------
        user_input : str
            Player's input text.
        console
            Unused; kept so existing CLI call sites stay source-compatible.

        Returns
        -------
        str
            The narration text for this turn.
        """
        user_input = user_input.strip()
        if not user_input:
            return "请说点什么吧。"

        self._last_user_input = user_input

        # ---- Shared pre-dispatch: game-over gate + ! commands ----
        early = self._pre_dispatch(user_input)
        if early is not None:
            return early

        if not self._llm_available or self._orchestrator_agent is None:
            return self._fallback_response(user_input)

        orch_input, game_ctx = self._build_orchestrator_payload(user_input)

        t0 = time.time()
        try:
            result = Runner.run_sync(
                self._orchestrator_agent,
                input=orch_input,
                context=game_ctx,
                max_turns=12,
            )
            response = result.final_output
        except Exception as e:
            self._debug(f"[Orch] Runner 失败: {e}")
            return self._fallback_response(user_input)

        elapsed_ms = int((time.time() - t0) * 1000)

        narration, self._last_suggestions = self._resolve_narration(game_ctx, response)
        narration = _strip_duplicated_npc_lines(narration, game_ctx.npc_messages)

        if self.debug:
            self._debug(
                f"[Orch] 完成  |  耗时 {elapsed_ms}ms  |  输出 {len(response)}字符  |  "
                f"输入 ~{self._estimate_tokens(orch_input)}tok"
            )
            for entry in game_ctx.debug_log:
                self._debug(entry)

        self._record_turn(user_input, response, game_ctx)
        self._last_npc_messages = list(game_ctx.npc_messages)
        self._last_dice_rolls = list(game_ctx.dice_rolls)

        # ---- Memory (synchronous here; the async path offloads it) ----
        if not self._game_over and narration:
            self._write_turn_memories(user_input, narration, game_ctx)

        narration = self._turn_tail(narration)
        self._log_turn_state()

        self._last_gm_response = narration
        return narration

    async def process_streaming(self, user_input: str) -> AsyncGenerator["str | StatusEvent", None]:
        """Async generator — same pipeline as :meth:`process`, with SSE events.

        Yields:
            str          — narrative text delta for the player
            StatusEvent  — thinking / tool_call / npc_message status for the UI

        Caller should read ``self._last_gm_response``, ``self.player_state``
        and ``self.scene_npcs`` after the generator is exhausted.
        """
        user_input = user_input.strip()
        if not user_input:
            yield "请说点什么吧。"
            return

        self._last_user_input = user_input

        early = self._pre_dispatch(user_input)
        if early is not None:
            self._last_gm_response = early
            yield early
            return

        if not self._llm_available or self._orchestrator_agent is None:
            response = self._fallback_response(user_input)
            self._last_gm_response = response
            yield response
            return

        orch_input, game_ctx = self._build_orchestrator_payload(user_input)

        event_queue: asyncio.Queue = asyncio.Queue()
        hooks = AgentStatusHooks(event_queue)

        t0 = time.time()
        response = ""
        try:
            run_task = asyncio.create_task(
                Runner.run(
                    self._orchestrator_agent,
                    input=orch_input,
                    context=game_ctx,
                    max_turns=12,
                    hooks=hooks,
                )
            )

            # Relay status events while the agent graph is working.
            while not run_task.done():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    if isinstance(event, StatusEvent):
                        yield event
                except asyncio.TimeoutError:
                    pass

            while not event_queue.empty():
                event = event_queue.get_nowait()
                if isinstance(event, StatusEvent):
                    yield event

            response = run_task.result().final_output
        except Exception as e:
            self._debug(f"[Orch] Runner 失败: {e}")
            fallback = self._fallback_response(user_input)
            self._last_gm_response = fallback
            yield fallback
            return

        elapsed_ms = int((time.time() - t0) * 1000)

        narration, self._last_suggestions = self._resolve_narration(game_ctx, response)
        narration = _strip_duplicated_npc_lines(narration, game_ctx.npc_messages)

        if self.debug:
            self._debug(
                f"[Orch] 完成  |  耗时 {elapsed_ms}ms  |  输出 {len(response)}字符  |  "
                f"输入 ~{self._estimate_tokens(orch_input)}tok"
            )
            for entry in game_ctx.debug_log:
                self._debug(entry)

        # NPC lines are emitted as their own bubbles before the narration,
        # mirroring how the tools actually produced them this turn.
        for msg in game_ctx.npc_messages:
            yield StatusEvent(
                type="npc_message",
                npc_name=msg.get("name", ""),
                npc_text=msg.get("text", ""),
            )
        self._last_npc_messages = list(game_ctx.npc_messages)

        # Dice results: the Narrator is forbidden from printing numbers, so
        # this is the only place the player sees what was actually rolled.
        for roll_info in game_ctx.dice_rolls:
            yield StatusEvent(
                type="dice_roll",
                roll_kind=roll_info.get("kind", "dice"),
                roll_label=roll_info.get("label", ""),
                roll_detail=roll_info.get("detail", ""),
                roll_success=roll_info.get("success"),
            )
        self._last_dice_rolls = list(game_ctx.dice_rolls)

        if narration:
            for sentence in _split_sentences(narration):
                yield sentence
                await asyncio.sleep(0.03)

        self._record_turn(user_input, response, game_ctx)

        # ---- Memory: run off the critical path so SSE is not blocked ----
        if not self._game_over and narration:
            await self._awrite_turn_memories(user_input, narration, game_ctx)

        # ---- Periodic NPC tick (async-native in this path) ----
        self._turn_count += 1
        if self._turn_count % 3 == 0:
            tick_text = await self._arun_npc_tick()
            if tick_text and tick_text.strip():
                yield "\n\n" + tick_text
                narration += "\n\n" + tick_text

        self._log_turn_state()
        self._last_gm_response = narration

    # ------------------------------------------------------------------
    #  Memory writing (sync + async variants over the same content)
    # ------------------------------------------------------------------

    def _turn_memory_payloads(
        self, user_input: str, narration: str, game_ctx: GameContext
    ) -> tuple[str, list[tuple[str, str]]]:
        """Build the main-memory text and the per-NPC memory payloads."""
        clean = self._clean_narration(narration)
        npc_payloads = [
            (
                npc_name,
                f"以下事件与{npc_name}相关，请从{npc_name}的视角提取记忆:\n"
                f"玩家: {user_input}\n事件: {clean[:300]}",
            )
            for npc_name in game_ctx.scene_npcs
        ]
        return clean, npc_payloads

    def _write_turn_memories(
        self, user_input: str, narration: str, game_ctx: GameContext
    ) -> None:
        """Persist this turn's memories synchronously (CLI path)."""
        clean, npc_payloads = self._turn_memory_payloads(user_input, narration, game_ctx)
        try:
            self._write_memory(user_input, clean, [])
            for npc_name, payload in npc_payloads:
                self._write_npc_memory(npc_name, payload)
        except Exception as e:
            self._debug(f"[DEBUG] 记忆写入失败: {e}")

    async def _awrite_turn_memories(
        self, user_input: str, narration: str, game_ctx: GameContext
    ) -> None:
        """Persist this turn's memories without blocking the event loop.

        Memory extraction needs one LLM round-trip per NPC plus one for the
        main log.  Done synchronously inside an async generator these calls
        froze the whole server for seconds; here they run concurrently on the
        async client.
        """
        clean, npc_payloads = self._turn_memory_payloads(user_input, narration, game_ctx)
        try:
            await asyncio.gather(
                self._awrite_memory(user_input, clean),
                *[self._awrite_npc_memory(n, p) for n, p in npc_payloads],
                return_exceptions=True,
            )
        except Exception as e:
            self._debug(f"[DEBUG] 记忆写入失败: {e}")

    async def _awrite_memory(self, user_input: str, narration: str) -> None:
        """Async counterpart of :meth:`_write_memory`."""
        if not self._llm_available or not self.llm:
            return
        combined = f"玩家({self.player.name}): {user_input}\n结果: {narration}"
        try:
            extracted = await self.llm.aextract_memory(combined)
            if not extracted:
                return
            mem_id = self.memory.add(
                content=extracted,
                context={"emotion": self.player_state.get_state()["emotion"]},
            )
            self._recent_events.append(extracted)
            for s in self.memory.search(extracted, n=3):
                if s.get("id") != mem_id:
                    self.memory.link(mem_id, s["id"], "关联到")
            if len(self._recent_events) >= 2:
                prev = self.memory.search(self._recent_events[-2], n=1)
                if prev and prev[0].get("id") != mem_id:
                    self.memory.link(mem_id, prev[0]["id"], "发生在...之后")
            self._debug(f"[记忆] 主: {extracted[:100]}")
        except Exception as e:
            self._debug(f"[记忆] 写入失败: {e}")

    async def _awrite_npc_memory(self, npc_name: str, raw_interaction: str) -> None:
        """Async counterpart of :meth:`_write_npc_memory`."""
        if not self._llm_available or not self.llm:
            return
        try:
            extracted = await self.llm.aextract_memory(raw_interaction)
            if not extracted:
                return
            npc_mem_id = self.memory.npc_add(
                content=extracted,
                context={"emotion": self.player_state.get_state()["emotion"]},
                npc_name=npc_name,
            )
            for s in self.memory.search(extracted, n=2):
                if s.get("id") != npc_mem_id:
                    self.memory.link(npc_mem_id, s["id"], "关联到")
            self._debug(f"[记忆] NPC「{npc_name}」: {extracted[:100]}")
        except Exception as e:
            self._debug(f"[记忆] NPC「{npc_name}」写入失败: {e}")

    # ------------------------------------------------------------------
    #  Autonomous NPC tick
    # ------------------------------------------------------------------

    async def _arun_npc_tick(self) -> str:
        """Let every scene NPC autonomously act after some time has passed."""
        if not self._npc_agents or not self.scene_npcs:
            return ""

        from trpg_agent.npc_agent import build_npc_tick_input

        async def _tick_one(name: str) -> str | None:
            agent = self._npc_agents.get(name)
            if agent is None:
                return None
            npc_state = self.npc_store.get_state(name)
            state_dict = npc_state.get_state() if npc_state else {
                "emotion": "calm", "stamina": "fresh", "hp": "?", "max_hp": "?"
            }
            npc_input = build_npc_tick_input(
                npc_name=name,
                npc_state=state_dict,
                scene=f"时间: {self._time_of_day}, 天气: {self._weather}",
                turns_since_last_action=3,
                memory=self.memory,
            )
            try:
                result = await Runner.run(
                    agent,
                    input=npc_input,
                    context={"memory": self.memory, "npc_name": name},
                    max_turns=2,
                )
                response = result.final_output.strip()
                if response and response.lower() != "<silent>":
                    self.npc_store.append_history(name, "user", "[时间流逝]")
                    self.npc_store.append_history(name, "assistant", response)
                    self.npc_store.save_state(name)
                    return f"{name}: {response}"
                return None
            except Exception:
                return None

        results = await asyncio.gather(
            *[_tick_one(name) for name in self.scene_npcs],
            return_exceptions=True,
        )
        return "\n".join(r for r in results if isinstance(r, str) and r)

    def _run_npc_tick(self) -> str:
        """Synchronous wrapper around :meth:`_arun_npc_tick` for the CLI path."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._arun_npc_tick())
        # Already inside a loop: the async path calls _arun_npc_tick directly.
        self._debug("[Orch] Tick 跳过——异步事件循环已在运行")
        return ""
    def _handle_world_builder_command(self, command: str) -> str:
        """处理以 ! 开头的世界构建命令。

        支持命令：
        - `!npc <描述>` — 自然语言创建 NPC
        - `!world <描述>` — 自然语言添加世界观知识
        - `!simulate` — 模拟场景中 NPC 的自主行动

        Parameters
        ----------
        command : str
            以 ! 开头的命令文本。

        Returns
        -------
        str
            命令执行结果。
        """
        if not self.world_builder:
            return "世界构建器未初始化（LLM 未连接）"

        parts = command.split(maxsplit=1)
        if len(parts) < 2:
            return (
                "用法：\n"
                "!npc <自然语言描述NPC>\n"
                "!world <自然语言描述世界观>\n"
                "!simulate — 模拟场景NPC自主行动"
            )

        cmd = parts[0].lower()
        desc = parts[1].strip()

        if cmd == "!npc":
            world_ctx = self.world.get("description", "")
            result = self.world_builder.create_npc_from_description(
                description=desc,
                world_context=world_ctx,
                player_name=self.player.name,
            )
            # The WorldBuilder only writes the character card.  Without the two
            # steps below the new NPC exists on disk but is not in the scene and
            # has no Agent, so invoke_npc could never reach it.
            self._admit_new_npcs_to_scene()
            return result

        elif cmd == "!world":
            return self.world_builder.add_world_knowledge(description=desc)

        elif cmd == "!simulate":
            if not self.scene_npcs:
                return "当前场景中没有 NPC 可以模拟行动"

            actions = []
            # 顺序模拟，让后行动的 NPC 知道先行动 NPC 的行为，避免矛盾
            for npc_name in self.scene_npcs[:3]:
                action = self.world_builder.simulate_npc_actions(
                    npc_name=npc_name,
                    scene_context=self.world.get("description", ""),
                    time_of_day=self._time_of_day,
                    weather=self._weather,
                    other_npc_actions=actions,
                )
                if action:
                    actions.append(action)

            if actions:
                return "【世界模拟】\n" + "\n".join(actions)
            return "【世界模拟】场景中 NPC 此刻暂无自主行动"

        else:
            return (
                f"未知命令: {cmd}\n"
                "用法：\n"
                "!npc <自然语言描述NPC>\n"
                "!world <自然语言描述世界观>\n"
                "!simulate — 模拟场景NPC自主行动"
            )

    def _narrate_world_builder_result(self, result: str, original_cmd: str) -> str:
        """把世界构建命令的结果交给 GM Agent 做叙事过渡。

        避免新 NPC/世界观"突然冒出来"的出戏感。
        """
        if not self._llm_available or not self.llm:
            return result

        scene_text = ", ".join(self.scene_npcs) if self.scene_npcs else "无人"

        system = (
            "玩家刚用世界构建能力创造了一个新角色或新知识。请用 1-2 句话自然地将这个变化融入当前场景。\n"
            "如果是新 NPC，描述他/她如何出现在场景中（不要直接说'突然出现'，用合理的方式引入）。\n"
            "如果是世界观知识，描述环境细节如何印证了这一知识。\n"
            "用第二人称，不超过 100 字。\n"
            "以 JSON 格式返回：\n"
            '{\n'
            '  "narration": "叙事过渡文本",\n'
            '  "suggestions": ["建议1", "建议2", "建议3"]\n'
            '}'
        )

        messages = [{
            "role": "user",
            "content": (
                f"当前场景NPC：{scene_text}\n"
                f"时间：{self._time_of_day} 天气：{self._weather}\n"
                f"新创建的内容：{result}\n"
                f"玩家的原始指令：{original_cmd}\n\n"
                "请将这个变化自然地融入场景。"
            ),
        }]

        try:
            transition = self.llm.chat_json(system=system, messages=messages)
            narration = transition.get("narration", result)
            suggestions = transition.get("suggestions", [])

            lines = [narration]
            for i, s in enumerate(suggestions, 1):
                lines.append(f"{i}. {s}")
            return "\n\n".join(lines)
        except Exception:
            return result

    # NOTE: automatic world simulation was removed when the two pipelines merged.
    # Autonomous NPC behaviour now lives in _arun_npc_tick(), which drives the real
    # NPC Agents (personality + memory) instead of the WorldBuilder's flat LLM call,
    # and runs identically for CLI and web.  `!simulate` still triggers it manually.

    def _fallback_response(self, user_input: str) -> str:
        """Generate a simple response when LLM is unavailable."""
        # Check for dice expressions
        m = _DICE_EXPR_RE.search(user_input)
        if m:
            count_str, sides_str, mod_str = m.groups()
            count = int(count_str) if count_str else 1
            sides = int(sides_str)
            modifier = int(mod_str) if mod_str else 0
            expr = f"{count}d{sides}"
            if modifier:
                expr += f"+{modifier}"
            try:
                results, total = roll(expr)
                parts = [f"投掷 {expr} = {' + '.join(str(r) for r in results)}"]
                if modifier:
                    parts.append(f" + {modifier}")
                parts.append(f" = {total}")
                return "".join(parts)
            except ValueError:
                pass

        # Check for info query
        if any(kw in user_input for kw in ["查看", "属性", "状态", "角色卡"]):
            state = self.player_state.get_state()
            return (
                f"{self.player.summary()}\n\n"
                f"【当前状态】\n"
                f"HP: {state['hp']}/{state['max_hp']}\n"
                f"情绪：{state['emotion']}\n"
                f"信任度：{state['trust']}\n"
                f"体力：{state['stamina']}"
            )

        return f"你站在{self.world.get('name', '未知世界')}中，四周弥漫着雾气。冒险等待着你..."

    def _write_npc_memory(self, npc_name: str, raw_interaction: str) -> None:
        """Extract and persist NPC interaction memory using LLM."""
        if not self._llm_available or not self.llm:
            return
        try:
            extracted = self.llm.extract_memory(raw_interaction)
            if not extracted:
                return
            npc_mem_id = self.memory.npc_add(
                content=extracted,
                context={"emotion": self.player_state.get_state()["emotion"]},
                npc_name=npc_name,
            )
            # Link to similar main memories (cross-collection linking)
            similar = self.memory.search(extracted, n=2)
            for s in similar:
                if s.get("id") != npc_mem_id:
                    self.memory.link(npc_mem_id, s["id"], "关联到")
            self._debug(f"[记忆] NPC「{npc_name}」: {extracted[:100]}")
        except Exception as e:
            self._debug(f"[记忆] NPC「{npc_name}」写入失败: {e}")

    def _write_memory(
        self, user_input: str, narration: str, tool_results: List[str]
    ) -> str | None:
        """Extract and persist adventure memory. Returns the new memory ID."""
        if not self._llm_available or not self.llm:
            return None

        combined = f"玩家({self.player.name}): {user_input}\n结果: {narration}"
        if tool_results:
            combined += "\n" + "\n".join(tool_results)

        try:
            extracted = self.llm.extract_memory(combined)
            if not extracted:
                return None

            mem_id = self.memory.add(
                content=extracted,
                context={"emotion": self.player_state.get_state()["emotion"]},
            )
            self._recent_events.append(extracted)

            # Link to semantically similar memories
            similar = self.memory.search(extracted, n=3)
            for s in similar:
                if s.get("id") != mem_id:
                    self.memory.link(mem_id, s["id"], "关联到")

            # Link to the most recent memory (temporal chain)
            if len(self._recent_events) >= 2:
                # Find the second-most-recent memory ID by searching for its content
                prev_content = self._recent_events[-2]
                prev_results = self.memory.search(prev_content, n=1)
                if prev_results and prev_results[0].get("id") != mem_id:
                    self.memory.link(mem_id, prev_results[0]["id"], "发生在...之后")

            link_count = len(similar)
            self._debug(f"[记忆] 主: {extracted[:100]}  (链接 {link_count}条)")

            return mem_id
        except Exception:
            return None

    # ------------------------------------------------------------------
    #  Opening scene generation
    # ------------------------------------------------------------------

    def get_npc_detail(self, name: str) -> Optional[Dict[str, Any]]:
        """Full inspection payload for one NPC: card, state, history, memories.

        Backs the NPC panel, which previously could only show HP/emotion.
        """
        npc = self.npc_store.find_by_name(name)
        if npc is None:
            return None

        state = self.npc_store.get_state(name)
        state_dict = state.get_state() if state else {}

        memories: List[str] = []
        try:
            for m in self.memory.npc_full_retrieve(name, name, n=8):
                content = m.get("content") if isinstance(m, dict) else str(m)
                if content:
                    memories.append(content)
        except Exception as e:
            self._debug(f"[NPC] 记忆读取失败 ({name}): {e}")

        personality = npc.personality or {}
        return {
            "name": npc.name,
            "core": list(npc.core or []),
            "attributes": dict(npc.attributes or {}),
            "skills": list(npc.skills or []),
            "relations": dict(npc.relations or {}),
            "personality": {
                "tone": personality.get("tone", ""),
                "verbal_tics": personality.get("verbal_tics", ""),
                "emotion_map": personality.get("emotion_map", {}),
            },
            "state": state_dict,
            "in_scene": name in self.scene_npcs,
            "has_agent": name in self._npc_agents,
            "history": list(self.npc_store.get_history(name)),
            "memories": memories,
        }

    def reroll_opening(self) -> str:
        """Regenerate the opening narration, keeping the world and cast.

        Lets the player reject a bad opening without re-entering the whole
        setup form or paying for a second full reset.
        """
        setup = self._last_setup or {}
        self.history_messages.clear()
        self._recent_events.clear()
        self._last_gm_response = ""
        self._last_suggestions = []
        self._last_npc_messages = []
        self._last_dice_rolls = []
        self._turn_count = 0

        return self.generate_opening(
            custom_worldview=setup.get("worldview", ""),
            custom_npc_setup=setup.get("npc_setup", ""),
            world_options=setup.get("world_options") or {},
            # NPCs already exist on stage; _seed_npc_specs is idempotent.
            npc_specs=setup.get("npc_specs") or [],
        )

    def apply_player_card(self, player_data: Dict[str, Any]) -> None:
        """Replace the player character from a structured card.

        Posted by the start screen so a new game can define the protagonist
        without editing config.yaml.  Rebuilds the state machine because
        max HP is derived from 体质.
        """
        character = Character.from_dict(player_data)
        self.player = character
        self.player_state = StateMachine(max_hp=calc_max_hp(character.attributes))
        self._debug(
            f"[角色] 应用玩家角色卡: {character.name} "
            f"HP {self.player_state.max_hp} 属性 {character.attributes}"
        )

    def _seed_npc_specs(self, npc_specs: List[Dict[str, Any]]) -> None:
        """Create NPCs from structured specs supplied by the start screen.

        Unlike the free-text path, the numbers here come straight from the
        player: any attribute they filled in is used verbatim, and only the
        missing dimensions are inferred by the LLM.  Called before the opening
        prompt so these characters are already on stage.
        """
        for spec in npc_specs:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name") or "").strip()
            if not name:
                continue

            raw_core = spec.get("core") or ""
            if isinstance(raw_core, list):
                core = [str(c).strip() for c in raw_core if str(c).strip()]
            else:
                core = [c.strip() for c in str(raw_core).replace("；", ";").split(";") if c.strip()]
            if not core:
                core = [f"{name} — 起始场景角色"]

            tone = str(spec.get("tone") or "").strip() or "正常"

            existing = self.npc_store.find_by_name(name)
            if existing is not None:
                if name not in self.scene_npcs:
                    self.scene_npcs.append(name)
                continue

            # Player-supplied attributes win; infer only what is missing.
            supplied = spec.get("attributes") or {}
            attributes: Dict[str, int] = {}
            if isinstance(supplied, dict):
                for key, value in supplied.items():
                    try:
                        attributes[str(key)] = max(1, min(20, int(value)))
                    except (TypeError, ValueError):
                        continue
            if len(attributes) < len(_NPC_ATTRIBUTE_KEYS):
                try:
                    inferred = self._generate_npc_attributes(name, core, tone)
                except Exception:
                    inferred = {}
                for key in _NPC_ATTRIBUTE_KEYS:
                    attributes.setdefault(key, int(inferred.get(key, 10)))

            relations: Dict[str, str] = {}
            raw_relations = spec.get("relations")
            if isinstance(raw_relations, dict):
                relations = {str(k): str(v) for k, v in raw_relations.items() if str(k).strip()}
            relation_to_player = str(spec.get("relation") or "").strip()
            if relation_to_player:
                relations[self.player.name] = relation_to_player

            verbal_tics = str(spec.get("verbal_tics") or "").strip() or "无特殊语言习惯"

            try:
                self.npc_store.create(
                    name=name,
                    core=core,
                    attributes=attributes,
                    relations=relations,
                    personality={
                        "tone": tone,
                        "verbal_tics": verbal_tics,
                        "emotion_map": {
                            "calm": f"以{tone}的态度说话",
                            "wary": "警惕地观察",
                            "hostile": "表现出敌意",
                        },
                    },
                )
            except Exception as e:
                self._debug(f"[开场] NPC「{name}」创建失败: {e}")
                continue

            if name not in self.scene_npcs:
                self.scene_npcs.append(name)
            self._debug(f"[开场] 结构化创建 NPC: {name} (语调 {tone}) {attributes}")

        self._sync_npc_agents()

    # ------------------------------------------------------------------
    #  Opening scene generation
    # ------------------------------------------------------------------

    def generate_opening(
        self,
        custom_worldview: str = "",
        custom_npc_setup: str = "",
        world_options: Optional[Dict[str, Any]] = None,
        npc_specs: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Generate the opening scene narration and seed the starting NPCs.

        Parameters
        ----------
        custom_worldview : str
            自定义世界观描述（自然语言），若为空则使用 config.yaml 中的默认设定。
        custom_npc_setup : str
            自定义初始 NPC 描述（自由文本，向后兼容的旧入口）。
        world_options : dict, optional
            结构化世界设定，例如 ``{"climate": "终年薄雾", "era": "蒸汽纪元",
            "tone": "黑暗惊悚", "time_of_day": "黄昏", "weather": "细雨",
            "extra": "自由补充"}``。会拼进世界描述并初始化时间/天气。
        npc_specs : list of dict, optional
            结构化 NPC 定义，每项形如 ``{"name": ..., "core": ...,
            "tone": ..., "relation": ..., "attributes": {"力量": 12, ...}}``。
            这些 NPC 会被**直接建卡**，不经过 LLM 猜测；attributes 缺失的维度
            由 LLM 推断补齐。

        The LLM returns JSON with ``narration`` and ``npcs`` so the world
        begins with interactive characters already present.
        """
        if not self._llm_available or not self.llm:
            world_name = self.world.get("name", "未知世界")
            return (
                f"欢迎来到{world_name}。你是{self.player.name}。\n" f"冒险即将开始..."
            )

        # 优先使用自定义世界观，否则用 config.yaml 默认
        world_options = world_options or {}
        # Remember the setup so reroll_opening() can replay it verbatim.
        self._last_setup = {
            "worldview": custom_worldview,
            "npc_setup": custom_npc_setup,
            "world_options": dict(world_options),
            "npc_specs": list(npc_specs or []),
        }
        if custom_worldview:
            world_desc = custom_worldview.strip()
            # 结构化选项（气候/纪元/基调等）拼进世界描述，使其同样影响
            # 之后每一回合的叙事，而不仅仅是开场。
            option_text = _format_world_options(world_options)
            if option_text:
                world_desc = f"{world_desc}\n\n{option_text}"
            # 同步更新 GM 的世界状态，使之影响之后每一回合的叙事。
            # 名称同样在这里派生，否则 CLI 直接调用本方法时世界名会停留在
            # config.yaml 的旧值（此前只有 /api/start 做了这件事）。
            self.world["description"] = world_desc
            self.world["name"] = (
                str(world_options.get("name") or "").strip()
                or _derive_world_name(custom_worldview)
            )
        else:
            world_desc = self.world.get("description", self.world.get("name", ""))

        # 初始时间/天气：玩家显式指定时覆盖默认的「清晨 / 薄雾」
        if str(world_options.get("time_of_day") or "").strip():
            self._time_of_day = str(world_options["time_of_day"]).strip()
        if str(world_options.get("weather") or "").strip():
            self._weather = str(world_options["weather"]).strip()

        # 结构化 NPC 先建卡，这样 LLM 写开场时它们已经在场，
        # 且属性完全按玩家填的数值，不被 LLM 猜测覆盖。
        if npc_specs:
            self._seed_npc_specs(npc_specs)

        player_desc = "，".join(self.player.core)
        player_card = self.player.summary()

        # Only setting-specific character cards define a pathway/sequence; a
        # generic config.yaml must not get "途径：未知，序列9" injected into
        # every opening prompt.
        pathway = getattr(self.player, "pathway", "")
        if pathway:
            pathway_line = f"途径：{pathway}，序列{getattr(self.player, 'sequence', 9)}\n"
        else:
            pathway_line = ""

        # Check for an active scenario.  Skipped when the player supplied their
        # own worldview: otherwise the bundled scenario (black cat / Marsha /
        # Lily) bleeds into a completely unrelated custom setting.
        if custom_worldview:
            scenario_text = ""
            self._debug("[开场] 使用自定义世界观，跳过内置剧本检索")
        else:
            scenario_knowledge = self.knowledge.query(
                "剧本 节点0 开场 核心事件", self.player.name
            )
            scenario_text = "\n".join(scenario_knowledge[:3]) if scenario_knowledge else ""

        # Query for any existing memories (returning player)
        memories = self.memory.search("冒险 故事 经历", n=5)
        memory_text = ""
        if memories:
            memory_text = "\n".join(f"- {m['content']}" for m in memories[:5])

        # List NPCs that are already registered and on stage.  Structured
        # start-screen NPCs are created before this prompt, so the LLM must be
        # told to feature them rather than re-invent them in its `npcs` array.
        existing_npc_text = ""
        if self.scene_npcs:
            roster = []
            for name in self.scene_npcs:
                npc = self.npc_store.find_by_name(name)
                desc = "；".join(npc.core[:2]) if npc and npc.core else ""
                tone = (npc.personality or {}).get("tone", "") if npc else ""
                detail = " / ".join(x for x in (desc, f"语调：{tone}" if tone else "") if x)
                roster.append(f"- {name}" + (f"（{detail}）" if detail else ""))
            existing_npc_text = (
                "\n以下 NPC 已注册并在场，请在开场中自然地让他们登场，"
                "**不要把他们再放进 npcs 数组**：\n" + "\n".join(roster)
            )

        # 自定义 NPC 描述（自然语言）
        npc_instruction = ""
        if custom_npc_setup:
            npc_instruction = (
                f"\n\n玩家自定义了以下初始角色，请解析并创建：\n{custom_npc_setup}\n"
                f"请从中提取有名字、有身份的角色，注册为 NPC 并加入场景。"
            )

        if scenario_text:
            system = (
                f"你是 TRPG 主持人。\n"
                f"世界设定：{world_desc}\n\n"
                f"玩家扮演 {self.player.name}，{player_desc}\n"
                f"{pathway_line}"
                f"以下剧本已加载，请严格按照剧本节点0生成开场：\n{scenario_text}\n\n"
                f'以 JSON 格式回复：\n'
                f'{{"narration": "开场叙述（不超过200字）", '
                f'"npcs": [{{"name": "NPC称呼", "core": ["角色描述"], '
                f'"relations": {{"{self.player.name}": "关系"}}}}], '
                f'"suggestions": ["建议1", "建议2", "建议3"]}}\n\n'
                f"要求：\n"
                f"- 严格按照剧本节点0的核心事件生成开场\n"
                f"- 只注册当前物理在场的角色。任务目标、传闻人物只在 narration 提及即可\n"
                f"{existing_npc_text}"
                f"{npc_instruction}"
            )
        else:
            system = (
                f"你是 TRPG 主持人。\n"
                f"世界设定：{world_desc}\n\n"
                f"玩家扮演 {self.player.name}，{player_desc}\n"
                f"{pathway_line}\n"
                f"请设计冒险的开场场景，并以 JSON 格式回复：\n\n"
                f'{{"narration": "开场叙述（不超过200字）", '
                f'"npcs": [{{"name": "NPC称呼", "core": ["角色描述"], '
                f'"relations": {{"{self.player.name}": "关系"}}}}], '
                f'"suggestions": ["建议1", "建议2", "建议3"]}}\n\n'
                f"要求：\n"
                f"- 用第二人称「你」称呼玩家\n"
                f"- 简要设定场景，1-2句环境描写后迅速引入NPC和事件线索\n"
                f"- 只注册当前物理在场的角色。任务目标、传闻人物只在 narration 提及即可\n"
                f"{existing_npc_text}"
                f"{npc_instruction}"
            )

        if memory_text:
            system += f"\n以下是之前的冒险记录，请据此生成连续的开场：\n{memory_text}"

        messages = [{"role": "user", "content": "请开始冒险。"}]

        try:
            result = self.llm.chat_json(system=system, messages=messages)

            narration = result.get("narration", "")
            npcs = result.get("npcs", [])
            suggestions = result.get("suggestions", [])

            # Create seed NPCs
            # Cap the cast: an over-eager model returns a whole tavern's worth
            # of extras, which then each get their own Agent and memory store.
            seed_budget = max(0, _MAX_OPENING_NPCS - len(self.scene_npcs))
            for npc_data in npcs:
                if seed_budget <= 0:
                    self._debug(
                        f"[开场] 达到 NPC 上限 {_MAX_OPENING_NPCS}，忽略其余返回项"
                    )
                    break
                name = npc_data.get("name", "")
                core = npc_data.get("core", [])
                relations = npc_data.get("relations", {})

                if not name or not isinstance(name, str) or not name.strip():
                    continue
                name = name.strip()

                # Never re-create a character that is already on stage; the
                # prompt forbids it but the model still does it.
                if self.npc_store.find_by_name(name) is not None:
                    self._debug(f"[开场] 已存在的 NPC，跳过: {name}")
                    continue

                if not isinstance(core, list) or not core:
                    core_text = npc_data.get("core", "")
                    core = (
                        [core_text]
                        if isinstance(core_text, str) and core_text.strip()
                        else [f"{name} — 起始场景角色"]
                    )

                if not isinstance(relations, dict):
                    relations = {}

                existing = self.npc_store.find_by_name(name)
                if existing is not None:
                    if name not in self.scene_npcs:
                        self.scene_npcs.append(name)
                    continue

                # Determine personality_tone from core description
                tone = "正常"
                if core:
                    first_core = str(core[0]) if core else ""
                    if any(kw in first_core for kw in ["温柔", "活泼", "开朗"]):
                        tone = "温和友好"
                    elif any(kw in first_core for kw in ["冷酷", "严肃", "沉默"]):
                        tone = "冷淡严肃"
                    elif any(kw in first_core for kw in ["狡猾", "精明", "圆滑"]):
                        tone = "精明圆滑"

                attributes = self._generate_npc_attributes(name, core, tone)

                self.npc_store.create(
                    name=name,
                    core=core,
                    attributes=attributes,
                    relations=relations,
                    personality={
                        "tone": tone,
                        "verbal_tics": "无特殊语言习惯",
                        "emotion_map": {
                            "calm": f"以{tone}的态度说话",
                            "wary": "警惕地观察",
                            "hostile": "表现出敌意",
                        },
                    },
                )
                if name not in self.scene_npcs:
                    self.scene_npcs.append(name)
                seed_budget -= 1

            if self.debug and npcs:
                print(f"[DEBUG] 开局创建 NPC: {[n.get('name') for n in npcs]}")

            # Seed NPCs created above need Agents before the first turn
            self._sync_npc_agents()

            # Treat opening as first GM output — write memory + set context
            self._last_suggestions = suggestions
            self._last_gm_response = narration
            self._recent_events.append(narration)
            self._write_memory("游戏开始", narration, [])

            return narration
        except Exception:
            fallback = f"你站在{world_desc}的边缘，冒险即将开始。"
            self._last_gm_response = fallback
            return fallback

    # ===================================================================
    #  SOFT-DELETED: _parse_opening_response
    #  generate_opening() 现在使用 llm.chat_json() 统一解析。
    # ===================================================================
    #
    # def _parse_opening_response(self, raw: str) -> dict:
    #     """Parse the opening scene JSON response from the LLM."""
    #     ...

    # ------------------------------------------------------------------
    #  NPC lookup helper
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    #  Save / Load
    # ------------------------------------------------------------------
    #  Game over confirmation (called from process() / API, not from SDK tools)
    # ------------------------------------------------------------------

    def _confirm_game_over(self) -> str:
        """Execute actual game-over cleanup after player confirmation."""
        import glob as _glob

        if not self._game_over_pending:
            return "没有待确认的游戏结束。"

        # Delete save file
        if os.path.exists("data/save.json"):
            os.remove("data/save.json")

        # Clear NPC state files
        for f in _glob.glob("data/chroma/npcs/*_state.json"):
            try:
                os.remove(f)
            except Exception:
                pass

        # Clear run-time state
        self.scene_npcs.clear()
        self._time_of_day = "黄昏"
        self._weather = "阴"

        # Clear all memory collections
        try:
            all_ids = self.memory._collection.get()["ids"]
            if all_ids:
                self.memory._collection.delete(ids=all_ids)
        except Exception:
            pass
        try:
            all_npc_ids = self.memory._npc_collection.get()["ids"]
            if all_npc_ids:
                self.memory._npc_collection.delete(ids=all_npc_ids)
        except Exception:
            pass

        self._game_over = True
        self._game_over_pending = False
        cause = self._game_over_cause
        self._game_over_cause = ""
        return f"游戏结束: {cause}。冒险终结。"

    def _cancel_game_over(self) -> str:
        """Cancel a pending game-over, resuming normal gameplay."""
        self._game_over_pending = False
        self._game_over_cause = ""
        return "已取消游戏结束。冒险继续。"

    def new_game_reset(self, use_scenario: bool = True) -> None:
        """Wipe every trace of the previous run so a new game starts clean.

        Without this, starting a new game only cleared the vector memories:
        NPC cards and their state files survived in ChromaDB and came straight
        back on the next start, dragging the previous run's cast along.
        """
        try:
            self.memory.clear()
        except Exception as e:
            self._debug(f"[重置] 记忆清理失败: {e}")

        try:
            self.npc_store.clear_all()
        except Exception as e:
            self._debug(f"[重置] NPC 清理失败: {e}")

        self._npc_agents.clear()
        self.scene_npcs.clear()
        self.history_messages.clear()
        self._recent_events.clear()
        self._last_gm_response = ""
        self._last_suggestions = []
        self._turn_count = 0
        self._last_scene_context = ""
        self._game_over = False
        self._game_over_pending = False
        self._game_over_cause = ""
        self._time_of_day = "清晨"
        self._weather = "薄雾"
        self.player_state = StateMachine(max_hp=calc_max_hp(self.player.attributes))

        # A new game invalidates the old save file
        try:
            if os.path.exists("data/save.json"):
                os.remove("data/save.json")
        except OSError:
            pass

        # Re-seed NPCs from the player's backstory / scenario
        if self._llm_available:
            self._register_backstory_npcs(use_scenario=use_scenario)
            # Freshly seeded NPCs need their own Agents too, otherwise every
            # invoke_npc would silently fall back to the legacy LLM path.
            self._sync_npc_agents()

    # ------------------------------------------------------------------

    def save(self, path: str = "data/save.json", label: str = "") -> None:
        """Save game state for resumption.

        Also records the world/player card so a slot can be described in the
        save list without loading it.
        """
        import datetime

        data = {
            "version": SAVE_VERSION,
            "label": label or self.world.get("name", "冒险"),
            "world": dict(self.world),
            "player_card": {
                "name": self.player.name,
                "core": list(self.player.core),
                "attributes": dict(self.player.attributes),
                "skills": list(self.player.skills),
                "pathway": getattr(self.player, "pathway", ""),
                "sequence": getattr(self.player, "sequence", 9),
                "anchor": getattr(self.player, "anchor", ""),
                "relations": dict(self.player.relations or {}),
            },
            "time_of_day": self._time_of_day,
            "weather": self._weather,
            "player_state": self.player_state.to_dict(),
            "scene_npcs": self.scene_npcs,
            "recent_events": list(self._recent_events),
            "last_gm_response": self._last_gm_response,
            "last_suggestions": list(self._last_suggestions),
            "turn_count": self._turn_count,
            "last_scene_context": self._last_scene_context,
            "updated_at": datetime.datetime.now().isoformat(),
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_save(self, path: str = "data/save.json") -> bool:
        """Load game state from a save file. Returns True if successful."""
        if not os.path.exists(path):
            return False

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Saves written before multi-slot support have no version field; they
        # simply lack the world/player blocks, which stay at current values.
        world = data.get("world")
        if isinstance(world, dict) and world:
            self.world = dict(world)

        card = data.get("player_card")
        if isinstance(card, dict) and card.get("name"):
            try:
                self.player = Character.from_dict(card)
            except ValueError as e:
                self._debug(f"[存档] 角色卡恢复失败，保留当前角色: {e}")

        self._time_of_day = data.get("time_of_day", "黄昏")
        self._weather = data.get("weather", "阴")
        self.scene_npcs = data.get("scene_npcs", [])
        self._recent_events = deque(data.get("recent_events", []), maxlen=10)
        self._last_gm_response = data.get("last_gm_response", "")
        self._last_suggestions = list(data.get("last_suggestions") or [])
        self._turn_count = data.get("turn_count", 0)
        self._last_scene_context = data.get("last_scene_context", "")

        ps = data.get("player_state", {})
        if ps:
            self.player_state = StateMachine.from_dict(ps)

        self._debug(
            f"[DEBUG] 存档加载成功 ({path})\n"
            f"  场景: {self.scene_npcs}\n"
            f"  时间: {self._time_of_day} 天气: {self._weather}"
        )

        # Ensure NPC Agents exist for restored NPCs
        self._sync_npc_agents()

        return True

    def generate_continuation(self) -> str:
        """Generate a continuation narration after loading a save."""
        if not self._llm_available or not self.llm:
            return "你回到了之前冒险的地方。"

        scene_text = ", ".join(self.scene_npcs) if self.scene_npcs else "无人"
        player_card = self.player.summary()

        system = (
            "玩家刚刚重新进入游戏。根据以下存档信息，生成一段简短的续接叙述（不超过100字），"
            "用第二人称描述玩家当前所处的环境和身边的NPC。不要引入新事件，只做状态回顾。"
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"时间: {self._time_of_day} 天气: {self._weather}\n"
                    f"场景NPC: {scene_text}\n"
                    f"最近事件: {'; '.join(list(self._recent_events)[-3:])}\n"
                    f"{player_card}\n\n"
                    "请续接。"
                ),
            }
        ]

        try:
            result = self.llm.chat_json(system=system, messages=messages)
            return result.get("narration", "你环顾四周，回忆着之前的经历。")
        except Exception:
            return "你站在之前离开的地方，一切如旧。"

    def _find_npc_in_input(self, user_input: str) -> Optional[str]:
        """Find an NPC name mentioned in user input."""
        for npc_name in self.scene_npcs:
            if npc_name in user_input:
                return npc_name
        # Also search all known NPCs
        for npc in self.npc_store.all():
            if npc.name in user_input:
                return npc.name
        return None


# ===================================================================
#  SOFT-DELETED: keyword-based state update
#  状态触发现在由 LLM GM Agent 在对话中判断。
# ===================================================================
#
# def _update_state_from_keywords(self, text: str) -> List[str]:
#     """在 *text* 中搜索状态触发关键词并更新状态机。"""
#     ...


_NPC_ATTRIBUTE_KEYS = ("力量", "敏捷", "体质", "智力", "感知", "魅力")

# Hard ceiling on how many characters the opening may put on stage.  Each NPC
# costs its own Agent, state file and vector collection, and an over-eager
# model will happily populate a whole tavern.
_MAX_OPENING_NPCS = 6

_WORLD_OPTION_LABELS = (
    ("era", "纪元 / 时代"),
    ("climate", "气候"),
    ("tone", "叙事基调"),
    ("tech", "技术水平"),
    ("magic", "超自然程度"),
    ("danger", "危险程度"),
    ("extra", "补充设定"),
)


def _format_world_options(options: Dict[str, Any]) -> str:
    """Render structured world options into a prompt-friendly block."""
    if not options:
        return ""
    lines = []
    for key, label in _WORLD_OPTION_LABELS:
        value = str(options.get(key) or "").strip()
        if value:
            lines.append(f"- {label}：{value}")
    return "【世界细节】\n" + "\n".join(lines) if lines else ""


def _derive_world_name(description: str) -> str:
    """Pick a short display name out of a free-form worldview description.

    Players write a paragraph, not a title, so the first clause up to the
    first separator is used rather than the whole opening sentence.
    """
    text = (description or "").replace("\n", " ").strip()
    if not text:
        return "自定义世界"
    # Stop at the first clause separator, then fall back to a hard length cap.
    for sep in ("，", ",", "。", "、", "—", "－", "-", "：", ":"):
        idx = text.find(sep)
        if 0 < idx <= 20:
            return text[:idx].strip()
    return text[:20].strip()


def _strip_duplicated_npc_lines(narration: str, npc_messages: list[dict]) -> str:
    """Remove NPC dialogue the Narrator copied verbatim into the prose.

    NPC lines are already rendered as their own chat bubbles, so repeating them
    in the narration shows the player the same sentence twice.  The prompt
    forbids it, but an LLM instruction is probabilistic; this is the
    deterministic backstop.

    Only quoted spans that match an actual NPC line are dropped, so ordinary
    narration containing quotation marks is left alone.
    """
    if not narration or not npc_messages:
        return narration

    _skip = set(" \t\r\n，。！？、…—～\"'“”「」『』（）()《》,.!?~-")

    def _norm(text: str) -> str:
        # Compare on content alone: punctuation and whitespace vary between
        # the NPC output and the Narrator's re-quoting of it.
        return "".join(ch for ch in text if ch not in _skip)

    spoken = [_norm(m.get("text", "")) for m in npc_messages]
    spoken = [s for s in spoken if len(s) >= 6]
    if not spoken:
        return narration

    def _is_duplicate(inner: str) -> bool:
        norm = _norm(inner)
        if len(norm) < 6:
            return False
        for line in spoken:
            # Either side may be a subset: the Narrator often quotes a fragment.
            if norm in line or line in norm:
                return True
            overlap = sum(1 for ch in set(norm) if ch in line)
            if overlap / max(len(set(norm)), 1) > 0.8:
                return True
        return False

    # Drop quoted spans that reproduce an NPC line.
    cleaned = re.sub(
        r"[「“\"']([^「」“”\"']{4,120})[」”\"']",
        lambda m: "" if _is_duplicate(m.group(1)) else m.group(0),
        narration,
    )

    # Also drop unquoted verbatim copies: the Narrator often writes
    # "马文说：东边老磨坊，夜里总有光。" with no quotation marks at all.
    for msg in npc_messages:
        line = (msg.get("text") or "").strip()
        if len(_norm(line)) < 6:
            continue
        if line in cleaned:
            cleaned = cleaned.replace(line, "")
            continue
        # Fall back to the longest verbatim run we can find in the prose.
        removed = _remove_longest_verbatim(cleaned, line)
        if removed is not None:
            cleaned = removed

    # Tidy up the punctuation left behind by a removed quote.
    cleaned = re.sub(r"[：:，,]\s*(?=[。！？\n]|$)", "", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"^[\s，。、：:]+", "", cleaned).strip()

    # Never return an empty narration just because it was all quotes.
    return cleaned or narration


def _remove_longest_verbatim(narration: str, line: str) -> str | None:
    """Delete the longest verbatim run of *line* found inside *narration*.

    Punctuation and whitespace are treated as insignificant: the comparison is
    done on a normalised copy and the matched span is then mapped back onto the
    raw text.  Returns ``None`` when no meaningful run (>= 10 normalised chars)
    matches.
    """
    skip = set(" \t\r\n，。！？、…—～\"'“”「」『』（）()《》,.!?~-")

    def norm(s: str) -> str:
        return "".join(ch for ch in s if ch not in skip)

    target = norm(line)
    if len(target) < 6:
        return None

    # (raw_index, normalised_char) for every kept character of the narration.
    pairs = [(i, ch) for i, ch in enumerate(narration) if ch not in skip]
    flat = "".join(ch for _, ch in pairs)
    if not flat:
        return None

    # Prefer the longest run, shrinking down to 10 normalised characters.
    for size in range(len(target), 9, -1):
        for start in range(0, len(target) - size + 1):
            fragment = target[start:start + size]
            pos = flat.find(fragment)
            if pos < 0:
                continue
            raw_start = pairs[pos][0]
            raw_end = pairs[pos + size - 1][0] + 1
            return narration[:raw_start] + narration[raw_end:]
    return None


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences for the streaming effect.

    ``re.split`` with a capturing group yields an odd-length list whenever the
    text does not end with a delimiter.  The loop below already appends that
    trailing fragment, so the previous extra append duplicated the final
    sentence for every narration that ended without punctuation.
    """
    parts = re.split(r'([。！？；\n]+)', text)
    sentences = []
    for i in range(0, len(parts), 2):
        sentence = parts[i]
        if i + 1 < len(parts):
            sentence += parts[i + 1]
        if sentence.strip():
            sentences.append(sentence)
    if not sentences:
        sentences = [text]
    return sentences
