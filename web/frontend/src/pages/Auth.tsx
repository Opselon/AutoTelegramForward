import React, { useState } from 'react'
import { api } from '../api'
import { LanguagePicker } from '../components/LanguagePicker'

import { applySession } from '../session'
import { isRtlLang, makeT, preferredLang, rememberLang, type UiLang } from '../locales/engine'

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

  return (
    <div
      dir={isRtl ? 'rtl' : 'ltr'}
      style={{
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '20px 16px',
        background: '#0b0f19',
        backgroundImage:
          'radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.18) 0px, transparent 50%), radial-gradient(at 100% 100%, rgba(245, 158, 11, 0.12) 0px, transparent 50%)',
        color: '#f8fafc',
        fontFamily: "'Vazirmatn', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
      }}
    >
      {/* Language picker: the same ten catalogs the dashboard uses */}
      <div
        style={{
          width: '100%',
          maxWidth: 390,
          display: 'flex',
          justifyContent: 'flex-end',
          marginBottom: 12,
        }}
      >
        <LanguagePicker
          lang={lang}
          onChange={(next) => {
            setLang(next)
            rememberLang(next)
          }}
        />
      </div>

      <div
        className="glass-panel"
        style={{
          maxWidth: 390,
          width: '100%',
          padding: '32px 24px',
          background: 'rgba(18, 24, 38, 0.85)',
          backdropFilter: 'blur(20px)',
          WebkitBackdropFilter: 'blur(20px)',
          border: '1px solid rgba(255, 255, 255, 0.1)',
          borderRadius: 18,
          boxShadow: '0 25px 60px -15px rgba(0, 0, 0, 0.7)',
        }}
      >
        {/* Brand Icon & Title */}
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <div
            style={{
              width: 56,
              height: 56,
              margin: '0 auto 14px',
              borderRadius: 16,
              background: 'linear-gradient(135deg, #6366f1 0%, #a855f7 100%)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: 26,
              boxShadow: '0 8px 24px rgba(99, 102, 241, 0.45)',
            }}
          >
            ⚡
          </div>
          <h2 style={{ margin: '0 0 6px', fontSize: '1.35rem', fontWeight: 800, letterSpacing: '-0.02em' }}>
            AutoTelegramForward <span style={{ color: '#818cf8' }}>PRO</span>
          </h2>
          <p style={{ margin: 0, fontSize: '0.825rem', color: '#94a3b8', lineHeight: 1.5 }}>
            {isRegister
              ? t('ui_create_your_control_panel_account')
              : t('ui_sign_in_to_your_control_panel')}
          </p>
        </div>

        {/* Telegram Bot Hint */}
        <div
          style={{
            background: 'rgba(99, 102, 241, 0.1)',
            border: '1px solid rgba(99, 102, 241, 0.25)',
            borderRadius: 10,
            padding: '10px 14px',
            marginBottom: 20,
            fontSize: '0.8rem',
            lineHeight: 1.5,
            color: '#c7d2fe',
          }}
        >
          💡 <b>{t('ui_tip_label')}:</b> {t('ui_telegram_bot_hint')}
        </div>

        {error && (
          <div
            style={{
              padding: '10px 14px',
              marginBottom: 16,
              borderRadius: 8,
              background: 'rgba(239, 68, 68, 0.15)',
              border: '1px solid rgba(239, 68, 68, 0.35)',
              color: '#f87171',
              fontSize: '0.825rem',
              lineHeight: 1.4,
            }}
          >
            {error}
          </div>
        )}

        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {isRegister && (
            <div>
              <label style={{ display: 'block', fontSize: '0.775rem', color: '#94a3b8', marginBottom: 6 }}>
                {t('ui_display_name_optional')}
              </label>
              <input
                type="text"
                placeholder={t('ui_e_g_john_doe')}
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className="input-field"
                style={{ fontSize: 16 }}
              />
            </div>
          )}

          <div>
            <label style={{ display: 'block', fontSize: '0.775rem', color: '#94a3b8', marginBottom: 6 }}>
              {t('ui_username')}
            </label>
            <input
              type="text"
              placeholder={t('ui_username_min_4_chars')}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              className="input-field"
              style={{ fontSize: 16 }}
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
            />
          </div>

          <div>
            <label style={{ display: 'block', fontSize: '0.775rem', color: '#94a3b8', marginBottom: 6 }}>
              {t('ui_password')}
            </label>
            <input
              type="password"
              placeholder={t('ui_secure_password_min_6_chars')}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={6}
              className="input-field"
              style={{ fontSize: 16 }}
              autoComplete={isRegister ? 'new-password' : 'current-password'}
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="btn btn-primary"
            style={{
              padding: '12px',
              fontSize: '0.95rem',
              fontWeight: 700,
              borderRadius: 10,
              marginTop: 4,
            }}
          >
            {loading ? (t('ui_please_wait')) : isRegister ? (t('ui_create_account')) : (t('ui_sign_in'))}
          </button>
        </form>

        <div style={{ marginTop: 20, textAlign: 'center', fontSize: '0.825rem', color: '#94a3b8' }}>
          {isRegister ? (t('ui_already_have_an_account')) : (t('ui_don_t_have_an_account'))}{' '}
          <button
            type="button"
            onClick={() => {
              setIsRegister(!isRegister)
              setError(null)
            }}
            style={{
              background: 'none',
              border: 'none',
              color: '#818cf8',
              fontWeight: 700,
              cursor: 'pointer',
              padding: 0,
              textDecoration: 'underline',
              fontFamily: 'inherit',
            }}
          >
            {isRegister ? (t('ui_sign_in')) : (t('ui_register'))}
          </button>
        </div>
      </div>
    </div>
  )
}
