/** Shared types for the new-game setup screen. */

export const NPC_ATTRIBUTE_KEYS = ['力量', '敏捷', '体质', '智力', '感知', '魅力'] as const

export const PLAYER_ATTRIBUTE_KEYS = NPC_ATTRIBUTE_KEYS

export interface SkillSpec {
  id: string
  name: string
  value: number
}

export interface PlayerSpec {
  name: string
  core: string
  anchor: string
  attributes: Record<string, number>
  skills: SkillSpec[]
}

export interface NpcSpec {
  /** Local-only key for React list rendering. */
  id: string
  name: string
  /** Background lines, semicolon separated in the UI. */
  core: string
  tone: string
  relation: string
  verbalTics: string
  attributes: Record<string, number>
}

export interface WorldSpec {
  name: string
  description: string
  era: string
  climate: string
  tone: string
  tech: string
  magic: string
  danger: string
  extra: string
  timeOfDay: string
  weather: string
}

let npcSeq = 0
let skillSeq = 0

export function makeSkill(name = '', value = 50): SkillSpec {
  skillSeq += 1
  return { id: `skill-${Date.now()}-${skillSeq}`, name, value }
}

export function makeDefaultPlayer(): PlayerSpec {
  return {
    name: '',
    core: '',
    anchor: '',
    attributes: Object.fromEntries(PLAYER_ATTRIBUTE_KEYS.map((k) => [k, 10])),
    skills: [makeSkill('观察', 50), makeSkill('交涉', 50), makeSkill('潜行', 50)],
  }
}

/** Convert the player card into the `/api/start` payload, or undefined when untouched. */
export function playerToPayload(player: PlayerSpec) {
  if (!player.name.trim()) return undefined
  return {
    name: player.name.trim(),
    core: player.core.trim() || `${player.name.trim()} — 冒险者`,
    anchor: player.anchor.trim(),
    attributes: player.attributes,
    skills: player.skills
      .filter((s) => s.name.trim())
      .map((s) => ({ name: s.name.trim(), value: s.value })),
  }
}

/** Everything the setup screen can export to / import from a JSON preset. */
export interface SetupPreset {
  version: 1
  savedAt: string
  name: string
  world: WorldSpec
  npcs: NpcSpec[]
  player: PlayerSpec
}

export function buildPreset(
  name: string,
  world: WorldSpec,
  npcs: NpcSpec[],
  player: PlayerSpec,
): SetupPreset {
  return {
    version: 1,
    savedAt: new Date().toISOString(),
    name: name.trim() || world.name.trim() || '未命名预设',
    world,
    npcs,
    player,
  }
}

/** Validate and normalise an imported preset; throws on bad input. */
export function parsePreset(raw: unknown): SetupPreset {
  if (!raw || typeof raw !== 'object') throw new Error('预设格式无效')
  const obj = raw as Record<string, any>
  if (!obj.world || typeof obj.world !== 'object') throw new Error('预设缺少 world 字段')

  const world: WorldSpec = { ...makeEmptyWorld(), ...obj.world }
  const npcs: NpcSpec[] = Array.isArray(obj.npcs)
    ? obj.npcs.map((n: any) => ({
        ...makeEmptyNpc(),
        ...n,
        // Regenerate local ids so React keys stay unique after import.
        id: makeEmptyNpc().id,
        attributes: {
          ...Object.fromEntries(NPC_ATTRIBUTE_KEYS.map((k) => [k, 10])),
          ...(n?.attributes || {}),
        },
      }))
    : []
  const player: PlayerSpec = {
    ...makeDefaultPlayer(),
    ...(obj.player || {}),
    attributes: {
      ...Object.fromEntries(PLAYER_ATTRIBUTE_KEYS.map((k) => [k, 10])),
      ...(obj.player?.attributes || {}),
    },
    skills: Array.isArray(obj.player?.skills)
      ? obj.player.skills.map((s: any) => makeSkill(s?.name ?? '', Number(s?.value) || 50))
      : makeDefaultPlayer().skills,
  }

  return {
    version: 1,
    savedAt: typeof obj.savedAt === 'string' ? obj.savedAt : new Date().toISOString(),
    name: typeof obj.name === 'string' ? obj.name : '导入的预设',
    world,
    npcs,
    player,
  }
}

export function makeEmptyNpc(): NpcSpec {
  npcSeq += 1
  return {
    id: `npc-${Date.now()}-${npcSeq}`,
    name: '',
    core: '',
    tone: '',
    relation: '',
    verbalTics: '',
    attributes: Object.fromEntries(NPC_ATTRIBUTE_KEYS.map((k) => [k, 10])),
  }
}

export function makeEmptyWorld(): WorldSpec {
  return {
    name: '',
    description: '',
    era: '',
    climate: '',
    tone: '',
    tech: '',
    magic: '',
    danger: '',
    extra: '',
    timeOfDay: '',
    weather: '',
  }
}

/** Convert the UI world card into the `/api/start` payload shape. */
export function worldToPayload(world: WorldSpec) {
  return {
    worldview: world.description.trim() || undefined,
    world_options: {
      name: world.name.trim(),
      era: world.era.trim(),
      climate: world.climate.trim(),
      tone: world.tone.trim(),
      tech: world.tech.trim(),
      magic: world.magic.trim(),
      danger: world.danger.trim(),
      extra: world.extra.trim(),
      time_of_day: world.timeOfDay.trim(),
      weather: world.weather.trim(),
    },
  }
}

/** Convert NPC cards into the `/api/start` payload shape, dropping unnamed rows. */
export function npcsToPayload(npcs: NpcSpec[]) {
  return npcs
    .filter((n) => n.name.trim())
    .map((n) => ({
      name: n.name.trim(),
      core: n.core.trim(),
      tone: n.tone.trim(),
      relation: n.relation.trim(),
      verbal_tics: n.verbalTics.trim(),
      attributes: n.attributes,
    }))
}
