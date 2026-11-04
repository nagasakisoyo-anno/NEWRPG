import { WorldSpec } from '../types/setup'

interface WorldBuilderProps {
  world: WorldSpec
  onChange: (world: WorldSpec) => void
  disabled?: boolean
}

const PRESETS: { label: string; patch: Partial<WorldSpec> }[] = [
  {
    label: '蒸汽迷雾',
    patch: {
      name: '雾都',
      description:
        '一座被煤烟与浓雾笼罩的工业都市，蒸汽机轰鸣不息，而雾里藏着不该存在的东西。',
      era: '蒸汽纪元',
      climate: '终年浓雾，细雨不断',
      tone: '黑暗惊悚',
      tech: '蒸汽机械、煤气灯',
      magic: '隐秘存在，常人不知',
      danger: '中等，夜间危险',
      timeOfDay: '黄昏',
      weather: '薄雾',
    },
  },
  {
    label: '剑与魔法',
    patch: {
      name: '艾瑞大陆',
      description:
        '王国林立的古典大陆，魔法是公开的力量，古代遗迹里埋着足以改写格局的秘密。',
      era: '中世纪',
      climate: '四季分明，内陆温带',
      tone: '史诗冒险',
      tech: '冷兵器、炼金术',
      magic: '公开且体系化',
      danger: '荒野凶险，城镇安全',
      timeOfDay: '清晨',
      weather: '晴',
    },
  },
  {
    label: '赛博都市',
    patch: {
      name: '新港',
      description:
        '霓虹与酸雨交织的巨型都市，巨企取代政府，义体改造与数据走私是日常生意。',
      era: '近未来',
      climate: '酸雨频繁，常年湿热',
      tone: '冷硬写实',
      tech: '义体、脑机接口、AI',
      magic: '不存在',
      danger: '高，街区帮派割据',
      timeOfDay: '深夜',
      weather: '酸雨',
    },
  },
]

function Field({
  label,
  value,
  onChange,
  placeholder,
  disabled,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder: string
  disabled?: boolean
}) {
  return (
    <div>
      <label className="block text-xs text-coal-400 mb-1">{label}</label>
      <input
        type="text"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="input-field w-full text-sm py-1 disabled:opacity-50"
      />
    </div>
  )
}

export default function WorldBuilder({
  world,
  onChange,
  disabled = false,
}: WorldBuilderProps) {
  const set = (patch: Partial<WorldSpec>) => onChange({ ...world, ...patch })

  return (
    <div className="rounded border border-coal-700 bg-coal-800/40 p-3 space-y-3">
      {/* Presets */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-coal-400">模板：</span>
        {PRESETS.map((p) => (
          <button
            key={p.label}
            type="button"
            disabled={disabled}
            onClick={() => set(p.patch)}
            className="px-2 py-0.5 text-xs rounded border border-coal-600 text-coal-300 hover:border-brass-600 hover:text-brass-400 transition-colors disabled:opacity-50"
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* World name */}
      <Field
        label="世界名称"
        value={world.name}
        onChange={(v) => set({ name: v })}
        placeholder="留空则自动取描述的第一句"
        disabled={disabled}
      />

      {/* Core description */}
      <div>
        <label className="block text-xs text-coal-400 mb-1">
          世界观描述
          <span className="ml-2 text-coal-500">（留空则用 config.yaml 默认设定）</span>
        </label>
        <textarea
          value={world.description}
          disabled={disabled}
          onChange={(e) => set({ description: e.target.value })}
          rows={3}
          placeholder="例：雾锁星港，一座悬浮于云海之上的钟表城邦，所有居民的寿命都由一枚怀表计量。"
          className="input-field w-full text-sm resize-none disabled:opacity-50"
        />
      </div>

      {/* Structured details */}
      <div className="grid grid-cols-2 gap-x-3 gap-y-2">
        <Field
          label="纪元 / 时代"
          value={world.era}
          onChange={(v) => set({ era: v })}
          placeholder="如：蒸汽纪元"
          disabled={disabled}
        />
        <Field
          label="气候"
          value={world.climate}
          onChange={(v) => set({ climate: v })}
          placeholder="如：终年浓雾"
          disabled={disabled}
        />
        <Field
          label="叙事基调"
          value={world.tone}
          onChange={(v) => set({ tone: v })}
          placeholder="如：黑暗惊悚"
          disabled={disabled}
        />
        <Field
          label="技术水平"
          value={world.tech}
          onChange={(v) => set({ tech: v })}
          placeholder="如：蒸汽机械"
          disabled={disabled}
        />
        <Field
          label="超自然程度"
          value={world.magic}
          onChange={(v) => set({ magic: v })}
          placeholder="如：隐秘存在"
          disabled={disabled}
        />
        <Field
          label="危险程度"
          value={world.danger}
          onChange={(v) => set({ danger: v })}
          placeholder="如：夜间危险"
          disabled={disabled}
        />
        <Field
          label="开场时间"
          value={world.timeOfDay}
          onChange={(v) => set({ timeOfDay: v })}
          placeholder="默认 清晨"
          disabled={disabled}
        />
        <Field
          label="开场天气"
          value={world.weather}
          onChange={(v) => set({ weather: v })}
          placeholder="默认 薄雾"
          disabled={disabled}
        />
      </div>

      {/* Extra notes */}
      <div>
        <label className="block text-xs text-coal-400 mb-1">补充设定</label>
        <textarea
          value={world.extra}
          disabled={disabled}
          onChange={(e) => set({ extra: e.target.value })}
          rows={2}
          placeholder="势力、禁忌、特殊规则……这些会一并影响之后每一回合的叙事"
          className="input-field w-full text-sm resize-none disabled:opacity-50"
        />
      </div>
    </div>
  )
}
