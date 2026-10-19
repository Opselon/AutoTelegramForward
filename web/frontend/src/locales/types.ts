/**
 * The dashboard's languages, in one place.
 *
 * The dashboard used to carry its own two-language traffic (`isRtl ? '…' : '…'`
 * in 458 places, plus a pile of Persian-only literals that showed Persian to an
 * English user), while the bot and the mobile app had already moved to a
 * catalog with strict parity across the ten languages the product ships. This
 * module is the same ten, one key set, one lookup.
 *
 * `en` is the reference locale: every key exists in it, and a key that is
 * missing anywhere else falls back to it — never to Persian, which is what a
 * Persian default did to every new user (see `App.tsx` / `pages/Auth.tsx`).
 */

/** The ten languages the product ships. Adding one is a ten-line change here. */
export type UiLang = 'en' | 'fa' | 'pt' | 'uz' | 'ru' | 'id' | 'es' | 'fr' | 'it' | 'zh'

/** The reference locale: complete by construction, the fallback for the rest. */
export const REFERENCE_LANG: UiLang = 'en'

export interface LangMeta {
  code: UiLang
  /** The language's name in its own script — what the picker shows. */
  endonym: string
  /** Right-to-left script. Drives *layout* (`direction`, icon side) only. */
  rtl: boolean
}

/**
 * The picker's order and the validation list for a stored preference.
 *
 * Endonyms, not flags: `فارسی` is readable to the person who needs it, and a
 * flag names a country rather than a language (Persian is not one country's).
 */
export const SUPPORTED_LANGS: readonly LangMeta[] = [
  { code: 'en', endonym: 'English', rtl: false },
  { code: 'fa', endonym: 'فارسی', rtl: true },
  { code: 'pt', endonym: 'Português', rtl: false },
  { code: 'uz', endonym: "O'zbekcha", rtl: false },
  { code: 'ru', endonym: 'Русский', rtl: false },
  { code: 'id', endonym: 'Bahasa Indonesia', rtl: false },
  { code: 'es', endonym: 'Español', rtl: false },
  { code: 'fr', endonym: 'Français', rtl: false },
  { code: 'it', endonym: 'Italiano', rtl: false },
  { code: 'zh', endonym: '中文', rtl: false },
] as const

const CODES: readonly UiLang[] = SUPPORTED_LANGS.map((l) => l.code)

/** `true` only for a code the product ships; a stray string is not a language. */
export function isSupportedLang(value: unknown): value is UiLang {
  return typeof value === 'string' && (CODES as readonly string[]).includes(value)
}

/**
 * The first supported language for a preference chain.
 *
 * `navigator.language` is `fa-IR`, `pt-BR`, `zh-CN`, … — the region is not a
 * language the catalog knows, so match on the primary subtag. An unsupported
 * preference yields `en`, never Persian.
 */
export function normalizeLang(value: string | null | undefined): UiLang {
  const raw = String(value ?? '').trim().toLowerCase()
  if (!raw) return REFERENCE_LANG
  if (isSupportedLang(raw)) return raw
  const primary = raw.split(/[-_]/)[0]
  return isSupportedLang(primary) ? primary : REFERENCE_LANG
}

/** Where the choice lives. One key, read by the shell and by the login page. */
export const LANG_STORAGE_KEY = 'atf_lang'

/* eslint-disable no-restricted-globals */
/**
 * The language stored for this browser, or the browser's own preference.
 *
 * Read through a guard because a browser with storage blocked (private mode,
 * a partitioned iframe — which is exactly how the Telegram Mini App runs this)
 * throws on `localStorage` rather than returning null.
 */
export function preferredLang(): UiLang {
  let stored: string | null = null
  try {
    stored = localStorage.getItem(LANG_STORAGE_KEY)
  } catch {
    /* storage blocked: fall through to the browser's own language */
  }
  if (stored) return normalizeLang(stored)
  return normalizeLang(typeof navigator === 'undefined' ? null : navigator.language)
}

/** Remember the choice. Fails soft: a blocked store is not an error here. */
export function rememberLang(lang: UiLang): void {
  try {
    localStorage.setItem(LANG_STORAGE_KEY, lang)
  } catch {
    /* storage blocked */
  }
}
/* eslint-enable no-restricted-globals */

/** A complete catalog: every key, one value per language. */
export type Catalog = Record<string, string>

/** Words the user sees for a key, with `{name}` placeholders substituted. */
export type Translate = (key: string, params?: Record<string, string | number>) => string
