/**
 * IncidentCenter — the §9 error-intelligence console.
 *
 * Where the error ring answers "what failed", an incident answers "is it
 * STILL failing, did it recover, did it come back, and which jobs did it
 * touch". Every row is a real backend response from the worker's incident
 * tracker (GET /worker/incidents, POST /worker/incidents/ack) — the
 * component owns no state of its own, renders the unreachable case as the
 * worker being offline, and NEVER invents incidents.
 *
 * Patterns are rendered as SUSPECTED with an imperative hint: the hint says
 * what to CHECK, the error code only proves what FAILED. The UI never
 * promotes a guess to a confirmed root cause.
 */
import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, AlertTriangle, ChevronDown, ChevronRight, Check, Loader2 } from 'lucide-react'
import { api } from '../api'
import { ConfirmDialog } from './ConfirmDialog'
import { isWorkerUnreachable, type WorkerIncident } from '../workerOps'
import { makeT, type UiLang } from '../locales/engine'

const POLL_MS = 10_000

const STATE_TONE: Record<string, string> = {
  open: 'var(--color-danger)',
  recurrent: 'var(--color-danger)',
  acknowledged: 'var(--color-warning)',
  suppressed: 'var(--color-text-muted)',
  recovered: 'var(--color-success)',
}

type StateFilter = '' | 'open' | 'recurrent' | 'acknowledged' | 'recovered' | 'suppressed'

const STATES: { value: StateFilter; key: string }[] = [
  { value: '', key: 'ui_incident_state_all' },
  { value: 'open', key: 'ui_incident_state_open' },
  { value: 'recurrent', key: 'ui_incident_state_recurrent' },
  { value: 'acknowledged', key: 'ui_incident_state_acknowledged' },
  { value: 'recovered', key: 'ui_incident_state_recovered' },
  { value: 'suppressed', key: 'ui_incident_state_suppressed' },
]

function fmtTs(ts?: string): string {
  if (!ts) return '—'
  const ms = Date.parse(ts)
  if (!Number.isFinite(ms)) return '—'
  return new Date(ms).toISOString().replace('T', ' ').replace('.000Z', 'Z')
}

