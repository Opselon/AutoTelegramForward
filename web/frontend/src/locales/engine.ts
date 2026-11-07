/**
 * The translation engine: one key set, ten languages, English as reference.
 *
 * `t(key, params?, lang?)` is the only way a visible string reaches the screen.
 * `makeT(lang)` binds a language once for a component:
 *
 *     const t = makeT(lang)
 *     <span>{t('ui_active_rules')}</span>
 *     <span>{t('ui_forwarding_from', { origins: 'A / B' })}</span>
 *
 * ## Why this file is separate from `index.ts`
 *
 * The ten catalogs are ~550 KB of source. Importing `./locales` from every
 * panel pulled *all ten* into the initial bundle — a Persian user downloaded
 * Russian, Chinese and Portuguese before the dashboard painted. The engine
 * keeps only the English reference static (it is the documented fallback and
 * must never be missing) and registers the other nine on demand, so a first
 * load ships two catalogs at most: `en` and the user's own.
 *
 * `index.ts` still exports the fully static `CATALOGS` record the catalog
 * contract test asserts against; the app imports this module instead.
 *
 * ## Rules the tests enforce (`tests/uiCatalog.test.mjs`)
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
import type { Catalog, Translate, UiLang } from './types.js'
import { REFERENCE_LANG, normalizeLang } from './types.js'

export * from './types.js'

/**
 * Catalogs currently in memory. `en` is registered at module load because it is
 * the fallback for every other language; the rest arrive through
 * `ensureLangLoaded` (one small chunk each, cached by the browser forever).
 */
const registry: Partial<Record<UiLang, Catalog>> = { [REFERENCE_LANG]: en }

/** Layout direction. Never used to pick a string — that is `t`'s job. */
export function isRtlLang(lang: string | null | undefined): boolean {
  return normalizeLang(lang) === 'fa'
}

/** Register catalogs that were loaded eagerly (the static test entry point). */
let registryVersion = 1

export function registerCatalogs(catalogs: Partial<Record<UiLang, Catalog>>): void {
  Object.assign(registry, catalogs)
  // Every landing catalog invalidates cached `t` bindings — see makeT.
  registryVersion += 1
}

/** The catalog in memory for `lang`, or undefined when it has not arrived yet. */
export function catalogFor(lang: string | null | undefined): Catalog | undefined {
  return registry[normalizeLang(lang)]
}

export type CatalogLoader = () => Promise<Catalog>

/**
 * One loader per non-reference locale. Written out rather than built from a
 * template string so the bundler can see every chunk statically.
 */
const LOADERS: Record<Exclude<UiLang, 'en'>, CatalogLoader> = {
  fa: () => import('./fa.js').then((m) => m.fa),
  pt: () => import('./pt.js').then((m) => m.pt),
  uz: () => import('./uz.js').then((m) => m.uz),
  ru: () => import('./ru.js').then((m) => m.ru),
  id: () => import('./id.js').then((m) => m.id),
  es: () => import('./es.js').then((m) => m.es),
  fr: () => import('./fr.js').then((m) => m.fr),
  it: () => import('./it.js').then((m) => m.it),
  zh: () => import('./zh.js').then((m) => m.zh),
}

const pending = new Map<UiLang, Promise<void>>()

/** `true` once the catalog for `lang` is in memory. */
export function isLangLoaded(lang: string | null | undefined): boolean {
  return registry[normalizeLang(lang)] !== undefined
}

/**
 * Load a locale, once.
 *
 * An unsupported code normalises to `en`, which is always present. A failed
 * load resolves instead of rejecting: the dashboard renders the English
 * fallback rather than a blank panel, and the next switch retries.
 */
export function ensureLangLoaded(lang: string | null | undefined): Promise<void> {
  const code = normalizeLang(lang)
  if (registry[code]) return Promise.resolve()
  const already = pending.get(code)
  if (already) return already
  const load = LOADERS[code as Exclude<UiLang, 'en'>]
  if (!load) return Promise.resolve()
  const promise = load()
    .then((catalog) => {
      registerCatalogs({ [code]: catalog })
    })
    .catch(() => {
      pending.delete(code)
    })
  pending.set(code, promise)
  return promise
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
  const code: UiLang = normalizeLang(lang)
  const catalog = registry[code] ?? registry[REFERENCE_LANG]
  let value = catalog?.[key]
  if (value === undefined) {
    if (code !== REFERENCE_LANG) reportMissing(key, code)
    value = registry[REFERENCE_LANG]?.[key]
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
 *
 * One closure per language code *per registry version*, cached. Identity
 * stability matters because components bind `makeT(lang)` during render and
 * list `t` in effect/callback deps — a fresh closure per render re-ran those
 * effects on every pass (an infinite fetch loop). But the binding must ALSO
 * change identity when a catalog chunk lands mid-session: memoized consumers
 * (the shell's nav labels) key their own memos on `t`, so a stale identity
 * would pin pre-load English strings on screen. Hence the version stamp:
 * stable within a registry version, new after every load. The lookup itself
 * still reads the registry at call time, so a mid-session RE-load of the same
 * language renders fresh strings through either identity.
 */
const translateCache = new Map<string, { at: number; translate: Translate }>()

export function makeT(lang: string | null | undefined): Translate {
  const code = normalizeLang(lang)
  const cached = translateCache.get(code)
  if (cached && cached.at === registryVersion) return cached.translate
  const bound: Translate = (key: string, params?: Record<string, string | number>) =>
    t(key, params, code)
  translateCache.set(code, { at: registryVersion, translate: bound })
  return bound
}
