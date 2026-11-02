# TRPG Agent

AI 驱动的单人跑团应用。玩家扮演角色冒险，AI 担任 GM（叙事/检定）+ NPC 扮演。

## 快速开始

```bash
# 1. 配置 API Key（.env）
DEEPSEEK_API_KEY=sk-xxx

# 2. 安装依赖
pip install -r requirements.txt
cd web && npm install && cd ..

# 3. CLI 模式
python -m trpg_agent.main --debug

# 4. Web 模式（前端 5000 / 后端 8000）
.\dev.ps1
```

> 首次启动会从 HuggingFace 下载 `BAAI/bge-small-zh-v1.5` 向量模型（约 100MB）。
> 国内网络可先设置 `HF_ENDPOINT=https://hf-mirror.com`（启动脚本已自动设置）。

## 架构

基于 **OpenAI Agents SDK** 的多 Agent 编排，DeepSeek 通过 Chat Completions 接入。

**当前有两条管线，注意区别：**

| 入口 | 方法 | 使用的 Agent |
|------|------|--------------|
| CLI `main.py` | `GameMaster.process()` | Orchestrator → Judge / NPC / Narrator |
| Web `POST /api/action` | `GameMaster.process_streaming()` | 单 GM Agent（旧管线，便于 SSE 逐句推送） |

设置 `TRPG_LEGACY_MODE=1` 可让 CLI 也走旧管线。

### 新管线（CLI）

```
Player Input
  │
  ▼
Orchestrator          只做路由，不写叙事
  ├─ invoke_judge      → Judge Agent（DC / 技能 / 战斗 / 休息）
  ├─ invoke_npc        → 该 NPC 的独立 Agent（人格 + 记忆 + 状态）
  ├─ invoke_npcs       → 多个 NPC 并行回应
  ├─ broadcast_event   → 在场 NPC 自行决定是否反应
  ├─ search_knowledge / search_memory
  └─ invoke_narrator   → Narrator Agent（零工具，只输出 JSON）
  │
  ▼
{"narration": "...", "suggestions": ["...", "...", "..."]}
```

### 工具（19 个 @function_tool）

| 类别 | 工具 |
|------|------|
| 判定 | roll_dice, difficulty_check, skill_check, combat_attack, take_rest |
| 查询 | get_player_state, get_npc_state |
| 场景 | create_npc, remove_npc, set_scene |
| 角色 | invoke_npc, invoke_npcs, broadcast_event |
| 检索 | search_knowledge, search_memory |
| 子 Agent | invoke_judge, invoke_narrator |
| 终结 | game_over（二次确认 → GameMaster 执行清档） |

### 子 Agent 与上下文注入

所有工具通过 `RunContextWrapper[GameContext]` 拿到可变游戏状态。
**调用子 Agent 的 `Runner.run` 必须显式传 `context=`**，否则子 Agent 的工具里
`ctx.context` 是 `None`（这是本项目历史上最容易踩的坑）。

## 记忆系统

```
主记忆库 (ChromaDB collection: memories)
  ├── 语义检索 + NetworkX 图遍历 (2跳)
  ├── 边关系: "导致了" / "关联到" / "反驳了" / "发生在...之后"
  └── 只存玩家事件，不含 NPC 记忆

NPC 记忆库 (ChromaDB collection: npc_memories, 按 npc_name 过滤)
  ├── 语义检索 + 图遍历 (只查该 NPC 自己的记忆)
  └── 独立存储，NPC 间记忆隔离
```

### 上下文管理

| 组件 | 容量 | 策略 |
|------|------|------|
| `history_messages` | 最近 5 轮 | `_trim_history()` 截断 |
| `_recent_events` | 最近 10 条 | `deque(maxlen=10)` |
| 记忆检索结果 | 最多 8 条 | 语义 top 5 + 图扩展 + 去重 |
| NPC 对话历史 | 每 NPC 10 轮 | `NPCStore` 滑动窗口 |

