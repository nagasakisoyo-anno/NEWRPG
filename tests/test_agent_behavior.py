"""Agent behavior regression tests — prompt validation, tool selection, response parsing.

These tests focus on regression safety: verifying that prompt templates,
tool functions, and response parsing behave correctly without requiring
a live LLM connection. Mock LLMs are used where needed.
"""

import json
import os
import types


from trpg_agent.agent_config import GameContext
from trpg_agent.event_stream import TOOL_DISPLAY_NAMES, make_tool_event, StatusEvent
from trpg_agent.orchestrator import (
    ORCHESTRATOR_SYSTEM_PROMPT,
    build_orchestrator_instructions,
)
from trpg_agent.narrator import NARRATOR_SYSTEM_PROMPT


def tool_names(tools) -> set[str]:
    """Collect tool names from either real SDK objects or bare functions.

    The real ``@function_tool`` decorator returns a ``FunctionTool`` carrying
    a ``.name``; test doubles may leave the plain function in place, which
    only has ``__name__``.  Checking both keeps these assertions honest under
    the real SDK instead of silently passing on a stub.
    """
    names = set()
    for t in tools:
        name = getattr(t, "name", None) or getattr(t, "__name__", None)
        names.add(name or str(t))
    return names


# ===================================================================
# Prompt template tests
#
# The legacy single-GM-Agent prompt is gone: the Orchestrator owns routing
# discipline and the Narrator owns narrative discipline.  These tests assert
# that no rule from the old GM prompt was lost in the merge.
# ===================================================================


class TestOrchestratorRoutingPrompt:
    """Routing discipline inherited from the old GM prompt."""

    def test_prompt_contains_required_sections(self):
        required = [
            "## 路由规则",
            "## 执行顺序",
            "## 在场约束",
            "invoke_judge",
            "invoke_narrator",
            "invoke_npcs",
        ]
        for keyword in required:
            assert keyword in ORCHESTRATOR_SYSTEM_PROMPT, f"missing: {keyword}"

    def test_prompt_forbids_moral_judgment(self):
        assert "禁止道德评判" in ORCHESTRATOR_SYSTEM_PROMPT

    def test_prompt_keeps_rest_as_only_heal(self):
        """take_rest must stay the only HP recovery route."""
        assert "take_rest" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "凭空回血" in ORCHESTRATOR_SYSTEM_PROMPT

    def test_prompt_forbids_speaking_for_npcs(self):
        assert "严禁你自己替NPC说话" in ORCHESTRATOR_SYSTEM_PROMPT

    def test_prompt_requires_knowledge_lookup_before_inventing(self):
        assert "search_knowledge" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "凭空编造" in ORCHESTRATOR_SYSTEM_PROMPT

    def test_build_orchestrator_instructions_fills_placeholders(self):
        result = build_orchestrator_instructions(
            time_of_day="正午",
            weather="晴",
            scene_npcs="老酒保",
            world_setting="TestWorld",
            player_name="TestPlayer",
            player_card="TestCard",
        )
        assert "{world_setting}" not in result
        assert "{player_name}" not in result
        assert "{scene_npcs}" not in result
        assert "TestWorld" in result
        assert "TestPlayer" in result
        assert "TestCard" in result
        assert "正午" in result


class TestNarratorDisciplinePrompt:
    """Narrative discipline inherited from the old GM prompt."""

    def test_output_format_constraint(self):
        assert '"narration"' in NARRATOR_SYSTEM_PROMPT
        assert '"suggestions"' in NARRATOR_SYSTEM_PROMPT
        assert "json" in NARRATOR_SYSTEM_PROMPT.lower()

    def test_includes_pacing_rules(self):
        assert "每次只推进一小步" in NARRATOR_SYSTEM_PROMPT
        assert "信息分层揭示" in NARRATOR_SYSTEM_PROMPT

    def test_includes_self_check(self):
        assert "输出前自检" in NARRATOR_SYSTEM_PROMPT

    def test_forbids_dice_numbers_and_player_puppeting(self):
        assert "骰子数值" in NARRATOR_SYSTEM_PROMPT
        assert "不要替玩家角色说话" in NARRATOR_SYSTEM_PROMPT


# ===================================================================
# Token estimation tests
# ===================================================================


class TestTokenEstimation:
    """Test the _estimate_tokens static method."""

    def test_pure_chinese(self):
        from trpg_agent.game_master import GameMaster
        # 4 Chinese chars ~= 2 tokens
        tokens = GameMaster._estimate_tokens("你好世界")
        assert 1 <= tokens <= 4

    def test_pure_english(self):
        from trpg_agent.game_master import GameMaster
        # "hello world" (11 chars) ~= 2-3 tokens
        tokens = GameMaster._estimate_tokens("hello world")
        assert 1 <= tokens <= 5

    def test_empty_string(self):
        from trpg_agent.game_master import GameMaster
        assert GameMaster._estimate_tokens("") == 0

    def test_mixed_text(self):
        from trpg_agent.game_master import GameMaster
        tokens = GameMaster._estimate_tokens("你好world")
        assert tokens >= 1


# ===================================================================
# JSON response parsing tests
# ===================================================================


