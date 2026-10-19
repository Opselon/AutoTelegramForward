/**
 * The dashboard's catalog: one key set, ten languages, English as reference.
 *
 * `t(key, params?, lang?)` is the only way a visible string reaches the screen.
 * `makeT(lang)` binds a language once for a component:
 *
 *     const t = makeT(lang)
 *     <span>{t('ui_active_rules')}</span>
 *     <span>{t('ui_forwarded_from', { origins: 'A / B' })}</span>
 *
 * Rules the tests enforce (`tests/uiCatalog.test.mjs`):
 *
 * - every key exists in all ten locales with no empty value;
 * - a value keeps the same `{placeholder}` set as its English reference;
 * - no English value contains RTL characters (a Persian leak under `en` was
 *   rendered to English users in production);
 * - no value contains U+FFFD (a mojibake'd translation);
 * - every key used by `t('…')` in `src/**` exists in the catalog.
 *
 * A missing key falls back to `en`, then to the key itself, and warns once in a
 * development build: silently rendering nothing would hide a typo in a key name
 * until someone happened to open that screen in that language.
 */
import { en } from './en.js'
import { fa } from './fa.js'
import { pt } from './pt.js'
import { uz } from './uz.js'
import { ru } from './ru.js'
import { id } from './id.js'
import { es } from './es.js'
import { fr } from './fr.js'
import { it } from './it.js'
import { zh } from './zh.js'
import type { Catalog, Translate, UiLang } from './types.js'
import { REFERENCE_LANG, isSupportedLang, normalizeLang } from './types.js'

export * from './types.js'

/**
 * Every catalog, keyed by language. `en` is complete; a key missing elsewhere is
 * a defect the parity test reports, and at runtime it renders the English text
 * rather than a blank line or the raw key.
 */
export const CATALOGS: Record<UiLang, Catalog> = { en, fa, pt, uz, ru, id, es, fr, it, zh }

/** Layout direction. Never used to pick a string — that is `t`'s job. */
export function isRtlLang(lang: string | null | undefined): boolean {
  return normalizeLang(lang) === 'fa'
}

const reported = new Set<string>()
function reportMissing(key: string, lang: UiLang): void {
  if (reported.has(lang + '\u0000' + key)) return
  reported.add(lang + '\u0000' + key)
  // `import.meta.env` is undefined outside Vite (the node tests compile this
  // module too), so read it defensively.
  const dev = (import.meta as { env?: { DEV?: boolean } }).env?.DEV === true
  if (dev && typeof console !== 'undefined') {
    console.warn(`[i18n] missing ${lang} value for "${key}"`)
  }
}

/** `{name}` / `{count}` substitution; an unknown placeholder is left intact. */
export function interpolate(template: string, params?: Record<string, string | number>): string {
  if (!params) return template
  return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (whole, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : whole,
  )
}

/**
 * The visible text for `key`.
 *
 * `lang` defaults to the reference locale so a helper with no language in scope
 * still renders English instead of a key; every component passes its own.
 */
export function t(
  key: string,
  params?: Record<string, string | number>,
  lang: string = REFERENCE_LANG,
): string {
  const code: UiLang = isSupportedLang(lang) ? lang : normalizeLang(lang)
  const catalog = CATALOGS[code] ?? CATALOGS.en
  let value = catalog[key]
  if (value === undefined) {
    if (code !== REFERENCE_LANG) reportMissing(key, code)
    value = CATALOGS.en[key]
  }
  if (value === undefined) {
    reportMissing(key, REFERENCE_LANG)
    return key
  }
  return interpolate(value, params)
}

/**
 * `t` bound to one language.
 *
 * The old two-language helper was `makeT(lang)` returning `(persian, english)`:
 * every call site handed over both strings, so a third language meant editing
 * all 458 of them. A catalog key is language-neutral, so the call sites do not
 * change when a language is added.
 */
export function makeT(lang: string | null | undefined): Translate {
  const code = normalizeLang(lang)
  return (key: string, params?: Record<string, string | number>) => t(key, params, code)
}
