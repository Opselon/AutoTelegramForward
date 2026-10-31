/**
 * The operator's theme: dark by default (the product's identity), light as a
 * first-class surface. One custom property swap on `<html data-theme>` restyles
 * every token consumer at once — no component reads the theme, which is why a
 * light theme costs no per-panel code.
 *
 * Storage follows the language preference's shape (`atf_lang` → `atf_theme`),
 * and `index.html` applies the stored value *before* the first paint, so a
 * light-theme reader never sees a dark flash while the bundle parses.
 */

export type Theme = 'dark' | 'light'

const STORAGE_KEY = 'atf_theme'

export function isTheme(value: string | null | undefined): value is Theme {
  return value === 'dark' || value === 'light'
}

/** Stored choice wins; a first visit (or corrupt value) follows the OS hint.
    This must mirror the pre-paint script in index.html exactly, or the React
    boot would re-assert a different surface than the one already painted. */
export function preferredTheme(): Theme {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (isTheme(stored)) return stored
  } catch {
    /* private mode: the choice just does not persist */
  }
  return systemTheme()
}

/** The theme the `color-scheme`-aware browser hints at, for first-visit default. */
export function systemTheme(): Theme {
  try {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

/**
 * Apply a theme to the document: the attribute the token block keys on, the
 * `color-scheme` that native form controls and scrollbars follow, and the
 * address-bar tint. Idempotent, so the pre-paint script and React can both
 * call it.
 */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement
  root.dataset.theme = theme
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')
  if (meta) meta.content = theme === 'light' ? '#f2f4f9' : '#070b15'
}

/** Apply now and remember. Returns the applied value for the caller. */
export function setTheme(theme: Theme): Theme {
  applyTheme(theme)
  try {
    localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    /* ignore: theme still applies for this visit */
  }
  return theme
}

export function toggleTheme(current: Theme): Theme {
  return setTheme(current === 'dark' ? 'light' : 'dark')
}
