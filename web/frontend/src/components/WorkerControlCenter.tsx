/**
 * Go Worker Control Center — the operational flight deck for the worker
 * itself (§6 of the milestone brief). Five sections over one data source:
 * worker overview, queue monitor, per-task rows, the 11-stage pipeline, and
 * the error/warning center.
 *
 * Contract rules this honors:
 *  - every displayed number comes from an API response; there are no
 *    decorative or simulated values anywhere;
 *  - the worker OFFLINE state is explicit and first-class (503 with
 *    worker_reachable=false) — never rendered as a zeroed "idle" dashboard;
 *  - utilization = active / configured pool size, never goroutine count;
 *  - the AI stage is only "healthy" after a real success, never because a
 *    task enabled it;
 *  - no message content, tokens or secrets are ever shown — the error ring
 *    already sanitizes, and this layer adds no raw-AI-response rendering.
 *
 * Polling (not SSE): the worker's ops surface is a small bounded snapshot
 * and a 10s poll matches the existing DevOps panel cadence; the fetcher
 * re-arms on every filter change so a stale response never paints over a
 * newer one.
 *
 * Every visible label resolves through the catalog (makeT(lang)); the panel
 * inherits the shell's language and falls back to English, never Persian.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  Boxes,
  CheckCircle2,
  ChevronDown,
  Cpu,
  Database,
  Gauge,
  Layers,
  RefreshCw,
  Send,
  Server,
  Timer,
  Workflow,
  XCircle,
} from 'lucide-react'
import { Button, EmptyState } from './ui/Primitives'
import { api } from '../api'
import { makeT, type UiLang } from '../locales/engine'
import {
  isWorkerUnreachable,
  PIPELINE_STAGES,
  type LoadStatus,
  type PipelineStageRow,
  type WorkerErrorItem,
  type WorkerInfo,
  type WorkerQueue,
  type WorkerTaskRow,
} from '../workerOps'

const POLL_MS = 10_000
const ERROR_LIMIT = 100

type Severity = 'all' | 'ERROR' | 'WARN'
type Component = 'all' | string

function fmtMs(n: number | undefined | null): string {
  if (n == null || !isFinite(n)) return '—'
  if (n < 1) return `${(n * 1000).toFixed(0)}µs`
  if (n < 10) return `${n.toFixed(2)}ms`
  return `${n.toFixed(0)}ms`
}

function fmtUptime(seconds: number | undefined | null): string {
  if (seconds == null) return '—'
  const s = Math.floor(seconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m ${s % 60}s`
  return `${s}s`
}

function fmtBytes(n: number | undefined | null): string {
  if (n == null) return '—'
  const u = ['B', 'KB', 'MB', 'GB']
  let v = n
  let i = 0
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${u[i]}`
}

function shortSha(sha: string | undefined | null): string {
  if (!sha) return '—'
  return sha.length > 10 ? sha.slice(0, 10) : sha
}

const STATE_TONE: Record<string, string> = {
  ready: 'var(--color-success)',
  starting: 'var(--color-accent-violet)',
  degraded: 'var(--color-warning)',
  stopping: 'var(--color-warning)',
}

const LOAD_TONE: Record<LoadStatus, string> = {
  idle: 'var(--color-info)',
  normal: 'var(--color-success)',
  high: 'var(--color-warning)',
  saturated: 'var(--color-danger)',
  degraded: 'var(--color-warning)',
  unhealthy: 'var(--color-danger)',
}

const HEALTH_TONE: Record<string, string> = {
  healthy: 'var(--color-success)',
  degraded: 'var(--color-warning)',
  failed: 'var(--color-danger)',
  disabled: 'var(--color-text-muted)',
  waiting: 'var(--color-accent-violet)',
}

function Chip({
  ok,
  label,
  title,
}: {
  ok: boolean | null
  label: string
  title?: string
}) {
  const color = ok === null ? 'var(--color-text-muted)' : ok ? 'var(--color-success)' : 'var(--color-danger)'
  return (
    <span className="ops-chip" title={title} style={{ color }}>
      {ok === null ? '—' : ok ? <CheckCircle2 size={13} /> : <XCircle size={13} />}
      <span style={{ marginLeft: 5 }}>{label}</span>
    </span>
  )
}

/**
 * One latency trio drawn as a compact inline bar chart: three segments
 * scaled to p99 so no percentile ever reads as zero-width noise.
 */