class TestResponseParsing:
    """Test that the GM's JSON response parsing handles various formats.

    These tests verify the extraction logic in process() and process_streaming()
    by testing the core patterns directly.
    """

    @staticmethod
    def _parse_gm_response(text: str) -> dict:
        """Run the REAL extractor used by all three pipelines.

        This used to be a hand-copied duplicate of game_master's parsing, so the
        tests kept passing while production silently drifted.
        """
        from trpg_agent.game_master import GameMaster

        narration, suggestions = GameMaster._parse_gm_output(text)
        return {"narration": narration, "suggestions": suggestions}

    def test_parse_valid_json(self):
        result = self._parse_gm_response(
            '{"narration": "门打开了。", "suggestions": ["进去", "离开", "敲门"]}'
        )
        assert result["narration"] == "门打开了。"
        assert len(result["suggestions"]) == 3

    def test_parse_json_with_code_block(self):
        result = self._parse_gm_response(
            '```json\n{"narration": "风吹过树林。", "suggestions": ["前行", "返回", "观察"]}\n```'
        )
        assert result["narration"] == "风吹过树林。"
        assert len(result["suggestions"]) == 3

    def test_parse_json_with_extra_text(self):
        result = self._parse_gm_response(
            'I think the scene is ready.\n\n{"narration": "酒馆里很安静。", "suggestions": ["点酒", "离开", "交谈"]}\n\nThat should work.'
        )
        assert "酒馆" in result["narration"]
        assert len(result["suggestions"]) == 3

    def test_fallback_extracts_numbered_suggestions(self):
        """When JSON is completely broken, extract numbered lines as suggestions."""
        text = "酒馆很热闹。\n1. 走向吧台\n2. 观察角落\n3. 和老者搭话"
        result = self._parse_gm_response(text)
        assert len(result["suggestions"]) >= 1

    def test_narration_is_not_empty(self):
        result = self._parse_gm_response(
            '{"narration": "阳光洒在石板上。", "suggestions": ["前进", "等待", "搜索"]}'
        )
        assert result["narration"] != ""


# ===================================================================
# Tool availability tests
# ===================================================================


class TestToolAvailability:
    """Verify all expected tools are importable and properly decorated."""

    def test_all_tools_importable(self):
        """Every tool should be importable from trpg_agent.tools."""
        tool_names = [
            "roll_dice", "difficulty_check", "skill_check", "combat_attack",
            "take_rest", "get_player_state", "get_npc_state",
            "create_npc", "invoke_npc", "invoke_npcs",
            "remove_npc", "set_scene", "game_over",
            "search_knowledge", "search_memory",
            "invoke_judge", "invoke_narrator", "broadcast_event",
        ]
        for name in tool_names:
            __import__("trpg_agent.tools", fromlist=[name])

    def test_core_tools_present(self):
        """Verify every tool the single pipeline depends on exists."""
        from trpg_agent import tools as tmod
        expected = [
            "roll_dice", "difficulty_check", "skill_check", "combat_attack",
            "take_rest", "get_player_state", "get_npc_state",
            "create_npc", "invoke_npc", "invoke_npcs",
            "remove_npc", "set_scene", "game_over",
            "search_knowledge", "search_memory", "clear_tool_cache",
            "invoke_judge", "invoke_narrator", "broadcast_event",
        ]
        for name in expected:
            assert hasattr(tmod, name), f"tools.py missing: {name}"

    def test_judge_owns_take_rest(self):
        """take_rest is the only HP recovery path, so the Judge must own it.

        Regression: the retired GM Agent never registered take_rest, so the
        web pipeline could not heal at all.
        """
        from trpg_agent.judge import create_judge_agent
        names = tool_names(create_judge_agent().tools)
        assert "take_rest" in names, names

    def test_orchestrator_is_router_only(self):
        """The Orchestrator must route, not roll dice itself."""
        from trpg_agent.orchestrator import create_orchestrator_agent
        from trpg_agent import tools as tmod

        agent = create_orchestrator_agent(tools=[
            tmod.invoke_judge, tmod.invoke_narrator, tmod.broadcast_event,
            tmod.invoke_npc, tmod.invoke_npcs,
            tmod.search_memory, tmod.search_knowledge,
            tmod.create_npc, tmod.remove_npc, tmod.set_scene,
            tmod.get_player_state, tmod.get_npc_state, tmod.game_over,
        ])
        names = tool_names(agent.tools)
        for mechanic in ("roll_dice", "skill_check", "difficulty_check",
                         "combat_attack", "take_rest"):
            assert mechanic not in names, f"Orchestrator must not own {mechanic}"
        assert "invoke_judge" in names


class TestLegacyPipelineRemoved:
    """The old single-GM-Agent pipeline must stay gone."""

    def test_agent_config_exports_no_gm_agent(self):
        import trpg_agent.agent_config as ac
        for gone in ("create_gm_agent", "build_gm_instructions",
                     "create_tool_advisor", "_GM_SYSTEM_PROMPT"):
            assert not hasattr(ac, gone), f"legacy symbol still present: {gone}"

    def test_game_master_has_single_pipeline(self):
        from trpg_agent.game_master import GameMaster
        for gone in ("_process_multi_agent", "_run_tool_advisor",
                     "_run_tool_advisor_async", "_maybe_trigger_world_simulation"):
            assert not hasattr(GameMaster, gone), f"legacy method present: {gone}"
        for required in ("_pre_dispatch", "_record_turn", "_resolve_narration",
                         "_awrite_turn_memories", "_arun_npc_tick"):
            assert hasattr(GameMaster, required), f"missing: {required}"

    def test_model_name_is_configurable(self):
        """deepseek-v4-flash was retired; the name must not be hardcoded."""
        import trpg_agent.agent_config as ac
        import trpg_agent.llm as llm_mod
        assert ac.DEFAULT_MODEL == os.environ.get("TRPG_MODEL", "deepseek-chat")
        assert llm_mod.DEFAULT_MODEL == ac.DEFAULT_MODEL

    def test_llm_exposes_async_api(self):
        """The SSE path must never block the event loop on a sync HTTP call."""
        from trpg_agent.llm import LLM
        for meth in ("achat", "achat_json", "aextract_memory"):
            assert hasattr(LLM, meth), f"LLM missing async {meth}"

    def test_world_builder_npc_is_admitted_to_scene(self):
        """`!npc` must put the new NPC in the scene and give it an Agent.

        Regression: WorldBuilder only wrote the character card, so a freshly
        created NPC was absent from scene_npcs and _npc_agents and every
        invoke_npc against it answered "找不到 NPC".
        """
        from trpg_agent.game_master import GameMaster

        assert hasattr(GameMaster, "_admit_new_npcs_to_scene")

        gm = GameMaster.__new__(GameMaster)
        gm.scene_npcs = []
        gm._npc_agents = {}
        gm._debug_log = []
        gm.debug = False
        gm._llm_available = False  # skips Agent creation, keeps scene admission

        class _NPC:
            def __init__(self, name):
                self.name = name

        class _Store:
            def all(self):
                return [_NPC("马文"), _NPC("艾莉")]

        gm.npc_store = _Store()
        gm._admit_new_npcs_to_scene()
        assert gm.scene_npcs == ["马文", "艾莉"], gm.scene_npcs

        # Idempotent: a second call must not duplicate entries.
        gm._admit_new_npcs_to_scene()
        assert gm.scene_npcs == ["马文", "艾莉"], gm.scene_npcs


