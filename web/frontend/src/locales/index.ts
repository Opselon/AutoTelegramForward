/**
 * The dashboard's catalog: one key set, ten languages, English as reference.
 *
 * This module is the **static** entry point. It holds every catalog eagerly and
 * exists for the catalog contract test (`tests/uiCatalog.test.mjs`), which
 * asserts all ten locales against each other, and for any tool that needs the
 * complete key set without a browser. The app itself imports `./engine`, which
 * keeps only `en` static and fetches the other nine on demand — see the note at
 * the top of `engine.ts` for why that split exists: ten eagerly imported
 * catalogs are ~550 KB of source, and a first load shipped all of it, in
 * languages the reader does not speak.
 *
 * `t(key, params?, lang?)` is the only way a visible string reaches the screen.
 * `makeT(lang)` binds a language once for a component:
 *
 *     const t = makeT(lang)
 *     <span>{t('ui_active_rules')}</span>
 *     <span>{t('ui_forwarded_from', { origins: 'A / B' })}</span>
 *
 * Rules the tests enforce:
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
import { registerCatalogs } from './engine.js'
import type { Catalog, UiLang } from './types.js'

export * from './types.js'
export * from './engine.js'

/**
 * Every catalog, keyed by language. `en` is complete; a key missing elsewhere is
 * a defect the parity test reports, and at runtime it renders the English text
 * rather than a blank line or the raw key.
 */
export const CATALOGS: Record<UiLang, Catalog> = { en, fa, pt, uz, ru, id, es, fr, it, zh }

// Register eagerly, so `t`/`makeT` re-exported from this entry point resolve
// against all ten catalogs exactly as they did before the engine was split out.
registerCatalogs(CATALOGS)
