import React, { useEffect, useState } from 'react'
/**
 * DevOps console: the pipeline flight deck. Live logs, the delivery signal
 * chain, recent errors and the maintenance actions. The largest tab by far,
 * which is why the shell never pays for it.
 *
 * Structure: four sibling sections — Topology (signal chain), the Log Console
 * (the hero instrument: filters, history, purge, terminal), Backup & Restore,
 * Recent Pipeline Errors. The last two used to be nested inside the log
 * explorer's panel card; they present themselves as sections and now are ones.
 * Styling lives in the `ops-*` classes (index.css) — role tokens only, mono
 * via --font-mono, motion guarded by prefers-reduced-motion.
 */

import {
  Activity,
  ArrowDown,
  ArrowUp,
  Cable,
  Check,
  ChevronDown,
  Copy,
  Database,
  Download,
  Eraser,
  HeartPulse,
  History,
  Lock,
  RefreshCw,
  Search,
  Terminal,
  Upload,
  X,
} from 'lucide-react'
import type { DashboardCtx } from '../App'
import { LogPolicyPanel } from '../components/LogPolicyPanel'

export default function DevopsPanel({ d }: { d: DashboardCtx }) {
  const {
    lang,
    rules,
    recentErrors,
    logs,
    logFilterService,
    setLogFilterService,
    logFilterLevel,
    setLogFilterLevel,
    logSearch,
    setLogSearch,
    logScope,
    setLogScope,
    currentUser,
    logAutoRefresh,
    setLogAutoRefresh,
    logStats,
    logFilterRule,
    setLogFilterRule,
    logOrder,
    setLogOrder,
    logPageSize,
    setLogPageSize,
    logOffset,
    setLogOffset,
    logTotal,
    logHasMore,
    logExporting,
    purging,
    purgeWindowH,
    setPurgeWindowH,
    purgeResult,
    backupBusy,
    backupPassphrase,
    setBackupPassphrase,
    backupCounts,
    backupEncrypted,
    importText,
    setImportText,
    importPassphrase,
    setImportPassphrase,
    importOverwrite,
    setImportOverwrite,
    importStats,
    setImportStats,
    copiedLogId,
    setCopiedLogId,
    servicesHealth,
    t,
    fetchLogs,
    handleExportLogs,
    handlePurgeLogs,
    handleBackupExport,
    handleBackupImport,
    onBackupFileChosen,
  } = d

  // The search field used to push every keystroke straight into the shared
  // `logSearch`, which re-armed the 3s polling effect each time. The input
  // keeps its own draft and commits it debounced (300ms), so both the initial
  // fetch and the poller read a settled value.
  const [searchDraft, setSearchDraft] = useState(logSearch)
  useEffect(() => {
    // An external write (e.g. the correlation-id chip) must land in the field.
    setSearchDraft(logSearch)
  }, [logSearch])
  useEffect(() => {
    if (searchDraft === logSearch) return
    const timer = setTimeout(() => setLogSearch(searchDraft), 300)
    return () => clearTimeout(timer)
  }, [searchDraft, logSearch, setLogSearch])

  // Tenant scope of the console: visible to admins only (the API pins everyone
  // else to their own events regardless of what this control would say).
  const canPickScope = !!currentUser?.is_admin

  return (
    <>
      {/* ---------------------------------------------------------- Topology -- */}
      <section className="glass-panel ops-section" style={{ '--ops-i': 0 } as React.CSSProperties}>
        <div className="ops-head">
          <h3 className="ops-head__title">
            <HeartPulse
              size={17}
              color={servicesHealth?.status === 'healthy' ? 'var(--color-success)' : 'var(--color-danger)'}
            />
            <span>{t('ui_services_topology_live_health')}</span>
          </h3>
          <span
            className={`badge ${servicesHealth?.status === 'healthy' ? 'badge--success' : servicesHealth ? 'badge--danger' : 'badge--neutral'}`}
          >
            {servicesHealth
              ? servicesHealth.status === 'healthy'
                ? t('ui_all_services_healthy')
                : t('ui_degraded')
              : t('ui_probing')}
          </span>
        </div>

        {/* Node chain: Web → API → Core → Logger, connection arrows labelled */}
        <div className="ops-chain">
          {(() => {
            const hmap: Record<string, { online: boolean; detail: string; latency_ms: number } | undefined> = {}
            for (const s of servicesHealth?.services || []) hmap[s.name] = s
            // The port labels are the ones the stack actually binds:
            // core gRPC 50051, logger gRPC 50052. They read 6001/6002,
            // the pre-compose ports, so an operator checking a listener
            // looked for a port nothing has ever listened on.
            const nodes = [
              { key: 'web', title: t('ui_web_gateway'), sub: 'atf-web · :8088', color: 'var(--color-warning)' },
              { key: 'api', title: t('ui_rest_control_plane'), sub: 'atf-api · :8080', color: 'var(--color-accent-violet)' },
              { key: 'core', title: t('ui_python_core_mtproto'), sub: 'atf-core · gRPC :50051', color: 'var(--color-info)' },
              { key: 'logger', title: t('ui_central_logger'), sub: 'atf-logger · gRPC :50052', color: 'var(--color-success)' },
            ]
            const svc = (k: string) => hmap[k]
            return nodes.map((n, i) => {
              const s = svc(n.key)
              const online = s?.online ?? false
              return (
                <React.Fragment key={n.key}>
                  {i > 0 && (
                    <div className={`ops-link${online ? ' ops-link--on' : ''}`} dir="ltr">
                      <div className="ops-link__wire" />
                      <Cable size={12} color={online ? 'var(--color-success)' : 'var(--color-danger)'} />
                      <span className="ops-node__sub">
                        {i === 1
                          ? t('ui_rest_proxy_jwt')
                          : i === 2
                          ? t('ui_grpc_control')
                          : t('ui_grpc_logs')}
                      </span>
                    </div>
                  )}
                  <div className={`ops-node${online ? ' ops-node--on' : ''}`}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', minWidth: 0 }}>
                      <span className="ops-node__dot" aria-hidden="true" />
                      <div style={{ minWidth: 0 }}>
                        <div className="ops-node__title" style={{ color: n.color }}>{n.title}</div>
                        <div className="ops-node__sub" dir="ltr">{n.sub}</div>
                      </div>
                    </div>
                    <div style={{ textAlign: 'right', minWidth: 0, maxWidth: '100%' }} dir="ltr">
                      <div
                        className="ops-node__verdict"
                        style={{ color: online ? 'var(--color-success)' : 'var(--color-danger)' }}
                      >
                        {online ? t('ui_online') : t('ui_down')}
                        {s && s.latency_ms > 0 ? ` · ${s.latency_ms}ms` : ''}
                      </div>
                      {s?.detail && (
                        <div
                          className="ops-node__sub"
                          style={{
                            maxWidth: 320,
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                          }}
                        >
                          {s.detail}
                        </div>
                      )}
                    </div>
                  </div>
                </React.Fragment>
              )
            })
          })()}
        </div>

        {/* Degraded-service hint — name the failing service and
            give the remedy this deployment actually uses. The
            hint used to print `systemctl --user restart atf`,
            which is the bare-metal unit; this stack runs under
            docker compose, where that command restarts nothing
            and the operator is left believing they acted. */}
        {servicesHealth && servicesHealth.status !== 'healthy' && (
          <div
            className="p-note p-note--danger"
            style={{ marginBlockStart: 'var(--space-3)', fontSize: 'var(--type-xs)' }}
          >
            {t('ui_down_services')}
            {servicesHealth.services.filter((x) => !x.online).map((x) => x.name).join(', ')}
            <span style={{ color: 'var(--color-text-muted)' }}>
              {' — '}docker compose restart {servicesHealth.services.filter((x) => !x.online).map((x) => x.name).join(' ')}
            </span>
          </div>
        )}
      </section>

      {/* ------------------------------------------------- Collection policy -- */}
      {/* Collection policy: what each process is actually recording, and the
          admin-only control that changes it through the core itself. */}
      <LogPolicyPanel lang={lang} />

      {/* ----------------------------------------------------- Log console -- */}
      {/* The hero instrument: filter bar, quick metrics, history paging,
          purge, and the terminal. The explorer used to be a card that
          swallowed the backup and errors sections; it is one section now. */}
      <section className="glass-panel ops-section" style={{ '--ops-i': 1 } as React.CSSProperties}>
        <div className="ops-head">
          <h3 className="ops-head__title">
            <Terminal size={17} color="var(--color-accent-violet)" />
            <span>{t('ui_live_logs')}</span>
          </h3>
          <div className="ops-head__meta">
            <button
              type="button"
              onClick={() => setLogAutoRefresh(!logAutoRefresh)}
              className={`ops-live${logAutoRefresh ? ' ops-live--on' : ''}`}
              aria-pressed={logAutoRefresh}
            >
              <span className="ops-live__dot" aria-hidden="true" />
              <span>{logAutoRefresh ? t('ui_live_stream_active_3s') : t('ui_live_stream_paused')}</span>
            </button>
            <button
              type="button"
              onClick={() => fetchLogs()}
              className="btn btn--ghost btn--sm"
              title={t('ui_refresh_now')}
            >
              <RefreshCw size={14} />
              <span>{t('ui_refresh')}</span>
            </button>
          </div>
        </div>

        {/* Filter & Search — the Field pattern: label above the control, so a
            narrow card can never clip a label off its edge. Controls fill
            their grid cell; the search spans the full row beneath them. */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
            gap: 'var(--space-2)',
            marginBlockEnd: 'var(--space-3)',
            paddingBlockEnd: 'var(--space-3)',
            borderBottom: '1px solid var(--color-border)',
          }}
        >
          {/* Tenant scope: admins only. Switching refetches — the
              poller effect re-arms on `logScope` too. */}
          {canPickScope && (
            <div className="ops-filter">
              <div className="scope-toggle" role="group" aria-label={t('ui_log_scope_all_tenants')}>
                <button
                  type="button"
                  className={`scope-toggle__btn${logScope === 'mine' ? ' is-active' : ''}`}
                  aria-pressed={logScope === 'mine'}
                  onClick={() => setLogScope('mine')}
                >
                  {t('ui_log_scope_my_events')}
                </button>
                <button
                  type="button"
                  className={`scope-toggle__btn${logScope === 'all' ? ' is-active' : ''}`}
                  aria-pressed={logScope === 'all'}
                  onClick={() => setLogScope('all')}
                >
                  {t('ui_log_scope_all_tenants')}
                </button>
              </div>
            </div>
          )}

          <div className="ops-filter">
            <label className="ops-toolbar__label" htmlFor="ops-log-service">
              {t('ui_service')}
            </label>
            <span className="ops-selectwrap">
              <select
                id="ops-log-service"
                value={logFilterService}
                onChange={(e) => {
                  setLogFilterService(e.target.value)
                  fetchLogs(e.target.value, logFilterLevel, logSearch)
                }}
                className="ops-select"
              >
                <option value="all">{t('ui_all_services')}</option>
                <option value="core">{t('ui_service_core')}</option>
                <option value="bot">{t('ui_service_bot')}</option>
                <option value="api">{t('ui_service_api')}</option>
                <option value="pipeline">{t('ui_service_pipeline')}</option>
                <option value="boot">{t('ui_service_boot')}</option>
              </select>
              <ChevronDown size={14} aria-hidden="true" />
            </span>
          </div>

          <div className="ops-filter">
            <label className="ops-toolbar__label" htmlFor="ops-log-level">
              {t('ui_level')}
            </label>
            <span className="ops-selectwrap">
              <select
                id="ops-log-level"
                value={logFilterLevel}
                onChange={(e) => {
                  setLogFilterLevel(e.target.value)
                  fetchLogs(logFilterService, e.target.value, logSearch)
                }}
                className="ops-select"
              >
                <option value="all">{t('ui_all_levels')}</option>
                <option value="ERROR">❌ {t('ui_level_error')}</option>
                <option value="WARN">⚠️ {t('ui_level_warn')}</option>
                <option value="INFO">ℹ️ {t('ui_level_info')}</option>
                <option value="DEBUG">🔍 {t('ui_level_debug')}</option>
              </select>
              <ChevronDown size={14} aria-hidden="true" />
            </span>
          </div>

          {/* v18: scope the console to one task's history. This is the
              same attribution the per-task Logs tab renders, surfaced
              where an operator already has the global view open. */}
          <div className="ops-filter">
            <label className="ops-toolbar__label" htmlFor="ops-log-task">
              {t('ui_task')}
            </label>
            <span className="ops-selectwrap">
              <select
                id="ops-log-task"
                value={logFilterRule}
                onChange={(e) => {
                  setLogFilterRule(e.target.value)
                  setLogOffset(0)
                  fetchLogs(logFilterService, logFilterLevel, logSearch, { rule: e.target.value, offset: 0 })
                }}
                className="ops-select"
              >
                <option value="all">{t('ui_all_tasks')}</option>
                {(Array.isArray(rules) ? rules : []).map((r: any) => (
                  <option key={r.id} value={r.id}>
                    {(r.name || r.id.slice(0, 8)).slice(0, 28)}
                  </option>
                ))}
              </select>
              <ChevronDown size={14} aria-hidden="true" />
            </span>
          </div>

          {/* v18: read direction — newest-first for triage, oldest-first
              for replaying how a task behaved. */}
          <div className="ops-filter">
            <label className="ops-toolbar__label" htmlFor="ops-log-order">
              {t('ui_order')}
            </label>
            <span className="ops-selectwrap">
              <select
                id="ops-log-order"
                value={logOrder}
                onChange={(e) => {
                  const order = e.target.value as 'desc' | 'asc'
                  setLogOrder(order)
                  setLogOffset(0)
                  fetchLogs(logFilterService, logFilterLevel, logSearch, { order, offset: 0 })
                }}
                className="ops-select"
              >
                <option value="desc">{t('ui_newest_first')}</option>
                <option value="asc">{t('ui_oldest_first')}</option>
              </select>
              <ChevronDown size={14} aria-hidden="true" />
            </span>
          </div>

          {/* Text Search Input — debounced; typing never re-arms the
              poller per keystroke and never fires a fetch per key. */}
          <div className="ops-filter" style={{ gridColumn: '1 / -1' }}>
            <label className="ops-toolbar__label" htmlFor="ops-log-search">
              {t('ui_search_logs_live')}
            </label>
            <span className="ops-selectwrap">
              <input
                id="ops-log-search"
                type="text"
                placeholder={t('ui_search_logs_live')}
                value={searchDraft}
                onChange={(e) => setSearchDraft(e.target.value)}
                className="input-field"
                style={{ paddingBlock: 'var(--space-2)', paddingInline: 'var(--space-3)', paddingInlineEnd: 'var(--space-6)', fontSize: 'var(--type-sm)', minBlockSize: 40 }}
              />
              {searchDraft ? (
                <button
                  type="button"
                  onClick={() => setSearchDraft('')}
                  className="btn btn--ghost btn--icon btn--sm"
                  style={{ position: 'absolute', insetInlineEnd: 4, minInlineSize: 32, minBlockSize: 32 }}
                  aria-label={t('ui_cancel')}
                  title={t('ui_cancel')}
                >
                  <X size={14} />
                </button>
              ) : (
                <Search size={14} aria-hidden="true" />
              )}
            </span>
          </div>
        </div>

        {/* Quick Metrics Badges */}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)', marginBlockEnd: 'var(--space-3)', fontSize: 'var(--type-xs)' }}>
          <span className="badge badge--neutral">
            {logTotal === 0
              ? t('ui_no_events')
              : `${logTotal === 0 ? 0 : logOffset + 1}–${logOffset + (Array.isArray(logs) ? logs.length : 0)} / ${logTotal.toLocaleString()}`}
          </span>
          {logStats?.oldest_ts ? (
            <span className="badge badge--neutral" title={t('ui_oldest_record_on_disk')}>
              {t('ui_history_from')}{' '}
              {new Date(logStats.oldest_ts * 1000).toLocaleDateString()}
            </span>
          ) : null}
          {logStats && (
            <>
              <span className="badge badge--success">{t('ui_level_info')}: {logStats.by_level?.INFO ?? 0}</span>
              <span className="badge badge--warning">{t('ui_level_warn')}: {logStats.by_level?.WARN ?? 0}</span>
              <span className="badge badge--danger">{t('ui_level_error')}: {logStats.by_level?.ERROR ?? 0}</span>
              <span className="badge badge--warning">
                {t('ui_total_archive')} {logStats.total}
              </span>
            </>
          )}
        </div>

        {/* v18 history navigation: offset paging over the full archive plus
            a downloadable copy of exactly what is on screen. */}
        <div className="ops-toolbar">
          <span className="ops-toolbar__label">
            <History size={12} aria-hidden="true" /> {t('ui_history')}
          </span>
          <button
            type="button"
            onClick={() => {
              const next = Math.max(0, logOffset - logPageSize)
              setLogOffset(next)
              fetchLogs(logFilterService, logFilterLevel, logSearch, { offset: next })
            }}
            disabled={logOffset === 0}
            className="btn btn--ghost btn--sm"
          >
            <ArrowUp size={12} aria-hidden="true" /> {t('ui_newer')}
          </button>
          <button
            type="button"
            onClick={() => {
              const next = logOffset + logPageSize
              setLogOffset(next)
              fetchLogs(logFilterService, logFilterLevel, logSearch, { offset: next })
            }}
            disabled={!logHasMore}
            className="btn btn--ghost btn--sm"
          >
            <ArrowDown size={12} aria-hidden="true" /> {t('ui_older')}
          </button>
          <label className="ops-toolbar__label">
            {t('ui_page_size')}
            <select
              value={logPageSize}
              onChange={(e) => {
                const size = Number(e.target.value)
                setLogPageSize(size)
                setLogOffset(0)
                fetchLogs(logFilterService, logFilterLevel, logSearch, { limit: size, offset: 0 })
              }}
              className="ops-select"
            >
              {[50, 100, 200, 500].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <span style={{ fontSize: 'var(--type-xs)', color: logHasMore ? 'var(--color-warning)' : 'var(--color-success)' }}>
            {logHasMore
              ? t('ui_older_events_exist')
              : t('ui_reached_the_end_of_history')}
          </span>
          <button
            type="button"
            onClick={() => handleExportLogs('json')}
            disabled={logExporting}
            className="btn btn--ghost btn--sm"
          >
            {logExporting ? (
              <RefreshCw size={12} className="spin" aria-hidden="true" />
            ) : (
              <>
                <Download size={12} aria-hidden="true" /> JSON
              </>
            )}
          </button>
          <button
            type="button"
            onClick={() => handleExportLogs('csv')}
            disabled={logExporting}
            className="btn btn--ghost btn--sm"
          >
            {logExporting ? (
              <RefreshCw size={12} className="spin" aria-hidden="true" />
            ) : (
              <>
                <Download size={12} aria-hidden="true" /> CSV
              </>
            )}
          </button>
        </div>

        {/* Log purge controls — keeps the archive from growing unbounded. */}
        <div className="ops-toolbar">
          <span className="ops-toolbar__label">
            <Eraser size={12} aria-hidden="true" /> {t('ui_purge_archive')}
          </span>
          <label className="ops-toolbar__label" style={{ color: 'var(--color-text)' }}>
            {t('ui_keep_last')}
            <input
              type="number" min={0} max={720} step={1} value={purgeWindowH}
              onChange={(e) => setPurgeWindowH(Math.max(0, Math.min(720, Number(e.target.value) || 0)))}
              style={{
                inlineSize: 64,
                background: 'var(--color-surface-inset)',
                color: 'var(--color-text)',
                border: '1px solid var(--color-border)',
                borderRadius: 'var(--radius-sm)',
                padding: '4px 6px',
                fontSize: 'var(--type-xs)',
              }}
            />
            {t('ui_h')}
          </label>
          {(logFilterLevel !== 'all' || logFilterService !== 'all') && (
            <span className="badge badge--neutral">
              {t('ui_scoped')} {[logFilterLevel !== 'all' && logFilterLevel, logFilterService !== 'all' && logFilterService].filter(Boolean).join(' · ')}
            </span>
          )}
          <button
            type="button"
            onClick={handlePurgeLogs}
            disabled={purging}
            className="btn btn--danger btn--sm"
          >
            {purging ? <RefreshCw size={12} className="spin" aria-hidden="true" /> : <Eraser size={12} aria-hidden="true" />}
            {t('ui_purge_old_logs')}
          </button>
          {purgeResult && (
            <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-success)', display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <Check size={12} aria-hidden="true" /> {t('ui_purged')}: {purgeResult.purged} · {t('ui_left')}: {purgeResult.remaining}
            </span>
          )}
        </div>

        {/* Terminal Log Console — the hero instrument. */}
        <div className="ops-console">
          {(!Array.isArray(logs) || logs.length === 0) ? (
            <div className="ops-console__empty">
              <div style={{ fontSize: 'var(--type-xl)', marginBlockEnd: 'var(--space-2)' }}>📋</div>
              <div>{t('ui_no_log_events_matching_current_filters')}</div>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {logs.map((l, idx) => {
                const lvl = (l.level || l.severity || 'INFO').toUpperCase()
                const isErr = lvl === 'ERROR'
                const isWarn = lvl === 'WARN'
                const isDbg = lvl === 'DEBUG'
                const logId = l.id ?? `${l.ts}-${idx}`

                const lvlColor = isErr ? 'var(--color-danger)' : isWarn ? 'var(--color-warning)' : isDbg ? 'var(--color-text-subtle)' : 'var(--color-info)'
                const lvlBg = isErr
                  ? 'var(--color-danger-soft)'
                  : isWarn
                  ? 'var(--color-warning-soft)'
                  : isDbg
                  ? 'var(--color-hover-wash)'
                  : 'var(--color-info-soft)'

                const timeStr = l.ts ? new Date(l.ts * 1000).toLocaleTimeString() : '--:--:--'
                const logLineText = `[${timeStr}] [${lvl}] [${l.service || 'core'}/${l.category || 'sys'}] ${l.message || l.detail || ''}`

                return (
                  <div
                    key={logId}
                    className="ops-row"
                    style={{
                      background: isErr ? 'var(--color-danger-soft)' : 'transparent',
                      borderInlineStartColor: lvlColor,
                    }}
                  >
                    <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--space-2)', flex: 1 }}>
                      {/* Time */}
                      <span className="ops-row__time">{timeStr}</span>

                      {/* Level Badge */}
                      <span className="ops-row__level" style={{ background: lvlBg, color: lvlColor }}>
                        {lvl}
                      </span>

                      {/* Service & Category */}
                      <span className="ops-row__svc">
                        {l.service || 'core'}
                        {l.category ? `:${l.category}` : ''}
                      </span>

                      {/* v18 attribution: which task (and which API call)
                          produced this line. Clicking narrows the whole
                          console to that subject instead of making the
                          operator retype an id into the search box. */}
                      {l.rule_id && (
                        <button
                          type="button"
                          onClick={() => {
                            setLogFilterRule(l.rule_id as string)
                            setLogOffset(0)
                            fetchLogs(logFilterService, logFilterLevel, logSearch, { rule: l.rule_id as string, offset: 0 })
                          }}
                          title={t('ui_filter_to_this_task')}
                          className="ops-chip"
                        >
                          {t('ui_app_task')}{(l.rule_id as string).slice(0, 8)}
                        </button>
                      )}
                      {l.correlation_id && (
                        <button
                          type="button"
                          onClick={() => {
                            setLogSearch(l.correlation_id as string)
                            setLogOffset(0)
                            fetchLogs(logFilterService, logFilterLevel, l.correlation_id as string, { offset: 0 })
                          }}
                          title={t('ui_trace_this_request_end_to_end')}
                          className="ops-chip ops-chip--info"
                        >
                          {t('ui_corr')}{(l.correlation_id as string).slice(0, 8)}
                        </button>
                      )}

                      {/* Message */}
                      <span style={{ color: isErr ? 'var(--color-danger-text)' : 'var(--color-text)', fontWeight: isErr ? 600 : 400 }}>
                        {l.message || l.detail}
                      </span>

                      {/* Additional Detail (if message and detail are both present) */}
                      {l.message && l.detail && l.detail !== l.message && (
                        <div
                          dir="ltr"
                          style={{
                            inlineSize: '100%',
                            marginBlockStart: 'var(--space-1)',
                            padding: '4px 8px',
                            borderRadius: 4,
                            background: 'var(--color-scrim)',
                            color: 'var(--color-text-muted)',
                            fontSize: 'var(--type-xs)',
                            whiteSpace: 'pre-wrap',
                            textAlign: 'left',
                            fontFamily: 'inherit',
                          }}
                        >
                          {l.detail}
                        </div>
                      )}
                    </div>

                    {/* Copy Line Button */}
                    <button
                      type="button"
                      onClick={() => {
                        navigator.clipboard.writeText(logLineText)
                        setCopiedLogId(logId)
                        setTimeout(() => setCopiedLogId(null), 2000)
                      }}
                      style={{
                        background: 'none',
                        border: 'none',
                        color: copiedLogId === logId ? 'var(--color-success)' : 'var(--color-text-muted)',
                        cursor: 'pointer',
                        padding: '2px 4px',
                        borderRadius: 4,
                        flexShrink: 0,
                        display: 'flex',
                        alignItems: 'center',
                        gap: 4,
                        fontSize: 'var(--type-xs)',
                      }}
                      aria-label={t('ui_copy_log_line')}
                      title={t('ui_copy_log_line')}
                    >
                      {copiedLogId === logId ? (
                        <>
                          <Check size={12} />
                          <span style={{ fontSize: 'var(--type-xs)' }}>{t('ui_copied')}</span>
                        </>
                      ) : (
                        <Copy size={12} />
                      )}
                    </button>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </section>

      {/* ----------------------------------------------- Backup & Restore -- */}
      {/* Migrate this install to another server. A sibling section — it used
          to live inside the log explorer's card, one feature buried in
          another. */}
      <section className="glass-panel ops-section" style={{ '--ops-i': 2 } as React.CSSProperties}>
        <div className="ops-head">
          <h3 className="ops-head__title">
            <Database size={17} color="var(--color-info)" />
            <span>{t('ui_backup_restore_server_migration')}</span>
          </h3>
        </div>
        <p style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginBlockStart: 0, marginBlockEnd: 'var(--space-3)' }}>
          {t('ui_every_setting_rule_filter_ai_config_and_telegram_session_is_pack')}
        </p>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 'var(--space-3)' }}>
          {/* EXPORT */}
          <div style={{ background: 'var(--color-surface-2)', border: '1px solid color-mix(in oklab, var(--color-info) 35%, transparent)', borderRadius: 'var(--radius-md)', padding: 'var(--space-3)' }}>
            <div style={{ fontSize: 'var(--type-sm)', fontWeight: 700, color: 'var(--color-info)', marginBlockEnd: 'var(--space-2)', display: 'flex', alignItems: 'center', gap: 6 }}>
              <Download size={14} /> {t('ui_1_back_up_this_server')}
            </div>
            <label className="ops-toolbar__label" style={{ display: 'block', marginBlockEnd: 'var(--space-1)' }}>
              {t('ui_file_passphrase_optional_recommended')}
            </label>
            <input
              type="password"
              value={backupPassphrase}
              onChange={(e) => setBackupPassphrase(e.target.value)}
              placeholder={t('ui_strong_passphrase')}
              className="input-field"
              style={{ inlineSize: '100%', boxSizing: 'border-box', marginBlockEnd: 'var(--space-2)' }}
            />
            <button
              type="button"
              onClick={handleBackupExport}
              disabled={backupBusy}
              className="btn btn--sm"
              style={{
                inlineSize: '100%',
                justifyContent: 'center',
                background: 'var(--color-info-soft)',
                border: '1px solid color-mix(in oklab, var(--color-info) 40%, transparent)',
                color: 'var(--color-info)',
              }}
            >
              {backupBusy ? <RefreshCw size={12} className="spin" aria-hidden="true" /> : <Download size={12} aria-hidden="true" />}
              {t('ui_download_backup')}
            </button>
            {backupCounts && (
              <div style={{ marginBlockStart: 'var(--space-2)', fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
                {backupEncrypted && (
                  <span className="badge badge--success" style={{ marginInlineEnd: 6 }}>
                    <Lock size={10} aria-hidden="true" /> AES-GCM
                  </span>
                )}
                {Object.entries(backupCounts).map(([k, v]) => (
                  <span key={k} style={{ display: 'inline-block', margin: '2px 4px 0 0', padding: '2px 7px', background: 'var(--color-hover-wash)', borderRadius: 6 }}>
                    {k}: <b style={{ color: 'var(--color-text)' }}>{v}</b>
                  </span>
                ))}
              </div>
            )}
            {!backupEncrypted && backupCounts && (
              <div style={{ marginBlockStart: 'var(--space-2)', fontSize: 'var(--type-xs)', color: 'var(--color-warning)' }}>
                ⚠️ {t('ui_this_file_is_unencrypted_and_holds_live_credentials')}
              </div>
            )}
          </div>

          {/* IMPORT */}
          <div style={{ background: 'var(--color-surface-2)', border: '1px solid color-mix(in oklab, var(--color-accent-violet) 35%, transparent)', borderRadius: 'var(--radius-md)', padding: 'var(--space-3)' }}>
            <div style={{ fontSize: 'var(--type-sm)', fontWeight: 700, color: 'var(--color-accent-violet)', marginBlockEnd: 'var(--space-2)', display: 'flex', alignItems: 'center', gap: 6 }}>
              <Upload size={14} /> {t('ui_2_restore_on_this_server')}
            </div>
            <label className="ops-toolbar__label" style={{ display: 'block', marginBlockEnd: 'var(--space-1)' }}>
              {t('ui_backup_file')}
            </label>
            <input
              type="file" accept=".json,.enc,application/json"
              onChange={(e) => onBackupFileChosen(e.target.files?.[0])}
              style={{ inlineSize: '100%', boxSizing: 'border-box', fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginBlockEnd: 'var(--space-2)' }}
            />
            <label className="ops-toolbar__label" style={{ display: 'block', marginBlockEnd: 'var(--space-1)' }}>
              {t('ui_or_paste_the_bundle')}
            </label>
            <textarea
              value={importText}
              onChange={(e) => { setImportText(e.target.value); setImportStats(null) }}
              placeholder={t('ui_bundle_contents')}
              rows={3}
              style={{
                inlineSize: '100%',
                boxSizing: 'border-box',
                background: 'var(--color-surface-inset)',
                color: 'var(--color-text)',
                border: '1px solid var(--color-border)',
                borderRadius: 'var(--radius-sm)',
                padding: '7px 10px',
                fontSize: 'var(--type-xs)',
                fontFamily: 'var(--font-mono)',
                resize: 'vertical',
                marginBlockEnd: 'var(--space-2)',
              }}
            />
            <label className="ops-toolbar__label" style={{ display: 'block', marginBlockEnd: 'var(--space-1)' }}>
              {t('ui_passphrase_if_the_file_is_encrypted')}
            </label>
            <input
              type="password"
              value={importPassphrase}
              onChange={(e) => setImportPassphrase(e.target.value)}
              placeholder={t('ui_passphrase')}
              className="input-field"
              style={{ inlineSize: '100%', boxSizing: 'border-box', marginBlockEnd: 'var(--space-2)' }}
            />
            <label className="ops-toolbar__label" style={{ color: 'var(--color-text)', display: 'flex', alignItems: 'center', gap: 6, marginBlockEnd: 'var(--space-2)' }}>
              <input type="checkbox" checked={importOverwrite} onChange={(e) => setImportOverwrite(e.target.checked)} />
              {t('ui_full_replace_overwrite_existing_rows')}
            </label>
            <button
              type="button"
              onClick={handleBackupImport}
              disabled={backupBusy}
              className="btn btn--sm"
              style={{
                inlineSize: '100%',
                justifyContent: 'center',
                background: 'var(--color-primary-soft)',
                border: '1px solid color-mix(in oklab, var(--color-accent-violet) 40%, transparent)',
                color: 'var(--color-accent-violet)',
              }}
            >
              {backupBusy ? <RefreshCw size={12} className="spin" aria-hidden="true" /> : <Upload size={12} aria-hidden="true" />}
              {t('ui_restore_backup')}
            </button>
            {importStats && (
              <div style={{ marginBlockStart: 'var(--space-2)', display: 'flex', flexWrap: 'wrap', gap: 5 }}>
                {Object.entries(importStats).map(([k, v]) => (
                  <span key={k} style={{ fontSize: 'var(--type-xs)', padding: '3px 8px', borderRadius: 6, background: 'var(--color-hover-wash)', color: 'var(--color-text-muted)' }}>
                    {k}: <b style={{ color: 'var(--color-success)' }}>{v.imported}</b>{v.skipped ? <span style={{ color: 'var(--color-text-muted)' }}>/{v.skipped}</span> : null}{v.failed ? <span style={{ color: 'var(--color-danger)' }}> ✗{v.failed}</span> : null}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      </section>

      {/* ----------------------------------------- Recent Pipeline Errors -- */}
      {/* Triage queue for things to fix next. A sibling section. */}
      <section
        className="glass-panel ops-section"
        style={{ '--ops-i': 3, marginBlockEnd: 0 } as React.CSSProperties}
      >
        <div className="ops-head">
          <h3 className="ops-head__title">
            <Activity size={16} color="var(--color-danger)" />
            <span>{t('ui_recent_pipeline_errors_triage_queue')}</span>
            {(recentErrors || []).length > 0 && (
              <span className="badge badge--warning">{recentErrors.length}</span>
            )}
          </h3>
        </div>

        {(!Array.isArray(recentErrors) || recentErrors.length === 0) ? (
          <div style={{ textAlign: 'center', padding: 'var(--space-4)', color: 'var(--color-text-muted)', fontSize: 'var(--type-sm)' }}>
            <div style={{ fontSize: 22, marginBlockEnd: 6 }}>✅</div>
            <div>{t('ui_no_pipeline_errors_recorded_all_healthy')}</div>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
            {recentErrors.map((err, idx) => {
              const sev = (err.severity || 'error').toLowerCase()
              const isErr = sev === 'error'
              return (
                <div
                  key={`${err.ts}-${idx}`}
                  dir="ltr"
                  style={{
                    textAlign: 'left',
                    background: 'var(--color-surface-2)',
                    padding: '10px 14px',
                    borderRadius: 'var(--radius-sm)',
                    fontSize: 'var(--type-xs)',
                    border: `1px solid ${isErr ? 'color-mix(in oklab, var(--color-danger) 35%, transparent)' : 'color-mix(in oklab, var(--color-warning) 35%, transparent)'}`,
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 4,
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 'var(--space-2)', flexWrap: 'wrap' }}>
                    <span style={{ fontWeight: 700, color: isErr ? 'var(--color-danger-text)' : 'var(--color-warning)' }}>
                      {err.error_name || err.category}
                    </span>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      {err.rule_id && (
                        <span className="ops-chip" style={{ cursor: 'default' }}>
                          {err.rule_id.substring(0, 8)}
                        </span>
                      )}
                      <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
                        {err.ts ? new Date(err.ts * 1000).toLocaleString() : ''}
                      </span>
                    </div>
                  </div>
                  {err.detail && (
                    <div style={{ color: 'var(--color-text-muted)', fontSize: 'var(--type-xs)', fontFamily: 'var(--font-mono)', wordBreak: 'break-word' }}>
                      {err.detail.slice(0, 260)}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </section>
    </>
  )
}