class TestOpeningNpcCap:
    """The opening must not invent or duplicate cast members."""

    def test_cap_constant_exists(self):
        from trpg_agent.game_master import _MAX_OPENING_NPCS
        assert 1 <= _MAX_OPENING_NPCS <= 8

    def test_existing_npc_skipped_in_opening(self):
        """Regression: an already-seeded NPC was re-created from the LLM reply."""
        import inspect
        from trpg_agent.game_master import GameMaster
        src = inspect.getsource(GameMaster.generate_opening)
        assert "find_by_name(name) is not None" in src
        assert "跳过" in src

    def test_seed_budget_decrements(self):
        import inspect
        from trpg_agent.game_master import GameMaster
        src = inspect.getsource(GameMaster.generate_opening)
        assert "_MAX_OPENING_NPCS" in src
        assert "seed_budget -= 1" in src


class TestNpcLineDeduplication:
    """NPC lines are already shown as their own bubbles; the Narrator must not
    repeat them in the prose.  Regression for the duplicated reply in the UI."""

    NPC = [{"name": "马文", "text": "怪事？东边老磨坊，夜里总有光。没人敢去。"}]

    def test_quoted_repeat_is_removed(self):
        from trpg_agent.game_master import _strip_duplicated_npc_lines
        out = _strip_duplicated_npc_lines(
            "马文把杯子推过来，低声说：「东边老磨坊，夜里总有光。没人敢去。」窗外雾气漫过街石。",
            self.NPC,
        )
        assert "老磨坊" not in out
        assert "窗外雾气漫过街石" in out

    def test_unquoted_repeat_is_removed(self):
        from trpg_agent.game_master import _strip_duplicated_npc_lines
        out = _strip_duplicated_npc_lines(
            "马文擦着杯子说：东边老磨坊，夜里总有光。没人敢去。", self.NPC
        )
        assert "老磨坊" not in out

    def test_normal_narration_untouched(self):
        from trpg_agent.game_master import _strip_duplicated_npc_lines
        text = "马文把擦净的杯子搁在吧台上，给你推来一杯麦酒。角落里的争论声压得更低了。"
        assert _strip_duplicated_npc_lines(text, self.NPC) == text

    def test_unrelated_quote_untouched(self):
        from trpg_agent.game_master import _strip_duplicated_npc_lines
        text = "旁边有人嘟囔「这鬼天气」。马文没抬头。"
        assert _strip_duplicated_npc_lines(text, self.NPC) == text

    def test_no_npc_messages_is_noop(self):
        from trpg_agent.game_master import _strip_duplicated_npc_lines
        text = "马文低声道：「东边老磨坊有光。」"
        assert _strip_duplicated_npc_lines(text, []) == text

    def test_never_returns_empty(self):
        """If the prose was nothing but the NPC line, keep the original."""
        from trpg_agent.game_master import _strip_duplicated_npc_lines
        text = "「东边老磨坊，夜里总有光。没人敢去。」"
        out = _strip_duplicated_npc_lines(text, self.NPC)
        assert out.strip(), "narration must never become empty"

    def test_both_pipelines_apply_it(self):
        """The sync and streaming paths must both run the dedupe step."""
        import inspect
        from trpg_agent.game_master import GameMaster
        for fn in (GameMaster.process, GameMaster.process_streaming):
            assert "_strip_duplicated_npc_lines" in inspect.getsource(fn), fn.__name__

    def test_narrator_prompt_forbids_repeating_npc_lines(self):
        from trpg_agent.narrator import NARRATOR_SYSTEM_PROMPT
        assert "不要把台词再抄一遍" in NARRATOR_SYSTEM_PROMPT or \
               "不要复述 NPC 的台词" in NARRATOR_SYSTEM_PROMPT


