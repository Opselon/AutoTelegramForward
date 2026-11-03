/**
 * Logging-pipeline health, read through the authoritative Control Plane
 * (`GET /logs/health`).
 *
 * Why this exists: an empty log console has two very different causes —
 * "nothing happened" and "the collector is dead". The queue snapshot and the
 * service health chips cannot tell them apart, because they answer whether the
 * *services* answer, not whether log *collection* works. This probe walks the
 * pipeline itself (API producer policy → logger ingest → core policy) and
 * renders the four honest states of the runtime-visibility contract:
 *
 *   healthy → degraded → unavailable → unknown
 *
 * An unknown/unreachable probe is rendered as `unknown`, never as healthy —
 * the same rule the shell's health chips follow.
 *
 * Polling policy: none. Loads on mount and refreshes on request; the live log
 * console next to it already refreshes on its own explicit cadence.
 */
import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { api } from '../api'
import { makeT, type UiLang } from '../locales/engine'
import { StatusIndicator, type StatusTone } from './ui/Primitives'

interface LoggingHealthService {
  service: string
  status: string
  detail?: string
  data_age_seconds?: number
}

interface LoggingHealth {
  status?: string
  services?: LoggingHealthService[]
}

function statusTone(status: string | undefined): StatusTone {
  switch (status) {
    case 'healthy':
      return 'success'
    case 'degraded':
      return 'warning'
    case 'unavailable':
      return 'danger'
    default:
      // `unknown`, or anything a future core adds — never painted as healthy.
      return 'unknown'
  }
}

function statusLabel(status: string | undefined, t: (key: string) => string): string {
  switch (status) {
    case 'healthy':
      return t('ui_health_healthy')
    case 'degraded':
      return t('ui_degraded')
    case 'unavailable':
      return t('ui_health_status_unavailable')
    default:
      return t('ui_status_unknown')
  }
}

export function LoggingHealth({ lang }: { lang: UiLang }) {
  const t = makeT(lang)
  const [health, setHealth] = useState<LoggingHealth | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setHealth(await api.getLogsHealth())
      setFailed(false)
    } catch {
      // A failed probe is a status, not an error banner: the pipeline being
      // unreachable is exactly what this component exists to surface.
      setFailed(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const services = health?.services ?? []
  return (
    <div
      className="logging-health"
      style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', flexWrap: 'wrap', minInlineSize: 0 }}
      role="status"
      aria-label={t('ui_logs_pipeline')}
    >
      <span
        style={{
          fontSize: 'var(--type-xs)',
          fontWeight: 700,
          color: 'var(--color-text-muted)',
          whiteSpace: 'nowrap',
        }}
      >
        {t('ui_logs_pipeline')}
      </span>
      {failed ? (
        <StatusIndicator tone="unknown" label={t('ui_status_unknown')} title={t('ui_status_unknown')} />
      ) : !health ? (
        <StatusIndicator tone="unknown" pulse label={t('ui_probing')} />
      ) : (
        services.map((svc) => (
          <StatusIndicator
            key={svc.service}
            tone={statusTone(svc.status)}
            pulse={svc.status === 'healthy'}
            label={`${svc.service} · ${statusLabel(svc.status, t)}`}
            title={svc.detail ?? undefined}
          />
        ))
      )}
      <button
        type="button"
        onClick={() => void load()}
        className="btn btn--ghost btn--icon btn--sm"
        aria-label={t('ui_refresh')}
        title={t('ui_refresh')}
        disabled={loading}
      >
        <RefreshCw size={12} aria-hidden="true" className={loading ? 'spin' : undefined} />
      </button>
    </div>
  )
}
