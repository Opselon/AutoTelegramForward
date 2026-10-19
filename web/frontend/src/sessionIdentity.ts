/**
 * Which browser tab is looking at whose data.
 *
 * Two things the dashboard used to get wrong, both about the identity behind the
 * rendered screen:
 *
 * 1. **A second tab was invisible.** The session token lives in
 *    `localStorage`, which every tab of the origin shares, but a tab only ever
 *    proved its session once, on mount. If another tab logged in as a different
 *    account (or logged out), this tab kept rendering the previous account's
 *    dashboard while every request it made was now authorised as the *other*
 *    account — the screen said one user, the server saw another. Nothing
 *    re-verified, because nothing told the tab that the stored session had
 *    changed. `isSessionStorageEvent` is that signal.
 * 2. **A new identity reused the mounted tree.** React keeps component state
 *    when the element type and position are unchanged, so adopting a different
 *    account rendered the same `<App>` instance — every list, page and cached
 *    response already fetched for the previous user stayed on screen until it
 *    happened to be refetched. `appKey` gives the tree an identity key, so a
 *    different account remounts it and drops that state instead of inheriting
 *    it.
 *
 * The rules are module-scope and framework-free so they can be run directly by
 * `node tests/sessionIdentity.test.mjs` (the repo has no test runner).
 */

/** Storage key that carries the session. Mirrors `TOKEN_KEY` in `session.ts`. */
export const SESSION_TOKEN_KEY = 'auth_token'

export interface AccountLike {
  user_id?: number | string | null
}

/**
 * A positive integer account id as a canonical string, `''` for anything else.
 *
 * Stored ids arrive as numbers from `/api/auth/me` and as strings from a JWT
 * `sub`; both must name the same account, so neither form may produce a
 * different key (that would remount the dashboard on every verify). Anything
 * that is not a positive integer names nobody.
 */
export function normaliseAccountId(value: unknown): string {
  if (typeof value === 'number') {
    return Number.isFinite(value) && value > 0 ? String(Math.trunc(value)) : ''
  }
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (!/^\d+$/.test(trimmed)) return ''
    const n = Number(trimmed)
    return n > 0 && Number.isSafeInteger(n) ? String(n) : ''
  }
  return ''
}

/** The dashboard tree's React key: stable per account, different across accounts. */
export function appKey(me: AccountLike | null | undefined): string {
  const id = normaliseAccountId(me?.user_id)
  return id === '' ? 'account:anonymous' : `account:${id}`
}

/** True when two identity values name the same account (no remount needed). */
export function describesSameAccount(prev: unknown, next: unknown): boolean {
  return normaliseAccountId(prev) === normaliseAccountId(next)
}

/**
 * True when a `storage` event means *our* session may have changed.
 *
 * `key === null` is `localStorage.clear()`, which removes the token just as
 * surely as overwriting it. Keys other than ours (language, dismissed notices)
 * are not a reason to re-verify, and a foreign/absent event is ignored.
 */
export function isSessionStorageEvent(
  ev: { key?: string | null } | null | undefined,
): boolean {
  if (!ev || typeof ev !== 'object') return false
  if (!Object.prototype.hasOwnProperty.call(ev, 'key')) return false
  return ev.key === null || ev.key === SESSION_TOKEN_KEY
}