function LatencyBars({
  l,
  unavailable,
}: {
  l: { p50: number; p95: number; p99: number }
  unavailable: string
}) {
  if (!l || [l.p50, l.p95, l.p99].some((v) => v == null || !isFinite(v))) {
    return <span className="ops-chip">{unavailable}</span>
  }
  const max = Math.max(l.p99, 0.001)
  const seg = (v: number, color: string) => (
    <span
      title={`${fmtMs(v)}`}
      style={{
        display: 'inline-block',
        height: 8,
        width: `${Math.max((v / max) * 100, 3)}%`,
        background: color,
        borderRadius: 3,
        marginRight: 4,
      }}
    />
  )
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 2, minWidth: 170 }}>
      {seg(l.p50, 'var(--color-success)')}
      {seg(l.p95, 'var(--color-warning)')}
      {seg(l.p99, 'var(--color-danger)')}
      <span style={{ fontSize: '0.75rem', marginLeft: 6, fontVariantNumeric: 'tabular-nums' }}>
        p99 {fmtMs(l.p99)}
      </span>
    </div>
  )
}

/**
 * The 11-stage pipeline as a horizontal signal chain. `disabled` (the task
 * switched a stage off) and `waiting` (dependency unreachable) are drawn
 * distinctly from `healthy`/`failed` so an operator reads WHY at a glance.
 */
function PipelineChain({
  stages,
  t,
}: {
  stages: PipelineStageRow[]
  t: (key: string, params?: Record<string, string | number>) => string
}) {
  if (!stages || stages.length === 0) {
    return <EmptyState icon={<Workflow size={22} />} title={t('ui_pipeline_telemetry_unavailable')} />
  }
  const known = new Map(stages.map((s) => [String(s.stage), s]))
  return (
    <div className="ops-chain" role="list" aria-label={t('ui_go_worker_pipeline_stages')}>
      {PIPELINE_STAGES.map((name, i) => {
        const s = known.get(name)
        const health = s?.health ?? 'waiting'
        const tone = s?.enabled === false ? HEALTH_TONE.disabled : HEALTH_TONE[health] ?? 'var(--color-text-muted)'
        return (
          <div key={name} role="listitem" className="ops-chain__node" style={{ '--tone': tone } as React.CSSProperties}>
            <span className="ops-chain__dot" style={{ background: tone }} />
            <span className="ops-chain__name">{name.replace('_', ' ')}</span>
            <span className="ops-chain__meta">
              {s ? (
                <>
                  {s.processed > 0 ? `${s.processed} · ${fmtMs(s.latency_ms_avg)}` : s.enabled === false ? 'off' : health}
                  {s.failures > 0 ? <span style={{ color: 'var(--color-danger)' }}> · {s.failures}✗</span> : null}
                </>
              ) : (
                t('ui_no_telemetry')
              )}
            </span>
            {i < PIPELINE_STAGES.length - 1 ? <span className="ops-chain__arrow" aria-hidden="true">→</span> : null}
          </div>
        )
      })}
    </div>
  )
}

