import { useState, useEffect } from 'react'
import { useGameStore, KnowledgeEntry } from '../store/gameStore';
import { apiPost, apiGet, apiJson } from '../api';

interface ModalKnowledgeProps {
  onClose: () => void
}

export default function ModalKnowledge({ onClose }: ModalKnowledgeProps) {
  const { knowledge, setKnowledge } = useGameStore()
  const [newKnowledge, setNewKnowledge] = useState('')
  const [isAdding, setIsAdding] = useState(false)
  const [addResult, setAddResult] = useState('')
  const [filterCategory, setFilterCategory] = useState('')
  const [mode, setMode] = useState<'narrate' | 'import'>('narrate')
  const [importCategory, setImportCategory] = useState('')
  const [importKnownBy, setImportKnownBy] = useState('所有人')

  // The backend returns real ChromaDB ids; keep them so entries are deletable.
  const normalise = (list: any[]): KnowledgeEntry[] =>
    list.map((k: any, i: number) => ({
      id: k.id || `k-${i}`,
      content: k.content || String(k),
      category: k.category || '未分类',
      known_by: k.known_by || '所有人',
    }))

  const refresh = async () => {
    const data = await apiJson(await apiGet('/api/knowledge'))
    if (data.knowledge) setKnowledge(normalise(data.knowledge))
  }

  useEffect(() => {
    apiGet('/api/knowledge')
      .then(apiJson)
      .then((data) => {
        if (data.knowledge) setKnowledge(normalise(data.knowledge))
      })
      .catch((err) => setAddResult(err?.message || '加载知识库失败'))
  }, [])

  const handleAdd = async () => {
    if (!newKnowledge.trim() || isAdding) return
    setIsAdding(true)
    setAddResult('')

    try {
      if (mode === 'import') {
        // Direct indexing: chunked like a .md file, no LLM rewriting.
        const data = await apiJson(
          await apiPost('/api/knowledge/add', {
            text: newKnowledge,
            category: importCategory,
            known_by: importKnownBy,
          }),
        )
        setAddResult(`已入库 ${data.added} 个片段`)
        if (data.knowledge) setKnowledge(normalise(data.knowledge))
      } else {
        // Narrated path: the GM rewrites it into setting prose first.
        const data = await apiJson(
          await apiPost('/api/command', { command: `!world ${newKnowledge}` }),
        )
        setAddResult(data.result || '添加完成')
        await refresh()
      }
      setNewKnowledge('')
    } catch (err) {
      setAddResult(`添加失败: ${err instanceof Error ? err.message : '未知错误'}`)
    } finally {
      setIsAdding(false)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      const data = await apiJson(await apiPost('/api/knowledge/delete', { id }))
      if (data.knowledge) setKnowledge(normalise(data.knowledge))
      setAddResult('已删除 1 个条目')
    } catch (err) {
      setAddResult(`删除失败: ${err instanceof Error ? err.message : '未知错误'}`)
    }
  }

  const filteredKnowledge = filterCategory
    ? knowledge.filter((k) => k.category === filterCategory)
    : knowledge

  const categories = [...new Set(knowledge.map((k) => k.category))]

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-content w-full max-w-2xl mx-auto mt-20 max-h-[70vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-coal-700">
          <h2 className="text-xl font-serif text-brass-400">世界观设置</h2>
          <button onClick={onClose} className="text-coal-400 hover:text-coal-200 text-2xl leading-none">
            ×
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {/* Filter */}
          {categories.length > 1 && (
            <div className="flex gap-2 flex-wrap">
              <button
                onClick={() => setFilterCategory('')}
                className={`px-2 py-1 text-xs rounded ${
                  !filterCategory ? 'bg-brass-700 text-coal-50' : 'bg-coal-800 text-coal-400'
                }`}
              >
                全部
              </button>
              {categories.map((cat) => (
                <button
                  key={cat}
                  onClick={() => setFilterCategory(cat)}
                  className={`px-2 py-1 text-xs rounded ${
                    filterCategory === cat ? 'bg-brass-700 text-coal-50' : 'bg-coal-800 text-coal-400'
                  }`}
                >
                  {cat}
                </button>
              ))}
            </div>
          )}

          {/* Knowledge list */}
          <div className="space-y-2">
            {filteredKnowledge.length === 0 ? (
              <p className="text-coal-500 text-sm italic">暂无世界观知识</p>
            ) : (
              filteredKnowledge.map((k) => (
                <div
                  key={k.id}
                  className="p-3 bg-coal-800 rounded border border-coal-700"
                >
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-sm text-coal-200 flex-1">{k.content}</p>
                    <button
                      onClick={() => handleDelete(k.id)}
                      title="删除这条知识"
                      className="shrink-0 px-1 text-xs text-coal-500 hover:text-rose-400 transition-colors"
                    >
                      删除
                    </button>
                  </div>
                  <div className="flex gap-2 mt-2 text-xs text-coal-500">
                    <span>分类: {k.category}</span>
                    <span>可见: {k.known_by}</span>
                  </div>
                </div>
              ))
            )}
          </div>

          {/* Add knowledge */}
          <div className="decorative-border p-4">
            <div className="flex items-center gap-2 mb-2">
              <h3 className="text-sm text-coal-300">添加世界观知识</h3>
              <div className="ml-auto flex gap-1">
                <button
                  onClick={() => setMode('narrate')}
                  className={`px-2 py-0.5 text-xs rounded ${
                    mode === 'narrate'
                      ? 'bg-brass-700 text-coal-50'
                      : 'bg-coal-800 text-coal-400'
                  }`}
                >
                  自然语言
                </button>
                <button
                  onClick={() => setMode('import')}
                  className={`px-2 py-0.5 text-xs rounded ${
                    mode === 'import'
                      ? 'bg-brass-700 text-coal-50'
                      : 'bg-coal-800 text-coal-400'
                  }`}
                >
                  整段导入
                </button>
              </div>
            </div>
            <p className="text-xs text-coal-500 mb-3">
              {mode === 'narrate'
                ? '用自然语言描述世界设定，系统会自动提炼为知识条目'
                : '直接粘贴整份设定文档（支持 Markdown），按段落切块入库，不经 LLM 改写'}
            </p>

            {mode === 'import' && (
              <div className="grid grid-cols-2 gap-2 mb-2">
                <input
                  type="text"
                  value={importCategory}
                  onChange={(e) => setImportCategory(e.target.value)}
                  placeholder="分类（可留空）"
                  className="input-field text-sm py-1"
                />
                <input
                  type="text"
                  value={importKnownBy}
                  onChange={(e) => setImportKnownBy(e.target.value)}
                  placeholder="可见角色，逗号分隔"
                  className="input-field text-sm py-1"
                />
              </div>
            )}

            <div className="flex gap-2">
              <textarea
                value={newKnowledge}
                onChange={(e) => setNewKnowledge(e.target.value)}
                placeholder={
                  mode === 'narrate'
                    ? '例如：贝克兰德的东区是贫民窟，治安很差...'
                    : '# 世界设定\n\n粘贴整份文档，可包含标题与多个段落……'
                }
                className={`input-field flex-1 resize-y ${
                  mode === 'import' ? 'min-h-[160px] font-mono text-xs' : 'min-h-[80px]'
                }`}
              />
            </div>
            <button
              onClick={handleAdd}
              disabled={isAdding || !newKnowledge.trim()}
              className="btn-primary mt-2 disabled:opacity-50"
            >
              {isAdding ? '添加中...' : '添加'}
            </button>
            {addResult && (
              <p className="mt-2 text-sm text-brass-300">{addResult}</p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
