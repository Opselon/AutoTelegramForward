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
function LatencyBars({ l }: { l: { p50: number; p95: number; p99: number } }) {
  if (!l || [l.p50, l.p95, l.p99].some((v) => v == null || !isFinite(v))) {
    return <span className="ops-chip">latency unavailable</span>
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
function PipelineChain({ stages }: { stages: PipelineStageRow[] }) {
  if (!stages || stages.length === 0) {
    return <EmptyState icon={<Workflow size={22} />} title="Pipeline telemetry unavailable" />
  }
  const known = new Map(stages.map((s) => [String(s.stage), s]))
  return (
    <div className="ops-chain" role="list" aria-label="Go Worker pipeline stages">
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
                'no telemetry'
              )}
            </span>
            {i < PIPELINE_STAGES.length - 1 ? <span className="ops-chain__arrow" aria-hidden="true">→</span> : null}
          </div>
        )
      })}
    </div>
  )
}

export default function WorkerControlCenter(): React.JSX.Element {
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
      // /ops returns the worker snapshot envelope; the info block may be
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
          <span className="ops-head__title">Go Worker</span>
          <span className="ops-head__meta">connecting…</span>
        </div>
        <EmptyState icon={<RefreshCw size={20} className="spin" />} title="Reaching the worker's ops surface…" />
      </div>
    )
  }

  if (offline) {
    return (
      <div className="ops-section">
        <div className="ops-head">
          <Server size={18} />
          <span className="ops-head__title">Go Worker</span>
          <span className="ops-head__meta">
            <span
              className="ops-levelchip"
              style={{ background: 'var(--color-danger)', color: 'var(--color-button-fg)' }}
            >
              OFFLINE
            </span>
          </span>
        </div>
        <EmptyState
          icon={<AlertTriangle size={22} />}
          title="Worker unreachable — no live snapshot"
          description={
            pollError
              ? `The admin API could not reach the worker's /ops surface (${pollError}). The dashboard below shows nothing rather than zeroes that would look like an idle, healthy worker.`
              : 'The admin API reported worker_reachable=false. Nothing here is simulated: when the worker returns, so does the data.'
          }
          action={<Button onClick={() => { setLoading(true); void refresh() }}>Retry</Button>}
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
          <span className="ops-head__title">Go Worker</span>
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
          <Metric icon={<Gauge size={14} />} label="Version" value={info?.version ?? '—'} hint={shortSha(info?.commit_sha)} title={info?.commit_sha} />
          <Metric icon={<Timer size={14} />} label="Uptime" value={fmtUptime(info?.uptime_seconds)} hint="since worker start" />
          <Metric icon={<Activity size={14} />} label="Utilization" value={`${(util * 100).toFixed(0)}%`} hint={`${info?.active ?? 0} / ${info?.num_workers ?? 0} workers`} title="active / configured pool size" />
          <Metric icon={<Cpu size={14} />} label="Goroutines" value={String(info?.goroutines ?? '—')} hint="process-level" />
          <Metric icon={<Boxes size={14} />} label="Memory" value={fmtBytes(info?.memory_bytes)} hint="runtime Sys" />
          <Metric icon={<Send size={14} />} label="Mode" value={info?.processing_mode ?? '—'} hint={info?.queue_backend ?? ''} />
        </div>

        <div className="ops-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', marginTop: 10 }}>
          <Chip ok={info?.db_connected ?? null} label="DB" title="delivery store reachable" />
          <Chip ok={info?.telegram_connected ?? null} label="Telegram" title="gateway probe ok" />
          <Chip ok={info?.ai_connected ?? null} label="9router / AI" title="last provider probe ok" />
          <Chip ok={info?.switches?.worker_enabled ? true : false} label="ATF_WORKER_ENABLED" />
          <Chip ok={info?.switches?.transforms ? true : false} label="ATF_WORKER_TRANSFORMS" />
          <Chip ok={info?.switches?.autoreply ? true : false} label="ATF_WORKER_AUTOREPLY" />
        </div>
      </div>

      <div className="ops-section">
        <div className="ops-head">
          <Layers size={18} />
          <span className="ops-head__title">Queue</span>
          <span className="ops-head__meta">
            <span className="ops-levelchip" style={{ background: LOAD_TONE[loadStatus], color: 'var(--color-button-fg)' }}>
              {loadStatus.toUpperCase()}
            </span>
          </span>
        </div>
        {queue ? (
          <>
            <div className="ops-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))' }}>
              <Metric icon={<Send size={14} />} label="Delivered" value={String(queue.delivered)} />
              <Metric icon={<Activity size={14} />} label="Processing" value={String(queue.processing)} />
              <Metric icon={<Timer size={14} />} label="Pending" value={String(queue.pending)} hint={queue.oldest_pending_age_seconds > 0 ? `oldest ${fmtUptime(queue.oldest_pending_age_seconds)} old` : 'empty'} />
              <Metric icon={<AlertTriangle size={14} />} label="Retry pending" value={String(queue.retry_pending)} />
              <Metric icon={<XCircle size={14} />} label="Failed" value={String(queue.failed)} />
              <Metric icon={<XCircle size={14} />} label="Dead letter" value={String(queue.dead_letter)} />
              <Metric icon={<Gauge size={14} />} label="Jobs/s" value={(queue.jobs_per_second ?? 0).toFixed(1)} />
              <Metric icon={<Database size={14} />} label="Backpressure" value={String(queue.capacity)} hint="queue capacity ceiling" />
            </div>
            <div style={{ marginTop: 10 }}>
              <div style={{ display: 'flex', gap: 18, alignItems: 'center', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>end-to-end latency</span>
                <LatencyBars l={queue.latency} />
              </div>
              <div style={{ display: 'flex', gap: 18, alignItems: 'center', flexWrap: 'wrap', marginTop: 6 }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>queue wait</span>
                <LatencyBars l={queue.queue_wait} />
              </div>
            </div>
          </>
        ) : (
          <EmptyState icon={<Layers size={22} />} title="Queue snapshot unavailable" />
        )}
      </div>

      <div className="ops-section">
        <div className="ops-head">
          <Workflow size={18} />
          <span className="ops-head__title">Pipeline</span>
          <span className="ops-head__meta">11 stages · live execution state</span>
        </div>
        <PipelineChain stages={(info as any)?.pipeline ?? []} />
        <p style={{ fontSize: '0.72rem', color: 'var(--color-text-muted)', margin: '8px 0 0' }}>
          The AI stage is only healthy after a real successful rewrite — enabling AI on a task never paints it green.
        </p>
      </div>

      <div className="ops-section">
        <div className="ops-head">
          <Activity size={18} />
          <span className="ops-head__title">Tasks</span>
          <span className="ops-head__meta">{tasks?.length ?? 0} configured</span>
        </div>
        {tasks && tasks.length > 0 ? (
          <div className="ops-console" style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
              <thead>
                <tr style={{ textAlign: 'left', color: 'var(--color-text-muted)' }}>
                  <th style={{ padding: '4px 8px' }}>Task</th>
                  <th style={{ padding: '4px 8px' }}>State</th>
                  <th style={{ padding: '4px 8px' }}>Delivered / Failed</th>
                  <th style={{ padding: '4px 8px' }}>Pending</th>
                  <th style={{ padding: '4px 8px' }}>Throughput</th>
                  <th style={{ padding: '4px 8px' }}>Features</th>
                  <th style={{ padding: '4px 8px' }}>Last error</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => {
                  const open = expandedTask === t.rule_id
                  return (
                    <>
                      <tr
                        key={t.rule_id}
                        style={{ cursor: 'pointer', borderTop: '1px solid var(--color-border)' }}
                        onClick={() => setExpandedTask(open ? null : t.rule_id)}
                        title="Click for the feature breakdown"
                      >
                        <td style={{ padding: '4px 8px' }}>
                          {open ? <ChevronDown size={12} /> : <ChevronRight />}
                          <code style={{ marginLeft: 4 }}>{t.rule_id.slice(0, 12)}</code>
                        </td>
                        <td style={{ padding: '4px 8px' }}>
                          <span style={{ color: t.enabled ? 'var(--color-success)' : 'var(--color-text-muted)' }}>
                            {t.enabled ? 'enabled' : 'disabled'}
                          </span>
                        </td>
                        <td style={{ padding: '4px 8px', fontVariantNumeric: 'tabular-nums' }}>
                          {t.delivered} / <span style={{ color: t.failed ? 'var(--color-danger)' : 'inherit' }}>{t.failed}</span>
                        </td>
                        <td style={{ padding: '4px 8px', fontVariantNumeric: 'tabular-nums' }}>{t.pending}</td>
                        <td style={{ padding: '4px 8px', fontVariantNumeric: 'tabular-nums' }}>{t.throughput_per_min.toFixed(1)}/min · {fmtMs(t.latency_ms_avg)}</td>
                        <td style={{ padding: '4px 8px' }}>
                          {[t.features.filter && 'F', t.features.replacement && 'R', t.features.ai && 'AI', t.features.cleaner && 'C', t.features.fen > 1 && `N${t.features.fen}`]
                            .filter(Boolean)
                            .join(' ') || '—'}
                        </td>
                        <td style={{ padding: '4px 8px', maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={t.last_error ?? undefined}>
                          {t.last_error ?? <span style={{ color: 'var(--color-success)' }}>ok</span>}
                        </td>
                      </tr>
                      {open ? (
                        <tr key={`${t.rule_id}-detail`}>
                          <td colSpan={7} style={{ padding: '8px 8px 12px 28px', color: 'var(--color-text-muted)' }}>
                            <div>source <code>{t.source}</code> → target <code>{t.target}</code></div>
                            <div>retries {t.retries} · worker {t.worker_id || '—'} · last success {t.last_success_at ?? '—'}</div>
                            <div>
                              filter {String(t.features.filter)} · replacement {String(t.features.replacement)} · ai {String(t.features.ai)} · cleaner {String(t.features.cleaner)} · header {String(t.features.header)} · footer {String(t.features.footer)} · media {String(t.features.media)} · fallback {String(t.features.fallback)}
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
          <EmptyState icon={<Activity size={22} />} title="No task rows" description="The worker derives these from durable delivery_jobs; none match yet." />
        )}
      </div>

      <div className="ops-section">
        <div className="ops-head">
          <AlertTriangle size={18} />
          <span className="ops-head__title">Errors &amp; warnings</span>
          <span className="ops-head__meta">
            {errorTotal} total · {visibleErrors.length} shown · deduplicated
          </span>
        </div>
        <div className="ops-filter" style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
          <select value={severity} onChange={(e) => setSeverity(e.target.value as Severity)} aria-label="Filter by severity">
            <option value="all">All severities</option>
            <option value="ERROR">ERROR</option>
            <option value="WARN">WARN</option>
          </select>
          <select value={component} onChange={(e) => setComponent(e.target.value)} aria-label="Filter by component">
            <option value="all">All components</option>
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
                  {e.retryable ? `retry ${e.retry_count}` : 'permanent'}
                </span>
                <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={e.message}>{e.message}</span>
                {e.count && e.count > 1 ? (
                  <span className="ops-levelchip" title={`repeated ${e.count} times since ${e.first_seen}`}>×{e.count}</span>
                ) : null}
              </div>
            ))}
          </div>
        ) : (
          <EmptyState icon={<CheckCircle2 size={22} />} title="No errors match this filter" />
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
