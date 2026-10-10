import React, { useState } from 'react'
import { api } from '../api'

export default function Auth({ onLogin }: { onLogin: () => void }) {
  const [lang, setLang] = useState<'fa' | 'en'>('fa')
  const [isRegister, setIsRegister] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const isRtl = lang === 'fa'

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setLoading(true)
    try {
      if (isRegister) {
        if (!username || !password) {
          throw new Error(isRtl ? 'نام کاربری و رمز عبور الزامی است' : 'Username and password are required')
        }
        const resp = await api.register({ username, password, display_name: displayName })
        if (!resp.success) {
          throw new Error(resp.message || (isRtl ? 'ثبت‌نام ناموفق بود' : 'Registration failed'))
        }
        // Auto-login immediately upon registration
        const loginResp = await api.login({ username, password })
        if (loginResp.success && (loginResp as any).token) {
          localStorage.setItem('auth_token', (loginResp as any).token)
          onLogin()
          return
        }
        setIsRegister(false)
        setError(isRtl ? 'حساب کاربری ایجاد شد. لطفاً وارد شوید.' : 'Account created. Please sign in.')
      } else {
        if (!username || !password) {
          throw new Error(isRtl ? 'نام کاربری و رمز عبور الزامی است' : 'Username and password are required')
        }
        const resp = await api.login({ username, password })
        if (!resp.success) {
          throw new Error(resp.message || (isRtl ? 'نام کاربری یا رمز عبور اشتباه است' : 'Invalid credentials'))
        }
        const token = (resp as any).token
        if (!token) throw new Error(isRtl ? 'توکن ورود دریافت نشد' : 'No token received')
        localStorage.setItem('auth_token', token)
        onLogin()
      }
    } catch (err: any) {
      setError(err.message || (isRtl ? 'عملیات با خطا مواجه شد' : 'Operation failed'))
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
      {/* Language Switcher */}
      <div style={{ width: '100%', maxWidth: 390, display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <button
          onClick={() => setLang(lang === 'fa' ? 'en' : 'fa')}
          style={{
            background: 'rgba(255, 255, 255, 0.06)',
            border: '1px solid rgba(255, 255, 255, 0.12)',
            color: '#cbd5e1',
            borderRadius: 8,
            padding: '6px 12px',
            fontSize: '0.8rem',
            cursor: 'pointer',
          }}
        >
          {lang === 'fa' ? '🌐 English' : '🌐 فارسی'}
        </button>
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
              ? isRtl
                ? 'ثبت‌نام و ایجاد حساب کاربری داشبورد'
                : 'Create your control panel account'
              : isRtl
              ? 'ورود به پنل مدیریت هوشمند و فوروارد'
              : 'Sign in to your control panel'}
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
          {isRtl ? (
            <>
              💡 <b>راهنمایی:</b> نام کاربری و رمز اختصاصی خود را می‌توانید مستقیماً از ربات تلگرام با لمس دکمه <b>«💻 داشبورد وب»</b> دریافت کنید.
            </>
          ) : (
            <>
              💡 <b>Tip:</b> You can get your dedicated username and password directly from the Telegram bot via the <b>Web Dashboard</b> button.
            </>
          )}
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
                {isRtl ? 'نام نمایشی (اختیاری)' : 'Display Name (optional)'}
              </label>
              <input
                type="text"
                placeholder={isRtl ? 'مثلاً: مدیر فروش' : 'e.g. John Doe'}
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className="input-field"
                style={{ fontSize: 16 }}
              />
            </div>
          )}

          <div>
            <label style={{ display: 'block', fontSize: '0.775rem', color: '#94a3b8', marginBottom: 6 }}>
              {isRtl ? 'نام کاربری' : 'Username'}
            </label>
            <input
              type="text"
              placeholder={isRtl ? 'نام کاربری (حداقل ۴ حرف)' : 'Username (min 4 chars)'}
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
              {isRtl ? 'رمز عبور' : 'Password'}
            </label>
            <input
              type="password"
              placeholder={isRtl ? 'رمز عبور امن (حداقل ۶ حرف)' : 'Secure password (min 6 chars)'}
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
            {loading ? (isRtl ? 'در حال برقراری ارتباط...' : 'Please wait...') : isRegister ? (isRtl ? 'ثبت‌نام حساب جدید' : 'Create Account') : (isRtl ? 'ورود به داشبورد' : 'Sign In')}
          </button>
        </form>

        <div style={{ marginTop: 20, textAlign: 'center', fontSize: '0.825rem', color: '#94a3b8' }}>
          {isRegister ? (isRtl ? 'از قبل حساب دارید؟' : 'Already have an account?') : (isRtl ? 'حساب کاربری ندارید؟' : "Don't have an account?")}{' '}
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
            {isRegister ? (isRtl ? 'ورود' : 'Sign In') : (isRtl ? 'ثبت‌نام در پنل' : 'Register')}
          </button>
        </div>
      </div>
    </div>
  )
}
