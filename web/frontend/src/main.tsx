import React, { StrictMode, useState, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import Auth from './pages/Auth.tsx'
import { api } from './api'
import {
  verifySession,
  applySession,
  clearSession,
  getToken,
  setToken,
  isTokenJwtExpired,
  type SessionState,
} from './session'

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
              clearSession()
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
  setToken(urlAuth.token)
  // A link token older than its exp is not a session: drop it before the
  // dashboard can render on it, the verify below reaches the same verdict.
  if (isTokenJwtExpired(urlAuth.token)) {
    clearSession()
  }
}

function Root() {
  // authority: session-core's settled state, null = verify not settled yet.
  const [session, setSession] = useState<SessionState | null>(null)
  const [settled, setSettled] = useState<boolean>(false)
  const [linkError, setLinkError] = useState<string | null>(null)
  const [exchanging, setExchanging] = useState<boolean>(!!codeExchange)

  const onLogin = () => {
    // The server just issued a token; prove it once and adopt the result.
    verifySession()
      .then((st) => {
        setSession(st)
        setSettled(true)
      })
      .catch(() => {
        setSession(null)
        setSettled(true)
      })
  }

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
          applySession(issued)
          onLogin()
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

  // BOOT REVALIDATION (root cause of the kick-out): a stored token is never
  // trusted on its own — session-core proves it against /api/auth/me once
  // per mount. The dashboard renders only after it settles.
  useEffect(() => {
    if (exchanging) return // the code exchange drives onLogin itself
    let cancelled = false
    verifySession()
      .then((st) => {
        if (cancelled) return
        setSession(st)
      })
      .finally(() => {
        if (!cancelled) setSettled(true)
      })
    return () => {
      cancelled = true
    }
  }, [exchanging])

  useEffect(() => {
    const handleAuthRequired = () => {
      // session-core already wiped the dead token; this just leaves the
      // dashboard for the login screen.
      clearSession()
      setSession(null)
      setSettled(true)
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

  if (!session) {
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
        <Auth onLogin={onLogin} />
      </>
    )
  }

  if (!settled) {
    // Verifying the stored session: never render the login screen on a token
    // that may still be valid (that was the false "log in again" prompt).
    return null
  }

  return <App me={session.me} />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <Root />
    </ErrorBoundary>
  </StrictMode>,
)
