/**
 * Session-core: the ONE place that owns the browser-side authentication state.
 *
 * Root cause this removes (the "kicked out / asked to log in again" bug):
 * previously every web mount read `localStorage.auth_token` as truth. That
 * value could be garbage (a revoked/expired 7-day JWT, a token for a deleted
 * account, a tma token stored under autofill, or plain corruption) and the
 * dashboard would render with it until the first API call 401'd — mid-session
 * — wiping everything. There was no single validated identity, no expiry
 * record and no explicit logout, so state was inferred per-component.
 *
 * Contract:
 * - BOOT: `verifySession()` fetches /api/auth/me WITHOUT trusting storage:
 *   a stored token that validates keeps the session; anything that is
 *   missing/unauthorized/inconsistent between `me.user_id` and the JWT `sub`
 *   (decoded locally) is CLEARED here, so the login screen sees a clean state.
 * - LOGIN: `applySession()` persists ONLY what the server just issued
 *   (never trusts a stale token), storing the JWT `exp` for the UI to show.
 * - SCREEN: the token in storage + the verified `MeInfo` are the data every
 *   component reads — no per-component inference from header strings.
 * - 401 (api.ts) dispatches `auth_required` → `clearSession()` + event.
 *   The mutation is idempotent so concurrent requests racing one expired
 *   token do not fight over inconsistent state.
 * - The JWT `exp` is checked BEFORE any request so a definitely-expired
 *   session logs out proactively instead of inviting a 401 round trip.
 */

import { api } from './api'
import type { MeInfo } from './types'

const TOKEN_KEY = 'auth_token'

export interface DecodedJwt {
  sub: string
  exp: number
  admin: boolean
}

function b64urlDecode(part: string): string {
  const pad = part.length % 4 ? '='.repeat(4 - (part.length % 4)) : ''
  return atob(part.replace(/-/g, '+').replace(/_/g, '/') + pad)
}

/** Decode the payload of a JWT WITHOUT verifying its signature — expiry and
 *  subject only. Signature trust stays with /api/auth/me. */
export function decodeJwt(token: string): DecodedJwt | null {
  const parts = token.split('.')
  if (parts.length !== 3 || !parts[1]) return null
  try {
    const payload = JSON.parse(b64urlDecode(parts[1]))
    if (typeof payload?.exp !== 'number' || typeof payload?.sub === 'undefined') {
      return null
    }
    return { sub: String(payload.sub), exp: payload.exp, admin: !!payload.admin }
  } catch {
    return null
  }
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token)
  } catch {
    // storage unavailable (private mode) — session lasts this page only
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY)
  } catch {}
}

/** A token whose exp is in the past (60 s tolerance) must never enter a
 *  request: log out locally, the user stays on the login screen. */
export function isTokenJwtExpired(token: string | null): boolean {
  if (!token) return true
  const dec = decodeJwt(token)
  if (!dec) return true
  return dec.exp <= Math.floor(Date.now() / 1000) + 60
}

export interface SessionState {
  token: string
  me: MeInfo
  decoded: DecodedJwt
}

/** PROOF a stored token still names a live account. Anything else = logout.
 *  Returns null whenever the session is unusable, so the caller never has to
 *  reason about partial state. */
export async function verifySession(): Promise<SessionState | null> {
  const token = getToken()
  if (!token || isTokenJwtExpired(token)) {
    clearToken()
    return null
  }
  try {
    const me = await api.me()
    if (!me || typeof me.user_id !== 'number' || me.user_id <= 0) {
      clearToken()
      return null
    }
    const decoded = decodeJwt(token)!
    // The stored token must still refer to the same account that /me just
    // resolved. A mismatch is corruption (or a re-used storage key): refuse.
    if (String(me.user_id) !== decoded.sub) {
      clearToken()
      return null
    }
    return { token, me, decoded }
  } catch {
    // 401: the stored token's signature/name is no longer valid on the
    // server — same outcome as missing credentials.
    clearToken()
    return null
  }
}

/** Persist a session the SERVER just issued (login/register/1-tap/TMA). */
export function applySession(token: string): void {
  setToken(token)
}

/** Deterministic logout (fired by logout + any 401). Idempotent. */
export function clearSession(): void {
  clearToken()
  try {
    window.dispatchEvent(new Event('session_cleared'))
  } catch {}
}

export function formatExpires(seconds: number): string {
  const d = new Date(seconds * 1000)
  return d.toLocaleString()
}
