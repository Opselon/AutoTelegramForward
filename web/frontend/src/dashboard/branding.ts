/**
 * Branding hop (A -> C -> B): the config both the shell and the rules panel read.
 *
 * C is one of the operator's own channels: a matched message is copied into C
 * first and only then delivered from C to B, so B's forward header names C
 * instead of the original source channel.
 *
 * Every key is optional on the wire and no configured trigger means "always
 * hop", so the editor writes nothing until the operator changes a field: an
 * untouched rule saves byte-identical metadata.
 *
 * Extracted from `App.tsx` because the lazy rules panel needs the same reader
 * and summary as the shell; importing the shell from a chunk would be a cycle.
 */
import { makeT, type UiLang } from '../locales/engine'

export const BRANDING_HOP_KEY = 'branding_hop'

export interface BrandingHopConfig {
  triggers_enabled: boolean
  origin_ids: string[]
  origin_titles: string[]
  keywords: string[]
  match_mode: 'any' | 'all'
  require_forward: boolean
  force_forward: boolean
}

export const BRANDING_HOP_DEFAULTS: BrandingHopConfig = {
  triggers_enabled: false,
  origin_ids: [],
  origin_titles: [],
  keywords: [],
  match_mode: 'any',
  require_forward: false,
  force_forward: true,
}

/** Trimmed, de-duplicated, non-empty entries — the server coerces the same way. */
export function brandingList(value: any): string[] {
  const items = Array.isArray(value) ? value : value === undefined || value === null || value === '' ? [] : [value]
  const out: string[] = []
  for (const item of items) {
    const text = String(item ?? '').trim()
    if (text && !out.includes(text)) out.push(text)
  }
  return out
}

/**
 * `metadata.branding_hop` as a complete config. A missing blob, a half-written
 * one, or a foreign type (another client may store anything under the key) all
 * fall back to the defaults instead of throwing while the editor renders.
 */
export function readBrandingHop(meta: Record<string, any> | null | undefined): BrandingHopConfig {
  const raw = meta && typeof meta === 'object' ? meta[BRANDING_HOP_KEY] : null
  const blob: Record<string, any> = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
  return {
    triggers_enabled: blob.triggers_enabled === true,
    origin_ids: brandingList(blob.origin_ids),
    origin_titles: brandingList(blob.origin_titles),
    keywords: brandingList(blob.keywords),
    match_mode: String(blob.match_mode || '').toLowerCase() === 'all' ? 'all' : 'any',
    require_forward: blob.require_forward === true,
    force_forward: blob.force_forward === undefined ? BRANDING_HOP_DEFAULTS.force_forward : blob.force_forward === true,
  }
}

/** The list fields are edited as one comma-separated text input. */
export function brandingListToText(value: string[] | undefined): string {
  return (value || []).join(', ')
}

export function brandingTextToList(raw: string): string[] {
  return brandingList(String(raw || '').split(/[,\n]/))
}

/**
 * One plain-language line describing the route that will actually run, so the
 * operator reads the outcome instead of inferring it from a pile of switches.
 * Mirrors the engine's decision: no trigger means "always hop",
 * `require_forward` is a gate on top of the match, and `force_forward` is what
 * makes B show C's header (a copy from C would not).
 */
export function brandingHopSummary(lang: UiLang, hop: BrandingHopConfig, route: string): string {
  const t = makeT(lang)
  const delivery = hop.force_forward
    ? t('ui_native_forward_from_c_b_shows_c')
    : t('ui_copy_from_c_no_branded_header')
  const gate = hop.triggers_enabled && hop.require_forward
    ? t('ui_only_if_the_message_is_itself_a_forward')
    : ''
  if (!hop.triggers_enabled) {
    return t('ui_hop_summary_every_message', { route, delivery })
  }
  const parts: string[] = []
  const origins = [...hop.origin_titles, ...hop.origin_ids]
  if (origins.length) {
    parts.push(t('ui_forwarded_from_origins', { origins: origins.join(' / ') }))
  }
  if (hop.keywords.length) {
    parts.push(t('ui_containing_keywords', { keywords: hop.keywords.join(' / ') }))
  }
  if (!parts.length) {
    return t('ui_hop_summary_every_message_gate', { route, delivery, gate })
  }
  const joiner = hop.match_mode === 'all' ? t('ui_and') : t('ui_or')
  return t('ui_hop_summary_only_matching', {
    route,
    scope: t('ui_only_messages'),
    parts: parts.join(joiner),
    delivery,
    gate,
  })
}
