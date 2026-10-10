import React, { StrictMode, useState, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import Auth from './pages/Auth.tsx'

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

function checkAndExtractUrlToken(): boolean {
  try {
    const params = new URLSearchParams(window.location.search)
    const token = params.get('token')
    if (token && token.length > 20) {
      localStorage.setItem('auth_token', token)
      // Clean query string from browser address bar smoothly
      const cleanUrl = window.location.pathname + window.location.hash
      window.history.replaceState({}, document.title, cleanUrl)
      return true
    }
  } catch (e) {
    console.warn('URL token parse failed:', e)
  }
  return false
}

function Root() {
  const [authed, setAuthed] = useState<boolean>(() => {
    // 1. First check if token is in the URL (Telegram 1-tap login link)
    const hasUrlToken = checkAndExtractUrlToken()
    if (hasUrlToken) return true

    // 2. Otherwise check existing stored session
    try {
      return !!localStorage.getItem('auth_token')
    } catch {
      return false
    }
  })

  useEffect(() => {
    const handleAuthRequired = () => {
      setAuthed(false)
    }
    window.addEventListener('auth_required', handleAuthRequired)
    return () => window.removeEventListener('auth_required', handleAuthRequired)
  }, [])

  if (!authed) {
    return <Auth onLogin={() => setAuthed(true)} />
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
