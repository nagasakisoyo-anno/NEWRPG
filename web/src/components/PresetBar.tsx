import { useRef, useState } from 'react'
import {
  NpcSpec,
  PlayerSpec,
  SetupPreset,
  WorldSpec,
  buildPreset,
  parsePreset,
} from '../types/setup'

const STORAGE_KEY = 'trpg-setup-presets'

interface PresetBarProps {
  world: WorldSpec
  npcs: NpcSpec[]
  player: PlayerSpec
  onApply: (preset: SetupPreset) => void
  disabled?: boolean
}

function readStored(): SetupPreset[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function writeStored(list: SetupPreset[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(list))
  } catch {
    // Quota or private mode — saving presets is best-effort.
  }
}

export default function PresetBar({
  world,
  npcs,
  player,
  onApply,
  disabled = false,
}: PresetBarProps) {
  const [stored, setStored] = useState<SetupPreset[]>(readStored)
  const [note, setNote] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const saveLocal = () => {
    const name = window.prompt('预设名称', world.name || '我的设定')
    if (name === null) return
    const preset = buildPreset(name, world, npcs, player)
    const next = [preset, ...stored.filter((p) => p.name !== preset.name)].slice(0, 12)
    setStored(next)
    writeStored(next)
    setNote(`已保存预设「${preset.name}」`)
  }

  const removeLocal = (name: string) => {
    const next = stored.filter((p) => p.name !== name)
    setStored(next)
    writeStored(next)
    setNote(`已删除预设「${name}」`)
  }

  const exportFile = () => {
    const preset = buildPreset(world.name || '设定', world, npcs, player)
    const blob = new Blob([JSON.stringify(preset, null, 2)], {
      type: 'application/json',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `trpg-preset-${preset.name}.json`
    a.click()
    URL.revokeObjectURL(url)
    setNote('已导出 JSON 文件')
  }

  const importFile = async (file: File) => {
    try {
      const preset = parsePreset(JSON.parse(await file.text()))
      onApply(preset)
      setNote(`已导入「${preset.name}」`)
    } catch (err) {
      setNote(`导入失败：${err instanceof Error ? err.message : '文件无法解析'}`)
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-coal-400">预设：</span>
        <button
          type="button"
          onClick={saveLocal}
          disabled={disabled}
          className="px-2 py-0.5 text-xs rounded border border-coal-600 text-coal-300 hover:border-brass-600 hover:text-brass-400 transition-colors disabled:opacity-50"
        >
          保存当前
        </button>
        <button
          type="button"
          onClick={exportFile}
          disabled={disabled}
          className="px-2 py-0.5 text-xs rounded border border-coal-600 text-coal-300 hover:border-brass-600 hover:text-brass-400 transition-colors disabled:opacity-50"
        >
          导出 JSON
        </button>
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          disabled={disabled}
          className="px-2 py-0.5 text-xs rounded border border-coal-600 text-coal-300 hover:border-brass-600 hover:text-brass-400 transition-colors disabled:opacity-50"
        >
          导入 JSON
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) importFile(file)
            e.target.value = ''
          }}
        />
      </div>

      {stored.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {stored.map((p) => (
            <span
              key={p.name}
              className="inline-flex items-center gap-1 px-2 py-0.5 text-xs rounded bg-coal-800 border border-coal-700"
            >
              <button
                type="button"
                disabled={disabled}
                onClick={() => {
                  onApply(parsePreset(p))
                  setNote(`已载入「${p.name}」`)
                }}
                className="text-coal-300 hover:text-brass-400 transition-colors disabled:opacity-50"
              >
                {p.name}
              </button>
              <button
                type="button"
                disabled={disabled}
                onClick={() => removeLocal(p.name)}
                className="text-coal-600 hover:text-rose-400 transition-colors disabled:opacity-50"
                title="删除这个预设"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      {note && <p className="text-xs text-coal-500">{note}</p>}
    </div>
  )
}
