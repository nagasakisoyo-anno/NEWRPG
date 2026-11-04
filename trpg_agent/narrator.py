"""Narrator Agent — pure scene narration, no mechanics.

The Narrator receives all results (checks, NPC dialogue, knowledge search)
and writes the final scene narration + player action suggestions.

The Narrator has ZERO tools and NEVER invokes other agents.
"""

from agents import Agent

from trpg_agent.agent_config import DEFAULT_MODEL

# ---------------------------------------------------------------------------
# Narrator system prompt
# ---------------------------------------------------------------------------

NARRATOR_SYSTEM_PROMPT = """\
你是一个TRPG游戏的叙述者，只负责把已经发生的结果写成场景叙述。你不需要做任何判定——所有检定和NPC决策都已经完成了。

## 你的输入
你会收到：
- 当前场景信息（时间、天气、在场NPC）
- 玩家做了什么
- 检定结果（如果做了检定）——由系统返回，你不需要计算
- NPC 的回应（如果NPC说话了）
- 相关知识（如果检索了）

## 最重要的一条：不要复述 NPC 的台词
NPC 的对话**已经由系统单独展示给玩家了**，是独立的聊天气泡。
如果你在 narration 里把同一句话再写一遍，玩家会看到两遍，这是严重错误。

- ❌ 错误：马文擦着杯子说：「东边老磨坊，夜里总有光。」
- ✅ 正确：马文擦杯子的手停了停，目光往东边偏了一下，又很快收回去。

你要写的是 NPC 说话时的**动作、神态、环境反应**，以及这句话在场景里激起的涟漪
（旁人的侧目、炉火的噼啪、雾气的流动），而不是台词本身。
绝对不要使用引号「」或 "" 包裹 NPC 的话再写一遍。

## 叙事规则
- 每段叙述不超过 150 字
- 只描述角色动作和场景环境，绝对不写骰子数值，也不要写
  "d20=15 ≥ DC12，成功" 这类系统判定文本
- 每次叙述引入新的推进元素：NPC的反应、环境变化、新线索的浮现
- 玩家是驱动故事的人——不要替玩家做决定
- 不要替玩家角色说话、做决定或执行动作
- 不要描述玩家角色的内心感受或潜意识冲动
- 检定失败不是"你做不到"，而是产生新的处境和后果

## 叙事节奏（核心）
- **每次只推进一小步。** 玩家说一句话 → 你描述即时反馈 → 等玩家再行动。
  不要在一段叙述里塞入任务目标、线索提示和行动建议
- **信息分层揭示。** NPC只透露符合ta身份和当前情境的信息。一个焦急的母亲不会突然说出
  "幽暗森林"和"银光草"——她只会说女儿病了、求路人帮忙。病因和解决方案需要玩家通过
  检定、追问、探索来逐步发现
- **建议选项保持"此时此地"。** 建议基于玩家当前能看到的、能做的，不要跨越逻辑链条预设任务
- 收到的 NPC 台词要融进叙事，不要擅自改写 NPC 说过的话

## 输出前自检（内部完成，不要输出这些思考）
1. narration 是否 ≤150 字？是否只有场景叙述（无建议、无骰子数值）？
2. suggestions 是否恰好 3 个？是否基于当前场景？
3. 是否违反规则（替玩家说话、编造设定、引入不在场 NPC）？

## 输出格式（最高优先级）
你必须且只能以如下 JSON 格式回复：
{"narration": "场景叙述", "suggestions": ["建议1", "建议2", "建议3"]}

严格规则：
- narration: 只能包含场景叙述。禁止写入建议、提示、编号列表
- suggestions: 始终恰好 3 个，简短且基于当前场景
- 确保 JSON 合法。不要 markdown 代码块、不要额外文字
- 不要输出 thinking、tool_calls 等其他字段，只输出 narration 和 suggestions
"""


# ---------------------------------------------------------------------------
# Narrator Agent factory
# ---------------------------------------------------------------------------

def create_narrator_agent() -> Agent:
    """Create the Narrator Agent for scene narration.

    The Narrator has zero tools — it's a pure LLM generation step
    that takes all collected results and writes the narrative output.
    """
    return Agent(
        name="Narrator",
        instructions=NARRATOR_SYSTEM_PROMPT,
        tools=[],  # zero tools — pure generation
        model=DEFAULT_MODEL,
    )


# ---------------------------------------------------------------------------
# Narrator input builder
# ---------------------------------------------------------------------------

def build_narrator_input(
    player_action: str,
    player_name: str,
    scene_context: str,
    check_results: str = "",
    npc_responses: str = "",
    knowledge_results: str = "",
    world_setting: str = "",
    raw_results: str = "",
) -> str:
    """Build the input message for the Narrator Agent.

    Parameters
    ----------
    player_action : str
        What the player just did.
    player_name : str
        Player character name.
    scene_context : str
        Current scene (time, weather, location, present NPCs).
    check_results : str
        Results from Judge Agent (dice rolls, checks, combat).
    npc_responses : str
        Responses from NPC Agents (one or more NPCs).
    knowledge_results : str
        Results from knowledge/memory searches.
    world_setting : str
        World description from config.
    """
    parts: list[str] = []

    if world_setting:
        parts.append(f"## 世界设定\n{world_setting}")

    parts.append(f"## 当前场景\n{scene_context}")
    parts.append(f"\n## 玩家 {player_name}\n行动: {player_action}")

    if check_results:
        parts.append(f"\n## 检定结果\n{check_results}")

    if npc_responses:
        parts.append(
            f"\n## NPC 回应（已单独展示给玩家，禁止在 narration 里复述台词）\n{npc_responses}"
        )

    if knowledge_results:
        parts.append(f"\n## 相关知识\n{knowledge_results}")

    if raw_results:
        parts.append(f"\n## 全部结果\n{raw_results}")

    # The Orchestrator funnels everything through raw_results, so the
    # no-duplicate rule has to be restated at the end of the actual input —
    # otherwise the NPC lines in that blob get copied straight into narration.
    parts.append(
        "\n请基于以上所有信息，生成场景叙述和3个行动建议。\n"
        "注意：上面出现的 NPC 台词已经由系统单独展示给玩家了。"
        "你只写 NPC 说话时的动作神态和场景反应，**不要把台词再抄一遍**，"
        "也不要用引号复述。\n"
        "按照 JSON 格式输出：{\"narration\": \"...\", \"suggestions\": [...]}"
    )

    return "\n".join(parts)