class TestCredentialLoading:
    """Key precedence: real env vars win over .env, and nothing leaks to logs."""

    def test_env_var_wins_over_dotenv(self, monkeypatch):
        from trpg_agent import config as cfg
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-environment")
        assert cfg.load_credentials() == cfg.KEY_SOURCE_ENV
        assert os.environ["DEEPSEEK_API_KEY"] == "sk-from-environment"

    def test_reports_missing_key(self, monkeypatch, tmp_path):
        from trpg_agent import config as cfg
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)  # no .env here
        assert cfg.load_credentials() == cfg.KEY_SOURCE_NONE
        assert cfg.has_api_key() is False
        assert "未配置" in cfg.describe()

    def test_dotenv_used_when_env_absent(self, monkeypatch, tmp_path):
        from trpg_agent import config as cfg
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        (tmp_path / ".env").write_text(
            "DEEPSEEK_API_KEY=sk-from-dotenv\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        assert cfg.load_credentials() == cfg.KEY_SOURCE_DOTENV
        assert os.environ["DEEPSEEK_API_KEY"] == "sk-from-dotenv"

    def test_key_is_masked_everywhere(self, monkeypatch):
        """The secret must never appear in diagnostics."""
        from trpg_agent import config as cfg
        secret = "sk-0123456789abcdefSECRET"
        monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
        cfg.load_credentials()
        for text in (cfg.masked_key(), cfg.describe(), json.dumps(cfg.status())):
            assert secret not in text, text
        assert "***" in cfg.masked_key()

    def test_status_shape(self, monkeypatch):
        from trpg_agent import config as cfg
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-abcdefghijklmnop")
        cfg.load_credentials()
        st = cfg.status()
        for key in ("has_api_key", "key_source", "key_preview", "model", "base_url"):
            assert key in st

    def test_empty_key_does_not_clobber_env(self, monkeypatch):
        """api_server passes os.environ.get(..., '') — it must not wipe the key."""
        import inspect
        from trpg_agent.game_master import GameMaster
        src = inspect.getsource(GameMaster.__init__)
        assert "if llm_api_key:" in src, "empty string would overwrite a real key"

    def test_llm_failure_is_logged_not_swallowed(self):
        import inspect
        from trpg_agent.game_master import GameMaster
        src = inspect.getsource(GameMaster.__init__)
        assert "except (RuntimeError, Exception)" not in src
        assert "兜底模式" in src


class TestPlayerCardFromDict:
    """The start screen posts a structured player card instead of YAML."""

    def test_builds_character(self):
        from trpg_agent.character import Character
        c = Character.from_dict({
            "name": "罗恩",
            "core": "破产的钟表学徒；欠着一屁股债",
            "anchor": "守护妹妹",
            "attributes": {"力量": 8, "敏捷": 12, "体质": 14, "智力": 16},
            "skills": [{"name": "观察", "value": 65}],
        })
        assert c.name == "罗恩"
        assert c.core == ["破产的钟表学徒", "欠着一屁股债"]
        assert c.attributes["体质"] == 14
        assert c.skills == [{"name": "观察", "value": 65}]
        assert c.anchor == "守护妹妹"

    def test_clamps_attribute_range(self):
        from trpg_agent.character import Character
        c = Character.from_dict({
            "name": "P", "core": ["x"], "attributes": {"力量": 99, "敏捷": -3},
        })
        assert c.attributes["力量"] == 20
        assert c.attributes["敏捷"] == 1

    def test_raises_instead_of_exiting(self):
        """Must not call sys.exit: an API request has to report the error."""
        import pytest
        from trpg_agent.character import Character
        with pytest.raises(ValueError):
            Character.from_dict({"core": ["x"], "attributes": {"力量": 10}})
        with pytest.raises(ValueError):
            Character.from_dict({"name": "P", "attributes": {"力量": 10}})
        with pytest.raises(ValueError):
            Character.from_dict({"name": "P", "core": ["x"]})

    def test_apply_player_card_rebuilds_hp(self):
        from trpg_agent.game_master import GameMaster
        gm = GameMaster.__new__(GameMaster)
        gm._debug_log = []
        gm.debug = False
        gm.apply_player_card({
            "name": "罗恩", "core": ["学徒"], "attributes": {"体质": 14},
        })
        assert gm.player.name == "罗恩"
        assert gm.player_state.max_hp == 42  # 体质 14 * 3


class TestDiceVisibility:
    """Dice results must reach the UI: the Narrator never prints numbers."""

    @staticmethod
    def _ctx():
        from trpg_agent.agent_config import GameContext
        return GameContext(
            player_state=None, npc_store=None, memory=None, knowledge=None,
            scene_npcs=[], time_of_day="正午", weather="晴",
            player_name="P", player_card="C",
            player_skills=[{"name": "观察", "value": 60}],
        )

    def test_context_has_dice_channel(self):
        assert self._ctx().dice_rolls == []

    def test_skill_check_records_roll(self):
        """Verify via the underlying implementation: the decorated tool is a
        FunctionTool object under the real SDK and is not directly callable."""
        from trpg_agent.tools import _record_roll
        from trpg_agent.check import skill_check as raw_skill_check

        ctx = self._ctx()
        wrapper = types.SimpleNamespace(context=ctx)
        result = raw_skill_check(60, modifier=0)
        _record_roll(
            wrapper, "check", "观察检定",
            f"d100={result['roll']} vs {result['effective_skill']}",
            bool(result["success"]),
        )
        assert len(ctx.dice_rolls) == 1
        entry = ctx.dice_rolls[0]
        assert entry["label"] == "观察检定"
        assert "d100=" in entry["detail"]
        assert isinstance(entry["success"], bool)

    def test_difficulty_check_records_roll(self):
        from trpg_agent.tools import _record_roll
        from trpg_agent.check import difficulty_check as raw_dc

        ctx = self._ctx()
        result = raw_dc(dc=12, modifier=2)
        _record_roll(
            types.SimpleNamespace(context=ctx), "check", "难度检定",
            f"d20={result['roll']}+2 = {result['total']} vs DC12",
            bool(result["success"]),
        )
        assert ctx.dice_rolls[0]["label"] == "难度检定"
        assert "DC12" in ctx.dice_rolls[0]["detail"]

    def test_check_tools_call_record_roll(self):
        """Guard the wiring: every check tool must record a visible roll."""
        import inspect
        from trpg_agent import tools as tmod

        src = inspect.getsource(tmod)
        for fn in ("def skill_check", "def difficulty_check", "def combat_attack",
                   "def take_rest", "def roll_dice"):
            start = src.index(fn)
            body = src[start:start + 2000]
            assert "_record_roll" in body, f"{fn} does not record a dice result"

    def test_record_roll_tolerates_missing_context(self):
        """Judge sub-agent calls must never crash on a bare context."""
        from trpg_agent.tools import _record_roll
        _record_roll(types.SimpleNamespace(context=None), "dice", "x", "y")

    def test_status_event_supports_dice_fields(self):
        from trpg_agent.event_stream import StatusEvent
        ev = StatusEvent(
            type="dice_roll", roll_kind="check", roll_label="观察检定",
            roll_detail="d100=66 vs 50", roll_success=False,
        )
        assert ev.type == "dice_roll"
        assert ev.roll_success is False


class TestSaveSlots:
    """Multiple named saves instead of a single fixed file."""

    def test_slot_path_rejects_traversal(self):
        import pytest
        from trpg_agent import saves
        for bad in ("../evil", "a/b", "", "x" * 50):
            with pytest.raises(ValueError):
                saves.slot_path(bad)

    def test_legacy_slot_maps_to_old_file(self):
        from trpg_agent import saves
        assert saves.slot_path(saves.LEGACY_SLOT) == saves.LEGACY_SAVE

    def test_new_slot_id_is_valid(self):
        from trpg_agent import saves
        assert saves.slot_path(saves.new_slot_id()).endswith(".json")

    def test_save_roundtrip_with_label(self, tmp_path):
        from trpg_agent.game_master import GameMaster, SAVE_VERSION
        from trpg_agent.state import StateMachine
        from trpg_agent.character import Character

        gm = GameMaster.__new__(GameMaster)
        gm._debug_log = []
        gm.debug = False
        gm.world = {"name": "雾锁星港", "description": "钟表城邦"}
        gm.player = Character.from_dict({
            "name": "罗恩", "core": ["学徒"], "attributes": {"体质": 12},
        })
        gm.player_state = StateMachine(max_hp=36)
        gm.scene_npcs = ["格里芬"]
        gm._time_of_day = "深夜"
        gm._weather = "细雨"
        gm._recent_events = __import__("collections").deque(["e1"], maxlen=10)
        gm._last_gm_response = "上一轮叙事"
        gm._last_suggestions = ["a", "b"]
        gm._turn_count = 5
        gm._last_scene_context = "深夜_细雨"

        path = str(tmp_path / "slot.json")
        gm.save(path, label="第一章")

        data = json.loads(open(path, encoding="utf-8").read())
        assert data["version"] == SAVE_VERSION
        assert data["label"] == "第一章"
        assert data["player_card"]["name"] == "罗恩"
        assert data["world"]["name"] == "雾锁星港"
        assert data["last_suggestions"] == ["a", "b"]

    def test_list_saves_reports_metadata(self, tmp_path, monkeypatch):
        from trpg_agent import saves

        monkeypatch.setattr(saves, "SAVES_DIR", str(tmp_path / "saves"))
        monkeypatch.setattr(saves, "LEGACY_SAVE", str(tmp_path / "nope.json"))
        os.makedirs(saves.SAVES_DIR, exist_ok=True)
        with open(os.path.join(saves.SAVES_DIR, "slot-a.json"), "w", encoding="utf-8") as f:
            json.dump({
                "label": "第一章", "updated_at": "2026-01-01T00:00:00",
                "world": {"name": "W"}, "player_card": {"name": "P"},
                "player_state": {"hp": 10, "max_hp": 20}, "turn_count": 3,
            }, f)

        entries = saves.list_saves()
        assert len(entries) == 1
        assert entries[0]["slot"] == "slot-a"
        assert entries[0]["label"] == "第一章"
        assert entries[0]["hp"] == 10


class TestKnowledgeManagement:
    """Pasted documents can be indexed and entries deleted."""

    def test_knowledge_exposes_add_text_and_delete(self):
        from trpg_agent.rag import KnowledgeBase
        assert hasattr(KnowledgeBase, "add_text")
        assert hasattr(KnowledgeBase, "delete")

    def test_add_text_chunks_and_dedupes(self):
        from trpg_agent.rag import KnowledgeBase

        kb = KnowledgeBase.__new__(KnowledgeBase)
        stored = []

        kb.add_knowledge = lambda content, known_by, category: stored.append(content)
        kb.list_all = lambda limit=1000: [{"content": c} for c in stored]

        added = kb.add_text("# 标题\n\n第一段内容。\n\n第二段内容。", known_by="所有人")
        assert added >= 1
        before = len(stored)
        # Re-importing the same text must not duplicate entries.
        kb.add_text("# 标题\n\n第一段内容。\n\n第二段内容。")
        assert len(stored) == before

    def test_add_text_ignores_blank(self):
        from trpg_agent.rag import KnowledgeBase
        kb = KnowledgeBase.__new__(KnowledgeBase)
        assert kb.add_text("   ") == 0


class TestRerollAndNpcDetail:
    def test_gamemaster_exposes_new_methods(self):
        from trpg_agent.game_master import GameMaster
        for meth in ("reroll_opening", "get_npc_detail", "apply_player_card"):
            assert hasattr(GameMaster, meth), meth

    def test_get_npc_detail_missing_returns_none(self):
        from trpg_agent.game_master import GameMaster
        gm = GameMaster.__new__(GameMaster)
        gm.npc_store = types.SimpleNamespace(find_by_name=lambda n: None)
        assert gm.get_npc_detail("无此人") is None


class TestStructuredSetup:
    """Start-screen structured world options and NPC specs."""

    def test_format_world_options_renders_filled_fields_only(self):
        from trpg_agent.game_master import _format_world_options
        out = _format_world_options(
            {"era": "蒸汽纪元", "climate": "终年浓雾", "tone": "", "extra": None}
        )
        assert "蒸汽纪元" in out and "终年浓雾" in out
        assert "叙事基调" not in out

    def test_format_world_options_empty_is_blank(self):
        from trpg_agent.game_master import _format_world_options
        assert _format_world_options({}) == ""

    def test_generate_opening_accepts_structured_params(self):
        import inspect
        from trpg_agent.game_master import GameMaster
        params = inspect.signature(GameMaster.generate_opening).parameters
        assert "world_options" in params
        assert "npc_specs" in params

    def test_seed_npc_specs_uses_supplied_attributes(self):
        """Player-filled attributes must be used verbatim, not re-guessed."""
        from trpg_agent.game_master import GameMaster

        created = {}

        class _Store:
            def find_by_name(self, name):
                return created.get(name)

            def create(self, name, core, attributes, relations=None, personality=None):
                created[name] = types.SimpleNamespace(
                    name=name, core=core, attributes=attributes,
                    relations=relations or {}, personality=personality or {},
                )
                return created[name]

            def all(self):
                return list(created.values())

        gm = GameMaster.__new__(GameMaster)
        gm.npc_store = _Store()
        gm.scene_npcs = []
        gm._npc_agents = {}
        gm._debug_log = []
        gm.debug = False
        gm._llm_available = False
        gm.player = types.SimpleNamespace(name="冒险者")

        gm._seed_npc_specs([
            {
                "name": "格里芬",
                "core": "年迈钟表匠；能听出每块表的病",
                "tone": "沙哑缓慢",
                "relation": "旧识",
                "attributes": {"力量": 6, "智力": 17, "感知": 18},
            },
            {"name": "", "core": "无名氏"},  # dropped
        ])

        assert gm.scene_npcs == ["格里芬"]
        npc = created["格里芬"]
        assert npc.attributes["力量"] == 6
        assert npc.attributes["智力"] == 17
        # Missing dimensions are filled, not left absent.
        assert npc.attributes["体质"] == 10
        assert npc.core == ["年迈钟表匠", "能听出每块表的病"]
        assert npc.personality["tone"] == "沙哑缓慢"
        assert npc.relations["冒险者"] == "旧识"

    def test_seed_npc_specs_clamps_out_of_range(self):
        from trpg_agent.game_master import GameMaster

        created = {}

        class _Store:
            def find_by_name(self, name):
                return created.get(name)

            def create(self, name, core, attributes, relations=None, personality=None):
                created[name] = types.SimpleNamespace(name=name, attributes=attributes)
                return created[name]

            def all(self):
                return list(created.values())

        gm = GameMaster.__new__(GameMaster)
        gm.npc_store = _Store()
        gm.scene_npcs = []
        gm._npc_agents = {}
        gm._debug_log = []
        gm.debug = False
        gm._llm_available = False
        gm.player = types.SimpleNamespace(name="P")

        gm._seed_npc_specs([
            {"name": "巨人", "attributes": {"力量": 999, "敏捷": -5, "体质": "x"}}
        ])
        attrs = created["巨人"].attributes
        assert attrs["力量"] == 20
        assert attrs["敏捷"] == 1
        assert attrs["体质"] == 10  # unparseable -> inferred default


class TestControlCharacterJson:
    """Regression: literal newlines inside JSON strings broke every parser.

    Models routinely emit multi-line narration.  Strict json.loads rejects raw
    control characters, so the reply fell through to the fallback branch and
    the whole JSON blob ended up inside `narration` with suggestions/npcs lost.
    """

    MULTILINE = '{"narration": "第一句。\n第二句。", "suggestions": ["a", "b", "c"]}'

    def test_parse_gm_output_accepts_literal_newlines(self):
        from trpg_agent.game_master import GameMaster
        narration, suggestions = GameMaster._parse_gm_output(self.MULTILINE)
        assert narration == "第一句。\n第二句。"
        assert suggestions == ["a", "b", "c"]

    def test_parse_gm_output_accepts_embedded_multiline(self):
        from trpg_agent.game_master import GameMaster
        narration, suggestions = GameMaster._parse_gm_output(
            '好的\n{"narration": "雾散了。\n天亮了。", "suggestions": ["x", "y"]}\n完毕'
        )
        assert narration == "雾散了。\n天亮了。"
        assert len(suggestions) == 2

    def test_chat_json_accepts_literal_newlines(self):
        from trpg_agent.llm import LLM
        parsed = LLM._parse_json_response(LLM.__new__(LLM), self.MULTILINE)
        assert parsed["narration"] == "第一句。\n第二句。"
        assert parsed["suggestions"] == ["a", "b", "c"]

    def test_chat_json_multiline_in_code_fence(self):
        from trpg_agent.llm import LLM
        parsed = LLM._parse_json_response(
            LLM.__new__(LLM),
            '```json\n{"narration": "一。\n二。", "suggestions": ["p"]}\n```',
        )
        assert parsed["narration"] == "一。\n二。"
        assert parsed["suggestions"] == ["p"]

    def test_opening_payload_survives_multiline(self):
        """The opening needs narration + npcs + suggestions, not a raw blob."""
        from trpg_agent.llm import LLM
        parsed = LLM._parse_json_response(
            LLM.__new__(LLM),
            '{"narration": "钟声敲过。\n雾漫进长街。", '
            '"npcs": [{"name": "格里芬", "core": ["钟表匠"]}], '
            '"suggestions": ["x", "y", "z"]}',
        )
        assert not parsed["narration"].startswith("{")
        assert [n["name"] for n in parsed["npcs"]] == ["格里芬"]
        assert len(parsed["suggestions"]) == 3


class TestCustomWorldview:
    """The start screen's worldview / NPC fields must actually take effect."""

    def test_derive_world_name_takes_first_clause(self):
        from trpg_agent.game_master import _derive_world_name
        assert _derive_world_name(
            "潮汐之城阿卡姆，一座建在巨鲸脊背上的港口，每逢月圆下沉三小时。"
        ) == "潮汐之城阿卡姆"

    def test_derive_world_name_handles_period_and_dash(self):
        from trpg_agent.game_master import _derive_world_name
        assert _derive_world_name("赛博朋克2099。霓虹与酸雨。") == "赛博朋克2099"
        assert _derive_world_name("雾都伦敦 - 蒸汽与侦探的年代") == "雾都伦敦"

    def test_derive_world_name_falls_back_when_empty(self):
        from trpg_agent.game_master import _derive_world_name
        assert _derive_world_name("") == "自定义世界"
        assert _derive_world_name("   ") == "自定义世界"

    def test_derive_world_name_caps_length(self):
        from trpg_agent.game_master import _derive_world_name
        name = _derive_world_name("这是一个非常非常长的没有任何标点符号的世界观描述会一直写下去")
        assert len(name) <= 20

    def test_backstory_seeding_can_skip_scenario(self):
        """A custom worldview must not drag in the bundled scenario cast.

        Regression: new_game_reset() always re-seeded the packaged scenario
        NPCs, so a brand-new custom world still opened with 玛莎太太 and the
        black-cat plot.
        """
        import inspect
        from trpg_agent.game_master import GameMaster

        for fn in (GameMaster._register_backstory_npcs, GameMaster.new_game_reset):
            assert "use_scenario" in inspect.signature(fn).parameters, fn.__name__

    def test_scenario_lookup_skipped_for_custom_worldview(self):
        """generate_opening must not query the scenario for a custom world."""
        import inspect
        from trpg_agent.game_master import GameMaster

        src = inspect.getsource(GameMaster.generate_opening)
        assert "if custom_worldview:" in src
        assert "scenario_text = \"\"" in src


class TestSentenceSplitting:
    """Regression: the streaming splitter duplicated the final sentence."""

    def test_no_trailing_punctuation_does_not_duplicate(self):
        from trpg_agent.game_master import _split_sentences
        assert _split_sentences("你好。世界") == ["你好。", "世界"]

    def test_trailing_punctuation_is_unchanged(self):
        from trpg_agent.game_master import _split_sentences
        assert _split_sentences("你好。世界。") == ["你好。", "世界。"]

    def test_single_sentence_not_duplicated(self):
        from trpg_agent.game_master import _split_sentences
        assert _split_sentences("只有一句话") == ["只有一句话"]


class TestNarratorInterception:
    """The Narrator's JSON must win over the Orchestrator's paraphrase."""

    @staticmethod
    def _ctx(narrator_output: str) -> GameContext:
        return GameContext(
            player_state=None, npc_store=None, memory=None, knowledge=None,
            scene_npcs=[], time_of_day="正午", weather="晴",
            player_name="P", player_card="C", narrator_output=narrator_output,
        )

    def test_prefers_intercepted_narrator_json(self):
        from trpg_agent.game_master import GameMaster
        ctx = self._ctx('{"narration": "真叙事。", "suggestions": ["a", "b", "c"]}')
        narration, suggestions = GameMaster._resolve_narration(ctx, "调度员瞎编的")
        assert narration == "真叙事。"
        assert len(suggestions) == 3

    def test_falls_back_to_final_output(self):
        from trpg_agent.game_master import GameMaster
        ctx = self._ctx("")
        narration, _ = GameMaster._resolve_narration(
            ctx, '{"narration": "兜底。", "suggestions": []}')
        assert narration == "兜底。"


class TestChineseMemorySearch:
    """Regression: the recent-events fast path never matched Chinese."""

    def test_bigram_tokens_for_chinese(self):
        from trpg_agent.tools import _query_tokens
        tokens = _query_tokens("码头的商人")
        assert "码头" in tokens

    def test_english_tokens_preserved(self):
        from trpg_agent.tools import _query_tokens
        tokens = _query_tokens("harbour merchant")
        assert "harbour" in tokens and "merchant" in tokens


# ===================================================================
# Tool cache tests
# ===================================================================


class TestToolCache:
    """Tests for the tool result caching mechanism."""

    def test_cache_returns_cached_value(self):
        from trpg_agent.tools import _cached, clear_tool_cache
        clear_tool_cache()

        call_count = [0]

        def producer():
            call_count[0] += 1
            return f"result-{call_count[0]}"

        r1 = _cached("test_tool", "key1", producer)
        r2 = _cached("test_tool", "key1", producer)
        assert r1 == r2 == "result-1"
        assert call_count[0] == 1  # producer only called once

    def test_cache_different_keys(self):
        from trpg_agent.tools import _cached, clear_tool_cache
        clear_tool_cache()

        call_count = [0]

        def producer():
            call_count[0] += 1
            return f"result-{call_count[0]}"

        r1 = _cached("test_tool", "key_a", producer)
        r2 = _cached("test_tool", "key_b", producer)
        assert r1 != r2
        assert call_count[0] == 2

    def test_clear_cache_resets(self):
        from trpg_agent.tools import _cached, clear_tool_cache
        clear_tool_cache()

        call_count = [0]

        def producer():
            call_count[0] += 1
            return f"result-{call_count[0]}"

        _cached("test_tool", "key1", producer)
        clear_tool_cache()
        _cached("test_tool", "key1", producer)
        assert call_count[0] == 2  # producer called again after clear


# ===================================================================
# Event stream tests
# ===================================================================


class TestEventStream:
    """Tests for SSE event stream infrastructure."""

    def test_all_tools_have_display_names(self):
        """Every tool used by the GM should have a display name."""
        known_tools = [
            "roll_dice", "difficulty_check", "skill_check", "combat_attack",
            "get_player_state", "get_npc_state",
            "create_npc", "invoke_npc", "invoke_npcs",
            "remove_npc", "set_scene", "game_over",
            "search_knowledge", "search_memory",
        ]
        for tool in known_tools:
            assert tool in TOOL_DISPLAY_NAMES, f"Missing display name for: {tool}"

    def test_make_tool_event_start(self):
        event = make_tool_event("tool_call_start", "roll_dice", '{"expression":"2d6"}')
        assert event.type == "tool_call_start"
        assert event.tool == "roll_dice"
        assert event.display in ("投骰子(2d6)", "投骰子")
        assert event.result in (None, "")  # start events have no result yet

    def test_make_tool_event_end(self):
        event = make_tool_event("tool_call_end", "difficulty_check", '{"dc":12}', "d20=15 ≥ DC12 → 成功")
        assert event.type == "tool_call_end"
        assert event.tool == "difficulty_check"
        assert "成功" in event.result

    def test_status_event_fields(self):
        event = StatusEvent(
            type="thinking_start",
            tool="",
            display="",
            result="",
            npc_name=None,
            npc_text=None,
        )
        assert event.type == "thinking_start"
        assert event.tool == ""


# ===================================================================
# GameContext tests
# ===================================================================


class TestGameContext:
    """Tests for the GameContext dataclass."""

    def test_default_values(self):
        ctx = GameContext(
            player_state=None,
            npc_store=None,
            memory=None,
            knowledge=None,
            scene_npcs=[],
            time_of_day="正午",
            weather="晴",
            player_name="TestPlayer",
            player_card="TestCard",
        )
        assert ctx.time_of_day == "正午"
        assert ctx.debug is False
        assert ctx.debug_log == []
        assert ctx.game_over is False
        assert ctx.npc_agents == {}

    def test_tool_cache_module_available(self):
        """tools.py must export clear_tool_cache and _cached functions."""
        from trpg_agent.tools import clear_tool_cache, _cached
        assert callable(clear_tool_cache)
        assert callable(_cached)


# ===================================================================
# Multi-agent prompt tests
# ===================================================================


class TestJudgePrompt:
    """Verify the Judge Agent prompt is well-formed."""

    def test_judge_prompt_contains_required_sections(self):
        from trpg_agent.judge import JUDGE_SYSTEM_PROMPT
        assert "检定工具选择规则" in JUDGE_SYSTEM_PROMPT
        assert "DC 参考" in JUDGE_SYSTEM_PROMPT
        assert "修正值参考" in JUDGE_SYSTEM_PROMPT
        assert "无需检定" in JUDGE_SYSTEM_PROMPT

    def test_judge_agent_created_with_correct_name(self):
        from trpg_agent.judge import create_judge_agent
        agent = create_judge_agent()
        assert agent.name == "Judge"

    def test_judge_input_builder_includes_required_fields(self):
        from trpg_agent.judge import build_judge_input
        result = build_judge_input(
            player_action="我爬墙",
            player_name="冒险者",
            player_skills=[{"name": "攀爬", "value": 60}],
            player_attributes={"力量": 14, "敏捷": 12},
            scene_context="酒馆外",
            hp_info="HP 12/12",
        )
        assert "冒险者" in result
        assert "爬墙" in result


class TestNarratorPrompt:
    def test_narrator_prompt_requires_json_output(self):
        from trpg_agent.narrator import NARRATOR_SYSTEM_PROMPT
        assert "narration" in NARRATOR_SYSTEM_PROMPT
        assert "suggestions" in NARRATOR_SYSTEM_PROMPT

    def test_narrator_has_no_tools(self):
        from trpg_agent.narrator import create_narrator_agent
        agent = create_narrator_agent()
        assert agent.tools == []

    def test_narrator_input_builder_collects_results(self):
        from trpg_agent.narrator import build_narrator_input
        result = build_narrator_input(
            player_action="进门",
            player_name="冒险者",
            scene_context="酒馆",
            check_results="d20=15 成功",
            npc_responses='酒保: "欢迎！"',
            knowledge_results="酒馆的历史",
        )
        assert "冒险者" in result
        assert "d20=15" in result
        assert "酒保" in result


class TestOrchestratorPrompt:
    def test_orchestrator_prompt_focuses_on_routing(self):
        from trpg_agent.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
        assert "路由规则" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "invoke_judge" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "invoke_narrator" in ORCHESTRATOR_SYSTEM_PROMPT

    def test_orchestrator_agent_created(self):
        from trpg_agent.orchestrator import create_orchestrator_agent
        agent = create_orchestrator_agent(tools=[])
        assert agent.name == "Orchestrator"


class TestNPCEventInputs:
    def test_event_input_includes_decision_prompt(self):
        from trpg_agent.npc_agent import build_npc_event_input
        result = build_npc_event_input(
            event="玩家走进酒馆",
            npc_name="酒保",
            npc_state={"emotion": "calm", "stamina": "fresh", "hp": 10, "max_hp": 10},
            scene="酒馆内部",
        )
        assert "酒保" in result
        assert "玩家走进酒馆" in result
        assert "<silent>" in result  # decision option

    def test_tick_input_includes_time_passage(self):
        from trpg_agent.npc_agent import build_npc_tick_input
        result = build_npc_tick_input(
            npc_name="门卫",
            npc_state={"emotion": "wary", "stamina": "tired", "hp": 8, "max_hp": 12},
            scene="门口",
            turns_since_last_action=3,
        )
        assert "门卫" in result
        assert "时间流逝" in result
        assert "3 个回合" in result
