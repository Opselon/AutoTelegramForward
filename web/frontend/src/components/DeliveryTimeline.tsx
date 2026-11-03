/**
 * Delivery timeline: the worker's persisted per-message processing history,
 * read through the authoritative Control Plane (`/diagnostic-events*`).
 *
 * What this adds to the queue view:
 *
 * - the queue answers "how many"; the diagnostic events answer "what exactly
 *   happened, in which stage, at which attempt, with which Telegram answer";
 * - an event row can be opened into the per-message drill-down
 *   (`/diagnostic-events/timeline`): one message's whole history, oldest
 *   first, rendered as a timeline;
 * - correlation IDs are copyable, so an operator can jump from here into the
 *   log console's correlation filter.
 *
 * Polling policy: none. The panel loads once on mount and refreshes on
 * explicit request, per the no-uncontrolled-polling contract — the queue
 * snapshot above it already has the shell's poller.
 */
import { useCallback, useEffect, useState } from 'react'
import { Copy, RefreshCw, RotateCcw, Waypoints } from 'lucide-react'
import { api } from '../api'
import type { DiagnosticEventEntry } from '../types'
import { makeT, type UiLang } from '../locales/engine'
import {
  Button,
  Card,
  EmptyState,
  ErrorState,
  SegmentedControl,
  SkeletonRows,
  StatusIndicator,
} from './ui/Primitives'

type SeverityFilter = 'all' | 'WARN' | 'ERROR'

function severityTone(severity: string): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  switch (severity.toUpperCase()) {
    case 'ERROR':
    case 'CRITICAL':
      return 'danger'
    case 'WARN':
    case 'WARNING':
      return 'warning'
    case 'INFO':
      return 'info'
    default:
      return 'neutral'
  }
}

