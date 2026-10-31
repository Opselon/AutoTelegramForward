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
  setToken,
  isTokenJwtExpired,
  type SessionState,
} from './session'
import { appKey, isSessionStorageEvent } from './sessionIdentity'
import { ensureLangLoaded, isRtlLang, makeT, preferredLang } from './locales/engine'
import { applyTheme, preferredTheme } from './theme'

// The shell has no component tree to inherit a language from, so it resolves
// the same preference the panels do (stored choice → browser → English).
const t = makeT(preferredLang())

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
        <div className="boot-screen">
          <div style={{ fontSize: 44, marginBottom: 12 }} aria-hidden="true">⚠️</div>
          <h2 className="boot-screen__title boot-screen__title--error">
            {t('ui_dashboard_failed_to_start')}
          </h2>
          <p className="boot-screen__body">
            {this.state.error?.message || t('ui_something_went_wrong_loading')}
          </p>
          <button
            onClick={() => {
              clearSession()
              window.location.href = '/'
            }}
            className="btn btn-primary"
          >
            {t('ui_retry_and_sign_in_again')}
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
          setLinkError(t('ui_login_link_expired'))
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setLinkError(
          `${t('ui_login_link_expired')} (${
            err instanceof Error ? err.message : String(err)
          })`,
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

  // The token lives in `localStorage`, which every tab of this origin shares:
  // a second tab can log in as another account, or log out, without this tab
  // knowing. Reaching for the new token on the next request would then act as
  // the other account while this screen still shows the previous one. So when
  // the stored session changes elsewhere, this tab re-proves the session
  // instead of keeping a dashboard it can no longer justify.
  useEffect(() => {
    const handleStorage = (ev: StorageEvent) => {
      if (!isSessionStorageEvent(ev)) return
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
    window.addEventListener('storage', handleStorage)
    return () => window.removeEventListener('storage', handleStorage)
  }, [])

  if (exchanging) {
    return (
      <div className="boot-screen boot-screen--quiet">
        {t('ui_signing_in')}
      </div>
    )
  }

  if (!session) {
    return (
      <>
        {linkError && (
          <div role="alert" className="boot-alert">
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

  // `key` is the account: adopting a different identity must remount the whole
  // dashboard, so no list, page or cached response fetched for the previous
  // account can stay on screen behind the new one.
  return <App key={appKey(session.me)} me={session.me} />
}

/**
 * Boot the shell in the reader's own language.
 *
 * Only the English catalog is in the initial bundle; the other nine arrive as
 * one small chunk each. That chunk is fetched *before* the first React render,
 * under the splash screen `index.html` already paints, so a Persian reader
 * never sees an English dashboard flash — and an English reader downloads no
 * catalog at all.
 */
const bootLang = preferredLang()
document.documentElement.lang = bootLang
document.documentElement.dir = isRtlLang(bootLang) ? 'rtl' : 'ltr'
// Re-assert the theme on boot: index.html's pre-paint script normally did it,
// but a cached document without the script (or a tampered attribute) must not
// leave the shell on the wrong surface. Preferred choice, then system hint —
// the same order the pre-paint script uses.
applyTheme(preferredTheme())

void ensureLangLoaded(bootLang).finally(() => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <ErrorBoundary>
        <Root />
      </ErrorBoundary>
    </StrictMode>,
  )
})