function ageOf(ts?: string): string {
  if (!ts) return '—'
  const ms = Date.parse(ts)
  if (!Number.isFinite(ms)) return '—'
  const sec = Math.max(0, Math.round((Date.now() - ms) / 1000))
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.round(sec / 60)}m`
  if (sec < 86400) return `${Math.round(sec / 3600)}h`
  return `${Math.round(sec / 86400)}d`
}

export function IncidentCenter({ lang }: { lang: UiLang }) {
  const t = makeT(lang)
  const [incidents, setIncidents] = useState<WorkerIncident[] | null>(null)
  const [unreachable, setUnreachable] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [stateFilter, setStateFilter] = useState<StateFilter>('open')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [acking, setAcing] = useState<string | null>(null)
  const [lastUpdate, setLastUpdate] = useState<number | null>(null)
  // Acknowledging gates through the shared alertdialog, not window.confirm.
  const [pendingAck, setPendingAck] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const r = await api.workerIncidents(stateFilter || undefined)
      if (isWorkerUnreachable(r)) {
        setUnreachable(true)
        setIncidents(null)
      } else {
        setUnreachable(false)
        setIncidents(r.incidents ?? [])
      }
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLastUpdate(Date.now())
      setLoading(false)
    }
  }, [stateFilter])

  useEffect(() => {
    void refresh()
    const timer = setInterval(() => void refresh(), POLL_MS)
    return () => clearInterval(timer)
  }, [refresh])

  const ack = useCallback(
    async (id: string) => {
      setAcing(id)
      try {
        await api.workerIncidentAck(id)
        await refresh()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setAcing(null)
      }
    },
    [refresh],
  )

  return (
    <div className="ops-console" role="region" aria-label={t('ui_incident_center')}>
      <div className="ops-head">
        <AlertTriangle size={18} />
        <span className="ops-head__title">{t('ui_incident_center')}</span>
        <span className="ops-head__meta">
          <select
            className="ops-filter"
            value={stateFilter}
            onChange={(e) => {
              setStateFilter(e.target.value as StateFilter)
              setLoading(true)
            }}
            aria-label={t('ui_incident_filter_state')}
          >
            {STATES.map((s) => (
              <option key={s.value} value={s.value}>
                {t(s.key)}
              </option>
            ))}
          </select>
          {lastUpdate ? (
            <span className="ops-chip ops-chip--info" title={new Date(lastUpdate).toISOString()}>
              {t('ui_last_update')} {new Date(lastUpdate).toLocaleTimeString()}
            </span>
          ) : null}
          <button
            type="button"
            className="ops-filter"
            onClick={() => {
              setLoading(true)
              void refresh()
            }}
          >
            <RefreshCw size={13} aria-hidden="true" /> {t('ui_refresh')}
          </button>
        </span>
      </div>

      {unreachable ? (
        <div className="ops-empty" style={{ padding: '16px 2px', color: 'var(--color-danger)', display: 'flex', gap: 8, alignItems: 'center' }}>
          <AlertTriangle size={15} aria-hidden="true" />
          <span>{t('ui_incident_worker_unreachable')}</span>
        </div>
      ) : error ? (
        <div className="ops-empty" style={{ padding: '16px 2px', color: 'var(--color-danger)', display: 'flex', gap: 8, alignItems: 'center' }}>
          <AlertTriangle size={15} aria-hidden="true" />
          <span>{t('ui_incident_fetch_failed')}: {error}</span>
        </div>
      ) : loading && incidents === null ? (
        <div className="ops-empty" style={{ padding: '16px 2px' }}>
          <Loader2 size={16} className="spin" aria-hidden="true" /> {t('ui_loading')}
        </div>
      ) : incidents && incidents.length === 0 ? (
        <div className="ops-empty" style={{ padding: '16px 2px', color: 'var(--color-text-muted)' }}>
          {t('ui_incident_empty')}
        </div>
      ) : (
        <div className="ops-loglist" role="list" style={{ marginTop: 4 }}>
          {incidents?.map((inc) => {
            const isOpen = expanded === inc.id
            return (
              <div key={inc.id} className="ops-logrow" role="listitem" style={{ borderBottom: '1px solid var(--color-border)' }}>
                <div
                  className="ops-logrow__head"
                  style={{ display: 'flex', gap: 8, alignItems: 'baseline', padding: '4px 2px', cursor: 'pointer' }}
                  onClick={() => setExpanded(isOpen ? null : inc.id)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      setExpanded(isOpen ? null : inc.id)
                    }
                  }}
                >
                  {isOpen ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
                  <span
                    className="ops-levelchip"
                    style={{ background: STATE_TONE[inc.state] ?? 'var(--color-text-muted)', color: 'var(--color-button-fg)' }}
                    title={t('ui_incident_state_hint')}
                  >
                    {inc.state.toUpperCase()}
                  </span>
                  <span style={{ fontSize: '0.75rem', fontWeight: 600 }}>{inc.component}</span>
                  <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>{inc.code}</span>
                  <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>
                    ×{inc.count}
                  </span>
                  {inc.recoveries > 0 ? (
                    <span style={{ fontSize: '0.75rem', color: 'var(--color-warning)' }}>
                      {t('ui_incident_recoveries', { n: inc.recoveries })}
                    </span>
                  ) : null}
                  <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)', marginLeft: 'auto' }} title={fmtTs(inc.last_seen)}>
                    {t('ui_incident_last_seen_ago', { age: ageOf(inc.last_seen) })}
                  </span>
                </div>
                {isOpen ? (
                  <div style={{ padding: '4px 6px 10px 20px', display: 'flex', flexDirection: 'column', gap: 6 }}>
                    <div style={{ fontSize: '0.75rem', color: 'var(--color-text)' }}>{inc.message}</div>
                    <div style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>
                      {t('ui_incident_span', { first: fmtTs(inc.first_seen), last: fmtTs(inc.last_seen) })}
                    </div>
                    {inc.pattern ? (
                      <div
                        style={{
                          fontSize: '0.75rem',
                          padding: '6px 8px',
                          border: '1px solid var(--color-border)',
                          borderRadius: 6,
                          background: 'var(--color-surface)',
                        }}
                      >
                        <strong>{t('ui_incident_pattern_suspected')}: {inc.pattern.name}</strong>
                        <div style={{ marginTop: 2 }}>{inc.pattern.hint}</div>
                      </div>
                    ) : null}
                    <div style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>
                      {t('ui_incident_affected', {
                        workers: inc.affected_workers.join(', ') || '—',
                        rules: inc.affected_rules.join(', ') || '—',
                        jobs: inc.related_jobs.join(', ') || '—',
                      })}
                    </div>
                    {inc.acked_by ? (
                      <div style={{ fontSize: '0.75rem', color: 'var(--color-warning)' }}>
                        {t('ui_incident_acked_by', { who: inc.acked_by, at: fmtTs(inc.acked_at) })}
                      </div>
                    ) : null}
                    {inc.state === 'open' || inc.state === 'recurrent' ? (
                      <div>
                        <button
                          type="button"
                          className="btn btn--ghost btn--sm"
                          onClick={(e) => {
                            e.stopPropagation()
                            setPendingAck(inc.id)
                          }}
                          disabled={acking === inc.id}
                        >
                          {acking === inc.id ? <Loader2 size={12} className="spin" aria-hidden="true" /> : <Check size={12} aria-hidden="true" />}{' '}
                          {t('ui_incident_ack')}
                        </button>
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </div>
            )
          })}
        </div>
      )}

      <ConfirmDialog
        open={pendingAck !== null}
        lang={lang}
        title={t('ui_incident_ack_confirm')}
        confirmLabel={t('ui_yes')}
        cancelLabel={t('ui_cancel')}
        onConfirm={() => {
          const id = pendingAck
          setPendingAck(null)
          if (id) void ack(id)
        }}
        onCancel={() => setPendingAck(null)}
      />
    </div>
  )
}