## 目录

```
trpg_agent/
├── main.py            CLI 入口 (Rich)
├── game_master.py     GM 核心：三条流程 + 记忆写入 + 存档 + 新游戏重置
├── orchestrator.py    Orchestrator Agent（路由器）
├── judge.py           Judge Agent（机制判定）
├── narrator.py        Narrator Agent（纯叙事）
├── tools.py           19 个 @function_tool
├── npc_agent.py       NPC Agent 工厂 + 输入构造
├── npc.py             NPC 角色卡 + ChromaDB 持久化 + 状态 JSON
├── character.py       玩家角色卡 (YAML → dataclass)
├── state.py           状态机 (HP/情绪/信任/体力)
├── dice.py            骰子 (d20/d100/ndm+k)
├── check.py           检定 (技能/难度/对抗)
├── memory.py          记忆（双 ChromaDB collection + NetworkX 图）
├── rag.py             知识库（Markdown 切块 + front-matter 权限）
├── world_builder.py   自然语言创建 NPC / 世界观 / NPC 模拟
├── llm.py             LLM 封装（DeepSeek，逐步被 SDK 替代）
├── agent_config.py    SDK 初始化、GameContext、GM system prompt
├── event_stream.py    SSE 状态事件（RunHooks）
└── api_server.py      FastAPI + SSE

config.yaml            玩家角色 & 世界设定 & 知识目录
data/save.json         游戏存档（运行时生成，不入库）
data/knowledge/*.md    世界设定 / 剧本
data/chroma/           ChromaDB 持久化（不入库）
```

## 功能

| 功能 | 说明 |
|------|------|
| 断点续存 | 退出/Ctrl+C 自动保存 `data/save.json`，重启续接 |
| 新游戏 | 清空记忆 + NPC + 存档，从背景故事重新播种 NPC |
| 多 Agent NPC | 每个 NPC 独立 Agent，自主决定是否回应 / 沉默 |
| 角色死亡 | HP≤0 → game_over → 二次确认后清档 |
| 剧本系统 | `data/knowledge/scenario_black_cat.md` 节点式剧本 |
| NPC 记忆 | 独立向量库，按 NPC 过滤，LLM 提取摘要 |
| 场景管理 | set_scene / remove_npc 维护场景 NPC 列表 |
| 世界模拟 | 每 3 回合 NPC 自主行动，GM 叙事包装 |
| 休息恢复 | `take_rest` 是 HP 唯一的恢复途径 |
| 知识目录 | `config.yaml` 的 `knowledge.files` 可指向任意目录（换世界观不用改代码） |

## 配置项

| 环境变量 | 作用 |
|----------|------|
| `DEEPSEEK_API_KEY` | 必填，LLM Key |
| `TRPG_CONFIG` | 角色卡配置路径（默认 `config.yaml`） |
| `TRPG_DEBUG` | `1` 开启调试日志 |
| `TRPG_API_TOKEN` | 设置后所有 API 需 Bearer Token（前端用 `VITE_API_TOKEN`） |
| `TRPG_CORS_ORIGINS` | 允许的来源，逗号分隔，默认 `*` |
| `TRPG_LEGACY_MODE` | `1` 让 CLI 走旧单 Agent 管线 |
| `HF_ENDPOINT` | 向量模型下载镜像 |

## 已知限制

- **Web 端仍使用旧单 Agent 管线**（`process_streaming`），新三 Agent 架构只在 CLI 生效；
  迁移时会失去当前逐句 SSE 推送的细粒度状态事件。
- **单用户**：`GameMaster` 是全局单例，回合用 `asyncio.Lock` 串行化，不支持并发玩家。
- **默认无鉴权**：不设置 `TRPG_API_TOKEN` 时任何本机进程都能读写游戏状态。
- **存档无版本号**：字段变更后旧存档可能加载失败。
