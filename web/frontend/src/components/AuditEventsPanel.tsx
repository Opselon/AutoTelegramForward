import { useCallback, useEffect, useState } from 'react'
import {
  Activity,
  RefreshCw,
  Search,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  HelpCircle,
  Clock,
} from 'lucide-react'
import { api } from '../api'
import type { AuditEventEntry } from '../types'
import { makeT, type UiLang } from '../locales/engine'

interface Props {
  lang: UiLang
}

const outcomeIcon = (outcome?: string) => {
  switch ((outcome || '').toUpperCase()) {
    case 'SUCCEEDED':
      return <CheckCircle2 size={14} color="var(--color-success)" />
    case 'FAILED':
      return <XCircle size={14} color="var(--color-danger)" />
    case 'OUTCOME_UNCONFIRMED':
      return <HelpCircle size={14} color="var(--color-warning)" />
    default:
      return <Clock size={14} color="var(--color-text-subtle)" />
  }
}

const severityColor = (sev?: string) => {
  switch ((sev || '').toUpperCase()) {
    case 'ERROR':
    case 'FATAL':
      return 'var(--color-danger)'
    case 'WARN':
      return 'var(--color-warning)'
    case 'DEBUG':
      return 'var(--color-text-subtle)'
    default:
      return 'var(--color-info)'
  }
}

const fmtTs = (ts: number) => {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString() + ' · ' + d.toLocaleDateString()
}

