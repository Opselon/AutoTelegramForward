/**
 * How a settings row decides what to show, and whether it can be handed back.
 *
 * Kept out of the component so the rules can be run directly (see
 * `scripts/verify_setting_provenance.mjs`) rather than only exercised by
 * clicking around: the interesting cases are all about absent data.
 *
 * The badge carries catalog keys, not sentences: the panel translates them in
 * whichever language is active, so a Russian or Chinese user reads "BOT" as
 * the language they read everything else in. Nothing here imports the catalog —
 * it stays a plain module with type-only imports, so the CI job can compile it
 * on its own.
 */
import type { SettingSourceInfo } from './types'

export type SettingMap = Record<string, any>
export type SourceMap = Record<string, SettingSourceInfo>

/**
 * The value a row displays.
 *
 * An unsaved edit wins, because that is what the user is looking at. Otherwise
 * it is the value the forwarder will use — the task's own if it has one, else
 * the bot's default, else the registry's — so the panel never shows a value the
 * task does not actually forward with.
 *
 * An older API returns no `sources` at all, in which case the stored value is
 * shown, exactly as before.
 */
export function rowValue(
  key: string,
  edits: SettingMap,
  settings: SettingMap,
  sources: SourceMap,
): any {
  if (Object.prototype.hasOwnProperty.call(edits, key)) return edits[key]
  const info = sources[key]
  if (!info) return settings[key]
  // `undefined` from a settled value means "use the stored value"; a value that
  // is legitimately null must still win, so compare by presence of the key.
  return 'value' in info ? info.value : settings[key]
}

/**
 * Whether the row can be handed back to the bot's default.
 *
 * Only when the task actually owns the value: an unsaved edit counts (dropping
 * it is the way to abandon the edit), as does a value the API attributes to the
 * task. Offering it on an already-inherited or already-default row would post a
 * no-op.
 */
export function canReset(key: string, edits: SettingMap, sources: SourceMap): boolean {
  if (Object.prototype.hasOwnProperty.call(edits, key)) return true
  return sources[key]?.source === 'task'
}

export interface RowBadge {
  /** Catalog key for the badge's own label ("TASK", "BOT", "DEFAULT"). */
  textKey: string
  /** The bot's name, when the badge names one; '' otherwise. */
  suffix: string
  color: string
  background: string
  /** Catalog key for the tooltip that explains where the value came from. */
  titleKey: string
}

const BADGES: Record<
  string,
  { textKey: string; color: string; background: string; titleKey: string }
> = {
  task: {
    textKey: 'ui_badge_task',
    color: '#4ade80',
    background: 'rgba(34,197,94,0.14)',
    titleKey: 'ui_badge_task_title',
  },
  bot: {
    textKey: 'ui_badge_bot',
    color: '#c4b5fd',
    background: 'rgba(139,92,246,0.16)',
    titleKey: 'ui_badge_bot_title',
  },
  default: {
    textKey: 'ui_badge_default',
    color: '#94a3b8',
    background: 'rgba(148,163,184,0.14)',
    titleKey: 'ui_badge_default_title',
  },
}

/**
 * The provenance badge for a row.
 *
 * The bot's badge carries its label so the user can tell *which* bot is
 * supplying the value; an absent or unknown source yields the default badge
 * rather than none, because a missing value is still a default.
 */
export function badgeFor(info: SettingSourceInfo | undefined): RowBadge | null {
  if (!info) return null
  const base = BADGES[info.source] ?? BADGES.default
  if (base === BADGES.bot && info.bot_label) {
    return { ...base, suffix: info.bot_label }
  }
  return { ...base, suffix: '' }
}

/** Whether the row's value is one this task inherited rather than chose. */
export function isInherited(info: SettingSourceInfo | undefined): boolean {
  return info?.source === 'bot'
}
