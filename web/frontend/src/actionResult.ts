/**
 * Did a control-plane action actually happen?
 *
 * The Go API answers a rule action (pause, resume, dead-letter replay) and a
 * dead-letter purge with `{success, message, error_code}`. `success` used to be
 * absent on a refusal, because the response was a protobuf struct whose scalars
 * are all `omitempty` — so a replay the core had refused arrived as
 * `{"message":"not_found"}`.
 *
 * The dashboard asked `resp.success === false`, which is never true when the key
 * is missing: a refused replay removed the row from the list and told the
 * operator it had been re-queued. The decision is here, in a module with no DOM
 * and no framework, so the CI job can compile it and assert on it directly.
 */

/** The documented dead-letter replay result codes. */
export const ACTION_REFUSALS = [
  'not_found',
  'already_pending',
  'already_sent',
  'replay_failed',
] as const

export type ActionBody = {
  success?: boolean | null
  message?: string | null
  error_code?: string | null
} | null | undefined

/** A localized "this was refused" reason: the code when present, else the text. */
export type Refusal = { refused: true; reason: string }

/**
 * True only when the server said `success: true`.
 *
 * Anything else — `false`, a missing field, a non-object body — is a refusal.
 * The asymmetry is deliberate: claiming an action failed is recoverable (the
 * caller re-reads the list), claiming it succeeded is not.
 */
export function isAccepted(body: ActionBody): boolean {
  return isPlainObject(body) && (body as { success?: unknown }).success === true
}

/** Why the action was refused, for a message the operator can act on. */
export function refusalReason(body: ActionBody, fallback = 'refused'): string {
  if (!isPlainObject(body)) return fallback
  const b = body as { error_code?: unknown; message?: unknown }
  for (const value of [b.error_code, b.message]) {
    if (typeof value === 'string' && value.trim() !== '') return value.trim()
  }
  return fallback
}

/**
 * The count a purge reports, read out of the server's sentence.
 *
 * `purged 3` is the core's own report; the dashboard shows the number rather
 * than a bare "purged", because a purge that matched nothing must not look like
 * a purge that removed rows.
 */
export function purgedCount(body: ActionBody): number | null {
  if (!isPlainObject(body)) return null
  const message = (body as { message?: unknown }).message
  if (typeof message !== 'string') return null
  const match = /(\d+)/.exec(message)
  if (!match) return null
  const n = Number.parseInt(match[1], 10)
  return Number.isFinite(n) ? n : null
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