function formatTime(ts: number, lang: UiLang): string {
  if (!ts) return '—'
  try {
    return new Date(ts * 1000).toLocaleTimeString(lang === 'fa' ? 'fa-IR' : undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return '—'
  }
}

export function DeliveryTimeline({ lang }: { lang: UiLang }) {
  const t = makeT(lang)
  const [events, setEvents] = useState<DiagnosticEventEntry[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [severity, setSeverity] = useState<SeverityFilter>('all')

  // Drill-down state: which message's history is open (null = the event list).
  const [timeline, setTimeline] = useState<DiagnosticEventEntry[] | null>(null)
  const [timelineFor, setTimelineFor] = useState<DiagnosticEventEntry | null>(null)
  const [timelineLoading, setTimelineLoading] = useState(false)
  const [timelineError, setTimelineError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const rows = await api.getDiagnosticEvents({ limit: 50 })
      setEvents(rows)
    } catch (e: any) {
      setLoadError(e?.message ?? 'error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const openTimeline = useCallback(async (event: DiagnosticEventEntry) => {
    setTimelineFor(event)
    setTimeline(null)
    setTimelineError(null)
    setTimelineLoading(true)
    try {
      const rows = await api.getMessageTimeline(event.source_peer_id, event.source_message_id)
      setTimeline(rows)
    } catch (e: any) {
      setTimelineError(e?.message ?? 'error')
    } finally {
      setTimelineLoading(false)
    }
  }, [])

  const visible = (events ?? []).filter((e) => {
    if (severity === 'all') return true
    return e.severity.toUpperCase() === severity
  })

  const copyText = (value: string) => {
    navigator.clipboard?.writeText(value).catch(() => {})
  }

  return (
    <Card
      title={
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          <Waypoints size={16} aria-hidden="true" />
          {t('ui_diagnostic_events')}
        </span>
      }
      action={
        <Button
          variant="ghost"
          size="sm"
          onClick={() => void load()}
          loading={loading}
          aria-label={t('ui_refresh')}
        >
          <RefreshCw size={14} aria-hidden="true" />
          <span>{t('ui_refresh')}</span>
        </Button>
      }
    >
      {timelineFor ? (
        /* ---- per-message drill-down ---------------------------------- */
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 'var(--space-2)',
              flexWrap: 'wrap',
            }}
          >
            <strong style={{ fontSize: 'var(--type-sm)' }}>
              {t('ui_msg')} #{timelineFor.source_message_id} · {timelineFor.source_peer_id}
            </strong>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setTimelineFor(null)
                setTimeline(null)
              }}
            >
              <RotateCcw size={13} aria-hidden="true" />
              <span>{t('ui_all')}</span>
            </Button>
          </div>
          {timelineLoading ? <SkeletonRows rows={4} lang={lang} /> : null}
          {timelineError ? <ErrorState title={t('ui_diagnostic_events')} reason={timelineError} onRetry={() => void openTimeline(timelineFor)} /> : null}
          {timeline && timeline.length === 0 ? (
            <EmptyState title={t('ui_empty_title')} description={t('ui_no_pending_jobs_in_queue')} />
          ) : null}
          {timeline && timeline.length > 0 ? (
            <ol className="timeline" style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {timeline.map((e) => (
                <li key={e.event_id || `${e.timestamp}-${e.event_name}`} className="timeline__item">
                  <span className="timeline__dot" aria-hidden="true" />
                  <div className="timeline__content">
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 'var(--space-2)',
                        flexWrap: 'wrap',
                      }}
                    >
                      <StatusIndicator
                        tone={severityTone(e.severity)}
                        label={`${e.severity} · ${e.event_name}`}
                      />
                      <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
                        {formatTime(e.timestamp, lang)}
                      </span>
                      {e.attempt > 0 ? (
                        <span className="badge badge-gray" style={{ fontSize: 'var(--type-xs)' }}>
                          #{e.attempt}
                        </span>
                      ) : null}
                    </div>
                    <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
                      {t('ui_stage')} {e.stage || '—'}
                      {e.reason ? ` · ${e.reason}` : ''}
                    </div>
                    {e.detail ? (
                      <div
                        dir="ltr"
                        style={{
                          textAlign: 'start',
                          fontSize: 'var(--type-xs)',
                          color: 'var(--color-text-subtle)',
                          wordBreak: 'break-word',
                        }}
                      >
                        {e.detail}
                      </div>
                    ) : null}
                  </div>
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : (
        /* ---- event list ---------------------------------------------- */
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 'var(--space-3)',
              flexWrap: 'wrap',
            }}
          >
            <SegmentedControl<SeverityFilter>
              options={[
                { value: 'all', label: t('ui_all') },
                { value: 'WARN', label: 'WARN' },
                { value: 'ERROR', label: 'ERROR' },
              ]}
              value={severity}
              onChange={setSeverity}
              ariaLabel={t('ui_filter_status')}
            />
          </div>

          {loading && events === null ? <SkeletonRows rows={4} lang={lang} /> : null}
          {loadError ? (
            <ErrorState title={t('ui_diagnostic_events')} reason={loadError} onRetry={() => void load()} />
          ) : null}
          {events !== null && !loadError && visible.length === 0 ? (
            <EmptyState title={t('ui_empty_title')} description={t('ui_no_pending_jobs_in_queue')} />
          ) : null}

          {visible.map((e) => (
            <div
              key={e.event_id || `${e.timestamp}-${e.event_name}-${e.job_id}`}
              className="timeline-event"
              style={{
                display: 'flex',
                flexDirection: 'column',
                gap: 'var(--space-1)',
                padding: 'var(--space-2) var(--space-3)',
                background: 'var(--color-surface-inset)',
                borderRadius: 'var(--radius-sm)',
                border: '1px solid var(--color-border)',
                minInlineSize: 0,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
                <StatusIndicator
                  tone={severityTone(e.severity)}
                  pulse={e.retryable}
                  label={`${e.severity} · ${e.event_name}`}
                />
                <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
                  {formatTime(e.timestamp, lang)}
                </span>
                <span style={{ flex: 1 }} />
                {e.source_message_id > 0 ? (
                  <Button variant="ghost" size="sm" onClick={() => void openTimeline(e)}>
                    <Waypoints size={12} aria-hidden="true" />
                    <span>{t('ui_delivery_timeline')}</span>
                  </Button>
                ) : null}
              </div>
              <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', overflowWrap: 'anywhere' }}>
                {t('ui_stage')} {e.stage || '—'}
                {e.task_id ? ` · ${t('ui_task')} ${e.task_id.slice(0, 8)}…` : ''}
                {e.reason ? ` · ${e.reason}` : ''}
              </div>
              {e.detail ? (
                <div
                  dir="ltr"
                  style={{
                    textAlign: 'start',
                    fontSize: 'var(--type-xs)',
                    color: 'var(--color-text-subtle)',
                    fontFamily: "'JetBrains Mono', Consolas, monospace",
                    wordBreak: 'break-word',
                  }}
                >
                  {e.detail}
                </div>
              ) : null}
              {e.correlation_id ? (
                <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', minInlineSize: 0 }}>
                  <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
                    {t('ui_correlation_id')}:
                  </span>
                  <code
                    className="font-mono-ltr"
                    style={{ fontSize: 'var(--type-xs)', overflowWrap: 'anywhere' }}
                  >
                    {e.correlation_id}
                  </code>
                  <button
                    type="button"
                    className="btn btn--ghost btn--icon btn--sm"
                    aria-label={t('ui_copy_log_line')}
                    title={t('ui_copy_log_line')}
                    onClick={() => copyText(e.correlation_id)}
                  >
                    <Copy size={12} aria-hidden="true" />
                  </button>
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}
