import { useState, useEffect } from 'react'
import { useGameStore, NPCInfo } from '../store/gameStore';
import { apiPost, apiGet, apiJson } from '../api';

interface ModalNPCProps {
  onClose: () => void
}

export default function ModalNPC({ onClose }: ModalNPCProps) {
  const { npcs, setNpcs } = useGameStore()
  const [newNpcDesc, setNewNpcDesc] = useState('')
  const [isCreating, setIsCreating] = useState(false)
  const [createResult, setCreateResult] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [detail, setDetail] = useState<any>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')

  const toggleDetail = async (name: string) => {
    if (expanded === name) {
      setExpanded(null)
      return
    }
    setExpanded(name)
    setDetail(null)
    setDetailError('')
    setDetailLoading(true)
    try {
      setDetail(await apiJson(await apiGet(`/api/npc/${encodeURIComponent(name)}`)))
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : '读取失败')
    } finally {
      setDetailLoading(false)
    }
  }

  useEffect(() => {
    apiGet('/api/npcs')
      .then(apiJson)
      .then((data) => {
        if (data.npcs) setNpcs(data.npcs)
      })
      .catch((err) => setCreateResult(err.message || '加载 NPC 失败'))
  }, [])

  const handleCreate = async () => {
    if (!newNpcDesc.trim() || isCreating) return
    setIsCreating(true)
    setCreateResult('')

    try {
      const data = await apiJson(await apiPost('/api/command', { command: `!npc ${newNpcDesc}` }))
      setCreateResult(data.result || '创建完成')

      // Refresh NPC list
      const npcData = await apiJson(await apiGet('/api/npcs'))
      if (npcData.npcs) setNpcs(npcData.npcs)

      setNewNpcDesc('')
    } catch (err) {
      setCreateResult(`创建失败: ${err instanceof Error ? err.message : '未知错误'}`)
    } finally {
      setIsCreating(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-content w-full max-w-2xl mx-auto mt-20 max-h-[70vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-coal-700">
          <h2 className="text-xl font-serif text-brass-400">NPC 管理</h2>
          <button onClick={onClose} className="text-coal-400 hover:text-coal-200 text-2xl leading-none">
            ×
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {/* NPC list */}
          <div>
            <h3 className="text-sm text-coal-400 mb-2">当前场景 NPC</h3>
            {npcs.length === 0 ? (
              <p className="text-coal-500 text-sm italic">场景中暂无 NPC</p>
            ) : (
              <div className="space-y-2">
                {npcs.map((npc: NPCInfo, i) => (
                  <div
                    key={i}
                    className="bg-coal-800 rounded border border-coal-700 overflow-hidden"
                  >
                    <div
                      className="p-3 cursor-pointer hover:bg-coal-750 transition-colors"
                      onClick={() => toggleDetail(npc.name)}
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-coal-200 font-medium">
                          <span className="mr-1 text-coal-500 text-xs">
                            {expanded === npc.name ? '▾' : '▸'}
                          </span>
                          {npc.name}
                        </span>
                        <span className="text-xs text-coal-400">
                          HP: {npc.hp}/{npc.max_hp}
                        </span>
                      </div>
                      <p className="text-xs text-coal-400 mt-1">
                        情绪: {npc.emotion} | 信任: {Math.round(npc.trust * 100)}%
                      </p>
                    </div>

                    {expanded === npc.name && (
                      <div className="border-t border-coal-700 p-3 text-xs space-y-3">
                        {detailLoading ? (
                          <p className="text-coal-500">加载中…</p>
                        ) : detail && detail.name === npc.name ? (
                          <>
                            {detail.core?.length > 0 && (
                              <div>
                                <p className="text-coal-400 mb-1">背景</p>
                                <ul className="list-disc list-inside text-coal-300 space-y-0.5">
                                  {detail.core.map((c: string, k: number) => (
                                    <li key={k}>{c}</li>
                                  ))}
                                </ul>
                              </div>
                            )}

                            <div>
                              <p className="text-coal-400 mb-1">人格</p>
                              <p className="text-coal-300">
                                语调：{detail.personality?.tone || '未设定'}
                                {detail.personality?.verbal_tics &&
                                  ` ｜ 习惯：${detail.personality.verbal_tics}`}
                              </p>
                            </div>

                            {detail.attributes && (
                              <div>
                                <p className="text-coal-400 mb-1">属性</p>
                                <div className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-coal-300">
                                  {Object.entries(detail.attributes).map(([k, v]) => (
                                    <span key={k}>
                                      {k} {String(v)}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            )}

                            {detail.relations &&
                              Object.keys(detail.relations).length > 0 && (
                                <div>
                                  <p className="text-coal-400 mb-1">关系</p>
                                  {Object.entries(detail.relations).map(([k, v]) => (
                                    <p key={k} className="text-coal-300">
                                      {k}：{String(v)}
                                    </p>
                                  ))}
                                </div>
                              )}

                            <div>
                              <p className="text-coal-400 mb-1">
                                私有记忆（{detail.memories?.length || 0}）
                              </p>
                              {detail.memories?.length > 0 ? (
                                <ul className="list-disc list-inside text-coal-300 space-y-0.5">
                                  {detail.memories.map((m: string, k: number) => (
                                    <li key={k}>{m}</li>
                                  ))}
                                </ul>
                              ) : (
                                <p className="text-coal-500 italic">还没有记忆</p>
                              )}
                            </div>

                            <div>
                              <p className="text-coal-400 mb-1">
                                最近对话（{detail.history?.length || 0}）
                              </p>
                              {detail.history?.length > 0 ? (
                                <div className="space-y-1 max-h-40 overflow-y-auto">
                                  {detail.history
                                    .slice(-8)
                                    .map((h: any, k: number) => (
                                      <p key={k} className="text-coal-300">
                                        <span className="text-coal-500">
                                          {h.role === 'user' ? '玩家' : npc.name}：
                                        </span>
                                        {String(h.content).slice(0, 160)}
                                      </p>
                                    ))}
                                </div>
                              ) : (
                                <p className="text-coal-500 italic">还没有对话</p>
                              )}
                            </div>

                            <p className="text-coal-600">
                              独立 Agent：{detail.has_agent ? '已就绪' : '未创建'}
                            </p>
                          </>
                        ) : (
                          <p className="text-rose-400">{detailError || '读取失败'}</p>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Create NPC */}
          <div className="decorative-border p-4">
            <h3 className="text-sm text-coal-300 mb-2">创建新 NPC</h3>
            <p className="text-xs text-coal-500 mb-3">
              用自然语言描述，系统会自动生成完整的角色卡
            </p>
            <div className="flex gap-2">
              <input
                type="text"
                value={newNpcDesc}
                onChange={(e) => setNewNpcDesc(e.target.value)}
                placeholder="例如：一个在码头工作的老渔民，性格豪爽..."
                className="input-field flex-1"
                onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
              />
              <button
                onClick={handleCreate}
                disabled={isCreating || !newNpcDesc.trim()}
                className="btn-primary disabled:opacity-50"
              >
                {isCreating ? '创建中...' : '创建'}
              </button>
            </div>
            {createResult && (
              <p className="mt-2 text-sm text-brass-300">{createResult}</p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
