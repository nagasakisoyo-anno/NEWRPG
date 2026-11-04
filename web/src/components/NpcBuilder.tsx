import { NpcSpec, NPC_ATTRIBUTE_KEYS, makeEmptyNpc } from '../types/setup'

interface NpcBuilderProps {
  npcs: NpcSpec[]
  onChange: (npcs: NpcSpec[]) => void
  disabled?: boolean
  max?: number
}

const TONE_PRESETS = [
  '温和友好',
  '冷淡寡言',
  '精明圆滑',
  '暴躁易怒',
  '神秘莫测',
  '天真热情',
  '谨慎多疑',
]

/** Attribute slider: 1-20, 10 is the human average. */
function AttrSlider({
  label,
  value,
  onChange,
  disabled,
}: {
  label: string
  value: number
  onChange: (v: number) => void
  disabled?: boolean
}) {
  const tint =
    value >= 15 ? 'text-emerald-400' : value <= 6 ? 'text-rose-400' : 'text-coal-200'
  return (
    <label className="flex items-center gap-2">
      <span className="w-8 shrink-0 text-xs text-coal-400">{label}</span>
      <input
        type="range"
        min={1}
        max={20}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
        className="flex-1 accent-brass-500 disabled:opacity-50"
      />
      <span className={`w-6 shrink-0 text-right text-xs font-mono ${tint}`}>{value}</span>
    </label>
  )
}

export default function NpcBuilder({
  npcs,
  onChange,
  disabled = false,
  max = 6,
}: NpcBuilderProps) {
  const update = (index: number, patch: Partial<NpcSpec>) => {
    onChange(npcs.map((n, i) => (i === index ? { ...n, ...patch } : n)))
  }

  const updateAttr = (index: number, key: string, value: number) => {
    const target = npcs[index]
    update(index, { attributes: { ...target.attributes, [key]: value } })
  }

  const add = () => {
    if (npcs.length >= max) return
    onChange([...npcs, makeEmptyNpc()])
  }

  const remove = (index: number) => {
    onChange(npcs.filter((_, i) => i !== index))
  }

  return (
    <div className="space-y-3">
      {npcs.length === 0 && (
        <p className="text-xs text-coal-500">
          未添加初始 NPC —— 开场将只包含角色背景故事里的人物。
        </p>
      )}

      {npcs.map((npc, i) => (
        <div
          key={npc.id}
          className="rounded border border-coal-700 bg-coal-800/40 p-3 space-y-3"
        >
          {/* Card header */}
          <div className="flex items-center gap-2">
            <span className="text-xs font-mono text-brass-500">NPC {i + 1}</span>
            <input
              type="text"
              value={npc.name}
              disabled={disabled}
              onChange={(e) => update(i, { name: e.target.value })}
              placeholder="姓名（必填）"
              className="input-field flex-1 text-sm py-1 disabled:opacity-50"
            />
            <button
              type="button"
              onClick={() => remove(i)}
              disabled={disabled}
              title="删除这个 NPC"
              className="px-2 py-1 text-xs text-coal-400 hover:text-rose-400 transition-colors disabled:opacity-50"
            >
              移除
            </button>
          </div>

          {/* Background */}
          <textarea
            value={npc.core}
            disabled={disabled}
            onChange={(e) => update(i, { core: e.target.value })}
            rows={2}
            placeholder="身份与背景，多条用分号分隔。例：这家酒馆的老板；二十年前当过兵"
            className="input-field w-full text-sm resize-none disabled:opacity-50"
          />

          {/* Tone + relation */}
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-xs text-coal-400 mb-1">说话语调</label>
              <input
                type="text"
                list={`tone-presets-${npc.id}`}
                value={npc.tone}
                disabled={disabled}
                onChange={(e) => update(i, { tone: e.target.value })}
                placeholder="如：冷淡寡言"
                className="input-field w-full text-sm py-1 disabled:opacity-50"
              />
              <datalist id={`tone-presets-${npc.id}`}>
                {TONE_PRESETS.map((t) => (
                  <option key={t} value={t} />
                ))}
              </datalist>
            </div>
            <div>
              <label className="block text-xs text-coal-400 mb-1">与玩家的关系</label>
              <input
                type="text"
                value={npc.relation}
                disabled={disabled}
                onChange={(e) => update(i, { relation: e.target.value })}
                placeholder="如：旧识 / 债主（可留空）"
                className="input-field w-full text-sm py-1 disabled:opacity-50"
              />
            </div>
          </div>

          {/* Verbal tic */}
          <div>
            <label className="block text-xs text-coal-400 mb-1">口头禅 / 语言习惯</label>
            <input
              type="text"
              value={npc.verbalTics}
              disabled={disabled}
              onChange={(e) => update(i, { verbalTics: e.target.value })}
              placeholder="如：总把话说一半（可留空）"
              className="input-field w-full text-sm py-1 disabled:opacity-50"
            />
          </div>

          {/* Attributes */}
          <details className="group">
            <summary className="cursor-pointer text-xs text-coal-400 hover:text-brass-500 transition-colors">
              属性数值（10 为常人；体质决定 HP）
            </summary>
            <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-2">
              {NPC_ATTRIBUTE_KEYS.map((key) => (
                <AttrSlider
                  key={key}
                  label={key}
                  value={npc.attributes[key] ?? 10}
                  disabled={disabled}
                  onChange={(v) => updateAttr(i, key, v)}
                />
              ))}
            </div>
            <p className="mt-2 text-xs text-coal-500">
              HP 上限 = 体质 × 3 ＝ {(npc.attributes['体质'] ?? 10) * 3}
            </p>
          </details>
        </div>
      ))}

      <button
        type="button"
        onClick={add}
        disabled={disabled || npcs.length >= max}
        className="w-full py-2 text-sm border border-dashed border-coal-600 rounded text-coal-400 hover:text-brass-400 hover:border-brass-600 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
      >
        {npcs.length >= max ? `最多 ${max} 个 NPC` : '+ 添加 NPC'}
      </button>
    </div>
  )
}
