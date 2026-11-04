# TRPG Agent

AI 驱动的单人跑团应用。玩家扮演角色冒险，AI 担任 GM（叙事/检定）+ NPC 扮演。

## 快速开始

### 0. 准备

- Python 3.11+（**不要**用 Windows 应用商店版的 `python` 存根，它执行后什么都不输出）
- Node.js 18+
- 一个 DeepSeek API Key

```powershell
# 配置 Key：复制 .env.example 为 .env 再填
Copy-Item .env.example .env
notepad .env
```

### 1. 建独立环境（只需一次）

本项目依赖 chromadb + sentence-transformers（含 torch，约 2GB），建议单独一个 conda 环境，别装进 base：

```powershell
conda create -n trpg python=3.11 -y
conda activate trpg
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
```

### 2. Web 开发预览（推荐）

```powershell
.\dev.ps1
```

脚本会自动找到 conda 环境 `trpg`（也可用 `-Python <解释器路径>` 或 `TRPG_PYTHON` 覆盖），
然后 `pip install -r requirements.txt` → `npm install` → 起 Vite（5000，HMR）
→ 起 uvicorn（8000，`--reload`）。打开：

- 前端：<http://127.0.0.1:5000>
- 接口文档：<http://127.0.0.1:8000/docs>

> 首次安装约 2GB（chromadb + torch，装完就不再下载）。
> 首次请求会下载 `BAAI/bge-small-zh-v1.5` 向量模型（约 100MB，脚本已默认走 hf-mirror）。
> 同一个 5000 端口就够用：`vite.config.ts` 把 `/api` 代理到 8000。

### 3. 手动起两个终端（等价做法）

```powershell
conda activate trpg

# 终端 A：后端
python -m uvicorn trpg_agent.api_server:app --reload --reload-dir trpg_agent --port 8000

# 终端 B：前端
cd web
npm run dev
```

### 4. CLI 模式

```powershell
conda activate trpg
python -m trpg_agent.main --debug
```

### 5. 其它脚本

| 脚本 | 用途 |
|------|------|
| `dev.ps1` / `dev.bat` | 开发预览：前端 HMR + 后端热重载 |
| `start.ps1` / `start.bat` | 单端口预览：先 `npm run build`，再由 8000 直接托管 `web/dist` |

> 没有 `DEEPSEEK_API_KEY` 也能启动、能点开界面，但 GM 只会返回兜底文本，
> 属于「验证 UI/接口通了」，不是能玩的预览。

## 架构

基于 **OpenAI Agents SDK** 的多 Agent 编排，DeepSeek 通过 Chat Completions 接入。

**一套管线，两个表现层。** CLI 与 Web 驱动的是同一个 Agent 图，差别只在呈现方式：

| 入口 | 方法 | 用途 |
|------|------|------|
| CLI `main.py` | `GameMaster.process()` | 同步执行，用于测试与调试 |
| Web `POST /api/action` | `GameMaster.process_streaming()` | 异步 + SSE 状态事件，用于真实游玩 |

两者共用 `_pre_dispatch`（结局确认 + `!` 命令）、`_build_orchestrator_payload`、
`_resolve_narration`、`_record_turn`，因此行为不会再各自漂移。
旧的单 GM Agent 管线、`TRPG_LEGACY_MODE` 和 Tool Advisor 已删除。

### 管线

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

### 工具（@function_tool）

| 类别 | 工具 |
|------|------|
| 判定（Judge 专属） | roll_dice, difficulty_check, skill_check, combat_attack, take_rest |
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

### 开局设置（网页端）

| 功能 | 说明 |
|------|------|
| 玩家角色卡 | 界面内填姓名、背景、心之锚、六维属性（滑条 + 点数预算）、技能列表；体质 × 3 = HP 上限 |
| 世界设定卡 | 世界名、描述，外加纪元 / 气候 / 基调 / 技术 / 超自然 / 危险度 / 开场时间 / 开场天气 / 补充设定；含 3 个一键模板 |
| 初始 NPC | 可增删的卡片列表（最多 6 个），每个含姓名、背景、语调、与玩家关系、口头禅和六维属性滑条 |
| 设定预设 | 整套设定可存为本地预设、导出 / 导入 JSON 文件 |

