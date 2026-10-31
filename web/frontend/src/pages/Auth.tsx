import React, { useState } from 'react'
import { ArrowRightLeft, Eye, EyeOff, Filter, Moon, Radio, Sparkles, Sun } from 'lucide-react'
import { api } from '../api'
import { LanguagePicker } from '../components/LanguagePicker'

import { applySession } from '../session'
import { isRtlLang, makeT, preferredLang, rememberLang, type UiLang } from '../locales/engine'
import { preferredTheme, toggleTheme, type Theme } from '../theme'

/**
 * Sign-in / registration screen.
 *
 * Presentation only — the register/login flow below is the same two calls
 * through `api` it has always been. The frame is a two-panel layout: on wide
 * screens a brand panel (wordmark, one-line value proposition, three feature
 * bullets) sits beside the form; below 1024 px only the form renders, with a
 * compact wordmark of its own. Every colour and spacing value resolves to the
 * design-system layer in `index.css` (`.auth-*`), so this file carries no
 * inline styles.
 */
export default function Auth({ onLogin }: { onLogin: () => void }) {
  // The login page is the first screen a user sees, so it must not guess: the
  // stored choice wins, then the browser's own language, then English. A
  // hardcoded 'fa' here rendered Persian to everybody who had never chosen.
  const [lang, setLang] = useState<UiLang>(() => preferredLang())
  const [isRegister, setIsRegister] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  // The theme toggle rides with the language picker: the login screen is where
  // a light-theme operator first meets the product, and where they can fix a
  // wrong guess before signing in.
  const [theme, setTheme] = useState<Theme>(() => preferredTheme())

  const isRtl = isRtlLang(lang)
  const t = makeT(lang)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      if (isRegister) {
        if (!username || !password) {
          throw new Error(t('ui_username_and_password_are_required'))
        }
        const resp = await api.register({ username, password, display_name: displayName })
        if (!resp.success) {
          throw new Error(resp.message || (t('ui_registration_failed')))
        }
        // Auto-login immediately upon registration
        const loginResp = await api.login({ username, password })
        if (loginResp.success && loginResp.token) {
          applySession(loginResp.token)
          onLogin()
          return
        }
        setIsRegister(false)
        setError(t('ui_account_created_please_sign_in'))
      } else {
        if (!username || !password) {
          throw new Error(t('ui_username_and_password_are_required'))
        }
        const resp = await api.login({ username, password })
        if (!resp.success) {
          throw new Error(resp.message || (t('ui_invalid_credentials')))
        }
        const token = resp.token
        if (!token) throw new Error(t('ui_no_token_received'))
        applySession(token)
        onLogin()
      }
    } catch (err: any) {
      setError(err.message || (t('ui_operation_failed')))
    } finally {
      setLoading(false)
    }
  }

  const features = [
    { icon: <ArrowRightLeft size={16} aria-hidden="true" />, text: t('ui_auth_feature_forwarding') },
    { icon: <Filter size={16} aria-hidden="true" />, text: t('ui_auth_feature_filters') },
    { icon: <Sparkles size={16} aria-hidden="true" />, text: t('ui_auth_feature_ai') },
  ]

  return (
    <div dir={isRtl ? 'rtl' : 'ltr'} className="auth-page">
      {/* Brand panel: the story, wide screens only. */}
      <aside className="auth-brand" aria-hidden="false">
        <div className="auth-brand__wordmark">
          <span className="auth-brand__mark" aria-hidden="true">
            <Radio size={22} />
          </span>
          <span className="auth-brand__name">
            AutoTelegramForward <em>PRO</em>
          </span>
        </div>
        <p className="auth-brand__prop">{t('ui_auth_value_prop')}</p>
        <ul className="auth-brand__features">
          {features.map((feature) => (
            <li key={feature.text} className="auth-brand__feature">
              <span className="auth-brand__feature-icon" aria-hidden="true">
                {feature.icon}
              </span>
              <span>{feature.text}</span>
            </li>
          ))}
        </ul>
      </aside>

      {/* Form panel. */}
      <main className="auth-main">
        <div className="auth-lang-row">
          <button
            type="button"
            onClick={() => setTheme((prev) => toggleTheme(prev))}
            className="btn btn--ghost btn--icon btn--sm theme-toggle"
            aria-label={theme === 'dark' ? t('ui_theme_switch_to_light') : t('ui_theme_switch_to_dark')}
            title={theme === 'dark' ? t('ui_theme_switch_to_light') : t('ui_theme_switch_to_dark')}
          >
            {theme === 'dark' ? <Sun size={16} aria-hidden="true" /> : <Moon size={16} aria-hidden="true" />}
          </button>
          <LanguagePicker
            lang={lang}
            onChange={(next) => {
              setLang(next)
              rememberLang(next)
            }}
          />
        </div>

        <div className="glass-panel auth-card">
          {/* Compact wordmark: the narrow screens' stand-in for the side panel. */}
          <div className="auth-brand-mini">
            <span className="auth-brand__mark auth-brand__mark--sm" aria-hidden="true">
              <Radio size={18} />
            </span>
            <span className="auth-brand__name auth-brand__name--sm">
              AutoTelegramForward <em>PRO</em>
            </span>
          </div>

          {/* Sign in / Register segmented toggle. */}
          <div className="auth-seg" role="tablist" aria-label={t('ui_sign_in')}>
            <button
              type="button"
              role="tab"
              aria-selected={!isRegister}
              className={`auth-seg__btn${isRegister ? '' : ' is-active'}`}
              onClick={() => {
                setIsRegister(false)
                setError(null)
              }}
            >
              {t('ui_sign_in')}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={isRegister}
              className={`auth-seg__btn${isRegister ? ' is-active' : ''}`}
              onClick={() => {
                setIsRegister(true)
                setError(null)
              }}
            >
              {t('ui_register')}
            </button>
          </div>

          <p className="auth-card__lead">
            {isRegister
              ? t('ui_create_your_control_panel_account')
              : t('ui_sign_in_to_your_control_panel')}
          </p>

          {/* Telegram Bot Hint */}
          <div className="auth-hint">
            <b>{t('ui_tip_label')}:</b> {t('ui_telegram_bot_hint')}
          </div>

          {error && (
            <div className="auth-error" role="alert">
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="auth-form">
            {isRegister && (
              <div className="field">
                <label className="field__label" htmlFor="auth-display-name">
                  {t('ui_display_name_optional')}
                </label>
                <input
                  id="auth-display-name"
                  type="text"
                  placeholder={t('ui_e_g_john_doe')}
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                  className="input-field auth-input"
                />
              </div>
            )}

            <div className="field">
              <label className="field__label" htmlFor="auth-username">
                {t('ui_username')}
              </label>
              <input
                id="auth-username"
                type="text"
                placeholder={t('ui_username_min_4_chars')}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                className="input-field auth-input"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
              />
            </div>

            <div className="field">
              <label className="field__label" htmlFor="auth-password">
                {t('ui_password')}
              </label>
              <div className="auth-pass">
                <input
                  id="auth-password"
                  type={showPassword ? 'text' : 'password'}
                  placeholder={t('ui_secure_password_min_6_chars')}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  minLength={6}
                  className="input-field auth-input"
                  autoComplete={isRegister ? 'new-password' : 'current-password'}
                />
                <button
                  type="button"
                  className="auth-pass__toggle"
                  aria-label={showPassword ? t('ui_hide_password') : t('ui_show_password')}
                  aria-pressed={showPassword}
                  title={showPassword ? t('ui_hide_password') : t('ui_show_password')}
                  onClick={() => setShowPassword((v) => !v)}
                >
                  {showPassword ? <EyeOff size={16} aria-hidden="true" /> : <Eye size={16} aria-hidden="true" />}
                </button>
              </div>
            </div>

            <button type="submit" disabled={loading} className="btn btn--primary auth-submit">
              {loading ? (t('ui_please_wait')) : isRegister ? (t('ui_create_account')) : (t('ui_sign_in'))}
            </button>
          </form>
        </div>
      </main>
    </div>
  )
}
