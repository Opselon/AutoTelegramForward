/**
 * One endpoint normalizer for every loop check in the dashboard.
 *
 * The rule editor used to inline the same `trim → lowercase → strip scheme →
 * strip t.me/ → strip @` pipeline in four places (the save handler and three
 * live-warning blocks), each a private arrow function with the same body. One
 * copy lives here: the client-side loop guard and the server's
 * `normalizeChatID` then cannot drift apart within a single release.
 *
 * Same semantics the old inline copies had, stated once: trim, lowercase,
 * strip `https?://`, strip `t.me/` / `telegram.me/`, strip leading `@` signs.
 */
export function normalizeEndpoint(raw?: string | null): string {
  let s = String(raw ?? '').trim().toLowerCase()
  s = s.replace(/^https?:\/\//, '')
  s = s.replace(/^(?:telegram\.me|t\.me)\//, '')
  s = s.replace(/^@+/, '')
  return s.trim()
}