export function AuditEventsPanel({ lang }: Props) {
  const t = makeT(lang)
  const [events, setEvents] = useState<AuditEventEntry[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [corrSearch, setCorrSearch] = useState('')
  const [fService, setFService] = useState('')
  const [fOutcome, setFOutcome] = useState('')
  const [selected, setSelected] = useState<AuditEventEntry | null>(null)

  const fetchEvents = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await api.queryAuditEvents({
        correlation_id: corrSearch || undefined,
        service: fService || undefined,
        outcome: fOutcome || undefined,
        limit: 100,
      })
      setEvents(res.events || [])
      setTotal(res.total || 0)
    } catch (e: any) {
      setError(e?.message || t('ui_could_not_load_audit_events'))
      setEvents([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }, [corrSearch, fService, fOutcome])

  useEffect(() => {
    fetchEvents()
  }, [fetchEvents])

  return (
    <div>
      <div style={{ marginBottom: 20, display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h2 style={{ fontSize: 'var(--type-lg)', fontWeight: 800, letterSpacing: '-0.02em', display: 'flex', alignItems: 'center', gap: 10 }}>
            <Activity size={22} color="var(--color-success)" />
            <span>{t('ui_audit_events_execution_assurance')}</span>
          </h2>
          <p style={{ fontSize: '0.825rem', color: 'var(--color-text-muted)', marginTop: 4 }}>
            {t('ui_durable_postgresql_audit_events_with_correlation_id_search_actua')}
          </p>
        </div>
        <button onClick={fetchEvents} className="btn btn-secondary" style={{ padding: '6px 12px', fontSize: '0.8rem' }}>
          <RefreshCw size={14} />
          <span>{t('ui_refresh')}</span>
        </button>
      </div>

      {/* Filters */}
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 16 }}>
        <div style={{ position: 'relative', flex: '1 1 260px', minWidth: 220 }}>
          <Search size={14} style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--color-text-muted)' }} />
          <input
            value={corrSearch}
            onChange={(e) => setCorrSearch(e.target.value)}
            placeholder={t('ui_search_by_correlation_id')}
            onKeyDown={(e) => e.key === 'Enter' && fetchEvents()}
            style={{
              width: '100%', padding: '8px 10px 8px 32px', borderRadius: 8,
              background: 'var(--color-surface-inset)', border: '1px solid var(--color-border)',
              color: 'var(--color-text)', fontSize: '0.825rem', outline: 'none',
            }}
          />
        </div>
        <select
          value={fService}
          onChange={(e) => setFService(e.target.value)}
          style={{ padding: '8px 10px', borderRadius: 8, background: 'var(--color-surface-inset)', border: '1px solid var(--color-border)', color: 'var(--color-text)', fontSize: '0.825rem' }}
        >
          <option value="">{t('ui_audit_events_all_services')}</option>
          <option value="core">core</option>
          <option value="api">api</option>
          <option value="logger">logger</option>
          <option value="web">web</option>
        </select>
        <select
          value={fOutcome}
          onChange={(e) => setFOutcome(e.target.value)}
          style={{ padding: '8px 10px', borderRadius: 8, background: 'var(--color-surface-inset)', border: '1px solid var(--color-border)', color: 'var(--color-text)', fontSize: '0.825rem' }}
        >
          <option value="">{t('ui_all_outcomes')}</option>
          <option value="SUCCEEDED">{t('ui_outcome_succeeded')}</option>
          <option value="FAILED">{t('ui_outcome_failed')}</option>
          <option value="ACCEPTED">{t('ui_outcome_accepted')}</option>
          <option value="IN_PROGRESS">{t('ui_outcome_in_progress')}</option>
          <option value="OUTCOME_UNCONFIRMED">{t('ui_outcome_unconfirmed')}</option>
        </select>
      </div>

      {error && (
        <div style={{ padding: '10px 14px', borderRadius: 8, background: 'var(--color-danger-soft)', border: '1px solid color-mix(in oklab, var(--color-danger) 35%, transparent)', color: 'var(--color-danger)', fontSize: '0.825rem', marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
          <AlertTriangle size={14} />
          <span>{error}</span>
        </div>
      )}

      {/* Summary line */}
      <div style={{ fontSize: '0.8rem', color: 'var(--color-text-muted)', marginBottom: 10 }}>
        {t('ui_total_2_event_s', { total_2: total })}
        {loading ? (t('ui_loading')) : ''}
      </div>

      {/* Event list */}
      {events.length === 0 && !loading && !error ? (
        <div style={{ textAlign: 'center', padding: '48px 20px', color: 'var(--color-text-muted)' }}>
          <Activity size={36} style={{ margin: '0 auto 12px', opacity: 0.5 }} />
          <div style={{ fontSize: '0.9rem', fontWeight: 600 }}>{t('ui_no_audit_events_found')}</div>
          <div style={{ fontSize: '0.8rem', marginTop: 6 }}>
            {t('ui_audit_events_appear_here_as_messages_flow_through_the_pipeline')}
          </div>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {events.map((evt) => (
            <button
              key={evt.event_id}
              onClick={() => setSelected(selected?.event_id === evt.event_id ? null : evt)}
              style={{
                textAlign: 'left', width: '100%', cursor: 'pointer',
                padding: '10px 14px', borderRadius: 10,
                background: 'var(--color-surface-2)', border: '1px solid var(--color-border)',
                display: 'flex', alignItems: 'center', gap: 12,
              }}
            >
              {outcomeIcon(evt.outcome)}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '0.825rem', fontWeight: 600, color: 'var(--color-text)', display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                  <span>{evt.event_type}</span>
                  {evt.reason_code && (
                    <span style={{ fontSize: 'var(--type-xs)', padding: '1px 6px', borderRadius: 4, background: 'var(--color-danger-soft)', color: 'var(--color-danger)' }}>
                      {evt.reason_code}
                    </span>
                  )}
                  {evt.rule_id && <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>rule={evt.rule_id.slice(0, 8)}</span>}
                </div>
                <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginTop: 3, display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                  <span>{evt.service}</span>
                  <span>{fmtTs(evt.ts_utc)}</span>
                  {evt.correlation_id && <span style={{ fontFamily: 'monospace' }}>{evt.correlation_id.slice(0, 12)}</span>}
                  {evt.result_summary && <span style={{ opacity: 0.85 }}>{evt.result_summary.slice(0, 80)}</span>}
                </div>
              </div>
              <span style={{ fontSize: 'var(--type-xs)', fontWeight: 700, color: severityColor(evt.severity) }}>
                {(evt.severity || 'INFO').toUpperCase()}
              </span>
              <span style={{ fontSize: 'var(--type-xs)', fontWeight: 700, color: outcomeColor(evt.outcome) }}>
                {(evt.outcome || '—').toUpperCase()}
              </span>
            </button>
          ))}
        </div>
      )}

      {/* Detail drawer */}
      {selected && (
        <div
          style={{
            marginTop: 16, padding: 16, borderRadius: 12,
            background: 'var(--color-surface-2)', border: '1px solid color-mix(in oklab, var(--color-success) 35%, transparent)',
          }}
        >
          <div style={{ fontSize: '0.9rem', fontWeight: 700, marginBottom: 10, display: 'flex', alignItems: 'center', gap: 8 }}>
            {outcomeIcon(selected.outcome)}
            <span>{selected.event_type} · {selected.event_id.slice(0, 12)}</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 10 }}>
            {detailFields(selected).map(([k, v]) => (
              v ? (
                <div key={k} style={{ fontSize: 'var(--type-xs)' }}>
                  <div style={{ color: 'var(--color-text-muted)', marginBottom: 2 }}>{k}</div>
                  <div style={{ color: 'var(--color-text)', fontFamily: k.includes('id') ? 'monospace' : 'inherit', wordBreak: 'break-all' }}>
                    {v}
                  </div>
                </div>
              ) : null
            ))}
          </div>
          {selected.metadata_json && selected.metadata_json !== '{}' && (
            <div style={{ marginTop: 12, fontSize: 'var(--type-xs)' }}>
              <div style={{ color: 'var(--color-text-muted)', marginBottom: 4 }}>{t('ui_metadata')}</div>
              <pre style={{ color: 'var(--color-text-muted)', background: 'var(--color-surface-inset)', padding: 10, borderRadius: 8, overflow: 'auto', margin: 0, fontSize: 'var(--type-xs)' }}>
                {selected.metadata_json}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function outcomeColor(outcome?: string) {
  switch ((outcome || '').toUpperCase()) {
    case 'SUCCEEDED':
      return 'var(--color-success)'
    case 'FAILED':
      return 'var(--color-danger)'
    case 'OUTCOME_UNCONFIRMED':
      return 'var(--color-warning)'
    default:
      return 'var(--color-text-subtle)'
  }
}

function detailFields(evt: AuditEventEntry): [string, string][] {
  return [
    ['event_type', evt.event_type],
    ['event_category', evt.event_category || ''],
    ['feature_id', evt.feature_id || ''],
    ['action', evt.action || ''],
    ['correlation_id', evt.correlation_id || ''],
    ['operation_id', evt.operation_id || ''],
    ['rule_id', evt.rule_id || ''],
    ['queue_job_id', evt.queue_job_id || ''],
    ['resource_type', evt.resource_type || ''],
    ['resource_id', evt.resource_id || ''],
    ['tenant_id', evt.tenant_id || ''],
    ['actor_type', evt.actor_type || ''],
    ['actor_id', evt.actor_id || ''],
    ['reason_code', evt.reason_code || ''],
    ['error_code', evt.error_code || ''],
    ['error_class', evt.error_class || ''],
    ['retryable', evt.retryable ? 'true' : ''],
    ['duration_ms', evt.duration_ms ? String(evt.duration_ms) : ''],
    ['result_summary', evt.result_summary || ''],
    ['ts_utc', fmtTs(evt.ts_utc)],
  ]
}