export default function WorkerControlCenter({ lang }: { lang: UiLang }): React.JSX.Element {
  const t = makeT(lang)
  const [info, setInfo] = useState<WorkerInfo | null>(null)
  const [queue, setQueue] = useState<WorkerQueue | null>(null)
  const [tasks, setTasks] = useState<WorkerTaskRow[] | null>(null)
  const [errors, setErrors] = useState<WorkerErrorItem[] | null>(null)
  const [errorTotal, setErrorTotal] = useState(0)
  const [offline, setOffline] = useState(false)
  const [loading, setLoading] = useState(true)
  const [severity, setSeverity] = useState<Severity>('all')
  const [component, setComponent] = useState<Component>('all')
  const [expandedTask, setExpandedTask] = useState<string | null>(null)
  const [pollError, setPollError] = useState<string | null>(null)
  const gen = useRef(0)

  const refresh = useCallback(async () => {
    const g = ++gen.current
    setPollError(null)
    try {
      const [st, q, ts, er] = await Promise.all([
        api.workerStatus(),
        api.workerQueue(),
        api.workerTasks(),
        api.workerErrors({ severity: severity === 'all' ? undefined : severity, limit: ERROR_LIMIT }),
      ])
      if (g !== gen.current) return // a newer fetch already painted
      if (isWorkerUnreachable(st)) {
        setOffline(true)
        setInfo(null); setQueue(null); setTasks(null); setErrors(null)
        return
      }
      setOffline(false)
      // /ops Returns the worker snapshot envelope; the info block may be
      // nested or flat depending on the worker's /ops shape.
      setInfo((st as any).worker ?? (st as any))
      if (!isWorkerUnreachable(q)) setQueue(q as WorkerQueue)
      if (isWorkerUnreachable(ts)) setTasks([])
      else setTasks((ts as any).tasks ?? [])
      if (isWorkerUnreachable(er)) {
        setErrors([])
        setErrorTotal(0)
      } else {
        setErrors((er as any).items ?? [])
        setErrorTotal((er as any).total ?? 0)
      }
    } catch (e: any) {
      if (g !== gen.current) return
      setPollError(e?.message ?? 'fetch failed')
      setOffline(true)
    } finally {
      if (g === gen.current) setLoading(false)
    }
  }, [severity])

  useEffect(() => {
    void refresh()
    const t = setInterval(refresh, POLL_MS)
    return () => clearInterval(t)
  }, [refresh])

  const comps = component === 'all' ? null : component
  const visibleErrors = (errors ?? []).filter(
    (e) => (component === 'all' || e.component === comps) && (severity === 'all' || e.severity === severity),
  )
  const knownComponents = Array.from(new Set((errors ?? []).map((e) => e.component))).sort()

  if (loading) {
    return (
      <div className="ops-section">
        <div className="ops-head">
          <Server size={18} />
          <span className="ops-head__title">{t('ui_go_worker')}</span>
          <span className="ops-head__meta">{t('ui_connecting_ellipsis')}</span>
        </div>
        <EmptyState icon={<RefreshCw size={20} className="spin" />} title={t('ui_reaching_the_worker_ops_surface')} />
      </div>
    )
  }

  if (offline) {
    return (
      <div className="ops-section">
        <div className="ops-head">
          <Server size={18} />
          <span className="ops-head__title">{t('ui_go_worker')}</span>
          <span className="ops-head__meta">
            <span
              className="ops-levelchip"
              style={{ background: 'var(--color-danger)', color: 'var(--color-button-fg)' }}
            >
              {t('ui_offline')}
            </span>
          </span>
        </div>
        <EmptyState
          icon={<AlertTriangle size={22} />}
          title={t('ui_unreachable_no_live_snapshot')}
          description={
            pollError
              ? t('ui_unreachable_admin_api_detail', { error: pollError })
              : t('ui_unreachable_not_simulated')
          }
          action={<Button onClick={() => { setLoading(true); void refresh() }}>{t('ui_retry')}</Button>}
        />
      </div>
    )
  }

  const util = info ? Math.min((info.active ?? 0) / Math.max(info.num_workers ?? 1, 1), 1) : 0
  const loadStatus = queue?.load_status ?? 'idle'

  return (
    <>
      <div className="ops-section">
        <div className="ops-head">
          <Server size={18} />
          <span className="ops-head__title">{t('ui_go_worker')}</span>
          <span className="ops-head__meta">
            <span
              className="ops-levelchip"
              style={{ background: STATE_TONE[info?.state ?? 'starting'], color: 'var(--color-button-fg)' }}
            >
              {info?.state?.toUpperCase() ?? '—'}
            </span>
            <span className="ops-live ops-live--on" aria-label="live" />
          </span>
        </div>

        <div className="ops-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))' }}>
          <Metric icon={<Gauge size={14} />} label={t('ui_version')} value={info?.version ?? '—'} hint={shortSha(info?.commit_sha)} title={info?.commit_sha} />
          <Metric icon={<Timer size={14} />} label={t('ui_uptime')} value={fmtUptime(info?.uptime_seconds)} hint={t('ui_since_worker_start')} />
          <Metric icon={<Activity size={14} />} label={t('ui_utilization')} value={`${(util * 100).toFixed(0)}%`} hint={t('ui_workers_active', { active: info?.active ?? 0, configured: info?.num_workers ?? 0 })} title={t('ui_active_configured_pool_size')} />
          <Metric icon={<Cpu size={14} />} label={t('ui_goroutines')} value={String(info?.goroutines ?? '—')} hint={t('ui_process_level')} />
          <Metric icon={<Boxes size={14} />} label={t('ui_memory')} value={fmtBytes(info?.memory_bytes)} hint={t('ui_runtime_sys')} />
          <Metric icon={<Send size={14} />} label={t('ui_mode')} value={info?.processing_mode ?? '—'} hint={info?.queue_backend ?? ''} />
        </div>

        <div className="ops-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', marginTop: 10 }}>
          <Chip ok={info?.db_connected ?? null} label="DB" title={t('ui_delivery_store_reachable')} />
          <Chip ok={info?.telegram_connected ?? null} label={t('ui_telegram_label')} title={t('ui_gateway_probe_ok')} />
          <Chip ok={info?.ai_connected ?? null} label={t('ui_9router_ai')} title={t('ui_last_provider_probe_ok')} />
          <Chip ok={info?.switches?.worker_enabled ? true : false} label="ATF_WORKER_ENABLED" />
          <Chip ok={info?.switches?.transforms ? true : false} label="ATF_WORKER_TRANSFORMS" />
          <Chip ok={info?.switches?.autoreply ? true : false} label="ATF_WORKER_AUTOREPLY" />
        </div>
      </div>

      <div className="ops-section">
        <div className="ops-head">
          <Layers size={18} />
          <span className="ops-head__title">{t('ui_queue')}</span>
          <span className="ops-head__meta">
            <span className="ops-levelchip" style={{ background: LOAD_TONE[loadStatus], color: 'var(--color-button-fg)' }}>
              {loadStatus.toUpperCase()}
            </span>
          </span>
        </div>
        {queue ? (
          <>
            <div className="ops-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))' }}>
              <Metric icon={<Send size={14} />} label={t('ui_delivered')} value={String(queue.delivered)} />
              <Metric icon={<Activity size={14} />} label={t('ui_processing')} value={String(queue.processing)} />
              <Metric icon={<Timer size={14} />} label={t('ui_pending')} value={String(queue.pending)} hint={queue.oldest_pending_age_seconds > 0 ? t('ui_oldest_old', { age: fmtUptime(queue.oldest_pending_age_seconds) }) : t('ui_empty')} />
              <Metric icon={<AlertTriangle size={14} />} label={t('ui_retry_pending')} value={String(queue.retry_pending)} />
              <Metric icon={<XCircle size={14} />} label={t('ui_failed')} value={String(queue.failed)} />
              <Metric icon={<XCircle size={14} />} label={t('ui_dead_letter')} value={String(queue.dead_letter)} />
              <Metric icon={<Gauge size={14} />} label={t('ui_jobs_per_second')} value={(queue.jobs_per_second ?? 0).toFixed(1)} />
              <Metric icon={<Database size={14} />} label={t('ui_backpressure')} value={String(queue.capacity)} hint={t('ui_queue_capacity_ceiling')} />
            </div>
            <div style={{ marginTop: 10 }}>
              <div style={{ display: 'flex', gap: 18, alignItems: 'center', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>{t('ui_end_to_end_latency')}</span>
                <LatencyBars l={queue.latency} unavailable={t('ui_latency_unavailable')} />
              </div>
              <div style={{ display: 'flex', gap: 18, alignItems: 'center', flexWrap: 'wrap', marginTop: 6 }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>{t('ui_queue_wait')}</span>
                <LatencyBars l={queue.queue_wait} unavailable={t('ui_latency_unavailable')} />
              </div>
            </div>
          </>
        ) : (
          <EmptyState icon={<Layers size={22} />} title={t('ui_queue_snapshot_unavailable')} />
        )}
      </div>

      <div className="ops-section">
        <div className="ops-head">
          <Workflow size={18} />
          <span className="ops-head__title">{t('ui_pipeline')}</span>
          <span className="ops-head__meta">{t('ui_pipeline_stages_live', { count: PIPELINE_STAGES.length })}</span>
        </div>
        <PipelineChain stages={(info as any)?.pipeline ?? []} t={t} />
        <p style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)', margin: '8px 0 0' }}>
          {t('ui_the_ai_stage_is_only_healthy_after_a_real_successful_rewrite')}
        </p>
      </div>

      <div className="ops-section">
        <div className="ops-head">
          <Activity size={18} />
          <span className="ops-head__title">{t('ui_tasks')}</span>
          <span className="ops-head__meta">{t('ui_tasks_configured', { count: tasks?.length ?? 0 })}</span>
        </div>
        {tasks && tasks.length > 0 ? (
          <div className="ops-console" style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
              <thead>
                <tr style={{ textAlign: 'left', color: 'var(--color-text-muted)' }}>
                  <th style={{ padding: '4px 8px' }}>{t('ui_task')}</th>
                  <th style={{ padding: '4px 8px' }}>{t('ui_state')}</th>
                  <th style={{ padding: '4px 8px' }}>{t('ui_delivered_failed')}</th>
                  <th style={{ padding: '4px 8px' }}>{t('ui_pending')}</th>
                  <th style={{ padding: '4px 8px' }}>{t('ui_throughput')}</th>
                  <th style={{ padding: '4px 8px' }}>{t('ui_features')}</th>
                  <th style={{ padding: '4px 8px' }}>{t('ui_last_error')}</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((row) => {
                  const open = expandedTask === row.rule_id
                  return (
                    <>
                      <tr
                        key={row.rule_id}
                        style={{ cursor: 'pointer', borderTop: '1px solid var(--color-border)' }}
                        onClick={() => setExpandedTask(open ? null : row.rule_id)}
                        title={t('ui_click_for_the_feature_breakdown')}
                      >
                        <td style={{ padding: '4px 8px' }}>
                          {open ? <ChevronDown size={12} /> : <ChevronRight />}
                          <code style={{ marginLeft: 4 }}>{row.rule_id.slice(0, 12)}</code>
                        </td>
                        <td style={{ padding: '4px 8px' }}>
                          <span style={{ color: row.enabled ? 'var(--color-success)' : 'var(--color-text-muted)' }}>
                            {row.enabled ? t('ui_enabled') : t('ui_disabled')}
                          </span>
                        </td>
                        <td style={{ padding: '4px 8px', fontVariantNumeric: 'tabular-nums' }}>
                          {row.delivered} / <span style={{ color: row.failed ? 'var(--color-danger)' : 'inherit' }}>{row.failed}</span>
                        </td>
                        <td style={{ padding: '4px 8px', fontVariantNumeric: 'tabular-nums' }}>{row.pending}</td>
                        <td style={{ padding: '4px 8px', fontVariantNumeric: 'tabular-nums' }}>{row.throughput_per_min.toFixed(1)}{t('ui_per_min')} {fmtMs(row.latency_ms_avg)}</td>
                        <td style={{ padding: '4px 8px' }}>
                          {[row.features.filter && 'F', row.features.replacement && 'R', row.features.ai && 'AI', row.features.cleaner && 'C', row.features.fen > 1 && `N${row.features.fen}`]
                            .filter(Boolean)
                            .join(' ') || '—'}
                        </td>
                        <td style={{ padding: '4px 8px', maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={row.last_error ?? undefined}>
                          {row.last_error ?? <span style={{ color: 'var(--color-success)' }}>{t('ui_ok')}</span>}
                        </td>
                      </tr>
                      {open ? (
                        <tr key={`${row.rule_id}-detail`} style={{ color: 'var(--color-text-muted)' }}>
                          <td colSpan={7} style={{ padding: '8px 8px 12px 28px' }}>
                            <div>{t('ui_source')} <code>{row.source}</code> {t('ui_arrow_to')} <code>{row.target}</code></div>
                            <div>{t('ui_retries_label')} {row.retries} {t('ui_worker_label')} {row.worker_id || '—'} {t('ui_last_success_label')} {row.last_success_at ?? '—'}</div>
                            <div>
                              {t('ui_filter_label')} {String(row.features.filter)} · {t('ui_replacement_label')} {String(row.features.replacement)} · {t('ui_ai_label')} {String(row.features.ai)} · {t('ui_cleaner_label')} {String(row.features.cleaner)} · {t('ui_header_label')} {String(row.features.header)} · {t('ui_footer_label')} {String(row.features.footer)} · {t('ui_media_label')} {String(row.features.media)} · {t('ui_fallback_label')} {String(row.features.fallback)}
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon={<Activity size={22} />} title={t('ui_no_task_rows')} description={t('ui_no_task_rows_description')} />
        )}
      </div>

      <div className="ops-section">
        <div className="ops-head">
          <AlertTriangle size={18} />
          <span className="ops-head__title">{t('ui_errors_warnings')}</span>
          <span className="ops-head__meta">
            {t('ui_total_shown_deduplicated', { total: errorTotal, shown: visibleErrors.length })}
          </span>
        </div>
        <div className="ops-filter" style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
          <select value={severity} onChange={(e) => setSeverity(e.target.value as Severity)} aria-label={t('ui_filter_by_severity')}>
            <option value="all">{t('ui_all_severities')}</option>
            <option value="ERROR">ERROR</option>
            <option value="WARN">WARN</option>
          </select>
          <select value={component} onChange={(e) => setComponent(e.target.value)} aria-label={t('ui_filter_by_component')}>
            <option value="all">{t('ui_all_components')}</option>
            {knownComponents.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>
        {visibleErrors.length > 0 ? (
          <div className="ops-console" style={{ maxHeight: 340, overflowY: 'auto' }}>
            {visibleErrors.map((e, i) => (
              <div key={`${e.correlation_id ?? i}-${e.ts}`} style={{ display: 'flex', gap: 10, padding: '5px 2px', borderBottom: '1px solid var(--color-border)', fontSize: '0.78rem' }}>
                <span style={{ color: e.severity === 'ERROR' ? 'var(--color-danger)' : 'var(--color-warning)', fontWeight: 600, minWidth: 44 }}>
                  {String(e.severity).toUpperCase()}
                </span>
                <span style={{ color: 'var(--color-text-muted)', minWidth: 130, fontFamily: 'var(--font-mono)' }} title={e.ts}>
                  {new Date(e.ts).toLocaleTimeString()}
                </span>
                <span style={{ minWidth: 120, fontFamily: 'var(--font-mono)' }}>{e.component}</span>
                <span style={{ minWidth: 90, fontFamily: 'var(--font-mono)' }}>{e.code || '—'}</span>
                <span style={{ color: e.retryable ? 'var(--color-warning)' : 'var(--color-text-muted)', minWidth: 70 }}>
                  {e.retryable ? t('ui_retry_label', { count: e.retry_count }) : t('ui_permanent')}
                </span>
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={e.message}>{e.message}</span>
                {e.count && e.count > 1 ? (
                  <span className="ops-levelchip" title={t('ui_repeated_times_since', { count: e.count, since: e.first_seen ?? '' })}>×{e.count}</span>
                ) : null}
              </div>
            ))}
          </div>
        ) : (
          <EmptyState icon={<CheckCircle2 size={22} />} title={t('ui_no_errors_match_this_filter')} />
        )}
      </div>
    </>
  )
}

function Metric({
  icon,
  label,
  value,
  hint,
  title,
}: {
  icon: React.ReactNode
  label: string
  value: string
  hint?: string
  title?: string
}) {
  return (
    <div className="ops-chip ops-chip--info" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 2 }} title={title}>
      <span style={{ display: 'flex', alignItems: 'center', gap: 5, color: 'var(--color-text-muted)', fontSize: '0.7rem' }}>
        {icon} {label}
      </span>
      <strong style={{ fontSize: '0.92rem' }}>{value}</strong>
      {hint ? <span style={{ fontSize: '0.68rem', color: 'var(--color-text-muted)' }}>{hint}</span> : null}
    </div>
  )
}

function ChevronRight() {
  return <span style={{ color: 'var(--color-text-muted)' }} aria-hidden="true">▸</span>
}
