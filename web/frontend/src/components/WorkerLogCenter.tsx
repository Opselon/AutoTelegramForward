/**
 * WorkerLogCenter — the Go Worker's operational log console.
 *
 * The backend already exists: `/api/logs` is a server-side-paginated,
 * bounded query over the central logger (atf-logger, logger:50052) that the
 * Go Worker ships every structured slog record to. This component is the
 * missing viewer half of section 9: live tail, severity/component/correlation
 * filters, expandable structured details, copy-sanitized-diagnostics and
 * CSV export — every row a real backend response, never a sample.
 *
 * Refresh cadence adheres to the shell's contract: polling, no SSE, and the
 * log source is the authority (a failed fetch paints an error state, never
 * fabricated rows).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { RefreshCw, Search, ChevronDown, ChevronRight, Copy, Download, Loader2, AlertTriangle } from 'lucide-react'
import { api } from '../api'
import type { LogItem } from '../types'
import { makeT, type UiLang } from '../locales/engine'

const POLL_MS = 10_000
const PAGE_SIZE = 50

type Severity = 'ERROR' | 'WARN' | 'INFO' | 'DEBUG'

const SEVERITIES: Severity[] = ['ERROR', 'WARN', 'INFO', 'DEBUG']

function sevTone(sev?: string): string {
  switch (sev) {
    case 'ERROR': return 'var(--color-danger)'
    case 'WARN': return 'var(--color-warning, #c9a227)'
    case 'DEBUG': return 'var(--color-text-muted)'
    default: return 'var(--color-info)'
  }
}

function fmtTs(ts?: number | string): string {
  if (!ts) return '—'
  const ms = typeof ts === 'string' ? Date.parse(ts) : ts * 1000
  if (!Number.isFinite(ms)) return '—'
  return new Date(ms).toISOString().replace('T', ' ').replace('.000Z', 'Z')
}

function copySanitized(entry: LogItem): void {
  const payload = {
    ts: entry.ts,
    severity: entry.severity ?? entry.level,
    service: entry.service,
    category: entry.category,
    message: entry.message,
    detail: entry.detail,
    rule_id: entry.rule_id,
    correlation_id: entry.correlation_id,
  }
  navigator.clipboard?.writeText(JSON.stringify(payload, null, 2)).catch(() => {})
}

function toCsv(rows: LogItem[]): string {
  const head = ['ts', 'severity', 'service', 'category', 'message', 'rule_id', 'correlation_id']
  const esc = (v: unknown) => {
    const s = String(v ?? '')
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s
  }
  return [head.join(',')]
    .concat(rows.map((r) => [fmtTs(r.ts), r.severity ?? r.level ?? '', r.service ?? '', r.category ?? '', r.message ?? '', r.rule_id ?? '', r.correlation_id ?? ''].map(esc).join(',')))
    .join('\n')
}

export default function WorkerLogCenter({ lang }: { lang: UiLang }) {
  const t = makeT(lang)

  const [rows, setRows] = useState<LogItem[] | null>(null)
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [selected, setSelected] = useState<Severity[]>(['ERROR', 'WARN'])
  const [service, setService] = useState('all')
  const [search, setSearch] = useState('')
  const [searchDraft, setSearchDraft] = useState('')
  const [correlationId, setCorrelationId] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdate, setLastUpdate] = useState<number | null>(null)
  const [live, setLive] = useState(true)
  const gen = useRef(0)

  const fetchPage = useCallback(
    async (opts: { offset: number; append?: boolean }) => {
      const g = ++gen.current
      setLoading(true)
      setError(null)
      try {
        const params: Parameters<typeof api.getLogsPage>[0] = {
          levels: selected,
          limit: PAGE_SIZE,
          offset: opts.offset,
          ascending: false,
        }
        if (service !== 'all') params.service = service
        if (search) params.search = search
        if (correlationId) params.correlation_id = correlationId
        const page = await api.getLogsPage(params)
        if (g !== gen.current) return // a newer fetch already painted
        setRows((prev) => (opts.append && prev ? prev.concat(page.logs) : page.logs))
        setTotal(page.total)
        setOffset(page.offset)
        setHasMore(page.has_more)
        setLastUpdate(Date.now())
      } catch (e: unknown) {
        if (g !== gen.current) return
        setError(e instanceof Error ? e.message : t('ui_log_fetch_failed'))
      } finally {
        if (g === gen.current) setLoading(false)
      }
    },
    [selected, service, search, correlationId],
  )

  // Live tail of the newest page (live mode) — one poll every POLL_MS.
  useEffect(() => {
    if (!live) return
    fetchPage({ offset: 0 })
    const timer = setInterval(() => fetchPage({ offset: 0 }), POLL_MS)
    return () => clearInterval(timer)
  }, [live, fetchPage])

  // Debounce the search box (300ms, like the shell's log search).
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearch(searchDraft)
      setOffset(0)
    }, 300)
    return () => clearTimeout(timer)
  }, [searchDraft])

  // A filter change resets the page to the newest window.
  useEffect(() => {
    setOffset(0)
    fetchPage({ offset: 0 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, service, correlationId])

  const toggleSeverity = (sev: Severity) => {
    setSelected((prev) => (prev.includes(sev) ? prev.filter((s) => s !== sev) : [...prev, sev]))
  }

  const exportCsv = () => {
    if (!rows || rows.length === 0) return
    const blob = new Blob([toCsv(rows)], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `worker-logs-${new Date().toISOString().replace(/[:.]/g, '-')}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const sevLabel = (s: Severity) =>
    s === 'ERROR' ? t('ui_log_severity_error') : s === 'WARN' ? t('ui_log_severity_warn') : s === 'INFO' ? t('ui_log_severity_info') : t('ui_log_severity_debug')

  return (
    <div className="ops-console" role="region" aria-label={t('ui_worker_log_center')}>
      <div className="ops-head">
        <h3 className="ops-head__title">{t('ui_worker_log_center')}</h3>
        <div className="ops-head__meta">
          {lastUpdate ? (
            <span className="ops-chip ops-chip--info" title={new Date(lastUpdate).toISOString()}>
              {t('ui_last_update')} {new Date(lastUpdate).toLocaleTimeString()}
            </span>
          ) : null}
          <button
            type="button"
            className="ops-filter"
            onClick={() => setLive((v) => !v)}
            aria-pressed={live}
            aria-label={t('ui_live_tail')}
          >
            <RefreshCw size={13} className={live && !loading ? 'spin-slow' : ''} aria-hidden="true" />
            {live ? t('ui_live_tail') : t('ui_paused')}
          </button>
          <button
            type="button"
            className="ops-filter"
            onClick={() => fetchPage({ offset: 0 })}
            disabled={loading}
            aria-label={t('ui_refresh')}
          >
            {loading ? <Loader2 size={13} className="spin" aria-hidden="true" /> : <RefreshCw size={13} aria-hidden="true" />}
            {t('ui_refresh')}
          </button>
          <button
            type="button"
            className="ops-filter"
            onClick={exportCsv}
            disabled={!rows || rows.length === 0}
            aria-label={t('ui_export_csv')}
          >
            <Download size={13} aria-hidden="true" />
            {t('ui_export_csv')}
          </button>
        </div>
      </div>

      {/* Filters: severity multi-select (ERROR/WARN default, INFO/DEBUG opt-in),
          component, free-text search, correlation-id lookup. */}
      <div className="ops-filters" style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center', padding: '8px 2px' }}>
        {SEVERITIES.map((sev) => (
          <button
            key={sev}
            type="button"
            className={`ops-chip ${selected.includes(sev) ? 'ops-chip--on' : 'ops-chip--off'}`}
            style={selected.includes(sev) ? { borderColor: sevTone(sev), color: sevTone(sev) } : {}}
            aria-pressed={selected.includes(sev)}
            onClick={() => toggleSeverity(sev)}
          >
            <span style={{ fontWeight: 700 }}>{sev}</span> {sevLabel(sev)}
          </button>
        ))}
        <select
          className="ops-filter"
          value={service}
          onChange={(e) => setService(e.target.value)}
          aria-label={t('ui_component')}
        >
          <option value="all">{t('ui_all_components')}</option>
          <option value="worker.main">worker.main</option>
          <option value="worker.delivery">worker.delivery</option>
          <option value="worker.scheduler">worker.scheduler</option>
          <option value="worker.autoreply">worker.autoreply</option>
          <option value="worker.ai">worker.ai</option>
          <option value="worker.queue">worker.queue</option>
        </select>
        <div className="ops-filter" style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}>
          <Search size={13} aria-hidden="true" style={{ opacity: 0.6, marginRight: 4 }} />
          <input
            type="search"
            placeholder={t('ui_search_logs')}
            value={searchDraft}
            onChange={(e) => setSearchDraft(e.target.value)}
            aria-label={t('ui_search_logs')}
            style={{ background: 'transparent', border: 'none', outline: 'none', minWidth: 140 }}
          />
        </div>
        <input
          type="text"
          className="ops-filter"
          placeholder={t('ui_correlation_id')}
          value={correlationId}
          onChange={(e) => setCorrelationId(e.target.value)}
          aria-label={t('ui_correlation_id')}
          style={{ minWidth: 130 }}
        />
      </div>

      {error ? (
        <div className="ops-empty" style={{ color: 'var(--color-danger)', display: 'flex', gap: 8, alignItems: 'center', padding: '12px 2px' }}>
          <AlertTriangle size={15} aria-hidden="true" />
          <span>{t('ui_log_fetch_failed')}: {error}</span>
        </div>
      ) : null}

      {rows === null && !error ? (
        <div className="ops-empty" style={{ padding: '16px 2px' }}>
          <Loader2 size={16} className="spin" aria-hidden="true" /> {t('ui_loading')}
        </div>
      ) : rows && rows.length === 0 && !error ? (
        <div className="ops-empty" style={{ padding: '16px 2px', color: 'var(--color-text-muted)' }}>
          {t('ui_log_empty')}
        </div>
      ) : rows && rows.length > 0 ? (
        <>
          <div className="ops-loglist" role="log" aria-live={live ? 'polite' : 'off'}>
            {rows.map((entry, i) => {
              const key = `${entry.id ?? entry.ts}-${i}`
              const isOpen = expanded === key
              return (
                <div key={key} className="ops-logrow" style={{ borderBottom: '1px solid var(--color-border)' }}>
                  <div
                    className="ops-logrow__head"
                    style={{ display: 'flex', gap: 8, alignItems: 'baseline', padding: '4px 2px', cursor: 'pointer' }}
                    onClick={() => setExpanded(isOpen ? null : key)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        setExpanded(isOpen ? null : key)
                      }
                    }}
                    aria-expanded={isOpen}
                  >
                    <span aria-hidden="true" style={{ color: 'var(--color-text-muted)', display: 'inline-flex' }}>
                      {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                    </span>
                    <span style={{ color: 'var(--color-text-muted)', fontVariantNumeric: 'tabular-nums', fontSize: '0.75rem' }}>
                      {fmtTs(entry.ts)}
                    </span>
                    <span style={{ fontWeight: 700, color: sevTone(entry.severity ?? entry.level), minWidth: 48 }}>
                      {(entry.severity ?? entry.level ?? '—').toUpperCase()}
                    </span>
                    <span style={{ color: 'var(--color-text-muted)', minWidth: 110 }}>{entry.service ?? '—'}</span>
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {entry.message ?? entry.category}
                    </span>
                    {entry.correlation_id ? (
                      <span className="ops-chip ops-chip--info" style={{ fontSize: '0.75rem' }} title={entry.correlation_id}>
                        {String(entry.correlation_id).slice(0, 12)}
                      </span>
                    ) : null}
                    <button
                      type="button"
                      className="ops-filter"
                      onClick={(e) => {
                        e.stopPropagation()
                        copySanitized(entry)
                      }}
                      aria-label={t('ui_copy_diagnostics')}
                      title={t('ui_copy_diagnostics')}
                    >
                      <Copy size={12} aria-hidden="true" />
                    </button>
                  </div>
                  {isOpen ? (
                    <div className="ops-logrow__detail" style={{ padding: '8px 2px 12px 22px', color: 'var(--color-text-muted)', fontSize: '0.75rem' }}>
                      <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                        {JSON.stringify(
                          {
                            ts: fmtTs(entry.ts),
                            severity: entry.severity ?? entry.level,
                            service: entry.service,
                            category: entry.category,
                            message: entry.message,
                            detail: entry.detail,
                            rule_id: entry.rule_id,
                            correlation_id: entry.correlation_id,
                          },
                          null,
                          2,
                        )}
                      </pre>
                    </div>
                  ) : null}
                </div>
              )
            })}
          </div>
          <div className="ops-logpager" style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '10px 2px' }}>
            <span style={{ color: 'var(--color-text-muted)', fontSize: '0.75rem' }}>
              {t('ui_log_count', { shown: rows.length, total })}
            </span>
            {hasMore ? (
              <button
                type="button"
                className="ops-filter"
                onClick={() => fetchPage({ offset: offset + rows.length, append: true })}
                disabled={loading}
              >
                {t('ui_load_more')}
              </button>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  )
}
