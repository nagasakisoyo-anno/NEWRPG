import {
  PlayerSpec,
  PLAYER_ATTRIBUTE_KEYS,
  makeSkill,
  makeDefaultPlayer,
} from '../types/setup'

interface PlayerBuilderProps {
  player: PlayerSpec
  onChange: (player: PlayerSpec) => void
  disabled?: boolean
}

/** Standard array style budget: 6 attributes averaging 10 => 60 points. */
const POINT_BUDGET = 60

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
  const modifier = Math.floor((value - 10) / 2)
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
      <span className="w-8 shrink-0 text-right text-xs font-mono text-coal-500">
        {modifier >= 0 ? `+${modifier}` : modifier}
      </span>
    </label>
  )
}

export default function PlayerBuilder({
  player,
  onChange,
  disabled = false,
}: PlayerBuilderProps) {
  const set = (patch: Partial<PlayerSpec>) => onChange({ ...player, ...patch })

  const spent = PLAYER_ATTRIBUTE_KEYS.reduce(
    (sum, k) => sum + (player.attributes[k] ?? 10),
    0,
  )
  const overBudget = spent > POINT_BUDGET
  const maxHp = (player.attributes['体质'] ?? 10) * 3

  const setAttr = (key: string, value: number) =>
    set({ attributes: { ...player.attributes, [key]: value } })

  const setSkill = (id: string, patch: Partial<{ name: string; value: number }>) =>
    set({
      skills: player.skills.map((s) => (s.id === id ? { ...s, ...patch } : s)),
    })

  return (
    <div className="rounded border border-coal-700 bg-coal-800/40 p-3 space-y-3">
      {/* Identity */}
      <div className="grid grid-cols-2 gap-2">
        <div>
          <label className="block text-xs text-coal-400 mb-1">角色姓名</label>
          <input
            type="text"
            value={player.name}
            disabled={disabled}
            onChange={(e) => set({ name: e.target.value })}
            placeholder="留空则用 config.yaml 的角色"
            className="input-field w-full text-sm py-1 disabled:opacity-50"
          />
        </div>
        <div>
          <label className="block text-xs text-coal-400 mb-1">
            心之锚
            <span className="ml-1 text-coal-500">（最在意的人或事）</span>
          </label>
          <input
            type="text"
            value={player.anchor}
            disabled={disabled}
            onChange={(e) => set({ anchor: e.target.value })}
            placeholder="如：守护妹妹（可留空）"
            className="input-field w-full text-sm py-1 disabled:opacity-50"
          />
        </div>
      </div>

      {/* Background */}
      <div>
        <label className="block text-xs text-coal-400 mb-1">角色背景</label>
        <textarea
          value={player.core}
          disabled={disabled}
          onChange={(e) => set({ core: e.target.value })}
          rows={2}
          placeholder="多条用分号分隔。例：破产的钟表学徒；欠着一屁股债；左手有旧伤"
          className="input-field w-full text-sm resize-none disabled:opacity-50"
        />
      </div>

      {/* Attributes */}
      <div>
        <div className="flex items-baseline justify-between mb-1">
          <span className="text-xs text-coal-400">属性（10 为常人，括号为调整值）</span>
          <span
            className={`text-xs font-mono ${overBudget ? 'text-amber-400' : 'text-coal-500'}`}
            title="建议总点数 60，超出不会被拒绝，只是会更强"
          >
            {spent} / {POINT_BUDGET}
          </span>
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-2">
          {PLAYER_ATTRIBUTE_KEYS.map((key) => (
            <AttrSlider
              key={key}
              label={key}
              value={player.attributes[key] ?? 10}
              disabled={disabled}
              onChange={(v) => setAttr(key, v)}
            />
          ))}
        </div>
        <p className="mt-2 text-xs text-coal-500">
          HP 上限 = 体质 × 3 ＝ <span className="text-coal-300">{maxHp}</span>
        </p>
      </div>

      {/* Skills */}
      <div>
        <div className="flex items-baseline justify-between mb-1">
          <span className="text-xs text-coal-400">技能（d100 检定，数值越高越容易成功）</span>
          <button
            type="button"
            disabled={disabled || player.skills.length >= 10}
            onClick={() => set({ skills: [...player.skills, makeSkill()] })}
            className="text-xs text-coal-500 hover:text-brass-400 transition-colors disabled:opacity-40"
          >
            + 添加技能
          </button>
        </div>
        <div className="space-y-2">
          {player.skills.map((skill) => (
            <div key={skill.id} className="flex items-center gap-2">
              <input
                type="text"
                value={skill.name}
                disabled={disabled}
                onChange={(e) => setSkill(skill.id, { name: e.target.value })}
                placeholder="技能名"
                className="input-field w-28 text-sm py-1 disabled:opacity-50"
              />
              <input
                type="range"
                min={1}
                max={99}
                value={skill.value}
                disabled={disabled}
                onChange={(e) => setSkill(skill.id, { value: Number(e.target.value) })}
                className="flex-1 accent-brass-500 disabled:opacity-50"
              />
              <span className="w-7 text-right text-xs font-mono text-coal-200">
                {skill.value}
              </span>
              <button
                type="button"
                disabled={disabled}
                onClick={() =>
                  set({ skills: player.skills.filter((s) => s.id !== skill.id) })
                }
                className="px-1 text-xs text-coal-500 hover:text-rose-400 transition-colors disabled:opacity-50"
                title="删除技能"
              >
                ×
              </button>
            </div>
          ))}
          {player.skills.length === 0 && (
            <p className="text-xs text-coal-500">没有技能 —— 所有技能检定按 50 处理。</p>
          )}
        </div>
      </div>

      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange(makeDefaultPlayer())}
        className="text-xs text-coal-500 hover:text-brass-400 transition-colors disabled:opacity-50"
      >
        重置为默认
      </button>
    </div>
  )
}
