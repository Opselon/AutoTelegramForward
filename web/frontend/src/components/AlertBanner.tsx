/**
 * The proactive pipeline notification: the newest ERROR/WARN the delivery
 * engine reported, rendered as a real notification instead of a raw backend
 * string.
 *
 * - Severity maps to a visual variant (error = danger, warning = caution);
 *   the text is never restyled into success.
 * - A FloodWait is recognised and explained: Telegram temporarily limits a
 *   *target*, this is not an internal service failure, and a countdown shows
 *   when the queued attempt may go through. The original diagnostic detail is
 *   always preserved in a secondary mono line.
 * - Dismissal is available; the shell records the dismissed identity so the
 *   same event does not return every poll cycle.
 */
import { useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { makeT, type UiLang } from '../locales/engine'

export interface PipelineAlert {
  kind: 'error' | 'warning'
  name: string
  detail: string
  ts: number
  /** The dismissal identities the shell remembers (historical string keys). */
  dismissKeys: string[]
}

/** "FloodWait 30s on target" / "flood_wait_30" → the wait in seconds. */
export function parseFloodWaitSeconds(detail: string): number | null {
  const match = /flood[_ ]?wait\D{0,6}(\d{1,6})/i.exec(detail || '')
  if (!match) return null
  const seconds = Number(match[1])
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null
}

export function AlertBanner({
  alert,
  onDismiss,
  lang,
}: {
  alert: PipelineAlert
  onDismiss: () => void
  lang: UiLang
}) {
  const t = makeT(lang)
  // The producer strips the duplicated exception prefix ("FloodWait: 30s on
  // target" → name "FloodWait", detail "30s on target"), so the detection
  // looks at both fields together.
  const floodSeconds = parseFloodWaitSeconds(`${alert.name} ${alert.detail}`)
  // The countdown is anchored to when the engine reported the FloodWait, so a
  // banner resurfaced after a while does not restart the clock.
  const [remaining, setRemaining] = useState<number | null>(floodSeconds)

  useEffect(() => {
    if (floodSeconds === null) return
    const reportedAt = alert.ts || Math.floor(Date.now() / 1000)
    const elapsed = Math.max(0, Math.floor(Date.now() / 1000) - reportedAt)
    setRemaining(Math.max(0, floodSeconds - elapsed))
    const timer = window.setInterval(
      () => setRemaining((prev) => (prev === null ? null : Math.max(0, prev - 1))),
      1000,
    )
    return () => window.clearInterval(timer)
  }, [floodSeconds, alert.ts])

  const isFlood = floodSeconds !== null
  const kind = isFlood && alert.kind === 'error' ? 'warning' : alert.kind

  return (
    <div className={`alert-banner alert-banner--${kind}${isFlood ? ' alert-banner--flood' : ''}`} role="alert">
      <span className="alert-banner__icon" aria-hidden="true">⚠️</span>
      <div className="alert-banner__body">
        <div className="alert-banner__headline">
          {isFlood ? (
            remaining !== null && remaining > 0
              ? t('ui_floodwait_banner', { secs: remaining })
              : t('ui_floodwait_active')
          ) : (
            <>
              {alert.name ? <strong>{alert.name}: </strong> : null}
              {alert.detail}
            </>
          )}
          {isFlood && remaining !== null && remaining > 0 ? (
            <span className="alert-banner__countdown" aria-hidden="true">
              {remaining}s
            </span>
          ) : null}
        </div>
        <div dir="ltr" className="alert-banner__text">
          {isFlood ? `${alert.name ? `${alert.name}: ` : ''}${alert.detail}` : alert.detail}
        </div>
      </div>
      <button
        type="button"
        onClick={onDismiss}
        className="alert-banner__close"
        aria-label={t('ui_dismiss')}
        title={t('ui_dismiss')}
      >
        <X size={14} aria-hidden="true" />
      </button>
    </div>
  )
}
