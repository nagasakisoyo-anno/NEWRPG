import { useState, useEffect, useRef } from 'react'
import { useGameStore } from '../store/gameStore';
import { apiPost, apiGet, apiJson } from '../api';
import WorldBuilder from './WorldBuilder'
import NpcBuilder from './NpcBuilder'
import PlayerBuilder from './PlayerBuilder'
import PresetBar from './PresetBar'
import {
  NpcSpec,
  PlayerSpec,
  WorldSpec,
  makeDefaultPlayer,
  makeEmptyWorld,
  npcsToPayload,
  playerToPayload,
  worldToPayload,
} from '../types/setup'

interface SaveMeta {
  slot: string
  label: string
  updated_at: string
  world: string
  player: string
  hp?: number
  max_hp?: number
  turn_count: number
  broken?: boolean
  legacy?: boolean
}

const LOADING_STEPS = [
  '连接 LLM...',
  '加载角色卡...',
  '初始化 NPC 存储...',
  '初始化记忆存储...',
  '初始化知识库...',
  '加载世界知识...',
  '正在生成开场叙述...',
]

export default function StartScreen() {
  const { setGameStarted, setIsLoading, isLoading, error, setError, addMessage, setSuggestions, setPlayerState, setNpcs } = useGameStore()
  const [mode, setMode] = useState<'new' | 'load'>('new')
  const [world, setWorld] = useState<WorldSpec>(makeEmptyWorld)
  const [npcs, setNpcSpecs] = useState<NpcSpec[]>([])
  const [player, setPlayer] = useState<PlayerSpec>(makeDefaultPlayer)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [savesList, setSavesList] = useState<SaveMeta[]>([])
  const [selectedSlot, setSelectedSlot] = useState('')
  const [llmWarning, setLlmWarning] = useState('')

  // 检查后端是否配好了 API Key —— 否则 GM 只会返回兜底文本，
  // 而玩家从界面上看不出任何异常。
  useEffect(() => {
    apiGet('/health')
      .then(apiJson)
      .then((data) => {
        if (data.llm_available === false) {
          setLlmWarning(
            '后端未配置 DEEPSEEK_API_KEY，GM 只会返回兜底文本。' +
              '请设置环境变量或填写 .env 后重启服务。',
          )
        }
      })
      .catch(() => {
        // 健康检查失败不阻塞开始界面，真正的错误会在启动时暴露
      })
  }, [])
  const [loadingStep, setLoadingStep] = useState(0)
  const loadingTimer = useRef<ReturnType<typeof setInterval>>()

  // 加载时轮换进度描述
  useEffect(() => {
    if (isLoading) {
      setLoadingStep(0)
      loadingTimer.current = setInterval(() => {
        setLoadingStep((prev) => (prev < LOADING_STEPS.length - 1 ? prev + 1 : prev))
      }, 1500)
    } else {
      clearInterval(loadingTimer.current)
    }
    return () => clearInterval(loadingTimer.current)
  }, [isLoading])

  // 切到「加载存档」时拉取槽位列表
  useEffect(() => {
    if (mode !== 'load') return
    apiGet('/api/saves')
      .then(apiJson)
      .then((data) => {
        const list: SaveMeta[] = data.saves || []
        setSavesList(list)
        setSelectedSlot(list[0]?.slot || '')
      })
      .catch((err) => setError(err?.message || '无法读取存档列表'))
  }, [mode])

  const handleStart = async () => {
    // Unnamed NPC cards are dropped silently; warn instead of losing them.
    const named = npcs.filter((n) => n.name.trim()).length
    if (npcs.length > named) {
      setError(`有 ${npcs.length - named} 个 NPC 没有填姓名，请补全或移除`)
      return
    }

    setIsLoading(true)
    setError(null)

    try {
      const res = await apiPost('/api/start', {
        ...worldToPayload(world),
        npcs: npcsToPayload(npcs),
        player: playerToPayload(player),
      })

      if (!res.ok) {
        const errText = await res.text()
        throw new Error(`启动失败 (${res.status}): ${errText}`)
      }

      const data = await res.json()
      if (data.error) {
        throw new Error(data.error)
      }

      // 如果有开场叙述，添加到消息列表
      if (data.opening) {
        addMessage('gm', data.opening)
      }

      // 建议由后端 JSON suggestions 字段提供
      if (data.suggestions?.length > 0) {
        setSuggestions(data.suggestions)
      }

      // 设置玩家状态
      if (data.state) {
        setPlayerState(data.state)
      }

      // 设置 NPC
      if (data.npcs) {
        setNpcs(data.npcs)
      }

      // 进入游戏界面
      setGameStarted(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '未知错误')
    } finally {
      setIsLoading(false)
    }
  }

  const handleLoad = async () => {
    setIsLoading(true)
    setError(null)

    try {
      const data = await apiJson(
        await apiPost('/api/load', selectedSlot ? { slot: selectedSlot } : {}),
      )

      if (data.loaded) {
        if (data.state) {
          setPlayerState(data.state)
        }
        if (data.npcs) {
          setNpcs(data.npcs)
        }
        // Restore the last scene so the player is not dropped into a blank log.
        if (data.lastResponse) {
          addMessage('gm', data.lastResponse)
        }
        if (data.suggestions?.length > 0) {
          setSuggestions(data.suggestions)
        }
        setGameStarted(true)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '未知错误')
    } finally {
      setIsLoading(false)
    }
  }

  const handleDeleteSave = async (slot: string) => {
    if (!window.confirm(`确定删除存档「${slot}」？此操作不可撤销。`)) return
    try {
      await apiJson(await apiPost('/api/save/delete', { slot }))
      const next = savesList.filter((s) => s.slot !== slot)
      setSavesList(next)
      if (selectedSlot === slot) setSelectedSlot(next[0]?.slot || '')
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    }
  }

  return (
    <div className="min-h-screen flex items-start justify-center bg-coal-950 overflow-y-auto py-10">
      {/* Background decoration */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none">
        <div className="absolute top-1/4 left-1/4 w-96 h-96 bg-brass-900/10 rounded-full blur-3xl" />
        <div className="absolute bottom-1/4 right-1/4 w-96 h-96 bg-blood-900/10 rounded-full blur-3xl" />
      </div>

      <div className="relative z-10 w-full max-w-2xl px-6">
        {/* Title */}
        <div className="text-center mb-8">
          <h1 className="text-5xl font-serif text-brass-400 glow-text mb-2">
            TRPG Agent
          </h1>
          <p className="text-coal-400 text-lg italic">自定义你的世界</p>
          <div className="mt-4 w-24 h-px bg-gradient-to-r from-transparent via-brass-600 to-transparent mx-auto" />
        </div>

        {/* Mode selection */}
        <div className="decorative-border p-6">
          <div className="flex gap-3 mb-6">
            <button
              onClick={() => setMode('new')}
              disabled={isLoading}
              className={`flex-1 py-2 rounded text-center transition-all duration-200 border ${
                mode === 'new'
                  ? 'bg-brass-700/20 border-brass-600 text-brass-300'
                  : 'bg-coal-800 border-coal-600 text-coal-400 hover:border-brass-700/50'
              } disabled:opacity-50 disabled:cursor-not-allowed`}
            >
              新游戏
            </button>
            <button
              onClick={() => setMode('load')}
              disabled={isLoading}
              className={`flex-1 py-2 rounded text-center transition-all duration-200 border ${
                mode === 'load'
                  ? 'bg-brass-700/20 border-brass-600 text-brass-300'
                  : 'bg-coal-800 border-coal-600 text-coal-400 hover:border-brass-700/50'
              } disabled:opacity-50 disabled:cursor-not-allowed`}
            >
              加载存档
            </button>
          </div>

          {mode === 'new' ? (
            <div className="space-y-5">
              {/* ---- Presets ---- */}
              <PresetBar
                world={world}
                npcs={npcs}
                player={player}
                disabled={isLoading}
                onApply={(preset) => {
                  setWorld(preset.world)
                  setNpcSpecs(preset.npcs)
                  setPlayer(preset.player)
                }}
              />

              {/* ---- Player card ---- */}
              <section>
                <h3 className="text-sm text-brass-400 mb-2">玩家角色</h3>
                <PlayerBuilder player={player} onChange={setPlayer} disabled={isLoading} />
              </section>

              {/* ---- World card ---- */}
              <section>
                <h3 className="text-sm text-brass-400 mb-2">世界设定</h3>
                <WorldBuilder world={world} onChange={setWorld} disabled={isLoading} />
              </section>

              {/* ---- NPC cards ---- */}
              <section>
                <div className="flex items-baseline justify-between mb-2">
                  <h3 className="text-sm text-brass-400">初始 NPC</h3>
                  <span className="text-xs text-coal-500">
                    {npcs.length > 0 ? `已添加 ${npcs.length} 个` : '可选'}
                  </span>
                </div>
                <NpcBuilder
                  npcs={npcs}
                  onChange={setNpcSpecs}
                  disabled={isLoading}
                  max={6}
                />
              </section>

              {/* Advanced toggle */}
              <button
                onClick={() => setShowAdvanced(!showAdvanced)}
                className="text-xs text-coal-500 hover:text-brass-500 transition-colors"
              >
                {showAdvanced ? '收起说明' : '这些设定会如何生效？'}
              </button>

              {showAdvanced && (
                <div className="p-3 bg-coal-800/50 rounded border border-coal-700 text-xs text-coal-400 space-y-1">
                  <p>· 玩家角色的属性决定检定调整值，体质 × 3 即 HP 上限。</p>
                  <p>· 技能是 d100 检定：数值越高越容易成功；没填的技能按 50 处理。</p>
                  <p>· 世界观描述与所有细节字段会写入世界状态，影响之后每一回合的叙事。</p>
                  <p>· 填了世界观后，内置剧本（东区的黑猫）不会再混入开场。</p>
                  <p>· 这里填的 NPC 按填写的数值直接建卡，不经 LLM 猜测；</p>
                  <p>　留空的属性由 LLM 按身份推断。</p>
                  <p>· 每个 NPC 都会获得独立 Agent、独立状态和私有记忆库。</p>
                  <p>· 全部留空则使用 config.yaml 的默认设定。</p>
                </div>
              )}

              <button
                onClick={handleStart}
                disabled={isLoading}
                className="w-full py-3 btn-primary text-lg disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isLoading ? LOADING_STEPS[loadingStep] : '开始冒险'}
              </button>

              {/* Loading progress bar */}
              {isLoading && (
                <div className="mt-3">
                  <div className="h-1 bg-coal-800 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-brass-600 rounded-full transition-all duration-1000 animate-pulse"
                      style={{ width: `${((loadingStep + 1) / LOADING_STEPS.length) * 100}%` }}
                    />
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="space-y-4">
              {savesList.length === 0 ? (
                <p className="text-sm text-coal-400 text-center py-4">
                  还没有任何存档 —— 先开始一局新游戏吧。
                </p>
              ) : (
                <div className="space-y-2 max-h-80 overflow-y-auto">
                  {savesList.map((s) => (
                    <div
                      key={s.slot}
                      onClick={() => !s.broken && setSelectedSlot(s.slot)}
                      className={`p-3 rounded border cursor-pointer transition-colors ${
                        selectedSlot === s.slot
                          ? 'border-brass-600 bg-coal-800'
                          : 'border-coal-700 bg-coal-800/40 hover:border-coal-600'
                      } ${s.broken ? 'opacity-50 cursor-not-allowed' : ''}`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-sm text-coal-100 truncate">
                              {s.label || s.slot}
                            </span>
                            {s.legacy && (
                              <span className="shrink-0 px-1 text-xs rounded bg-coal-700 text-coal-400">
                                旧存档
                              </span>
                            )}
                          </div>
                          <div className="mt-1 text-xs text-coal-500 space-x-2">
                            {s.player && <span>{s.player}</span>}
                            {s.world && <span>· {s.world}</span>}
                            {typeof s.hp === 'number' && (
                              <span>
                                · HP {s.hp}/{s.max_hp}
                              </span>
                            )}
                            <span>· 第 {s.turn_count} 回合</span>
                          </div>
                          <div className="mt-0.5 text-xs text-coal-600">
                            {s.updated_at ? s.updated_at.replace('T', ' ').slice(0, 19) : ''}
                          </div>
                        </div>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            handleDeleteSave(s.slot)
                          }}
                          disabled={isLoading}
                          className="shrink-0 px-2 py-0.5 text-xs text-coal-500 hover:text-rose-400 transition-colors disabled:opacity-50"
                        >
                          删除
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              <button
                onClick={handleLoad}
                disabled={isLoading || !selectedSlot}
                className="w-full py-3 btn-primary text-lg disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isLoading ? '加载中...' : '加载存档'}
              </button>
            </div>
          )}

          {llmWarning && (
            <div className="mt-4 p-3 bg-amber-900/25 border border-amber-700/50 rounded text-amber-300 text-sm">
              ⚠ {llmWarning}
            </div>
          )}

          {error && (
            <div className="mt-4 p-3 bg-blood-900/30 border border-blood-700/50 rounded text-blood-300 text-sm">
              {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <p className="text-center text-coal-500 text-sm mt-8">
          用括号声明行动，如 (我拔出弓箭，瞄准远处的兽人)
        </p>
      </div>
    </div>
  )
}
