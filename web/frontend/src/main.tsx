import React, { StrictMode, useState, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import Auth from './pages/Auth.tsx'
import { api } from './api'

// Global error boundary to prevent any blank screen crash
class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { hasError: boolean; error: Error | null }
> {
  constructor(props: { children: React.ReactNode }) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, errorInfo: any) {
    console.error('Unhandled UI error:', error, errorInfo)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div
          style={{
            minHeight: '100vh',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 24,
            textAlign: 'center',
            background: '#0b0f19',
            color: '#f8fafc',
            fontFamily: 'sans-serif',
          }}
        >
          <div style={{ fontSize: 44, marginBottom: 12 }}>⚠️</div>
          <h2 style={{ fontSize: '1.25rem', fontWeight: 700, marginBottom: 8, color: '#f87171' }}>
            خطا در اجرای داشبورد
          </h2>
          <p style={{ fontSize: '0.875rem', color: '#94a3b8', maxWidth: 400, marginBottom: 20 }}>
            {this.state.error?.message || 'مشکلی در بارگذاری رخ داد.'}
          </p>
          <button
            onClick={() => {
              localStorage.removeItem('auth_token')
              window.location.href = '/'
            }}
            style={{
              padding: '10px 20px',
              borderRadius: 8,
              border: 'none',
              background: '#6366f1',
              color: '#fff',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            تلاش مجدد و ورود مجدد
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

/** The two shapes a Telegram 1-tap login link can arrive in. */
type UrlAuth = { token?: string; code?: string }

/**
 * Read `?code=` (single-use login code) and `?token=` (legacy session link)
 * out of the URL and strip them from the address bar immediately, so the
 * credential stops travelling with the page, a bookmark or a screenshot.
 */
function extractUrlAuth(): UrlAuth {
  const found: UrlAuth = {}
  try {
    const params = new URLSearchParams(window.location.search)
    const token = params.get('token')
    const code = params.get('code')
    if (token && token.length > 20) found.token = token
    if (code) found.code = code
    if (found.token || found.code) {
      const cleanUrl = window.location.pathname + window.location.hash
      window.history.replaceState({}, document.title, cleanUrl)
    }
  } catch (e) {
    console.warn('URL login parse failed:', e)
  }
  return found
}

// Read once at module scope and exchange once: a login code is single-use, so
// two renders (React StrictMode renders effects twice) must not burn it twice.
const urlAuth = extractUrlAuth()
const codeExchange: ReturnType<typeof api.exchangeBotToken> | null = urlAuth.code
  ? api.exchangeBotToken({ login_code: urlAuth.code })
  : null
if (urlAuth.token) {
  try {
    localStorage.setItem('auth_token', urlAuth.token)
  } catch {}
}

function Root() {
  const [authed, setAuthed] = useState<boolean>(() => {
    // 1. A legacy link already carries the session token.
    if (urlAuth.token) return true

    // 2. Otherwise fall back to an existing stored session.
    try {
      return !!localStorage.getItem('auth_token')
    } catch {
      return false
    }
  })
  const [linkError, setLinkError] = useState<string | null>(null)
  const [exchanging, setExchanging] = useState<boolean>(!!codeExchange)

  useEffect(() => {
    if (!codeExchange) return
    let cancelled = false
    codeExchange
      .then((resp) => {
        if (cancelled) return
        // AuthResponse is a union (register/login/exchange), so read the token
        // the way the wire carries it rather than trusting a single member.
        const issued =
          resp && typeof (resp as { token?: unknown }).token === 'string'
            ? (resp as { token: string }).token
            : ''
        if (issued) {
          try {
            localStorage.setItem('auth_token', issued)
          } catch {}
          setAuthed(true)
        } else {
          // The core refuses used, expired and unknown codes alike.
          setLinkError('این لینک ورود منقضی شده یا قبلاً استفاده شده است. از ربات دوباره لینک بگیرید.')
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setLinkError(
          'این لینک ورود منقضی شده یا قبلاً استفاده شده است. از ربات دوباره لینک بگیرید. ' +
            `(${err instanceof Error ? err.message : String(err)})`,
        )
      })
      .finally(() => {
        if (!cancelled) setExchanging(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const handleAuthRequired = () => {
      setAuthed(false)
    }
    window.addEventListener('auth_required', handleAuthRequired)
    return () => window.removeEventListener('auth_required', handleAuthRequired)
  }, [])

  if (exchanging) {
    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#0b0f19',
          color: '#f8fafc',
          fontFamily: 'sans-serif',
          fontSize: '0.95rem',
        }}
      >
        در حال ورود…
      </div>
    )
  }

  if (!authed) {
    return (
      <>
        {linkError && (
          <div
            role="alert"
            style={{
              background: '#7f1d1d',
              color: '#fee2e2',
              padding: '10px 14px',
              fontSize: '0.85rem',
              textAlign: 'center',
            }}
          >
            {linkError}
          </div>
        )}
        <Auth onLogin={() => setAuthed(true)} />
      </>
    )
  }

  return <App />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <Root />
    </ErrorBoundary>
  </StrictMode>,
)
