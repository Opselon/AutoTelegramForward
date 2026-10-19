import { Globe } from 'lucide-react'
import { SUPPORTED_LANGS, isRtlLang, t, type UiLang } from '../locales'

/**
 * The language picker.
 *
 * It replaces a two-way `fa ↔ en` toggle that could only ever express two
 * languages: with ten catalogs behind it, a button labelled "English" meant
 * "anything that is not Persian", and a Portuguese user had no way to say so.
 *
 * Each option is the language's endonym, in its own script (`فارسی`, `Русский`,
 * `中文`), because that is what the person looking for their language
 * recognises — a flag names a country, not a language. The choice is applied by
 * the shell, which persists it and re-renders the tree.
 */
export function LanguagePicker({
  lang,
  onChange,
  compact = false,
}: {
  lang: UiLang
  onChange: (lang: UiLang) => void
  /** The header's tighter variant (the login page and the dashboard top bar). */
  compact?: boolean
}) {
  return (
    <label
      // The control itself stays LTR: the endonyms are their own scripts and a
      // reversed row reads as a jumble. Only the surrounding layout follows the
      // page direction.
      dir="ltr"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        background: 'rgba(15, 23, 42, 0.6)',
        border: '1px solid rgba(255, 255, 255, 0.12)',
        borderRadius: 8,
        padding: compact ? '4px 8px' : '6px 10px',
        color: '#cbd5e1',
        fontSize: compact ? '0.75rem' : '0.8rem',
      }}
    >
      <Globe size={13} aria-hidden="true" />
      <select
        className="lang-picker"
        value={lang}
        onChange={(event) => onChange(event.target.value as UiLang)}
        aria-label={t('ui_language_label', undefined, lang)}
        style={{
          background: 'transparent',
          border: 'none',
          color: 'inherit',
          font: 'inherit',
          outline: 'none',
          cursor: 'pointer',
          padding: 0,
          // The endonyms are always left-to-right strings, even on the RTL page.
          direction: isRtlLang(lang) ? 'ltr' : undefined,
        }}
      >
        {SUPPORTED_LANGS.map((entry) => (
          <option key={entry.code} value={entry.code} lang={entry.code}>
            {entry.endonym}
          </option>
        ))}
      </select>
    </label>
  )
}