> 结构化 NPC 按填写的数值**直接建卡**，不经 LLM 猜测；只有留空的属性才由 LLM 按身份推断。

### 游戏内

| 功能 | 说明 |
|------|------|
| 多 Agent NPC | 每个 NPC 独立 Agent，自主决定是否回应 / 沉默 |
| NPC 记忆 | 独立向量库，按 NPC 过滤，LLM 提取摘要 |
| NPC 详情面板 | 展开查看背景、人格、属性、关系、私有记忆与对话历史 |
| 判定可见 | 检定 / 战斗 / 休息的骰子明细以独立气泡呈现（叙事内仍不含数值） |
| 重掷开场 | 开场不满意可「换一个开场」，保留世界与 NPC 只重生成叙事 |
| 多存档槽 | 命名存档、列表选择、删除；兼容旧的 `data/save.json` |
| 知识库管理 | 自然语言添加，或整段粘贴设定文档按段落切块入库；条目可删除 |
| 角色死亡 | HP≤0 → game_over → 二次确认后清档 |
| 场景管理 | set_scene / remove_npc 维护场景 NPC 列表 |
| 世界模拟 | 每 3 回合在场 NPC 各自自主行动 |
| 休息恢复 | `take_rest` 是 HP 唯一的恢复途径 |
| 剧本系统 | `data/knowledge/scenario_black_cat.md` 节点式剧本（自定义世界观时自动隔离） |
| 知识目录 | `config.yaml` 的 `knowledge.files` 可指向任意目录（换世界观不用改代码） |

## 配置项

| 环境变量 | 作用 |
|----------|------|
| `DEEPSEEK_API_KEY` | 必填，LLM Key |
| `TRPG_CONFIG` | 角色卡配置路径（默认 `config.yaml`） |
| `TRPG_DEBUG` | `1` 开启调试日志 |
| `TRPG_API_TOKEN` | 设置后所有 API 需 Bearer Token（前端用 `VITE_API_TOKEN`） |
| `TRPG_CORS_ORIGINS` | 允许的来源，逗号分隔，默认 `*` |
| `TRPG_MODEL` | 模型名，默认 `deepseek-chat`（`deepseek-v4-flash` 已被官方退役） |
| `TRPG_BASE_URL` | API 地址，默认 `https://api.deepseek.com` |
| `HF_ENDPOINT` | 向量模型下载镜像 |

## 已知限制

- **单用户**：`GameMaster` 是全局单例，回合用 `asyncio.Lock` 串行化，不支持并发玩家。
- **默认无鉴权**：不设置 `TRPG_API_TOKEN` 时任何本机进程都能读写游戏状态。
- **存档已带版本号**（`version: 2`，含世界与角色卡）；更早的存档仍可读，但缺失的字段会沿用当前值。
- **Orchestrator 复述风险已兜底但未消除**：叙事以 `invoke_narrator` 在工具边界
  截获的 JSON 为准（`GameContext.narrator_output`），Orchestrator 的改写会被忽略；
  但若它干脆不调用 Narrator，仍会回退到解析其自身输出。

## 测试

需要已安装 `requirements.txt` 的 Python 环境（README 开头创建的那个 conda 环境）。

```powershell
conda activate trpg
pytest tests/ -q                       # 272 项单元/回归测试
python -B .harness/logic_check.py      # 51 项服务端逻辑回归（工具级）
python -B .harness/pipeline_smoke.py   # 单管线端到端烟测（CLI 与 Web 行为一致）
python -B .harness/live_api_check.py   # 真实 LLM 抽验新 API（需 DEEPSEEK_API_KEY）
```

`.harness/` 里的脚本自带 chromadb / openai-agents / pytest 的轻量替身，
因此在**没有装依赖**的解释器上也能跑，用于快速校验服务端逻辑：

```powershell
python -B .harness/run_tests.py        # 用替身执行 tests/ 中的纯逻辑用例
```

> 替身只保证逻辑一致，不保证 API 形态一致（例如真实 `@function_tool` 返回
> `FunctionTool` 对象而替身返回原函数）。涉及 SDK 对象结构的断言请用
> `tool_names()` 这类兼容读取，并以真实 `pytest` 的结果为准。
